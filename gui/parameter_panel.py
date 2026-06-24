"""
Parameter Panel
===============
Main parameter configuration widget.
Left: category tree. Right: filtered parameter table with inline editing.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSortFilterProxyModel, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.parameter_model import ParameterStore
from gui.parameter_model_qt import ParameterDelegate, ParameterTableModel


class ParameterPanel(QWidget):
    """Full parameter configuration panel."""

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

        # ── Toolbar ────────────────────────────────────────────────
        toolbar = QWidget()
        toolbar.setObjectName("paramToolbar")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)
        tb_layout.setSpacing(8)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("  Search parameters…")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search)
        self._search_box.setMinimumWidth(200)

        self._refresh_btn = QPushButton("⟳  Read All")
        self._refresh_btn.setObjectName("primaryBtn")
        self._refresh_btn.clicked.connect(self.refresh_requested)

        self._progress = QProgressBar()
        self._progress.setMaximumWidth(180)
        self._progress.setMinimumHeight(20)
        self._progress.hide()

        self._status_label = QLabel("")
        self._status_label.setObjectName("statusLabel")

        tb_layout.addWidget(self._search_box)
        tb_layout.addWidget(self._refresh_btn)
        tb_layout.addWidget(self._progress)
        tb_layout.addWidget(self._status_label)
        tb_layout.addStretch()

        root.addWidget(toolbar)

        # ── Splitter ────────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Category list
        self._cat_list = QListWidget()
        self._cat_list.setObjectName("categoryList")
        self._cat_list.setFixedWidth(180)
        self._cat_list.currentRowChanged.connect(self._on_category_selected)
        splitter.addWidget(self._cat_list)

        # Parameter table
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
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # DID
        hh.setSectionResizeMode(1, QHeaderView.Interactive)        # Name
        hh.setSectionResizeMode(2, QHeaderView.Interactive)        # Value
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # Unit
        hh.setSectionResizeMode(4, QHeaderView.Stretch)           # Desc
        hh.setDefaultSectionSize(160)
        hh.setStretchLastSection(True)

        splitter.addWidget(self._table)
        splitter.setSizes([180, 900])
        root.addWidget(splitter)

    def _populate_categories(self):
        self._cat_list.clear()
        icons = {
            "Motor": "⚙",
            "Encoder": "📡",
            "Mechanical": "🔩",
            "Power": "⚡",
            "Thermal": "🌡",
            "Communication": "📶",
            "Limits": "⛔",
            "CurrentController": "🔄",
            "TorqueController": "💪",
            "VelocityController": "🚀",
            "PositionController": "🎯",
            "Feedforward": "➕",
        }
        for cat in self._store.categories():
            count = len(self._store.parameters_in_category(cat))
            icon = icons.get(cat, "•")
            item = QListWidgetItem(f" {icon}  {cat}  ({count})")
            item.setData(Qt.UserRole, cat)
            self._cat_list.addItem(item)
        if self._cat_list.count():
            self._cat_list.setCurrentRow(0)

    def _on_category_selected(self, row: int) -> None:
        if row < 0:
            return
        self._search_box.clear()
        item = self._cat_list.item(row)
        cat = item.data(Qt.UserRole)
        self._model.set_category(cat)
        self._table.resizeColumnToContents(0)

    def _on_search(self, text: str) -> None:
        if text.strip():
            self._model.set_filter(text)
            self._cat_list.clearSelection()
        else:
            row = self._cat_list.currentRow()
            if row >= 0:
                item = self._cat_list.item(row)
                cat = item.data(Qt.UserRole)
                self._model.set_category(cat)

    # ── Called by main window ────────────────────────────────────────

    def on_read_progress(self, done: int, total: int) -> None:
        self._progress.show()
        self._progress.setMaximum(total)
        self._progress.setValue(done)
        if done >= total:
            self._progress.hide()
            self._status_label.setText(f"✓ {total} parameters read")

    def on_all_read_done(self) -> None:
        self._progress.hide()
        total = len(self._store.all_dids())
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        errors = sum(1 for pv in self._store.values.values() if pv.error)
        parts = [f"✓ {loaded} loaded"]
        if errors:
            parts.append(f"  ✗ {errors} errors")
        self._status_label.setText("  ".join(parts))

    def on_parameter_written(self, did: int) -> None:
        defn = self._store.get_definition(did)
        if defn:
            self._status_label.setText(f"✓ {defn.name} written")

    def refresh_categories(self) -> None:
        self._populate_categories()
