"""
UDS Client
==========
High-level service layer. Wires together:
    Transport  →  raw bytes in/out
    UDSCodec   →  encode requests, decode responses
    ParameterStore  →  update values after reads / writes

All blocking operations run on a worker QThread so the GUI stays
responsive. Signals are emitted on completion.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from core.parameter_model import ParameterStore
from transport.transport import AbstractTransport, TransportError
from uds.codec import DataCodec, NRC, ServiceID, UDSCodec, UDSDecodeError, UDSNegativeResponse

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Worker thread                                                       #
# ------------------------------------------------------------------ #

class _UDSWorker(QObject):
    """Runs in a QThread. Executes UDS requests sequentially."""

    # Emitted back to the client (main thread via queued connection)
    read_done     = Signal(int, object)   # did, value
    read_error    = Signal(int, str)      # did, message
    write_done    = Signal(int)           # did
    write_error   = Signal(int, str)      # did, message
    batch_done    = Signal()
    session_changed = Signal(int)         # session type
    error_occurred  = Signal(str)         # general error

    def __init__(self, transport: AbstractTransport, store: ParameterStore):
        super().__init__()
        self._transport = transport
        self._store = store
        self._codec = UDSCodec()

    @Slot(list)
    def read_all(self, dids: list) -> None:
        for did in dids:
            self._do_read(did)
        self.batch_done.emit()

    @Slot(int)
    def read_one(self, did: int) -> None:
        self._do_read(did)

    @Slot(int, object)
    def write_one(self, did: int, value: Any) -> None:
        self._do_write(did, value)

    @Slot(int)
    def set_session(self, session_type: int) -> None:
        try:
            req = self._codec.encode_diagnostic_session_control(session_type)
            resp = self._transport.send_and_wait(req)
            decoded = self._codec.decode_response(resp)
            self.session_changed.emit(session_type)
            log.info("Session changed to 0x%02X", session_type)
        except Exception as e:
            self.error_occurred.emit(f"Session change failed: {e}")

    @Slot()
    def send_tester_present(self) -> None:
        try:
            req = self._codec.encode_tester_present(suppress_response=True)
            self._transport.send(req)
        except Exception:
            pass  # best-effort keepalive

    def _do_read(self, did: int) -> None:
        defn = self._store.get_definition(did)
        if defn is None:
            return
        try:
            req = self._codec.encode_read_data_by_id(did)
            resp = self._transport.send_and_wait(req, timeout=0.5)
            decoded = self._codec.decode_response(resp)
            raw_bytes = decoded["data"]
            value = DataCodec.decode(raw_bytes, defn.param_type.value)
            self.read_done.emit(did, value)
        except UDSNegativeResponse as e:
            msg = f"NRC 0x{e.nrc:02X}: {NRC.description(e.nrc)}"
            log.warning("RDBI 0x%04X: %s", did, msg)
            self.read_error.emit(did, msg)
        except TransportError as e:
            log.warning("RDBI 0x%04X transport error: %s", did, e)
            self.read_error.emit(did, str(e))
        except Exception as e:
            log.exception("RDBI 0x%04X unexpected: %s", did, e)
            self.read_error.emit(did, str(e))

    def _do_write(self, did: int, value: Any) -> None:
        defn = self._store.get_definition(did)
        if defn is None:
            return
        try:
            data_bytes = DataCodec.encode(value, defn.param_type.value)
            req = self._codec.encode_write_data_by_id(did, data_bytes)
            resp = self._transport.send_and_wait(req, timeout=1.0)
            decoded = self._codec.decode_response(resp)
            self.write_done.emit(did)
            log.info("WDBI 0x%04X = %s  ✓", did, value)
        except UDSNegativeResponse as e:
            msg = f"NRC 0x{e.nrc:02X}: {NRC.description(e.nrc)}"
            log.warning("WDBI 0x%04X: %s", did, msg)
            self.write_error.emit(did, msg)
        except TransportError as e:
            log.warning("WDBI 0x%04X transport error: %s", did, e)
            self.write_error.emit(did, str(e))
        except Exception as e:
            log.exception("WDBI 0x%04X unexpected: %s", did, e)
            self.write_error.emit(did, str(e))


# ------------------------------------------------------------------ #
#  UDS Client (main-thread object)                                     #
# ------------------------------------------------------------------ #

class UDSClient(QObject):
    """
    Main-thread facade for UDS operations.

    Usage:
        client = UDSClient(transport, store)
        client.read_all_parameters()   # non-blocking, emits signals
        client.write_parameter(did, value)
    """

    # Progress / status
    read_progress    = Signal(int, int)  # done, total
    all_read_done    = Signal()
    parameter_read   = Signal(int, object)  # did, value
    parameter_written = Signal(int)          # did
    error_occurred   = Signal(str)

    def __init__(self, transport: AbstractTransport, store: ParameterStore,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._transport = transport
        self._store = store
        self._thread = QThread(self)
        self._worker = _UDSWorker(transport, store)
        self._worker.moveToThread(self._thread)

        # Wire worker signals → update store → emit client signals
        self._worker.read_done.connect(self._on_read_done)
        self._worker.read_error.connect(self._on_read_error)
        self._worker.write_done.connect(self._on_write_done)
        self._worker.write_error.connect(self._on_write_error)
        self._worker.batch_done.connect(self.all_read_done)
        self._worker.error_occurred.connect(self.error_occurred)

        # Connect store write requests → worker
        self._store.parameter_write_requested.connect(self._on_write_requested)

        self._total_to_read = 0
        self._read_count = 0

        self._thread.start()

    def read_all_parameters(self) -> None:
        dids = self._store.all_dids()
        self._total_to_read = len(dids)
        self._read_count = 0
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            self._worker, "read_all",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(list, dids),
        )

    def read_parameter(self, did: int) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            self._worker, "read_one",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(int, did),
        )

    def write_parameter(self, did: int, value: Any) -> None:
        """Called directly (bypasses store.request_write if needed)."""
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            self._worker, "write_one",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(int, did),
            Q_ARG(object, value),
        )

    def send_tester_present(self) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(
            self._worker, "send_tester_present",
            Qt.ConnectionType.QueuedConnection,
        )

    def shutdown(self) -> None:
        self._thread.quit()
        self._thread.wait(2000)

    # ── Private slots ───────────────────────────────────────────────

    @Slot(int, object)
    def _on_read_done(self, did: int, value: Any) -> None:
        self._store.update_from_device(did, value)
        self._read_count += 1
        if self._total_to_read:
            self.read_progress.emit(self._read_count, self._total_to_read)
        self.parameter_read.emit(did, value)

    @Slot(int, str)
    def _on_read_error(self, did: int, msg: str) -> None:
        self._store.set_error(did, msg)
        self._read_count += 1
        if self._total_to_read:
            self.read_progress.emit(self._read_count, self._total_to_read)

    @Slot(int)
    def _on_write_done(self, did: int) -> None:
        pv = self._store.get_value(did)
        if pv:
            pv.is_dirty = False
        self.parameter_written.emit(did)

    @Slot(int, str)
    def _on_write_error(self, did: int, msg: str) -> None:
        pv = self._store.get_value(did)
        if pv:
            pv.error = msg
        self.error_occurred.emit(f"Write 0x{did:04X} failed: {msg}")

    @Slot(int, object)
    def _on_write_requested(self, did: int, value: Any) -> None:
        self.write_parameter(did, value)
