"""
Connection Dialog
=================
Transport: Serial | CAN (PEAK) | TCP/IP (lokalni UDS server) | Mock
"""
from __future__ import annotations
import logging
from typing import Optional
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)
from transport.transport import AbstractTransport, CANTransport, MockTransport, SerialTransport, TransportError
from transport.tcp_transport import TCPTransport

log = logging.getLogger(__name__)


def _list_serial_ports():
    try:
        import serial.tools.list_ports
        return [p.device for p in serial.tools.list_ports.comports()]
    except Exception:
        return []


class _SerialPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QFormLayout(self)
        layout.setSpacing(10)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(30)
        refresh_btn.clicked.connect(self._refresh)
        row = QHBoxLayout()
        row.addWidget(self.port_combo, 1)
        row.addWidget(refresh_btn)
        self.baud_combo = QComboBox()
        for b in [9600,19200,38400,57600,115200,230400,460800,921600]:
            self.baud_combo.addItem(str(b), b)
        self.baud_combo.setCurrentText("115200")
        layout.addRow("Port:", row)
        layout.addRow("Baudrate:", self.baud_combo)
        self._refresh()

    def _refresh(self):
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        ports = _list_serial_ports()
        self.port_combo.addItems(ports if ports else ["COM1", "/dev/ttyUSB0"])
        if cur:
            idx = self.port_combo.findText(cur)
            if idx >= 0: self.port_combo.setCurrentIndex(idx)

    def get_kwargs(self):
        return {"port": self.port_combo.currentText(),
                "baudrate": self.baud_combo.currentData()}


class _CANPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QFormLayout(self)
        layout.setSpacing(10)
        self.interface_combo = QComboBox()
        self.interface_combo.addItems(["pcan","kvaser","socketcan","vector","ixxat"])
        self.interface_combo.currentTextChanged.connect(self._on_iface)
        self.channel_edit = QLineEdit("PCAN_USBBUS1")
        self.bitrate_combo = QComboBox()
        for b in [125000,250000,500000,1000000]:
            self.bitrate_combo.addItem(f"{b//1000} kbit/s", b)
        self.bitrate_combo.setCurrentText("250 kbit/s")
        # Device address (BL library: DEVICE_ADDRESS_VAL=0xA0)
        self.device_addr_edit = QLineEdit("0xA0")
        note = QLabel("TX=0x18DA<addr><0xF1>  RX=0x18DA<0xF1><addr>")
        note.setStyleSheet("color:#6C7086; font-size:11px;")
        layout.addRow("Interface:", self.interface_combo)
        layout.addRow("Channel:", self.channel_edit)
        layout.addRow("Bitrate:", self.bitrate_combo)
        layout.addRow("Device Address:", self.device_addr_edit)
        layout.addRow("", note)

    def _on_iface(self, iface):
        defaults = {"pcan":"PCAN_USBBUS1","socketcan":"can0","kvaser":"0"}
        if iface in defaults:
            self.channel_edit.setText(defaults[iface])

    def get_kwargs(self):
        return {
            "interface": self.interface_combo.currentText(),
            "channel":   self.channel_edit.text(),
            "bitrate":   self.bitrate_combo.currentData(),
            "device_address": int(self.device_addr_edit.text(), 0),
        }


class _TCPPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QFormLayout(self)
        layout.setSpacing(10)
        self.host_edit = QLineEdit("127.0.0.1")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(13400)
        note = QLabel(
            "Spoji se na lokalni UDS server (npr. BL library test app).\n"
            "Server mora koristiti isti length+CRC framing kao Serial transport.\n"
            "Port 13400 = DoIP standard."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#89B4FA; font-size:11px;")
        layout.addRow("Host:", self.host_edit)
        layout.addRow("Port:", self.port_spin)
        layout.addRow("", note)

    def get_kwargs(self):
        return {"host": self.host_edit.text(), "port": self.port_spin.value()}


class _MockPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel(
            "<b>Simulation Mode</b><br><br>"
            "Nema hardwarea — sve RDBI vraćaju preddefinirane vrijednosti.<br>"
            "Korisno za UI razvoj.<br><br>"
            "<i>Za testiranje pravog protokola, koristi TCP/IP transport<br>"
            "s lokalnom BL library test aplikacijom.</i>"
        )
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color:#888; padding:20px;")
        layout.addWidget(label)

    def get_kwargs(self):
        return {}


class ConnectionDialog(QDialog):
    connected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Motor Controller")
        self.setMinimumWidth(420)
        self.transport: Optional[AbstractTransport] = None
        self._build_ui()

    def _build_ui(self):
        vbox = QVBoxLayout(self)
        vbox.setSpacing(14)

        type_group = QGroupBox("Transport")
        tl = QHBoxLayout(type_group)
        self._type_combo = QComboBox()
        self._type_combo.addItems([
            "Serial (UART)",
            "CAN (PEAK / python-can)",
            "TCP/IP (lokalni UDS server)",
            "Simulation (Mock)",
        ])
        self._type_combo.currentIndexChanged.connect(self._on_type)
        tl.addWidget(self._type_combo)
        vbox.addWidget(type_group)

        cfg_group = QGroupBox("Configuration")
        cl = QVBoxLayout(cfg_group)
        self._stack = QStackedWidget()
        self._serial_page = _SerialPage()
        self._can_page    = _CANPage()
        self._tcp_page    = _TCPPage()
        self._mock_page   = _MockPage()
        for p in [self._serial_page, self._can_page, self._tcp_page, self._mock_page]:
            self._stack.addWidget(p)
        cl.addWidget(self._stack)
        vbox.addWidget(cfg_group)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        vbox.addWidget(self._status)

        btn_box = QDialogButtonBox()
        self._connect_btn = btn_box.addButton("Connect", QDialogButtonBox.AcceptRole)
        btn_box.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._connect_btn.clicked.connect(self._do_connect)
        btn_box.rejected.connect(self.reject)
        vbox.addWidget(btn_box)

    def _on_type(self, idx):
        self._stack.setCurrentIndex(idx)

    def _do_connect(self):
        idx = self._type_combo.currentIndex()
        self._status.setText("Connecting…")
        self._connect_btn.setEnabled(False)
        try:
            if idx == 0:
                t = SerialTransport()
                t.connect(**self._serial_page.get_kwargs())
            elif idx == 1:
                kw = self._can_page.get_kwargs()
                dev_addr = kw.pop("device_address", 0xA0)
                t = CANTransport(device_address=dev_addr)
                t.connect(**kw)
            elif idx == 2:
                t = TCPTransport()
                t.connect(**self._tcp_page.get_kwargs())
            else:
                t = MockTransport()
                t.connect()

            self.transport = t
            self.connected.emit(t)
            self._status.setStyleSheet("color:#A6E3A1;")
            self._status.setText(f"✓ Connected via {t.name}")
            self.accept()
        except TransportError as e:
            self._status.setStyleSheet("color:#F38BA8;")
            self._status.setText(f"✗ {e}")
            self._connect_btn.setEnabled(True)
        except Exception as e:
            self._status.setStyleSheet("color:#F38BA8;")
            self._status.setText(f"✗ {e}")
            self._connect_btn.setEnabled(True)
