"""
DTC Panel — ReadDTCInformation (0x19) + ClearDiagnosticInformation (0x14)
"""
from __future__ import annotations
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread, QObject, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from transport.transport import AbstractTransport, TransportError
from uds.codec import (
    DTCRecord, DTCStatusMask, NRC,
    ServiceID, UDSCodecExtended, UDSDecodeError, UDSNegativeResponse,
)

log = logging.getLogger(__name__)


# ── Worker ────────────────────────────────────────────────────────────

class _DTCWorker(QObject):
    dtcs_read  = Signal(list)    # list[DTCRecord]
    count_read = Signal(int)
    cleared    = Signal()
    error      = Signal(str)

    def __init__(self, transport: AbstractTransport):
        super().__init__()
        self._transport = transport

    @Slot(int)
    def read_dtcs(self, mask: int):
        try:
            req  = UDSCodecExtended.encode_read_dtc_by_status(mask)
            resp = self._transport.send_and_wait(req, timeout=2.0)
            records = UDSCodecExtended.decode_dtc_response(resp)
            self.dtcs_read.emit(records)
        except UDSNegativeResponse as e:
            self.error.emit(f"NRC 0x{e.nrc:02X}: {NRC.description(e.nrc)}")
        except Exception as e:
            self.error.emit(str(e))

    @Slot()
    def clear_dtcs(self):
        try:
            req  = UDSCodecExtended.encode_clear_dtc(0xFFFFFF)
            resp = self._transport.send_and_wait(req, timeout=5.0)
            if resp[0] == (ServiceID.CLEAR_DTC | 0x40):
                self.cleared.emit()
            else:
                self.error.emit(f"Unexpected response: {resp.hex()}")
        except UDSNegativeResponse as e:
            self.error.emit(f"NRC 0x{e.nrc:02X}: {NRC.description(e.nrc)}")
        except Exception as e:
            self.error.emit(str(e))


class _Bridge(QObject):
    sig_read  = Signal(int)
    sig_clear = Signal()


# ── Panel ─────────────────────────────────────────────────────────────

