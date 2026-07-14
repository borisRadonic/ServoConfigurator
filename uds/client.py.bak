"""
UDS Client — fixed thread crossing
====================================
Problem bio: QMetaObject.invokeMethod s Q_ARG(list, dids) crashira u PySide6
jer list nije Qt metatype.
Fix: koristiti Signal za thread crossing — jedini ispravan Qt pattern.
"""
from __future__ import annotations
import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from core.parameter_model import ParameterStore
from transport.transport import AbstractTransport, TransportError
from uds.codec import DataCodec, NRC, ServiceID, UDSCodec, UDSDecodeError, UDSNegativeResponse

log = logging.getLogger(__name__)


# ── Worker (runs in QThread) ─────────────────────────────────────────

class _Worker(QObject):
    read_done    = Signal(int, object)
    read_error   = Signal(int, str)
    write_done   = Signal(int)
    write_error  = Signal(int, str)
    batch_done   = Signal()
    error        = Signal(str)

    def __init__(self, transport: AbstractTransport, store: ParameterStore):
        super().__init__()
        self._transport = transport
        self._store = store
        self._codec = UDSCodec()

    @Slot(object)
    def read_all(self, dids):
        for did in dids:
            self._read(did)
        self.batch_done.emit()

    @Slot(int)
    def read_one(self, did: int):
        self._read(did)

    @Slot(int, object)
    def write_one(self, did: int, value: Any):
        self._write(did, value)

    @Slot()
    def tester_present(self):
        try:
            self._transport.send(self._codec.encode_tester_present(True))
        except Exception:
            pass

    def _read(self, did: int):
        defn = self._store.get_definition(did)
        if not defn:
            return
        try:
            req  = self._codec.encode_read_data_by_id(did)
            resp = self._transport.send_and_wait(req, timeout=0.5)
            dec  = self._codec.decode_response(resp)
            val  = DataCodec.decode(dec["data"], defn.param_type.value)
            self.read_done.emit(did, val)
        except UDSNegativeResponse as e:
            self.read_error.emit(did, f"NRC 0x{e.nrc:02X}: {NRC.description(e.nrc)}")
        except Exception as e:
            self.read_error.emit(did, str(e))

    def _write(self, did: int, value: Any):
        defn = self._store.get_definition(did)
        if not defn:
            return
        try:
            data = DataCodec.encode(value, defn.param_type.value)
            req  = self._codec.encode_write_data_by_id(did, data)
            resp = self._transport.send_and_wait(req, timeout=1.0)
            self._codec.decode_response(resp)
            self.write_done.emit(did)
            log.info("WDBI 0x%04X = %s ✓", did, value)
        except UDSNegativeResponse as e:
            self.write_error.emit(did, f"NRC 0x{e.nrc:02X}: {NRC.description(e.nrc)}")
        except Exception as e:
            self.write_error.emit(did, str(e))


# ── Signals used to safely cross thread boundary ─────────────────────
# This is the ONLY correct way to invoke slots on a worker thread
# in PySide6/PyQt5 — QMetaObject.invokeMethod with Q_ARG(list,...)
# does NOT work because list is not a registered Qt metatype.

class _Bridge(QObject):
    sig_read_all      = Signal(object)   # list of DIDs — object avoids metatype issue
    sig_read_one      = Signal(int)
    sig_write_one     = Signal(int, object)
    sig_tester_present = Signal()


# ── Public facade (lives on main thread) ─────────────────────────────

class UDSClient(QObject):
    read_progress     = Signal(int, int)
    all_read_done     = Signal()
    parameter_read    = Signal(int, object)
    parameter_written = Signal(int)
    error_occurred    = Signal(str)

    def __init__(self, transport: AbstractTransport, store: ParameterStore,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._store = store
        self._total = 0
        self._done  = 0

        self._thread = QThread(self)
        self._worker = _Worker(transport, store)
        self._worker.moveToThread(self._thread)

        # Bridge: main thread → worker thread (QueuedConnection automatic)
        self._bridge = _Bridge(self)
        self._bridge.sig_read_all.connect(self._worker.read_all)
        self._bridge.sig_read_one.connect(self._worker.read_one)
        self._bridge.sig_write_one.connect(self._worker.write_one)
        self._bridge.sig_tester_present.connect(self._worker.tester_present)

        # Worker → main thread
        self._worker.read_done.connect(self._on_read_done)
        self._worker.read_error.connect(self._on_read_error)
        self._worker.write_done.connect(self._on_write_done)
        self._worker.write_error.connect(self._on_write_error)
        self._worker.batch_done.connect(self.all_read_done)
        self._worker.error.connect(self.error_occurred)

        # Store write requests → write
        store.parameter_write_requested.connect(self._on_write_requested)

        self._thread.start()
        log.debug("UDSClient thread started")

    def read_all_parameters(self):
        dids = self._store.all_dids()
        self._total = len(dids)
        self._done  = 0
        log.info("Reading %d parameters…", self._total)
        self._bridge.sig_read_all.emit(dids)   # Signal carries list as object — works

    def read_parameter(self, did: int):
        self._bridge.sig_read_one.emit(did)

    def write_parameter(self, did: int, value: Any):
        self._bridge.sig_write_one.emit(did, value)

    def send_tester_present(self):
        self._bridge.sig_tester_present.emit()

    def shutdown(self):
        self._thread.quit()
        self._thread.wait(2000)

    @Slot(int, object)
    def _on_read_done(self, did: int, value: Any):
        self._store.update_from_device(did, value)
        self._done += 1
        if self._total:
            self.read_progress.emit(self._done, self._total)
        self.parameter_read.emit(did, value)

    @Slot(int, str)
    def _on_read_error(self, did: int, msg: str):
        self._store.set_error(did, msg)
        self._done += 1
        if self._total:
            self.read_progress.emit(self._done, self._total)

    @Slot(int)
    def _on_write_done(self, did: int):
        pv = self._store.get_value(did)
        if pv:
            pv.is_dirty = False
        self.parameter_written.emit(did)

    @Slot(int, str)
    def _on_write_error(self, did: int, msg: str):
        pv = self._store.get_value(did)
        if pv:
            pv.error = msg
        self.error_occurred.emit(f"Write 0x{did:04X}: {msg}")

    @Slot(int, object)
    def _on_write_requested(self, did: int, value: Any):
        self.write_parameter(did, value)
