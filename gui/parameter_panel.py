"""
Parameter Panel — redesigned
=============================
Fixes:
 - Parametre prikazuje ODMAH iz JSON (defaulti su prazan '-')
 - Čitanje sa uređaja pokreće se tek nakon connecta (auto ili F5)
 - Edit radi ispravno
 - Bolja vizualna hijerarhija
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton,
    QSplitter, QTableView, QVBoxLayout, QHBoxLayout, QWidget,
)
from core.parameter_model import ParameterStore
from gui.parameter_model_qt import ParameterDelegate, ParameterTableModel


CATEGORY_ICONS = {
    "Motor":              "⚙",
    "Encoder":            "📡",
    "Mechanical":         "🔩",
    "Power":              "⚡",
    "Thermal":            "🌡",
    "Communication":      "📶",
    "Limits":             "⛔",
    "CurrentController":  "🔄",
    "TorqueController":   "💪",
    "VelocityController": "🚀",
    "PositionController": "🎯",
    "Feedforward":        "➕",
}


class ParameterPanel(QWidget):
    refresh_requested = Signal()

    def __init__(self, store: ParameterStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._build_ui()
        self._populate_categories()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Toolbar ──────────────────────────────────────────────
        toolbar = QWidget()
        toolbar.setObjectName("paramToolbar")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(8, 6, 8, 6)
        tb.setSpacing(8)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("  Search parameters…")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search)
        self._search_box.setMinimumWidth(220)

        self._refresh_btn = QPushButton("⟳  Read All")
        self._refresh_btn.setObjectName("primaryBtn")
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setToolTip("Read all parameters from device (F5)")
        self._refresh_btn.clicked.connect(self.refresh_requested)

        self._progress = QProgressBar()
        self._progress.setMaximumWidth(160)
        self._progress.setMinimumHeight(18)
        self._progress.hide()

        self._status_label = QLabel("Not connected")
        self._status_label.setObjectName("statusLabel")
        self._status_label.setStyleSheet("color:#6C7086; font-size:12px;")

        tb.addWidget(self._search_box)
        tb.addWidget(self._refresh_btn)
        tb.addWidget(self._progress)
        tb.addWidget(self._status_label)
        tb.addStretch()
        root.addWidget(toolbar)

        # ── Splitter: category list | table ──────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Category list
        self._cat_list = QListWidget()
        self._cat_list.setObjectName("categoryList")
        self._cat_list.setFixedWidth(190)
        self._cat_list.currentRowChanged.connect(self._on_category)
        splitter.addWidget(self._cat_list)

        # Table
        self._model = ParameterTableModel(self._store)
        self._delegate = ParameterDelegate(self._store)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setItemDelegateForColumn(2, self._delegate)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        self._table.verticalHeader().setDefaultSectionSize(28)
        self._table.verticalHeader().hide()
        self._table.setShowGrid(False)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setDefaultSectionSize(160)

        splitter.addWidget(self._table)
        splitter.setSizes([190, 900])
        root.addWidget(splitter)

    def _populate_categories(self):
        self._cat_list.clear()
        for cat in self._store.categories():
            count = len(self._store.parameters_in_category(cat))
            icon = CATEGORY_ICONS.get(cat, "•")
            item = QListWidgetItem(f"  {icon}  {cat}  ({count})")
            item.setData(Qt.UserRole, cat)
            self._cat_list.addItem(item)
        if self._cat_list.count():
            self._cat_list.setCurrentRow(0)

    def _on_category(self, row: int):
        if row < 0: return
        self._search_box.clear()
        cat = self._cat_list.item(row).data(Qt.UserRole)
        self._model.set_category(cat)

    def _on_search(self, text: str):
        if text.strip():
            self._model.set_filter(text)
            self._cat_list.clearSelection()
        else:
            row = self._cat_list.currentRow()
            if row >= 0:
                cat = self._cat_list.item(row).data(Qt.UserRole)
                self._model.set_category(cat)

    # ── Called from main window ──────────────────────────────────

    def set_connected(self, connected: bool):
        self._refresh_btn.setEnabled(connected)
        if not connected:
            self._status_label.setText("Not connected")
            self._status_label.setStyleSheet("color:#6C7086; font-size:12px;")

    def on_read_progress(self, done: int, total: int):
        self._progress.show()
        self._progress.setMaximum(total)
        self._progress.setValue(done)
        self._status_label.setStyleSheet("color:#89B4FA; font-size:12px;")
        self._status_label.setText(f"Reading {done}/{total}…")

    def on_all_read_done(self):
        self._progress.hide()
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        errors = sum(1 for pv in self._store.values.values() if pv.error)
        msg = f"✓ {loaded} loaded"
        if errors: msg += f"   ✗ {errors} errors"
        self._status_label.setStyleSheet("color:#A6E3A1; font-size:12px;")
        self._status_label.setText(msg)

    def on_parameter_written(self, did: int):
        defn = self._store.get_definition(did)
        if defn:
            self._status_label.setStyleSheet("color:#A6E3A1; font-size:12px;")
            self._status_label.setText(f"✓ {defn.name} written")

    def refresh_categories(self):
        self._populate_categories()
