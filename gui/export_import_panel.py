"""
Export / Import Panel
=====================
Sub-tab in Configuration Management:
  Export: read loaded device values → JSON or CSV
  Import: load JSON/CSV → preview table → stage in BatchWriter
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton,
    QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QSplitter,
    QButtonGroup, QFrame,
)

from core.parameter_model import ParameterStore
from core.batch_writer import BatchWriter
from core.param_io import load_import, save_export, ImportResult

log = logging.getLogger(__name__)


class ExportImportPanel(QWidget):
    status_message = Signal(str)  # for main window status bar

    def __init__(self, store: ParameterStore,
                 batch: BatchWriter, parent=None):
        super().__init__(parent)
        self._store = store
        self._batch = batch
        self._import_result: Optional[ImportResult] = None
        self._device_address: Optional[int] = None
        self._build_ui()

    def set_device_address(self, addr: Optional[int]):
        self._device_address = addr

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)

        # ── Left: Export ─────────────────────────────────────────
        export_widget = QWidget()
        export_layout = QVBoxLayout(export_widget)
        export_layout.setSpacing(12)
        export_layout.setContentsMargins(16, 16, 8, 16)

        exp_title = QLabel("Export Parameters")
        exp_title.setStyleSheet("font-weight:bold; font-size:13px; color:#89B4FA;")
        export_layout.addWidget(exp_title)

        export_info = QLabel(
            "Export currently loaded device values\n"
            "to a file for backup or documentation.")
        export_info.setStyleSheet("color:#6C7086; font-size:12px;")
        export_info.setWordWrap(True)
        export_layout.addWidget(export_info)

        # Format selection
        fmt_group = QGroupBox("Format")
        fmt_layout = QHBoxLayout(fmt_group)
        self._exp_json = QRadioButton("JSON")
        self._exp_csv  = QRadioButton("CSV")
        self._exp_json.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self._exp_json)
        bg.addButton(self._exp_csv)
        fmt_layout.addWidget(self._exp_json)
        fmt_layout.addWidget(self._exp_csv)
        export_layout.addWidget(fmt_group)

        # Stats
        self._exp_count_lbl = QLabel("Parameters loaded: —")
        self._exp_count_lbl.setStyleSheet("color:#585B70; font-size:12px;")
        export_layout.addWidget(self._exp_count_lbl)

        self._export_btn = QPushButton("⬆  Export to File…")
        self._export_btn.setObjectName("primaryBtn")
        self._export_btn.setFixedHeight(36)
        self._export_btn.clicked.connect(self._do_export)
        export_layout.addWidget(self._export_btn)

        # Preview
        prev_lbl = QLabel("Preview (first 20 rows):")
        prev_lbl.setStyleSheet("color:#6C7086; font-size:11px; margin-top:8px;")
        export_layout.addWidget(prev_lbl)

        self._exp_preview = QPlainTextEdit()
        self._exp_preview.setReadOnly(True)
        self._exp_preview.setFont(QFont("Consolas, Courier New", 9))
        self._exp_preview.setStyleSheet(
            "background:#11111B; color:#6C7086; border:1px solid #313244; border-radius:4px;")
        self._exp_preview.setMaximumHeight(220)
        export_layout.addWidget(self._exp_preview, 1)

        self._refresh_export_btn = QPushButton("⟳  Refresh Preview")
        self._refresh_export_btn.clicked.connect(self._refresh_export_preview)
        export_layout.addWidget(self._refresh_export_btn)
        export_layout.addStretch()

        splitter.addWidget(export_widget)

        # ── Divider ───────────────────────────────────────────────
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet("color:#313244;")
        splitter.addWidget(line)

        # ── Right: Import ─────────────────────────────────────────
        import_widget = QWidget()
        import_layout = QVBoxLayout(import_widget)
        import_layout.setSpacing(12)
        import_layout.setContentsMargins(8, 16, 16, 16)

        imp_title = QLabel("Import Parameters")
        imp_title.setStyleSheet("font-weight:bold; font-size:13px; color:#A6E3A1;")
        import_layout.addWidget(imp_title)

        import_info = QLabel(
            "Load parameter values from JSON or CSV.\n"
            "Values are staged in Batch Write for review\n"
            "before being written to device.")
        import_info.setStyleSheet("color:#6C7086; font-size:12px;")
        import_info.setWordWrap(True)
        import_layout.addWidget(import_info)

        imp_btn_row = QHBoxLayout()
        self._import_btn = QPushButton("⬇  Import from File…")
        self._import_btn.setFixedHeight(36)
        self._import_btn.clicked.connect(self._do_import)

        self._stage_btn = QPushButton("→  Stage All in Batch")
        self._stage_btn.setObjectName("primaryBtn")
        self._stage_btn.setFixedHeight(36)
        self._stage_btn.setEnabled(False)
        self._stage_btn.clicked.connect(self._stage_imported)

        imp_btn_row.addWidget(self._import_btn)
        imp_btn_row.addWidget(self._stage_btn)
        import_layout.addLayout(imp_btn_row)

        # Import status
        self._imp_status = QLabel("")
        self._imp_status.setWordWrap(True)
        self._imp_status.setStyleSheet("font-size:12px;")
        import_layout.addWidget(self._imp_status)

        # Preview table
        prev_lbl2 = QLabel("Preview (values to be imported):")
        prev_lbl2.setStyleSheet("color:#6C7086; font-size:11px;")
        import_layout.addWidget(prev_lbl2)

        self._imp_table = QTableWidget(0, 4)
        self._imp_table.setHorizontalHeaderLabels(
            ["DID", "Parameter", "Import Value", "Unit"])
        self._imp_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._imp_table.setAlternatingRowColors(True)
        self._imp_table.setShowGrid(False)
        self._imp_table.verticalHeader().hide()
        self._imp_table.verticalHeader().setDefaultSectionSize(26)
        hh = self._imp_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        import_layout.addWidget(self._imp_table, 1)

        # Warnings box
        self._warnings_box = QPlainTextEdit()
        self._warnings_box.setReadOnly(True)
        self._warnings_box.setMaximumHeight(80)
        self._warnings_box.setFont(QFont("Consolas, Courier New", 9))
        self._warnings_box.setStyleSheet(
            "background:#1E1E2E; color:#FAB387; border:1px solid #45475A; border-radius:4px;")
        self._warnings_box.setPlaceholderText("Import warnings will appear here...")
        import_layout.addWidget(self._warnings_box)

        splitter.addWidget(import_widget)
        splitter.setSizes([450, 450])
        root.addWidget(splitter)

        # Initial refresh
        self._refresh_export_preview()

    # ── Export ────────────────────────────────────────────────────

    def _refresh_export_preview(self):
        from core.param_io import export_to_json, export_to_csv

        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        self._exp_count_lbl.setText(
            f"Parameters loaded: {loaded}" +
            (" — connect and read first" if loaded == 0 else ""))

        if loaded == 0:
            self._exp_preview.setPlainText("(no data loaded)")
            return

        if self._exp_json.isChecked():
            text = export_to_json(self._store, self._device_address)
            # Show first 20 lines
            lines = text.split('\n')
            preview = '\n'.join(lines[:20])
            if len(lines) > 20:
                preview += f'\n... ({len(lines)-20} more lines)'
        else:
            text = export_to_csv(self._store)
            lines = text.split('\n')
            preview = '\n'.join(lines[:21])
            if len(lines) > 21:
                preview += f'\n... ({len(lines)-21} more rows)'

        self._exp_preview.setPlainText(preview)

    def _do_export(self):
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        if loaded == 0:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "No Data",
                "No parameters loaded from device.\n"
                "Connect and read parameters first.")
            return

        ext = "json" if self._exp_json.isChecked() else "csv"
        filter_str = ("JSON Files (*.json)" if ext == "json"
                      else "CSV Files (*.csv)")

        from datetime import datetime
        default_name = f"parameters_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Parameters", default_name, filter_str)
        if not path:
            return

        n, err = save_export(self._store, Path(path), self._device_address)
        if err:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Export Failed", err)
        else:
            self.status_message.emit(f"✓ Exported {n} parameters to {Path(path).name}")
            self._refresh_export_preview()

    # ── Import ────────────────────────────────────────────────────

    def _do_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Parameters", "",
            "Parameter Files (*.json *.csv);;JSON (*.json);;CSV (*.csv);;All Files (*)")
        if not path:
            return

        result = load_import(Path(path), self._store)
        self._import_result = result
        self._imp_table.setRowCount(0)
        self._warnings_box.clear()

        if result.errors:
            self._imp_status.setText(f"✗ {'; '.join(result.errors)}")
            self._imp_status.setStyleSheet("color:#F38BA8; font-size:12px;")
            self._stage_btn.setEnabled(False)
            return

        # Populate table
        for entry in result.entries:
            row = self._imp_table.rowCount()
            self._imp_table.insertRow(row)
            items = [
                QTableWidgetItem(f"0x{entry['did']:04X}"),
                QTableWidgetItem(entry['name']),
                QTableWidgetItem(str(entry['value'])),
                QTableWidgetItem(entry.get('unit', '')),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignVCenter |
                    (Qt.AlignCenter if col in (0, 3) else Qt.AlignLeft))
                if col == 0:
                    item.setFont(QFont("Consolas, Courier New", 10))
                if col == 2:
                    item.setForeground(QColor("#A6E3A1"))
                self._imp_table.setItem(row, col, item)

        # Status
        fname = Path(path).name
        self._imp_status.setText(
            f"✓ {result.count} parameters from '{fname}'  —  "
            f"ready to stage in Batch Write")
        self._imp_status.setStyleSheet("color:#A6E3A1; font-size:12px;")
        self._stage_btn.setEnabled(result.count > 0)

        # Warnings
        if result.warnings:
            self._warnings_box.setPlainText('\n'.join(result.warnings))

        log.info("Import preview: %d params, %d warnings",
                 result.count, len(result.warnings))

    def _stage_imported(self):
        if not self._import_result or not self._import_result.entries:
            return
        n = 0
        for entry in self._import_result.entries:
            self._batch.stage(entry['did'], entry['value'])
            n += 1
        self.status_message.emit(
            f"✓ Staged {n} parameters in Batch Write — review and confirm")
        self._stage_btn.setEnabled(False)
        self._imp_status.setText(
            f"✓ {n} parameters staged in Batch Write")
