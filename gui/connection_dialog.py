from __future__ import annotations
import logging
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)
from transport.transport import AbstractTransport, CANTransport, MockTransport, SerialTransport, TransportError
from transport.tcp_transport import TCPTransport

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
        ref = QPushButton("⟳"); ref.setFixedWidth(28); ref.clicked.connect(self._refresh)
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
        if cur: idx = self.port.findText(cur)
        if cur and idx >= 0: self.port.setCurrentIndex(idx)
    def kwargs(self): return {"port":self.port.currentText(),"baudrate":self.baud.currentData()}

class _CANPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QFormLayout(self); lay.setSpacing(10)
        self.iface = QComboBox(); self.iface.addItems(["pcan","kvaser","socketcan","vector","ixxat"])
        self.iface.currentTextChanged.connect(self._on_iface)
        self.ch = QLineEdit("PCAN_USBBUS1")
        self.br = QComboBox()
        for b in [125000,250000,500000,1000000]: self.br.addItem(f"{b//1000} kbit/s", b)
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
    def kwargs(self):
        return {"interface":self.iface.currentText(),"channel":self.ch.text(),
                "bitrate":self.br.currentData(),"device_address":int(self.addr.text(),0)}

class _TCPPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QFormLayout(self); lay.setSpacing(10)
        self.host = QLineEdit("127.0.0.1")
        self.port = QSpinBox(); self.port.setRange(1,65535); self.port.setValue(13400)
        note = QLabel(
            "Lokalni UDS server (BL library test app).\n"
            "Server mora koristiti isti length+CRC framing.\n"
            "Port 13400 = DoIP standard."
        )
        note.setWordWrap(True); note.setStyleSheet("color:#89B4FA; font-size:11px;")
        lay.addRow("Host:", self.host); lay.addRow("Port:", self.port); lay.addRow("", note)
    def kwargs(self): return {"host":self.host.text(),"port":self.port.value()}

class _MockPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lbl = QLabel("<b>Simulation Mode</b><br><br>"
            "Vraća preddefinirane vrijednosti bez hardwarea.<br><br>"
            "<i>Za testiranje pravog UDS protokola koristi<br>"
            "TCP/IP transport s BL library test aplikacijom.</i>")
        lbl.setWordWrap(True); lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color:#6C7086; padding:20px;")
        lay.addWidget(lbl)
    def kwargs(self): return {}

class ConnectionDialog(QDialog):
    connected = Signal(object)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Motor Controller")
        self.setMinimumWidth(430)
        self.transport = None; self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setSpacing(14)

        tg = QGroupBox("Transport")
        tl = QHBoxLayout(tg)
        self._tc = QComboBox()
        self._tc.addItems(["Serial (UART)","CAN (PEAK / python-can)",
                            "TCP/IP (lokalni UDS server)","Simulation (Mock)"])
        self._tc.currentIndexChanged.connect(lambda i: self._stack.setCurrentIndex(i))
        tl.addWidget(self._tc); v.addWidget(tg)

        cg = QGroupBox("Configuration"); cl = QVBoxLayout(cg)
        self._stack = QStackedWidget()
        self._sp = _SerialPage(); self._cp = _CANPage()
        self._tp = _TCPPage();   self._mp = _MockPage()
        for p in [self._sp,self._cp,self._tp,self._mp]: self._stack.addWidget(p)
        cl.addWidget(self._stack); v.addWidget(cg)

        self._status = QLabel(""); self._status.setWordWrap(True); v.addWidget(self._status)

        bb = QDialogButtonBox()
        self._btn = bb.addButton("Connect", QDialogButtonBox.AcceptRole)
        bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._btn.clicked.connect(self._connect)
        bb.rejected.connect(self.reject); v.addWidget(bb)

    def _connect(self):
        idx = self._tc.currentIndex()
        self._status.setText("Connecting…"); self._btn.setEnabled(False)
        try:
            if idx == 0:
                t = SerialTransport(); t.connect(**self._sp.kwargs())
            elif idx == 1:
                kw = self._cp.kwargs(); da = kw.pop("device_address", 0xA0)
                t = CANTransport(device_address=da); t.connect(**kw)
            elif idx == 2:
                t = TCPTransport(); t.connect(**self._tp.kwargs())
            else:
                t = MockTransport(); t.connect()
            self.transport = t; self.connected.emit(t)
            self._status.setStyleSheet("color:#A6E3A1;")
            self._status.setText(f"✓ Connected via {t.name}")
            self.accept()
        except Exception as e:
            self._status.setStyleSheet("color:#F38BA8;")
            self._status.setText(f"✗ {e}"); self._btn.setEnabled(True)
