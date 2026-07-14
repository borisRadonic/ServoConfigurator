"""
Session & Raw UDS Panel
========================
- UDS session control (Default/Extended/Programming)
- Raw UDS console: type hex request, see response
- ECU Reset
"""
from __future__ import annotations
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread, QObject, Slot, QTimer
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget, QSizePolicy,
)

from transport.transport import AbstractTransport
from uds.codec import NRC, ServiceID, UDSNegativeResponse, UDSDecodeError

log = logging.getLogger(__name__)


SESSION_NAMES = {
    0x01: "Default Session",
    0x02: "Programming Session",
    0x03: "Extended Diagnostic Session",
}

RESET_NAMES = {
    0x01: "Hard Reset",
    0x02: "Key Off/On Reset",
    0x03: "Soft Reset",
}


class _RawWorker(QObject):
    response  = Signal(bytes)
    error     = Signal(str)

    def __init__(self, transport: AbstractTransport):
        super().__init__()
        self._transport = transport

    @Slot(bytes)
    def send_raw(self, payload: bytes):
        try:
            resp = self._transport.send_and_wait(payload, timeout=2.0)
            self.response.emit(resp)
        except Exception as e:
            self.error.emit(str(e))


class _Bridge(QObject):
    sig_send = Signal(bytes)


