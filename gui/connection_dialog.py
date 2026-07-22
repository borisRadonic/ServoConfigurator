"""
Connection Dialog — profile-aware
Hides transport options and simulation based on AppProfile.
"""
from __future__ import annotations
import logging
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)
from transport.transport import AbstractTransport, CANTransport, SerialTransport, TransportError

log = logging.getLogger(__name__)


def _ports():
    try:
        import serial.tools.list_ports
        return [p.device for p in serial.tools.list_ports.comports()]
    except: return []


class _SerialPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QFormLayout(self); lay.setSpacing(10)
        self.port = QComboBox(); self.port.setEditable(True)
        ref = QPushButton("⟳"); ref.setFixedWidth(28)
        ref.clicked.connect(self._refresh)
        row = QHBoxLayout(); row.addWidget(self.port, 1); row.addWidget(ref)
        self.baud = QComboBox()
        for b in [9600,19200,38400,57600,115200,230400,460800,921600]:
            self.baud.addItem(str(b), b)
        self.baud.setCurrentText("115200")
        lay.addRow("Port:", row); lay.addRow("Baudrate:", self.baud)
        self._refresh()

    def _refresh(self):
        cur = self.port.currentText(); self.port.clear()
        ps = _ports(); self.port.addItems(ps if ps else ["COM1","/dev/ttyUSB0"])
        if cur:
            idx = self.port.findText(cur)
            if idx >= 0: self.port.setCurrentIndex(idx)

    def kwargs(self): return {"port": self.port.currentText(),
                               "baudrate": self.baud.currentData()}


class _CANPage(QWidget):
    def __init__(self):
        super().__init__()
        import core.app_profile as _cdpm
        _can = _cdpm.profile.can
        lay = QFormLayout(self); lay.setSpacing(10)
        self.iface = QComboBox()
        self.iface.addItems(["pcan","kvaser","socketcan","vector","ixxat"])
        self.iface.currentTextChanged.connect(self._on_iface)
        self.ch = QLineEdit("PCAN_USBBUS1")
        self.br = QComboBox()
        for b in [125000,250000,500000,1000000]:
            self.br.addItem(f"{b//1000} kbit/s", b)
        dbr = getattr(_can, "default_bitrate", 250000)
        _br_txt = f"{dbr//1000} kbit/s"
        if self.br.findText(_br_txt) >= 0:
            self.br.setCurrentText(_br_txt)
        else:
            self.br.setCurrentText("250 kbit/s")
        self.addr = QLineEdit("0xA0")
        note = QLabel("TX: 0x18DA<addr><0xF1>   RX: 0x18DA<0xF1><addr>")
        note.setStyleSheet("color:#585B70; font-size:11px;")
        lay.addRow("Interface:", self.iface); lay.addRow("Channel:", self.ch)
        lay.addRow("Bitrate:", self.br); lay.addRow("Device Address:", self.addr)
        lay.addRow("", note)

    def _on_iface(self, v):
        d = {"pcan":"PCAN_USBBUS1","socketcan":"can0","kvaser":"0"}
        if v in d: self.ch.setText(d[v])

    def set_device_address(self, addr: int):
        self.addr.setText(f"0x{addr:02X}")

    def kwargs(self):
        return {"interface": self.iface.currentText(),
                "channel":   self.ch.text(),
                "bitrate":   self.br.currentData(),
                "device_address": int(self.addr.text(), 0)}


class _TCPPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QFormLayout(self); lay.setSpacing(10)
        self.host = QLineEdit("127.0.0.1")
        self.port = QSpinBox(); self.port.setRange(1,65535); self.port.setValue(13400)
        note = QLabel("Local UDS server (BL library test app).\n"
                       "Same length+CRC framing as Serial.")
        note.setWordWrap(True); note.setStyleSheet("color:#89B4FA; font-size:11px;")
        lay.addRow("Host:", self.host); lay.addRow("Port:", self.port)
        lay.addRow("", note)

    def kwargs(self): return {"host": self.host.text(), "port": self.port.value()}


