"""
Device Scanner
==============
Šalje UDS TesterPresent (3E 00) na functional broadcast adresu
0x18DB33F1 i presluškuje fizičke odgovore 0x18DAF1xx.

Svaki odgovor otkriva device_address iz CAN ID-a:
    RX: 0x18DA F1 <device_addr>  →  byte 0xFF & CAN_ID = device_address

Standardni UDS — nije ništa nestandardno:
  - Functional addressing: ISO 15765-2, SAE J1939
  - TesterPresent 3E 00 (sub=0x00 → respond): ISO 14229-1 §9.3.5
  - Svaki uređaj MORA odgovoriti ako sub bit 7 nije postavljen

Problem duplikata:
  - Ako više uređaja ima istu adresu, oba odgovaraju na isti CAN ID
  - CAN arbitration može prikriti jedan od odgovora
  - Scanner prikazuje upozorenje ako detektuje koliziju (timeout/corrupt)
  - Rješenje: promjena device_address kroz WDBI na DID koji mapira
    na NvField::DeviceAddress (offset 0x000A u EEPROM-u)

Samo CAN transport podržava scan — Serial i TCP imaju samo jedan
uređaj po liniji pa scan nema smisla.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import (
    QObject, QThread, Qt, Signal, Slot, QTimer,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from transport.transport import CANTransport, TransportError

log = logging.getLogger(__name__)

# Functional broadcast — all devices on bus receive this
FUNCTIONAL_TX_ID = 0x18DB33F1
# TesterPresent with sub=0x00 (respond, not suppress)
TESTER_PRESENT_RESPOND = bytes([0x3E, 0x00])
# Scan window — how long to wait for responses after broadcast
SCAN_WINDOW_MS = 300
# How many broadcasts to send (some devices may miss the first)
SCAN_REPEAT = 3
SCAN_REPEAT_INTERVAL_MS = 80


class _ScanWorker(QObject):
    """Runs in QThread. Sends broadcast and collects responses."""

    device_found = Signal(int, bool)  # device_addr, possible_collision
    scan_done    = Signal()
    error        = Signal(str)

    def __init__(self, transport: CANTransport):
        super().__init__()
        self._transport = transport
        self._found: dict[int, int] = {}   # addr → response count
        self._cancelled = False

    @Slot()
    def run(self):
        self._found.clear()
        self._cancelled = False

        # Register scan callback on transport
        self._transport.set_scan_callback(self._on_frame)

        try:
            # Send broadcast N times with short interval
            for i in range(SCAN_REPEAT):
                if self._cancelled:
                    break
                try:
                    self._transport.send_functional(TESTER_PRESENT_RESPOND)
                    log.debug("Scan broadcast %d sent", i + 1)
                except TransportError as e:
                    self.error.emit(f"Send failed: {e}")
                    return
                time.sleep(SCAN_REPEAT_INTERVAL_MS / 1000.0)

            # Wait for remaining responses
            remaining = SCAN_WINDOW_MS - SCAN_REPEAT * SCAN_REPEAT_INTERVAL_MS
            if remaining > 0:
                time.sleep(remaining / 1000.0)

        finally:
            self._transport.set_scan_callback(None)

        # Emit results
        for addr, count in sorted(self._found.items()):
            collision = count > SCAN_REPEAT
            self.device_found.emit(addr, collision)
            log.info("Scanner: 0x%02X  responses=%d  collision=%s",
                     addr, count, collision)

        self.scan_done.emit()

    def _on_frame(self, device_addr: int, data: bytes):
        """Called from CAN RX thread — just count, emit from main thread."""
        if len(data) >= 2 and data[0] == 0x7E:  # positive TesterPresent response
            self._found[device_addr] = self._found.get(device_addr, 0) + 1

    def cancel(self):
        self._cancelled = True


class _Bridge(QObject):
    sig_run = Signal()


class DeviceScannerDialog(QDialog):
    """
    Modal dialog for CAN device discovery.

    Usage:
        dlg = DeviceScannerDialog(can_transport, parent=self)
        if dlg.exec() == QDialog.Accepted:
            addr = dlg.selected_address   # None if none selected
    """

    def __init__(self, transport: CANTransport, parent=None):
        super().__init__(parent)
        self._transport = transport
        self._thread: Optional[QThread] = None
        self._worker: Optional[_ScanWorker] = None
        self._bridge: Optional[_Bridge] = None
        self.selected_address: Optional[int] = None

        self.setWindowTitle("CAN Device Scanner")
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(18, 16, 18, 16)

        # Info
        info = QLabel(
            "Sends <b>TesterPresent (3E 00)</b> on functional broadcast "
            "<b>0x18DB33F1</b>.<br>"
            "Each device replies with its <b>device_address</b> in the CAN ID.<br>"
            "<span style='color:#FAB387'>⚠ Duplicate addresses cause CAN collisions "
            "— change address via WDBI before use.</span>"
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        info.setStyleSheet("color:#BAC2DE; font-size:12px; padding:4px;")
        root.addWidget(info)

        # Controls
        ctrl = QHBoxLayout()
        self._scan_btn = QPushButton("🔍  Start Scan")
        self._scan_btn.setObjectName("primaryBtn")
        self._scan_btn.setFixedHeight(34)
        self._scan_btn.clicked.connect(self._start_scan)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.setMaximumWidth(120)
        self._progress.setMinimumHeight(18)
        self._progress.hide()

        self._status = QLabel("Ready")
        self._status.setStyleSheet("color:#6C7086; font-size:12px;")

        ctrl.addWidget(self._scan_btn)
        ctrl.addWidget(self._progress)
        ctrl.addWidget(self._status)
        ctrl.addStretch()
        root.addLayout(ctrl)

        # Results table
        res_group = QGroupBox("Discovered Devices")
        rl = QVBoxLayout(res_group)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Device Address", "CAN RX ID", "Status", ""])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(32)
        self._table.itemSelectionChanged.connect(self._on_selection)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        rl.addWidget(self._table)
        root.addWidget(res_group, 1)

        # Duplicate warning
        self._dup_warning = QLabel("")
        self._dup_warning.setWordWrap(True)
        self._dup_warning.setStyleSheet(
            "color:#F38BA8; font-size:12px; padding:4px;")
        self._dup_warning.hide()
        root.addWidget(self._dup_warning)

        # Buttons
        self._btn_box = QDialogButtonBox()
        self._connect_btn = self._btn_box.addButton(
            "Connect to Selected", QDialogButtonBox.AcceptRole)
        self._connect_btn.setObjectName("primaryBtn")
        self._connect_btn.setEnabled(False)
        self._connect_btn.clicked.connect(self._on_connect)
        self._btn_box.addButton("Close", QDialogButtonBox.RejectRole)
        self._btn_box.rejected.connect(self.reject)
        root.addWidget(self._btn_box)

    # ── Scan ─────────────────────────────────────────────────────

    def _start_scan(self):
        self._table.setRowCount(0)
        self._dup_warning.hide()
        self._connect_btn.setEnabled(False)
        self._scan_btn.setEnabled(False)
        self._progress.show()
        self._status.setText("Scanning…")
        self._status.setStyleSheet("color:#89B4FA; font-size:12px;")

        self._thread = QThread(self)
        self._worker = _ScanWorker(self._transport)
        self._bridge = _Bridge(self)
        self._worker.moveToThread(self._thread)

        self._bridge.sig_run.connect(self._worker.run)
        self._worker.device_found.connect(self._on_device_found)
        self._worker.scan_done.connect(self._on_scan_done)
        self._worker.error.connect(self._on_error)
        self._thread.start()
        self._bridge.sig_run.emit()

    def _on_device_found(self, addr: int, collision: bool):
        row = self._table.rowCount()
        self._table.insertRow(row)

        rx_id = 0x18DA0000 | (0xF1 << 8) | addr

        # Address
        addr_item = QTableWidgetItem(f"0x{addr:02X}  ({addr})")
        addr_item.setFont(QFont("Consolas, Courier New", 11))
        addr_item.setTextAlignment(Qt.AlignCenter)
        addr_item.setData(Qt.UserRole, addr)
        self._table.setItem(row, 0, addr_item)

        # CAN RX ID
        rx_item = QTableWidgetItem(f"0x{rx_id:08X}")
        rx_item.setFont(QFont("Consolas, Courier New", 10))
        rx_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, 1, rx_item)

        # Status
        if collision:
            status_item = QTableWidgetItem("⚠  DUPLICATE ADDRESS — collision detected")
            status_item.setForeground(QColor("#F38BA8"))
        else:
            status_item = QTableWidgetItem("✓  OK")
            status_item.setForeground(QColor("#A6E3A1"))
        self._table.setItem(row, 2, status_item)

        # Connect button per row
        conn_item = QTableWidgetItem("→ Select")
        conn_item.setTextAlignment(Qt.AlignCenter)
        conn_item.setForeground(QColor("#89B4FA"))
        self._table.setItem(row, 3, conn_item)

        if collision:
            for c in range(4):
                it = self._table.item(row, c)
                if it: it.setBackground(QColor("#3D1A1A"))

    def _on_scan_done(self):
        self._progress.hide()
        self._scan_btn.setEnabled(True)

        n = self._table.rowCount()
        if n == 0:
            self._status.setText("No devices found")
            self._status.setStyleSheet("color:#FAB387; font-size:12px;")
        else:
            self._status.setText(f"✓ {n} device{'s' if n > 1 else ''} found")
            self._status.setStyleSheet("color:#A6E3A1; font-size:12px;")

        # Check for duplicates
        duplicates = []
        for row in range(n):
            it = self._table.item(row, 2)
            if it and "DUPLICATE" in it.text():
                addr_it = self._table.item(row, 0)
                if addr_it:
                    duplicates.append(addr_it.text().split()[0])

        if duplicates:
            self._dup_warning.setText(
                f"⚠  Duplicate addresses detected: {', '.join(duplicates)}\n"
                "Connect to one device using physical addressing, then use\n"
                "Device → Change Device Address to assign a unique address,\n"
                "followed by ECU Reset. Then re-scan."
            )
            self._dup_warning.show()

        if self._thread:
            self._thread.quit()
            self._thread.wait(1000)

    def _on_error(self, msg: str):
        self._progress.hide()
        self._scan_btn.setEnabled(True)
        self._status.setText(f"✗ {msg}")
        self._status.setStyleSheet("color:#F38BA8; font-size:12px;")
        log.error("Scanner error: %s", msg)

    def _on_selection(self):
        rows = self._table.selectedItems()
        has_sel = len(rows) > 0
        # Don't allow connecting to a collision device
        if has_sel:
            row = self._table.currentRow()
            it = self._table.item(row, 2)
            if it and "DUPLICATE" in it.text():
                has_sel = False
        self._connect_btn.setEnabled(has_sel)

    def _on_connect(self):
        row = self._table.currentRow()
        if row < 0:
            return
        addr_item = self._table.item(row, 0)
        if addr_item:
            self.selected_address = addr_item.data(Qt.UserRole)
            self.accept()
