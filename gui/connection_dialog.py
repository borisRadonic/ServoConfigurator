"""
Connection Dialog
=================
Lets the user choose transport type, configure connection parameters,
and connect / disconnect.
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from transport.transport import AbstractTransport, CANTransport, MockTransport, SerialTransport, TransportError

log = logging.getLogger(__name__)


def _list_serial_ports() -> list[str]:
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
        self._refresh_ports()

        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(30)
        refresh_btn.clicked.connect(self._refresh_ports)
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(refresh_btn)

        self.baud_combo = QComboBox()
        for b in [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]:
            self.baud_combo.addItem(str(b), b)
        self.baud_combo.setCurrentText("115200")

        layout.addRow("Port:", port_row)
        layout.addRow("Baudrate:", self.baud_combo)

    def _refresh_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = _list_serial_ports()
        self.port_combo.addItems(ports if ports else ["COM1", "/dev/ttyUSB0"])
        if current:
            idx = self.port_combo.findText(current)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

    def get_kwargs(self) -> dict:
        return {
            "port": self.port_combo.currentText(),
            "baudrate": self.baud_combo.currentData(),
        }


class _CANPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QFormLayout(self)
        layout.setSpacing(10)

        self.interface_combo = QComboBox()
        self.interface_combo.addItems(["pcan", "kvaser", "socketcan", "vector", "ixxat"])
        self.interface_combo.currentTextChanged.connect(self._on_interface_changed)

        self.channel_edit = QLineEdit("PCAN_USBBUS1")

        self.bitrate_combo = QComboBox()
        for b in [125000, 250000, 500000, 1000000]:
            self.bitrate_combo.addItem(f"{b//1000} kbit/s", b)
        self.bitrate_combo.setCurrentText("250 kbit/s")

        # Device_Address from NvStore/EEPROM (VinBT-259/260)
        # Default 0xA0 = DEVICE_ADDRESS_VAL (BL library)
        # TX = 0x18DA<device_addr><0xF1>  e.g. 0x18DAA0F1
        # RX = 0x18DA<0xF1><device_addr>  e.g. 0x18DAF1A0
        self.device_addr_edit = QLineEdit("0xA0")

        layout.addRow("Interface:", self.interface_combo)
        layout.addRow("Channel:", self.channel_edit)
        layout.addRow("Bitrate:", self.bitrate_combo)
        layout.addRow("Device Address:", self.device_addr_edit)

    def _on_interface_changed(self, iface: str):
        defaults = {
            "pcan":      "PCAN_USBBUS1",
            "socketcan": "can0",
            "kvaser":    "0",
        }
        if iface in defaults:
            self.channel_edit.setText(defaults[iface])

    def get_kwargs(self) -> dict:
        return {
            "interface": self.interface_combo.currentText(),
            "channel": self.channel_edit.text(),
            "bitrate": self.bitrate_combo.currentData(),
            "device_address": int(self.device_addr_edit.text(), 0),
        }


class ConnectionDialog(QDialog):
    """
    Modal dialog for transport selection and connection setup.
    On accept(), exposes .transport with the connected transport instance.
    """

    connected = Signal(object)  # emits the transport

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Motor Controller")
        self.setMinimumWidth(400)
        self.transport: Optional[AbstractTransport] = None
        self._build_ui()

    def _build_ui(self):
        vbox = QVBoxLayout(self)
        vbox.setSpacing(14)

        # Transport type selector
        type_group = QGroupBox("Transport")
        type_layout = QHBoxLayout(type_group)
        self._type_combo = QComboBox()
        self._type_combo.addItems(["Serial (UART)", "CAN (PEAK/python-can)", "Simulation (Mock)"])
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_layout.addWidget(self._type_combo)
        vbox.addWidget(type_group)

        # Stacked config pages
        config_group = QGroupBox("Configuration")
        config_vbox = QVBoxLayout(config_group)
        self._stack = QStackedWidget()
        self._serial_page = _SerialPage()
        self._can_page = _CANPage()
        self._mock_page = QLabel(
            "Simulation mode – no hardware required.\n"
            "All reads return plausible default values."
        )
        self._mock_page.setAlignment(Qt.AlignCenter)
        self._mock_page.setStyleSheet("color: #888; font-style: italic;")
        self._stack.addWidget(self._serial_page)
        self._stack.addWidget(self._can_page)
        self._stack.addWidget(self._mock_page)
        config_vbox.addWidget(self._stack)
        vbox.addWidget(config_group)

        # Status
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        vbox.addWidget(self._status_label)

        # Buttons
        self._btn_box = QDialogButtonBox()
        self._connect_btn = self._btn_box.addButton("Connect", QDialogButtonBox.AcceptRole)
        self._cancel_btn = self._btn_box.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._connect_btn.clicked.connect(self._do_connect)
        self._cancel_btn.clicked.connect(self.reject)
        vbox.addWidget(self._btn_box)

    def _on_type_changed(self, idx: int):
        self._stack.setCurrentIndex(idx)

    def _do_connect(self):
        idx = self._type_combo.currentIndex()
        self._status_label.setText("Connecting…")
        self._connect_btn.setEnabled(False)

        try:
            if idx == 0:  # Serial
                kwargs = self._serial_page.get_kwargs()
                transport = SerialTransport()
                transport.connect(**kwargs)
            elif idx == 1:  # CAN
                kwargs = self._can_page.get_kwargs()
                device_address = kwargs.pop("device_address", 0xA0)
                transport = CANTransport(device_address=device_address)
                transport.connect(**kwargs)
            else:  # Mock
                transport = MockTransport()
                transport.connect()

            self.transport = transport
            self.connected.emit(transport)
            self._status_label.setStyleSheet("color: #4CAF50;")
            self._status_label.setText(f"✓ Connected via {transport.name}")
            self.accept()

        except TransportError as e:
            self._status_label.setStyleSheet("color: #F44336;")
            self._status_label.setText(f"✗ {e}")
            self._connect_btn.setEnabled(True)
        except Exception as e:
            self._status_label.setStyleSheet("color: #F44336;")
            self._status_label.setText(f"✗ Unexpected error: {e}")
            self._connect_btn.setEnabled(True)