class _MockPage(QWidget):
    def __init__(self, profile):
        super().__init__()
        lay = QVBoxLayout(self)
        html = ("<b>Simulation Mode</b><br><br>"
                "Returns pre-configured values without hardware.<br><br>")
        if profile.simulation.ecu_info:
            n = len(profile.simulation.ecu_info)
            html += f"<span style='color:#89B4FA'>ECU Info: {n} DIDs configured</span><br>"
        if profile.simulation.dtc_list:
            n = len(profile.simulation.dtc_list)
            html += f"<span style='color:#FAB387'>DTCs: {n} entries configured</span><br>"
        html += ("<br><span style='color:#585B70; font-size:11px;'>"
                 "Configure in app_config.yaml → [simulation]</span>")
        lbl = QLabel(html)
        lbl.setWordWrap(True); lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("padding:20px;")
        lay.addWidget(lbl)

    def kwargs(self): return {}


class ConnectionDialog(QDialog):
    connected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.app_profile import profile
        self._profile = profile
        self.setWindowTitle("Connect to Motor Controller")
        self.setMinimumWidth(440)
        self.transport = None
        self._build()

    def _build(self):
        p = self._profile
        v = QVBoxLayout(self); v.setSpacing(14)

        tg = QGroupBox("Transport")
        tl = QHBoxLayout(tg)
        self._tc = QComboBox()
        self._pages = []   # (label, page, transport_type)

        if p.transports.serial:
            pg = _SerialPage()
            self._tc.addItem("Serial (UART)", "serial")
            self._pages.append(pg)
        if p.transports.can:
            self._can_page = _CANPage()
            self._tc.addItem("CAN (PEAK / python-can)", "can")
            self._pages.append(self._can_page)
        else:
            self._can_page = None
        if p.transports.tcp:
            pg = _TCPPage()
            self._tc.addItem("TCP/IP (local UDS server)", "tcp")
            self._pages.append(pg)
        if p.transports.mock and p.simulation.enabled:
            pg = _MockPage(p)
            self._tc.addItem("Simulation (Mock)", "mock")
            self._pages.append(pg)

        self._tc.currentIndexChanged.connect(
            lambda i: self._stack.setCurrentIndex(i))
        tl.addWidget(self._tc); v.addWidget(tg)

        cg = QGroupBox("Configuration"); cl = QVBoxLayout(cg)
        self._stack = QStackedWidget()
        for pg in self._pages:
            self._stack.addWidget(pg)
        cl.addWidget(self._stack); v.addWidget(cg)

        self._status = QLabel(""); self._status.setWordWrap(True)
        v.addWidget(self._status)

        bb = QDialogButtonBox()
        self._btn = bb.addButton("Connect", QDialogButtonBox.AcceptRole)
        bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._btn.clicked.connect(self._connect)
        bb.rejected.connect(self.reject); v.addWidget(bb)

        if not self._pages:
            self._btn.setEnabled(False)
            self._status.setText("No transports enabled in app_config.yaml")

    def _connect(self):
        transport_type = self._tc.currentData()
        page = self._pages[self._tc.currentIndex()]
        self._status.setText("Connecting…"); self._btn.setEnabled(False)
        try:
            if transport_type == "serial":
                from transport.transport import SerialTransport
                t = SerialTransport(); t.connect(**page.kwargs())
            elif transport_type == "can":
                kw  = page.kwargs()
                da  = kw.pop("device_address", 0xA0)
                fd  = kw.pop("fd_mode", False)
                dbr = kw.pop("data_bitrate", 2000000)
                t   = CANTransport(device_address=da,
                                   fd_mode=fd, data_bitrate=dbr)
                t.connect(**kw)
            elif transport_type == "tcp":
                from transport.tcp_transport import TCPTransport
                t = TCPTransport(); t.connect(**page.kwargs())
            else:  # mock
                from transport.mock_transport import MockTransport
                t = MockTransport(); t.connect()

            self.transport = t; self.connected.emit(t)
            self._status.setStyleSheet("color:#A6E3A1;")
            self._status.setText(f"✓ Connected via {t.name}")
            self.accept()
        except Exception as e:
            self._status.setStyleSheet("color:#F38BA8;")
            self._status.setText(f"✗ {e}"); self._btn.setEnabled(True)
