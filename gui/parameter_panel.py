"""
Parameter Panel — clean rewrite
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

ICONS = {
    "Motor":"⚙", "Encoder":"📡", "Mechanical":"🔩",
    "Power":"⚡", "Thermal":"🌡", "Communication":"📶",
    "Limits":"⛔", "CurrentController":"🔄", "TorqueController":"💪",
    "VelocityController":"🚀", "PositionController":"🎯", "Feedforward":"➕",
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

        # Toolbar
        tb_widget = QWidget(); tb_widget.setObjectName("paramToolbar")
        tb = QHBoxLayout(tb_widget)
        tb.setContentsMargins(10, 7, 10, 7); tb.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("  🔍  Search parameters…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.setMinimumWidth(240)

        self._read_btn = QPushButton("⟳  Read All")
        self._read_btn.setObjectName("primaryBtn")
        self._read_btn.setEnabled(False)
        self._read_btn.setToolTip("Read all parameters from device (F5)")
        self._read_btn.clicked.connect(self.refresh_requested)

        self._progress = QProgressBar()
        self._progress.setMaximumWidth(180)
        self._progress.setMinimumHeight(18)
        self._progress.setTextVisible(False)
        self._progress.hide()

        self._status = QLabel("Connect to device to read values")
        self._status.setStyleSheet("color:#585B70; font-size:12px;")

        tb.addWidget(self._search)
        tb.addWidget(self._read_btn)
        tb.addWidget(self._progress)
        tb.addWidget(self._status)
        tb.addStretch()
        root.addWidget(tb_widget)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Category list
        self._cats = QListWidget()
        self._cats.setObjectName("categoryList")
        self._cats.setFixedWidth(195)
        self._cats.currentRowChanged.connect(self._on_cat)
        splitter.addWidget(self._cats)

        # Table
        self._model = ParameterTableModel(self._store)
        self._delegate = ParameterDelegate(self._store)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setItemDelegateForColumn(2, self._delegate)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.AnyKeyPressed
        )
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.verticalHeader().hide()
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setDefaultSectionSize(170)

        splitter.addWidget(self._table)
        splitter.setSizes([195, 900])
        root.addWidget(splitter)

    def _populate_categories(self):
        self._cats.clear()
        for cat in self._store.categories():
            n = len(self._store.parameters_in_category(cat))
            icon = ICONS.get(cat, "•")
            item = QListWidgetItem(f"  {icon}  {cat}  ({n})")
            item.setData(Qt.UserRole, cat)
            self._cats.addItem(item)
        if self._cats.count():
            self._cats.setCurrentRow(0)

    def _on_cat(self, row: int):
        if row < 0: return
        self._search.clear()
        cat = self._cats.item(row).data(Qt.UserRole)
        self._model.set_category(cat)

    def _on_search(self, text: str):
        if text.strip():
            self._model.set_filter(text)
            self._cats.clearSelection()
        else:
            row = self._cats.currentRow()
            if row >= 0:
                self._model.set_category(self._cats.item(row).data(Qt.UserRole))

    # Called from main window
    def set_connected(self, yes: bool):
        self._read_btn.setEnabled(yes)
        if not yes:
            self._status.setText("Connect to device to read values")
            self._status.setStyleSheet("color:#585B70; font-size:12px;")

    def on_read_progress(self, done: int, total: int):
        self._progress.show()
        self._progress.setMaximum(total)
        self._progress.setValue(done)
        self._status.setStyleSheet("color:#89B4FA; font-size:12px;")
        self._status.setText(f"Reading {done}/{total}…")

    def on_all_read_done(self):
        self._progress.hide()
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        errors = sum(1 for pv in self._store.values.values() if pv.error)
        msg = f"✓ {loaded} parameters read"
        if errors: msg += f"   ⚠ {errors} errors"
        self._status.setStyleSheet("color:#A6E3A1; font-size:12px;")
        self._status.setText(msg)

    def on_parameter_written(self, did: int):
        defn = self._store.get_definition(did)
        if defn:
            self._status.setStyleSheet("color:#A6E3A1; font-size:12px;")
            self._status.setText(f"✓ {defn.name} written to device")

    def refresh_categories(self):
        self._populate_categories()
