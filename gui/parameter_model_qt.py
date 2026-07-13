"""
Parameter Table Model + Delegate
=================================
Fixes:
 - setModelData now correctly triggers store.request_write
 - UserRole returns DID on ALL columns (not just value column)
 - Delegate createEditor works for bool as QComboBox
"""
from __future__ import annotations
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QSpinBox,
    QStyledItemDelegate, QStyleOptionViewItem, QWidget,
)
from core.parameter_model import ParameterStore, ParameterType

COL_DID=0; COL_NAME=1; COL_VALUE=2; COL_UNIT=3; COL_DESC=4; NUM_COLS=5
HEADERS = ["DID", "Parameter", "Value", "Unit", "Description"]

COLOR_DIRTY   = QColor("#FF9800")
COLOR_ERROR   = QColor("#F44336")
COLOR_LOADED  = QColor("#A6E3A1")
COLOR_UNLOADED= QColor("#6C7086")


class ParameterTableModel(QAbstractTableModel):
    def __init__(self, store: ParameterStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._dids: list[int] = []
        store.parameter_changed.connect(self._on_changed)

    def set_category(self, category: str) -> None:
        self.beginResetModel()
        self._dids = [d.did for d in self._store.parameters_in_category(category)]
        self.endResetModel()

    def set_filter(self, text: str) -> None:
        self.beginResetModel()
        lo = text.lower()
        self._dids = [
            d.did for d in self._store.definitions.values()
            if lo in d.name.lower() or lo in d.description.lower()
        ]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()): return len(self._dids)
    def columnCount(self, parent=QModelIndex()): return NUM_COLS

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]

    def flags(self, index: QModelIndex):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_VALUE:
            did = self._dids[index.row()] if index.row() < len(self._dids) else None
            if did is not None:
                defn = self._store.get_definition(did)
                if defn and not defn.read_only:
                    return base | Qt.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._dids):
            return None
        did = self._dids[index.row()]
        defn = self._store.get_definition(did)
        pv   = self._store.get_value(did)
        col  = index.column()

        if role == Qt.UserRole:
            return did  # always return DID regardless of column

        if role == Qt.DisplayRole:
            if col == COL_DID:   return defn.did_str
            if col == COL_NAME:  return defn.name
            if col == COL_UNIT:  return defn.unit
            if col == COL_DESC:  return defn.description
            if col == COL_VALUE:
                if pv and pv.error:    return f"ERR: {pv.error}"
                if pv and pv.is_loaded: return pv.display_value()
                return "–"

        if role == Qt.EditRole and col == COL_VALUE:
            if pv and pv.is_loaded:
                return pv.value
            return None

        if role == Qt.ForegroundRole and col == COL_VALUE:
            if pv:
                if pv.error:     return COLOR_ERROR
                if pv.is_dirty:  return COLOR_DIRTY
                if pv.is_loaded: return COLOR_LOADED
            return COLOR_UNLOADED

        if role == Qt.FontRole and col == COL_NAME:
            f = QFont("Consolas, Monospace")
            return f

        if role == Qt.ToolTipRole:
            tip = defn.description
            if pv and pv.error: tip += f"\n\nError: {pv.error}"
            if defn.min_val is not None:
                tip += f"\nRange: {defn.min_val} … {defn.max_val}"
                tip += f"\nStep: {defn.step}"
            return tip

        return None

    def setData(self, index: QModelIndex, value: Any, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or index.column() != COL_VALUE:
            return False
        if index.row() >= len(self._dids):
            return False
        did = self._dids[index.row()]
        # This triggers the write to device via store signal
        self._store.request_write(did, value)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.ForegroundRole])
        return True

    def _on_changed(self, did: int) -> None:
        if did not in self._dids:
            return
        row = self._dids.index(did)
        tl = self.index(row, COL_VALUE)
        self.dataChanged.emit(tl, tl, [Qt.DisplayRole, Qt.ForegroundRole])


class ParameterDelegate(QStyledItemDelegate):
    def __init__(self, store: ParameterStore, parent=None):
        super().__init__(parent)
        self._store = store

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem,
                     index: QModelIndex) -> Optional[QWidget]:
        did = index.data(Qt.UserRole)
        if did is None:
            return None
        defn = self._store.get_definition(did)
        if defn is None or defn.read_only:
            return None

        t = defn.param_type

        if t == ParameterType.BOOL:
            combo = QComboBox(parent)
            combo.addItem("False", False)
            combo.addItem("True",  True)
            return combo

        if t == ParameterType.ENUM:
            combo = QComboBox(parent)
            for k, v in sorted(defn.enum_values.items()):
                combo.addItem(v, k)
            return combo

        if t == ParameterType.FLOAT:
            sb = QDoubleSpinBox(parent)
            sb.setMinimum(defn.min_val if defn.min_val is not None else -1e9)
            sb.setMaximum(defn.max_val if defn.max_val is not None else  1e9)
            if defn.step:
                sb.setSingleStep(defn.step)
                s = str(defn.step)
                decimals = len(s.rstrip("0").split(".")[-1]) if "." in s else 2
                sb.setDecimals(min(max(decimals, 2), 8))
            else:
                sb.setDecimals(6)
            if defn.unit and defn.unit != "-":
                sb.setSuffix(f"  {defn.unit}")
            return sb

        # Integer / enum fallback
        sb = QSpinBox(parent)
        lo = int(defn.min_val) if defn.min_val is not None else -(2**31)
        hi = int(defn.max_val) if defn.max_val is not None else  (2**31 - 1)
        sb.setMinimum(max(lo, -(2**31)))
        sb.setMaximum(min(hi,   2**31 - 1))
        if defn.step:
            sb.setSingleStep(int(defn.step))
        return sb

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        did = index.data(Qt.UserRole)
        if did is None:
            return
        defn = self._store.get_definition(did)
        pv   = self._store.get_value(did)
        if not defn or not pv or not pv.is_loaded or pv.value is None:
            return

        val = pv.value
        t   = defn.param_type

        if isinstance(editor, QComboBox):
            if t == ParameterType.BOOL:
                editor.setCurrentIndex(1 if val else 0)
            else:
                idx = editor.findData(int(val))
                if idx >= 0:
                    editor.setCurrentIndex(idx)
        elif isinstance(editor, QDoubleSpinBox):
            editor.setValue(float(val))
        elif isinstance(editor, QSpinBox):
            editor.setValue(int(val))

    def setModelData(self, editor: QWidget, model, index: QModelIndex) -> None:
        did = index.data(Qt.UserRole)
        if did is None:
            return
        defn = self._store.get_definition(did)
        if not defn:
            return

        if isinstance(editor, QComboBox):
            val = editor.currentData()
        elif isinstance(editor, QDoubleSpinBox):
            val = editor.value()
        elif isinstance(editor, QSpinBox):
            val = editor.value()
        else:
            return

        # Directly call store.request_write — bypasses model.setData
        # to ensure the signal fires even if value appears unchanged
        self._store.request_write(did, val)

        # Also notify model for immediate visual update
        model.setData(index, val, Qt.EditRole)