class SessionPanel(QWidget):
    """UDS session control + raw console."""

    session_changed = Signal(int)   # notify main window

    def __init__(self, parent=None):
        super().__init__(parent)
        self._transport: Optional[AbstractTransport] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RawWorker] = None
        self._bridge: Optional[_Bridge] = None
        self._current_session = 0x01
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 18, 20, 18)

        # ── Session control ────────────────────────────────────────
        sess_group = QGroupBox("Diagnostic Session")
        sl = QHBoxLayout(sess_group)
        sl.setSpacing(10)

        self._sess_indicator = QLabel("● Default")
        self._sess_indicator.setStyleSheet(
            "color:#A6E3A1; font-weight:bold; font-size:13px; padding:0 8px;")

        self._sess_combo = QComboBox()
        for code, name in SESSION_NAMES.items():
            self._sess_combo.addItem(name, code)
        self._sess_combo.setMinimumWidth(200)

        self._sess_btn = QPushButton("Switch Session")
        self._sess_btn.setObjectName("primaryBtn")
        self._sess_btn.setEnabled(False)
        self._sess_btn.setFixedHeight(34)
        self._sess_btn.clicked.connect(self._switch_session)

        sl.addWidget(QLabel("Active:"))
        sl.addWidget(self._sess_indicator)
        sl.addSpacing(20)
        sl.addWidget(QLabel("Switch to:"))
        sl.addWidget(self._sess_combo)
        sl.addWidget(self._sess_btn)
        sl.addStretch()
        root.addWidget(sess_group)

        # ── ECU Reset ─────────────────────────────────────────────
        reset_group = QGroupBox("ECU Reset")
        rl = QHBoxLayout(reset_group)
        rl.setSpacing(10)

        self._reset_combo = QComboBox()
        for code, name in RESET_NAMES.items():
            self._reset_combo.addItem(name, code)
        self._reset_combo.setMinimumWidth(180)

        self._reset_btn = QPushButton("⚡  Send Reset")
        self._reset_btn.setEnabled(False)
        self._reset_btn.setFixedHeight(34)
        self._reset_btn.setStyleSheet(
            "QPushButton { background:#45475A; color:#F38BA8; border:1px solid #F38BA8; "
            "border-radius:6px; padding:5px 14px; font-weight:bold; }"
            "QPushButton:hover { background:#F38BA8; color:#1E1E2E; }"
            "QPushButton:disabled { color:#585B70; border-color:#313244; background:#262636; }")
        self._reset_btn.clicked.connect(self._send_reset)

        rl.addWidget(QLabel("Type:"))
        rl.addWidget(self._reset_combo)
        rl.addWidget(self._reset_btn)
        rl.addStretch()
        rl.addWidget(QLabel(
            "⚠  ECU may disconnect after reset"),)
        root.addWidget(reset_group)

        # ── Raw UDS Console ────────────────────────────────────────
        raw_group = QGroupBox("Raw UDS Console")
        raw_layout = QVBoxLayout(raw_group)
        raw_layout.setSpacing(8)

        # Log output
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setFont(QFont("Consolas, Courier New", 10))
        self._log.setStyleSheet(
            "background:#11111B; color:#CDD6F4; border:1px solid #313244; border-radius:4px;")
        self._log.setMinimumHeight(240)
        raw_layout.addWidget(self._log)

        # Input row
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        prompt = QLabel("REQ:")
        prompt.setStyleSheet("color:#89B4FA; font-family:monospace; font-weight:bold;")
        prompt.setFixedWidth(36)

        self._hex_input = QLineEdit()
        self._hex_input.setPlaceholderText(
            "Enter hex bytes e.g.  10 03  or  22 F1 97  or  3E 00")
        self._hex_input.setFont(QFont("Consolas, Courier New", 11))
        self._hex_input.setEnabled(False)
        self._hex_input.returnPressed.connect(self._send_raw)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("primaryBtn")
        self._send_btn.setEnabled(False)
        self._send_btn.setFixedWidth(80)
        self._send_btn.setFixedHeight(34)
        self._send_btn.clicked.connect(self._send_raw)

        self._clear_log_btn = QPushButton("Clear")
        self._clear_log_btn.setFixedWidth(70)
        self._clear_log_btn.setFixedHeight(34)
        self._clear_log_btn.clicked.connect(self._log.clear)

        # Quick command buttons
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        quick_row.addWidget(QLabel("Quick:"))
        for label, hex_str in [
            ("TesterPresent",   "3E 00"),
            ("DefaultSession",  "10 01"),
            ("ExtSession",      "10 03"),
            ("ReadActiveSess",  "22 F1 86"),
            ("ReadECUSerial",   "22 F1 8C"),
            ("ReadVIN",         "22 F1 90"),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton { background:#313244; color:#89B4FA; border:1px solid #45475A;"
                "border-radius:4px; padding:2px 8px; font-size:11px; }"
                "QPushButton:hover { background:#45475A; }"
                "QPushButton:disabled { color:#585B70; }")
            btn.setProperty("hex_cmd", hex_str)
            btn.clicked.connect(lambda checked, h=hex_str: self._quick_send(h))
            btn.setEnabled(False)
            self._quick_btns = getattr(self, '_quick_btns', [])
            self._quick_btns.append(btn)
            quick_row.addWidget(btn)
        quick_row.addStretch()

        input_row.addWidget(prompt)
        input_row.addWidget(self._hex_input, 1)
        input_row.addWidget(self._send_btn)
        input_row.addWidget(self._clear_log_btn)

        raw_layout.addLayout(quick_row)
        raw_layout.addLayout(input_row)
        root.addWidget(raw_group, 1)

    # ── Public API ────────────────────────────────────────────────

    def set_transport(self, transport: Optional[AbstractTransport]):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)

        self._transport = transport
        connected = transport is not None

        if connected:
            self._thread  = QThread(self)
            self._worker  = _RawWorker(transport)
            self._bridge  = _Bridge(self)
            self._worker.moveToThread(self._thread)
            self._bridge.sig_send.connect(self._worker.send_raw)
            self._worker.response.connect(self._on_response)
            self._worker.error.connect(self._on_error)
            self._thread.start()

        self._sess_btn.setEnabled(connected)
        self._reset_btn.setEnabled(connected)
        self._hex_input.setEnabled(connected)
        self._send_btn.setEnabled(connected)
        for btn in getattr(self, '_quick_btns', []):
            btn.setEnabled(connected)

        if not connected:
            self._current_session = 0x01
            self._update_session_indicator(0x01)
            self._log_line("system", "Disconnected")

    # ── Private ───────────────────────────────────────────────────

    def _switch_session(self):
        if not self._bridge: return
        code = self._sess_combo.currentData()
        self._log_line("tx", f"10 {code:02X}  → {SESSION_NAMES.get(code,'?')}")
        self._bridge.sig_send.emit(bytes([0x10, code]))

    def _send_reset(self):
        if not self._bridge: return
        code = self._reset_combo.currentData()
        self._log_line("tx", f"11 {code:02X}  → {RESET_NAMES.get(code,'?')}")
        self._bridge.sig_send.emit(bytes([0x11, code]))

    def _send_raw(self):
        if not self._bridge: return
        raw = self._hex_input.text().strip()
        if not raw: return
        try:
            payload = bytes.fromhex(raw.replace(" ", "").replace(":", ""))
        except ValueError:
            self._log_line("error", f"Invalid hex: {raw}")
            return
        self._log_line("tx", " ".join(f"{b:02X}" for b in payload))
        self._hex_input.clear()
        self._bridge.sig_send.emit(payload)

    def _quick_send(self, hex_str: str):
        self._hex_input.setText(hex_str)
        self._send_raw()

    def _on_response(self, resp: bytes):
        hex_str = " ".join(f"{b:02X}" for b in resp)

        if not resp:
            self._log_line("rx", "(empty response)")
            return

        sid = resp[0]
        if sid == 0x7F:
            nrc = resp[2] if len(resp) > 2 else 0
            desc = NRC.description(nrc)
            self._log_line("nrc", f"{hex_str}  ← NRC 0x{nrc:02X}: {desc}")
            return

        # Positive response
        actual_sid = sid - 0x40
        annotation = ""

        if actual_sid == 0x10 and len(resp) > 1:
            # Session response
            sess = resp[1]
            annotation = f"  ← Session: {SESSION_NAMES.get(sess, f'0x{sess:02X}')}"
            self._current_session = sess
            self._update_session_indicator(sess)
            self.session_changed.emit(sess)

        elif actual_sid == 0x11:
            annotation = "  ← ECU Reset acknowledged"

        elif actual_sid == 0x22 and len(resp) >= 3:
            # RDBI — try decode as ASCII
            did = (resp[1] << 8) | resp[2]
            data = resp[3:]
            try:
                text = data.decode("ascii").strip()
                annotation = f"  ← DID 0x{did:04X}: \"{text}\""
            except Exception:
                annotation = f"  ← DID 0x{did:04X}: {data.hex()}"

        elif actual_sid == 0x3E:
            annotation = "  ← TesterPresent OK"

        self._log_line("rx", hex_str + annotation)

    def _on_error(self, msg: str):
        self._log_line("error", msg)

    def _update_session_indicator(self, sess: int):
        name = SESSION_NAMES.get(sess, f"Session 0x{sess:02X}")
        colors = {0x01: "#A6E3A1", 0x02: "#F38BA8", 0x03: "#FAB387"}
        color = colors.get(sess, "#CDD6F4")
        self._sess_indicator.setText(f"● {name}")
        self._sess_indicator.setStyleSheet(
            f"color:{color}; font-weight:bold; font-size:13px; padding:0 8px;")

    def _log_line(self, kind: str, text: str):
        colors = {
            "tx":     "#89B4FA",
            "rx":     "#A6E3A1",
            "nrc":    "#F38BA8",
            "error":  "#F38BA8",
            "system": "#585B70",
        }
        prefixes = {
            "tx": "→ TX", "rx": "← RX",
            "nrc": "✗ NRC", "error": "✗ ERR", "system": "  SYS",
        }
        color  = colors.get(kind, "#CDD6F4")
        prefix = prefixes.get(kind, "   ")
        from PySide6.QtCore import QDateTime
        ts = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
        html = (f'<span style="color:#585B70">[{ts}]</span> '
                f'<span style="color:{color}; font-family:monospace">'
                f'<b>{prefix}</b>  {text}</span>')
        self._log.appendHtml(html)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
