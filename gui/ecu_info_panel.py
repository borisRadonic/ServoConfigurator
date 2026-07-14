"""
ECU Info Panel
==============
Reads standard ISO 14229-1 ECU identification DIDs (0xF186-0xF1A0).
"""
from __future__ import annotations
import logging
from typing import Optional

from PySide6.QtCore import Qt, QThread, QObject, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget, QFrame,
)

from transport.transport import AbstractTransport
from uds.codec import NRC, ServiceID, UDSCodecExtended, UDSNegativeResponse

log = logging.getLogger(__name__)


class _InfoWorker(QObject):
    field_read = Signal(int, str)   # did, value string
    all_done   = Signal()
    error      = Signal(int, str)   # did, error

    def __init__(self, transport: AbstractTransport):
        super().__init__()
        self._transport = transport

    @Slot(list)
    def read_all(self, dids: list):
        for did in dids:
            self._read_one(did)
        self.all_done.emit()

    def _read_one(self, did: int):
        try:
            req  = UDSCodecExtended.encode_read_ecu_info(did)
            resp = self._transport.send_and_wait(req, timeout=1.0)
            if len(resp) < 3 or resp[0] != (ServiceID.READ_DATA_BY_ID | 0x40):
                self.error.emit(did, "Bad response")
                return
            data = resp[3:]
            # Try ASCII first, fallback to hex
            try:
                text = data.decode("ascii").strip().rstrip("\x00")
                if not text: text = data.hex().upper()
            except Exception:
                text = data.hex().upper()
            self.field_read.emit(did, text)
        except UDSNegativeResponse as e:
            self.error.emit(did, f"NRC 0x{e.nrc:02X}")
        except Exception as e:
            self.error.emit(did, str(e))


class _Bridge(QObject):
    sig_read_all = Signal(object)


class ECUInfoPanel(QWidget):
    """Reads and displays ECU identification data."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._transport: Optional[AbstractTransport] = None
        self._thread:    Optional[QThread] = None
        self._worker:    Optional[_InfoWorker] = None
        self._bridge:    Optional[_Bridge] = None
        self._fields:    dict[int, QLineEdit] = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 18, 20, 18)

        # Toolbar
        tb = QHBoxLayout()
        self._read_btn = QPushButton("⟳  Read ECU Info")
        self._read_btn.setObjectName("primaryBtn")
        self._read_btn.setEnabled(False)
        self._read_btn.setFixedHeight(34)
        self._read_btn.clicked.connect(self._read_all)

        self._status = QLabel("Not connected")
        self._status.setStyleSheet("color:#585B70; font-size:12px;")

        tb.addWidget(self._read_btn)
        tb.addStretch()
        tb.addWidget(self._status)
        root.addLayout(tb)

        # Scroll area for fields
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(12)
        content_layout.setContentsMargins(0, 0, 12, 0)

        # Group DIDs by category
        groups = [
            ("Vehicle", [0xF190, 0xF197]),
            ("ECU Identity", [0xF18C, 0xF18A, 0xF186]),
            ("Software", [0xF188, 0xF189, 0xF194, 0xF195]),
            ("Hardware", [0xF191, 0xF192, 0xF193]),
            ("Part Numbers", [0xF187, 0xF18E]),
            ("Programming", [0xF18B, 0xF199, 0xF198, 0xF1A0]),
        ]

        for group_name, dids in groups:
            grp = QGroupBox(group_name)
            form = QFormLayout(grp)
            form.setSpacing(10)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

            for did in dids:
                name = UDSCodecExtended.ECU_INFO_DIDS.get(did, f"DID 0x{did:04X}")
                field = QLineEdit()
                field.setReadOnly(True)
                field.setPlaceholderText("—")
                field.setFont(QFont("Consolas, Courier New", 10))
                field.setStyleSheet(
                    "QLineEdit { background:#181825; border:1px solid #313244; "
                    "border-radius:4px; padding:4px 8px; color:#CDD6F4; }"
                    "QLineEdit[loaded='true'] { border-color:#45475A; color:#A6E3A1; }"
                    "QLineEdit[error='true']  { border-color:#F38BA8; color:#F38BA8; }")

                did_label = QLabel(f"<span style='color:#6C7086; font-size:10px; "
                                   f"font-family:monospace'>0x{did:04X}</span>  {name}")
                did_label.setTextFormat(Qt.RichText)
                form.addRow(did_label, field)
                self._fields[did] = field

            content_layout.addWidget(grp)

        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    # ── Public API ────────────────────────────────────────────────

    def set_transport(self, transport: Optional[AbstractTransport]):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)

        self._transport = transport
        connected = transport is not None

        if connected:
            self._thread  = QThread(self)
            self._worker  = _InfoWorker(transport)
            self._bridge  = _Bridge(self)
            self._worker.moveToThread(self._thread)
            self._bridge.sig_read_all.connect(self._worker.read_all)
            self._worker.field_read.connect(self._on_field_read)
            self._worker.error.connect(self._on_field_error)
            self._worker.all_done.connect(self._on_all_done)
            self._thread.start()

        self._read_btn.setEnabled(connected)
        self._status.setText("Ready" if connected else "Not connected")
        self._status.setStyleSheet(
            "color:#A6E3A1; font-size:12px;" if connected
            else "color:#585B70; font-size:12px;")

        if not connected:
            for field in self._fields.values():
                field.clear()
                field.setProperty("loaded", False)
                field.setProperty("error", False)
                field.style().unpolish(field)
                field.style().polish(field)

    # ── Private ───────────────────────────────────────────────────

    def _read_all(self):
        if not self._bridge: return
        # Clear all fields
        for field in self._fields.values():
            field.clear()
            field.setProperty("loaded", False)
            field.setProperty("error", False)
            field.style().unpolish(field)
            field.style().polish(field)
        self._read_btn.setEnabled(False)
        self._status.setText("Reading…")
        self._status.setStyleSheet("color:#89B4FA; font-size:12px;")
        self._bridge.sig_read_all.emit(list(self._fields.keys()))

    def _on_field_read(self, did: int, value: str):
        field = self._fields.get(did)
        if field:
            field.setText(value)
            field.setProperty("loaded", "true")
            field.setProperty("error", "false")
            field.style().unpolish(field)
            field.style().polish(field)

    def _on_field_error(self, did: int, msg: str):
        field = self._fields.get(did)
        if field:
            field.setText(msg)
            field.setProperty("loaded", "false")
            field.setProperty("error", "true")
            field.style().unpolish(field)
            field.style().polish(field)

    def _on_all_done(self):
        self._read_btn.setEnabled(True)
        loaded = sum(1 for f in self._fields.values() if f.property("loaded") == "true")
        total  = len(self._fields)
        self._status.setText(f"✓ {loaded}/{total} fields read")
        self._status.setStyleSheet("color:#A6E3A1; font-size:12px;")
        log.info("ECU info read: %d/%d", loaded, total)
