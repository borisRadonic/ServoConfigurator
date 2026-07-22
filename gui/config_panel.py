"""
Configuration Management Panel
================================
Four features in one tab with sub-tabs:
  1. Presets         — save/load/compare named configurations
  2. Compare         — side-by-side diff: preset vs device, or preset vs preset
  3. Batch Write     — stage multiple changes, write all at once
  4. Write History   — log of all writes with undo/redo
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from core.parameter_model import ParameterStore
from core.preset_manager import DiffEntry, Preset, PresetManager
from core.write_history import WriteHistory
from core.batch_writer import BatchWriter
from gui.export_import_panel import ExportImportPanel

log = logging.getLogger(__name__)


# ================================================================== #
#  1. Presets Panel                                                    #
# ================================================================== #

class PresetsPanel(QWidget):
    load_preset_requested   = Signal(object)   # Preset
    compare_preset_requested = Signal(str, str) # name_left, name_right

    def __init__(self, store: ParameterStore,
                 manager: PresetManager, parent=None):
        super().__init__(parent)
        self._store   = store
        self._manager = manager
        self._build_ui()
        manager.presets_changed.connect(self._refresh_list)
        self._refresh_list()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Left: preset list ───────────────────────────────────
        left = QVBoxLayout()

        lbl = QLabel("Saved Presets")
        lbl.setStyleSheet("font-weight:bold; color:#89B4FA;")
        left.addWidget(lbl)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_sel)
        left.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._save_btn   = QPushButton("💾  Save Current")
        self._save_btn.setObjectName("primaryBtn")
        self._save_btn.clicked.connect(self._save_preset)
        self._delete_btn = QPushButton("🗑  Delete")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_preset)
        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._delete_btn)
        left.addLayout(btn_row)

        io_row = QHBoxLayout()
        imp_btn = QPushButton("⬇  Import…")
        imp_btn.clicked.connect(self._import_preset)
        exp_btn = QPushButton("⬆  Export…")
        exp_btn.clicked.connect(self._export_preset)
        io_row.addWidget(imp_btn)
        io_row.addWidget(exp_btn)
        left.addLayout(io_row)

        # ── Right: detail + actions ─────────────────────────────
        right = QVBoxLayout()

        info_group = QGroupBox("Preset Details")
        form = QFormLayout(info_group)
        form.setSpacing(8)
        self._name_lbl  = QLabel("—")
        self._desc_lbl  = QLabel("—")
        self._desc_lbl.setWordWrap(True)
        self._date_lbl  = QLabel("—")
        self._count_lbl = QLabel("—")
        self._name_lbl.setStyleSheet("font-weight:bold;")
        form.addRow("Name:",        self._name_lbl)
        form.addRow("Description:", self._desc_lbl)
        form.addRow("Modified:",    self._date_lbl)
        form.addRow("Parameters:",  self._count_lbl)
        right.addWidget(info_group)

        action_group = QGroupBox("Actions")
        al = QVBoxLayout(action_group)

        self._load_btn = QPushButton("⬆  Load to Batch (stage for writing)")
        self._load_btn.setEnabled(False)
        self._load_btn.clicked.connect(self._load_preset)
        al.addWidget(self._load_btn)

        cmp_row = QHBoxLayout()
        cmp_lbl = QLabel("Compare with:")
        self._cmp_combo = QComboBox()
        self._cmp_combo.setMinimumWidth(160)
        self._cmp_btn = QPushButton("Compare →")
        self._cmp_btn.setEnabled(False)
        self._cmp_btn.clicked.connect(self._compare_preset)
        cmp_lbl2 = QLabel("Device")
        self._cmp_dev_btn = QPushButton("vs Device →")
        self._cmp_dev_btn.setEnabled(False)
        self._cmp_dev_btn.clicked.connect(self._compare_vs_device)
        cmp_row.addWidget(cmp_lbl)
        cmp_row.addWidget(self._cmp_combo, 1)
        cmp_row.addWidget(self._cmp_btn)
        al.addLayout(cmp_row)

        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Compare current preset vs:"))
        dev_row.addWidget(self._cmp_dev_btn)
        al.addLayout(dev_row)

        right.addWidget(action_group)
        right.addStretch()

        root.addLayout(left, 1)
        root.addLayout(right, 1)

    def _refresh_list(self):
        cur = self._list.currentItem()
        cur_name = cur.text() if cur else None
        self._list.clear()
        names = self._manager.list_presets()
        for name in names:
            self._list.addItem(name)
        self._cmp_combo.clear()
        self._cmp_combo.addItems(names)
        # Restore selection
        if cur_name:
            items = self._list.findItems(cur_name, Qt.MatchExactly)
            if items:
                self._list.setCurrentItem(items[0])

    def _on_sel(self, row: int):
        has = row >= 0
        self._delete_btn.setEnabled(has)
        self._load_btn.setEnabled(has)
        self._cmp_btn.setEnabled(has)
        self._cmp_dev_btn.setEnabled(has)
        if not has:
            self._name_lbl.setText("—")
            self._desc_lbl.setText("—")
            self._date_lbl.setText("—")
            self._count_lbl.setText("—")
            return
        name = self._list.item(row).text()
        preset = self._manager.load(name)
        if preset:
            self._name_lbl.setText(preset.name)
            self._desc_lbl.setText(preset.description or "—")
            self._date_lbl.setText(preset.modified or "—")
            self._count_lbl.setText(str(len(preset.values)))

    def _save_preset(self):
        # Count loaded values
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        if loaded == 0:
            QMessageBox.warning(self, "No Data",
                "No parameters loaded from device.\nConnect and read parameters first.")
            return
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name.strip():
            return
        name = name.strip().replace(" ", "_")
        desc, ok2 = QInputDialog.getText(self, "Description",
                                          "Optional description:")
        preset = Preset.from_store(self._store, name,
                                   description=desc if ok2 else "")
        if self._manager.save(preset):
            QMessageBox.information(self, "Saved",
                f"Preset '{name}' saved ({len(preset.values)} parameters).")

    def _delete_preset(self):
        item = self._list.currentItem()
        if not item:
            return
        name = item.text()
        r = QMessageBox.question(self, "Delete Preset",
            f"Delete preset '{name}'?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self._manager.delete(name)

    def _load_preset(self):
        item = self._list.currentItem()
        if not item:
            return
        preset = self._manager.load(item.text())
        if preset:
            self.load_preset_requested.emit(preset)

    def _compare_preset(self):
        item = self._list.currentItem()
        if not item:
            return
        right = self._cmp_combo.currentText()
        if right:
            self.compare_preset_requested.emit(item.text(), right)

    def _compare_vs_device(self):
        item = self._list.currentItem()
        if not item:
            return
        self.compare_preset_requested.emit(item.text(), "__device__")

    def _import_preset(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Preset", "",
            "JSON Files (*.json);;All Files (*)")
        if path:
            preset = self._manager.import_from_file(__import__('pathlib').Path(path))
            if preset:
                QMessageBox.information(self, "Imported",
                    f"Preset '{preset.name}' imported.")

    def _export_preset(self):
        item = self._list.currentItem()
        if not item:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Preset", f"{item.text()}.json",
            "JSON Files (*.json);;All Files (*)")
        if path:
            self._manager.export_to_file(item.text(),
                                          __import__('pathlib').Path(path))


# ================================================================== #
#  2. Compare Panel                                                    #
# ================================================================== #

class ComparePanel(QWidget):
    def __init__(self, store: ParameterStore,
                 manager: PresetManager, parent=None):
        super().__init__(parent)
        self._store   = store
        self._manager = manager
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # Header
        hdr = QHBoxLayout()
        self._title_lbl = QLabel("Select presets to compare")
        self._title_lbl.setStyleSheet("font-weight:bold; font-size:13px;")
        self._only_diff_btn = QPushButton("Show Differences Only")
        self._only_diff_btn.setCheckable(True)
        self._only_diff_btn.setChecked(True)
        self._only_diff_btn.clicked.connect(self._refresh_table)
        hdr.addWidget(self._title_lbl)
        hdr.addStretch()
        hdr.addWidget(self._only_diff_btn)
        root.addLayout(hdr)

        # Column headers for the two sides
        self._col_labels = QHBoxLayout()
        self._left_lbl  = QLabel("Left")
        self._right_lbl = QLabel("Right")
        for lbl in [self._left_lbl, self._right_lbl]:
            lbl.setStyleSheet(
                "background:#313244; color:#89B4FA; font-weight:bold;"
                "padding:6px 12px; border-radius:4px;")
        self._col_labels.addWidget(self._left_lbl, 1)
        self._col_labels.addWidget(self._right_lbl, 1)
        root.addLayout(self._col_labels)

        # Diff table
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["DID", "Parameter", "Left Value", "Right Value", "Unit"])
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(28)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setDefaultSectionSize(160)
        root.addWidget(self._table, 1)

        self._summary = QLabel("")
        self._summary.setStyleSheet("color:#6C7086; font-size:12px;")
        root.addWidget(self._summary)

        self._entries: list = []

    def show_comparison(self, left_name: str, right_name: str):
        """Load and display comparison. right_name='__device__' = device values."""
        if left_name == "__device__":
            left  = Preset.from_store(self._store, "Device")
        else:
            left  = self._manager.load(left_name)

        if right_name == "__device__":
            right = Preset.from_store(self._store, "Device")
        else:
            right = self._manager.load(right_name)

        if not left or not right:
            QMessageBox.warning(self.parent(), "Error", "Could not load preset.")
            return

        self._left_lbl.setText(f"  {left.name}")
        self._right_lbl.setText(f"  {right.name}")
        self._title_lbl.setText(
            f"Comparing: {left.name}  ↔  {right.name}")

        # Compute full diff (including same values for "show all" mode)
        all_dids = set(left.did_values.keys()) | set(right.did_values.keys())
        self._entries = []
        for did in sorted(all_dids):
            defn  = self._store.get_definition(did)
            name  = defn.name if defn else f"0x{did:04X}"
            unit  = defn.unit if defn else ""
            entry = DiffEntry(did=did, name=name, unit=unit,
                              left_value=left.did_values.get(did),
                              right_value=right.did_values.get(did))
            self._entries.append(entry)

        self._refresh_table()

    def _refresh_table(self):
        only_diff = self._only_diff_btn.isChecked()
        rows = [e for e in self._entries if (not only_diff or e.differs)]

        self._table.setRowCount(0)
        diff_count = sum(1 for e in self._entries if e.differs)

        for entry in rows:
            row = self._table.rowCount()
            self._table.insertRow(row)

            items = [
                QTableWidgetItem(f"0x{entry.did:04X}"),
                QTableWidgetItem(entry.name),
                QTableWidgetItem(entry.left_str),
                QTableWidgetItem(entry.right_str),
                QTableWidgetItem(entry.unit),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignVCenter |
                    (Qt.AlignCenter if col in (0, 4) else Qt.AlignLeft))
                if col == 0:
                    item.setFont(QFont("Consolas, Courier New", 10))
                self._table.setItem(row, col, item)

            if entry.differs:
                # Highlight differing values
                for col, color in [(2, QColor("#2D3B2D")), (3, QColor("#3B2D2D"))]:
                    bg = self._table.item(row, col)
                    if bg: bg.setBackground(color)
                self._table.item(row, 2).setForeground(QColor("#A6E3A1"))
                self._table.item(row, 3).setForeground(QColor("#F38BA8"))

        total = len(self._entries)
        self._summary.setText(
            f"{diff_count} differences / {total} total parameters"
            + (f"  (showing {len(rows)})" if only_diff else ""))


# ================================================================== #
#  3. Batch Write Panel                                                #
# ================================================================== #

class BatchPanel(QWidget):
    commit_requested = Signal()

    def __init__(self, store: ParameterStore,
                 batch: BatchWriter, parent=None):
        super().__init__(parent)
        self._store = store
        self._batch = batch
        self._build_ui()
        batch.staged_changed.connect(self._refresh)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # Info bar
        info_row = QHBoxLayout()
        self._count_lbl = QLabel("No staged changes")
        self._count_lbl.setStyleSheet("font-size:13px; color:#6C7086;")

        self._commit_btn = QPushButton("✓  Write All to Device")
        self._commit_btn.setObjectName("primaryBtn")
        self._commit_btn.setEnabled(False)
        self._commit_btn.setFixedHeight(34)
        self._commit_btn.clicked.connect(self._confirm_commit)

        self._discard_btn = QPushButton("✕  Discard All")
        self._discard_btn.setEnabled(False)
        self._discard_btn.setFixedHeight(34)
        self._discard_btn.clicked.connect(self._discard)

        info_row.addWidget(self._count_lbl)
        info_row.addStretch()
        info_row.addWidget(self._commit_btn)
        info_row.addWidget(self._discard_btn)
        root.addLayout(info_row)

        # Staged changes table
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["DID", "Parameter", "Current Value", "→ New Value", ""])
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(30)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        root.addWidget(self._table, 1)

        note = QLabel(
            "ℹ  In Batch Mode, parameter edits in the Parameters tab are staged here "
            "instead of being written immediately. Click Write All when ready.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#585B70; font-size:11px; padding:4px;")
        root.addWidget(note)

    def _refresh(self):
        staged = self._batch.staged
        n = len(staged)
        self._count_lbl.setText(
            f"{n} staged change{'s' if n != 1 else ''}" if n
            else "No staged changes")
        self._count_lbl.setStyleSheet(
            f"font-size:13px; color:{'#FAB387' if n else '#6C7086'};")
        self._commit_btn.setEnabled(n > 0)
        self._discard_btn.setEnabled(n > 0)

        self._table.setRowCount(0)
        for change in staged:
            row = self._table.rowCount()
            self._table.insertRow(row)

            did_item = QTableWidgetItem(f"0x{change.did:04X}")
            did_item.setFont(QFont("Consolas, Courier New", 10))
            did_item.setData(Qt.UserRole, change.did)

            old_str = "—" if change.old_value is None else str(change.old_value)
            new_str = str(change.new_value)

            items = [
                did_item,
                QTableWidgetItem(change.name),
                QTableWidgetItem(f"{old_str}  {change.unit}".strip()),
                QTableWidgetItem(f"{new_str}  {change.unit}".strip()),
                QTableWidgetItem("✕"),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignVCenter |
                    (Qt.AlignCenter if col in (0, 4) else Qt.AlignLeft))
                self._table.setItem(row, col, item)

            self._table.item(row, 3).setForeground(QColor("#A6E3A1"))

        # Remove button via click
        self._table.cellClicked.connect(self._on_cell_click)

    def _on_cell_click(self, row: int, col: int):
        if col == 4:
            did_item = self._table.item(row, 0)
            if did_item:
                did = did_item.data(Qt.UserRole)
                self._batch.unstage(did)

    def _confirm_commit(self):
        n = self._batch.count
        r = QMessageBox.question(
            self, "Confirm Write",
            f"Write {n} parameter{'s' if n > 1 else ''} to device?\n\n"
            + "\n".join(f"  • {c.name} = {c.new_value}"
                        for c in self._batch.staged[:10])
            + ("\n  …" if n > 10 else ""),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r == QMessageBox.Yes:
            self.commit_requested.emit()

    def _discard(self):
        r = QMessageBox.question(
            self, "Discard Changes",
            f"Discard {self._batch.count} staged changes?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self._batch.discard()

    def load_preset_to_batch(self, preset: Preset):
        """Stage all values from a preset."""
        loaded = 0
        for did, value in preset.did_values.items():
            defn = self._store.get_definition(did)
            if defn and not defn.read_only:
                self._batch.stage(did, value)
                loaded += 1
        if loaded:
            QMessageBox.information(
                self, "Preset Loaded to Batch",
                f"Staged {loaded} parameters from '{preset.name}'.\n"
                "Review and click Write All to apply.")


# ================================================================== #
#  4. History Panel                                                    #
# ================================================================== #

class HistoryPanel(QWidget):
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, history: WriteHistory, parent=None):
        super().__init__(parent)
        self._history = history
        self._build_ui()
        history.history_changed.connect(self._refresh)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        btn_row = QHBoxLayout()
        self._undo_btn = QPushButton("↩  Undo Last")
        self._undo_btn.setEnabled(False)
        self._undo_btn.setFixedHeight(34)
        self._undo_btn.clicked.connect(self.undo_requested)

        self._redo_btn = QPushButton("↪  Redo")
        self._redo_btn.setEnabled(False)
        self._redo_btn.setFixedHeight(34)
        self._redo_btn.clicked.connect(self.redo_requested)

        self._clear_btn = QPushButton("Clear History")
        self._clear_btn.setFixedHeight(34)
        self._clear_btn.clicked.connect(self._clear)

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet("color:#6C7086; font-size:12px;")

        btn_row.addWidget(self._undo_btn)
        btn_row.addWidget(self._redo_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._count_lbl)
        btn_row.addWidget(self._clear_btn)
        root.addLayout(btn_row)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Time", "Parameter", "Old Value", "→ New Value", "Unit"])
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(28)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        root.addWidget(self._table, 1)

    def _refresh(self):
        entries = self._history.entries   # newest first
        self._table.setRowCount(0)

        for i, entry in enumerate(entries):
            row = self._table.rowCount()
            self._table.insertRow(row)

            items = [
                QTableWidgetItem(entry.timestamp_short),
                QTableWidgetItem(entry.name),
                QTableWidgetItem(entry.old_str),
                QTableWidgetItem(entry.new_str),
                QTableWidgetItem(entry.unit),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignVCenter |
                    (Qt.AlignCenter if col in (0, 4) else Qt.AlignLeft))
                if col == 0:
                    item.setFont(QFont("Consolas, Courier New", 10))
                self._table.setItem(row, col, item)

            # Newest entry highlighted
            if i == 0:
                for col in range(5):
                    it = self._table.item(row, col)
                    if it: it.setBackground(QColor("#1E2D1E"))

            self._table.item(row, 3).setForeground(QColor("#A6E3A1"))

        n = len(entries)
        self._count_lbl.setText(f"{n} entr{'y' if n==1 else 'ies'}")
        self._undo_btn.setEnabled(self._history.can_undo)
        self._redo_btn.setEnabled(self._history.can_redo)

        # Update undo button label with what will be undone
        if self._history.can_undo and self._history.last_entry:
            last = self._history.last_entry
            self._undo_btn.setText(f"↩  Undo: {last.name} = {last.new_str}")
        else:
            self._undo_btn.setText("↩  Undo Last")

    def _clear(self):
        r = QMessageBox.question(self, "Clear History",
            "Clear all write history?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self._history.clear()


# ================================================================== #
#  Main Configuration Tab                                              #
# ================================================================== #

class ConfigPanel(QWidget):
    """
    Top-level Configuration Management tab.
    Wires Presets / Compare / Batch / History together.
    """

    # Emitted when batch should be committed (main window calls write_parameter)
    write_parameter = Signal(int, object)    # did, value

    def __init__(self, store: ParameterStore, parent=None):
        super().__init__(parent)
        self._store = store

        # Core objects
        from pathlib import Path
        app_dir = Path(__file__).parent.parent
        self._manager = PresetManager(app_dir, self)
        self._history = WriteHistory(self)
        self._batch   = BatchWriter(store, self)

        self._build_ui()
        self._wire_signals()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._tabs = __import__('PySide6.QtWidgets',
                                 fromlist=['QTabWidget']).QTabWidget()
        self._tabs.setDocumentMode(True)

        # Panels
        self._presets_panel = PresetsPanel(self._store, self._manager)
        self._compare_panel = ComparePanel(self._store, self._manager)
        self._batch_panel   = BatchPanel(self._store, self._batch)
        self._history_panel = HistoryPanel(self._history)

        self._export_import_panel = ExportImportPanel(self._store, self._batch)
        self._export_import_panel.status_message.connect(self.statusMessage)

        self._tabs.addTab(self._presets_panel,       "📋  Presets")
        self._tabs.addTab(self._compare_panel,       "⇆  Compare")
        self._tabs.addTab(self._batch_panel,         "✏  Batch Write")
        self._tabs.addTab(self._history_panel,       "🕐  Write History")
        self._tabs.addTab(self._export_import_panel, "⬆⬇  Export/Import")
        root.addWidget(self._tabs)

    def _wire_signals(self):
        # Presets → Compare
        self._presets_panel.compare_preset_requested.connect(
            self._on_compare_requested)

        # Presets → Batch (load preset to batch)
        self._presets_panel.load_preset_requested.connect(
            self._batch_panel.load_preset_to_batch)

        # Batch commit → write
        self._batch_panel.commit_requested.connect(self._commit_batch)

        # History undo/redo
        self._history_panel.undo_requested.connect(self._do_undo)
        self._history_panel.redo_requested.connect(self._do_redo)

    def _on_compare_requested(self, left: str, right: str):
        self._compare_panel.show_comparison(left, right)
        self._tabs.setCurrentIndex(1)  # switch to Compare tab

    def _commit_batch(self):
        def write_fn(did: int, value):
            self.write_parameter.emit(did, value)
        n = self._batch.commit(write_fn, self._history)
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, "Batch Write",
                                f"Writing {n} parameters to device…")

    def _do_undo(self):
        def write_fn(did: int, value):
            self.write_parameter.emit(did, value)
        entry = self._history.undo(write_fn)
        if entry:
            self.statusMessage(
                f"Undo: {entry.name} restored to {entry.old_str}")

    def _do_redo(self):
        def write_fn(did: int, value):
            self.write_parameter.emit(did, value)
        entry = self._history.redo(write_fn)
        if entry:
            self.statusMessage(f"Redo: {entry.name} = {entry.new_str}")

    def set_device_address(self, addr) -> None:
        """Called by main_window when connection changes."""
        self._export_import_panel.set_device_address(addr)

    def statusMessage(self, msg: str):
        """Try to show in main window status bar."""
        w = self.window()
        if hasattr(w, 'statusBar'):
            w.statusBar().showMessage(msg, 4000)

    # ── Called from main window ─────────────────────────────────

    def on_parameter_written(self, did: int, old_value, new_value):
        """Called after each successful write to record in history."""
        defn = self._store.get_definition(did)
        if defn:
            self._history.record(did, defn.name, old_value, new_value,
                                  defn.unit)

    def stage_parameter(self, did: int, value) -> bool:
        """
        Called from parameter table when batch mode is active.
        Returns True if staged (caller should NOT write immediately).
        """
        self._batch.stage(did, value)
        return True

    @property
    def batch_mode(self) -> bool:
        return True  # always available; user switches via Batch tab

    @property
    def has_staged(self) -> bool:
        return self._batch.count > 0