class DTCPanel(QWidget):
    """Diagnostic Trouble Code reader and clearer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._transport: Optional[AbstractTransport] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_DTCWorker] = None
        self._bridge: Optional[_Bridge] = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 18, 20, 18)

        # ── Controls ─────────────────────────────────────────────
        ctrl_group = QGroupBox("DTC Query")
        cl = QHBoxLayout(ctrl_group)
        cl.setSpacing(10)

        mask_label = QLabel("Status Filter:")
        self._mask_combo = QComboBox()
        self._mask_combo.addItem("All DTCs",             DTCStatusMask.ALL)
        self._mask_combo.addItem("Confirmed only",       DTCStatusMask.CONFIRMED)
        self._mask_combo.addItem("Pending only",         DTCStatusMask.PENDING)
        self._mask_combo.addItem("Test Failed",          DTCStatusMask.TEST_FAILED)
        self._mask_combo.addItem("Warning Indicator",    DTCStatusMask.WARNING_INDICATOR_REQUESTED)
        self._mask_combo.setMinimumWidth(180)

        self._read_btn = QPushButton("⟳  Read DTCs")
        self._read_btn.setObjectName("primaryBtn")
        self._read_btn.setEnabled(False)
        self._read_btn.setFixedHeight(34)
        self._read_btn.clicked.connect(self._read_dtcs)

        self._clear_btn = QPushButton("🗑  Clear All")
        self._clear_btn.setEnabled(False)
        self._clear_btn.setFixedHeight(34)
        self._clear_btn.setFixedWidth(110)
        self._clear_btn.clicked.connect(self._clear_dtcs)

        self._status_label = QLabel("Not connected")
        self._status_label.setStyleSheet("color:#585B70; font-size:12px;")

        cl.addWidget(mask_label)
        cl.addWidget(self._mask_combo)
        cl.addWidget(self._read_btn)
        cl.addWidget(self._clear_btn)
        cl.addStretch()
        cl.addWidget(self._status_label)
        root.addWidget(ctrl_group)

        # ── Summary bar ───────────────────────────────────────────
        summary_row = QHBoxLayout()
        self._total_label    = QLabel("Total: —")
        self._confirmed_label = QLabel("Confirmed: —")
        self._pending_label  = QLabel("Pending: —")
        for lbl in [self._total_label, self._confirmed_label, self._pending_label]:
            lbl.setStyleSheet("font-size:13px; font-weight:bold; color:#CDD6F4; padding: 4px 12px;")
        summary_row.addWidget(self._total_label)
        summary_row.addWidget(self._confirmed_label)
        summary_row.addWidget(self._pending_label)
        summary_row.addStretch()
        root.addLayout(summary_row)

        # ── DTC Table ─────────────────────────────────────────────
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["DTC Code", "Status Byte", "Test Failed", "Confirmed", "Pending"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(30)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)

        root.addWidget(self._table, 1)

    # ── Public API ────────────────────────────────────────────────

    def set_transport(self, transport: Optional[AbstractTransport]):
        # Shutdown old thread
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)

        self._transport = transport
        connected = transport is not None

        if connected:
            self._thread  = QThread(self)
            self._worker  = _DTCWorker(transport)
            self._bridge  = _Bridge(self)
            self._worker.moveToThread(self._thread)
            self._bridge.sig_read.connect(self._worker.read_dtcs)
            self._bridge.sig_clear.connect(self._worker.clear_dtcs)
            self._worker.dtcs_read.connect(self._on_dtcs_read)
            self._worker.cleared.connect(self._on_cleared)
            self._worker.error.connect(self._on_error)
            self._thread.start()

        self._read_btn.setEnabled(connected)
        self._clear_btn.setEnabled(connected)
        self._status_label.setText("Ready" if connected else "Not connected")
        self._status_label.setStyleSheet(
            "color:#A6E3A1; font-size:12px;" if connected
            else "color:#585B70; font-size:12px;")

        if not connected:
            self._table.setRowCount(0)
            self._update_summary([])

    # ── Private ───────────────────────────────────────────────────

    def _read_dtcs(self):
        if not self._bridge: return
        mask = self._mask_combo.currentData()
        self._read_btn.setEnabled(False)
        self._status_label.setText("Reading…")
        self._status_label.setStyleSheet("color:#89B4FA; font-size:12px;")
        self._bridge.sig_read.emit(mask)

    def _clear_dtcs(self):
        if not self._bridge: return
        r = QMessageBox.question(
            self, "Clear DTCs",
            "Clear ALL diagnostic trouble codes on the ECU?\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes: return
        self._clear_btn.setEnabled(False)
        self._status_label.setText("Clearing…")
        self._status_label.setStyleSheet("color:#FAB387; font-size:12px;")
        self._bridge.sig_clear.emit()

    def _on_dtcs_read(self, records: list):
        self._read_btn.setEnabled(True)
        self._table.setRowCount(0)

        # Sort: confirmed first, then pending, then rest
        def sort_key(r: DTCRecord):
            if r.status & DTCStatusMask.CONFIRMED: return 0
            if r.status & DTCStatusMask.PENDING:   return 1
            return 2

        records.sort(key=sort_key)

        for rec in records:
            row = self._table.rowCount()
            self._table.insertRow(row)

            # DTC code
            code_item = QTableWidgetItem(rec.dtc_str)
            code_item.setFont(QFont("Consolas, Courier New", 11))
            code_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 0, code_item)

            # Status byte hex
            status_item = QTableWidgetItem(f"0x{rec.status:02X}")
            status_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 1, status_item)

            # Boolean columns
            tf = rec.status & DTCStatusMask.TEST_FAILED
            co = rec.status & DTCStatusMask.CONFIRMED
            pe = rec.status & DTCStatusMask.PENDING

            for col, active in [(2, tf), (3, co), (4, pe)]:
                text = "✓" if active else "–"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if active:
                    item.setForeground(QColor("#F38BA8") if col == 2 else
                                       QColor("#FAB387") if col == 3 else
                                       QColor("#F9E2AF"))
                self._table.setItem(row, col, item)

            # Row colour for confirmed DTCs
            if co:
                for c in range(5):
                    it = self._table.item(row, c)
                    if it: it.setBackground(QColor("#3D1A1A"))

        n = len(records)
        msg = f"✓ {n} DTC{'s' if n!=1 else ''} read"
        self._status_label.setText(msg)
        self._status_label.setStyleSheet("color:#A6E3A1; font-size:12px;")
        self._update_summary(records)
        log.info("DTC read: %d records", n)

    def _on_cleared(self):
        self._clear_btn.setEnabled(True)
        self._table.setRowCount(0)
        self._update_summary([])
        self._status_label.setText("✓ DTCs cleared")
        self._status_label.setStyleSheet("color:#A6E3A1; font-size:12px;")
        log.info("DTCs cleared")

    def _on_error(self, msg: str):
        self._read_btn.setEnabled(True)
        self._clear_btn.setEnabled(True)
        self._status_label.setText(f"✗ {msg}")
        self._status_label.setStyleSheet("color:#F38BA8; font-size:12px;")
        log.error("DTC error: %s", msg)

    def _update_summary(self, records: list):
        total     = len(records)
        confirmed = sum(1 for r in records if r.status & DTCStatusMask.CONFIRMED)
        pending   = sum(1 for r in records if r.status & DTCStatusMask.PENDING)

        self._total_label.setText(f"Total: {total if total else '—'}")
        color_c = "#F38BA8" if confirmed else "#585B70"
        color_p = "#F9E2AF" if pending   else "#585B70"
        self._confirmed_label.setText(f"Confirmed: {confirmed if total else '—'}")
        self._confirmed_label.setStyleSheet(f"font-size:13px; font-weight:bold; color:{color_c}; padding:4px 12px;")
        self._pending_label.setText(f"Pending: {pending if total else '—'}")
        self._pending_label.setStyleSheet(f"font-size:13px; font-weight:bold; color:{color_p}; padding:4px 12px;")
