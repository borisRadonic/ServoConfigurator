"""
Parameter Table Model + Delegate  — fixed
==========================================
Root causes fixed:
  1. EditRole returned None when not loaded → editor empty → no write
     Fix: EditRole returns a sane default from definition even when not loaded
  2. delegate.setModelData called model.setData which Qt may skip if value
     appears unchanged.
     Fix: delegate calls store.request_write() directly, always.
  3. UserRole must be available on every column so delegate can get DID.
"""
from __future__ import annotations
import math
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QSpinBox,
    QStyledItemDelegate, QStyleOptionViewItem, QWidget,
)
from core.parameter_model import ParameterStore, ParameterType

COL_DID=0; COL_NAME=1; COL_VALUE=2; COL_UNIT=3; COL_DESC=4
HEADERS = ["DID", "Parameter", "Value", "Unit", "Description"]

C_DIRTY   = QColor("#FF9800")
C_ERROR   = QColor("#F44336")
C_LOADED  = QColor("#A6E3A1")
C_PENDING = QColor("#89B4FA")
C_UNLOADED= QColor("#585B70")


class ParameterTableModel(QAbstractTableModel):
    def __init__(self, store: ParameterStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._dids: list[int] = []
        store.parameter_changed.connect(self._on_changed)

    def set_category(self, cat: str):
        self.beginResetModel()
        self._dids = [d.did for d in self._store.parameters_in_category(cat)]
        self.endResetModel()

    def set_filter(self, text: str):
        self.beginResetModel()
        lo = text.lower()
        self._dids = [
            d.did for d in self._store.definitions.values()
            if lo in d.name.lower() or lo in d.description.lower()
        ]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()): return len(self._dids)
    def columnCount(self, parent=QModelIndex()): return 5

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]

    def flags(self, index: QModelIndex):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_VALUE and index.row() < len(self._dids):
            defn = self._store.get_definition(self._dids[index.row()])
            if defn and not defn.read_only:
                f |= Qt.ItemIsEditable
        return f

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._dids):
            return None
        did  = self._dids[index.row()]
        defn = self._store.get_definition(did)
        pv   = self._store.get_value(did)
        col  = index.column()

        # Always expose DID on every column for delegate
        if role == Qt.UserRole:
            return did

        if role == Qt.DisplayRole:
            if col == COL_DID:   return defn.did_str
            if col == COL_NAME:  return defn.name
            if col == COL_UNIT:  return defn.unit
            if col == COL_DESC:  return defn.description
            if col == COL_VALUE:
                if pv and pv.error:     return f"! {pv.error}"
                if pv and pv.is_dirty:  return f"↑ {pv.display_value()}"
                if pv and pv.is_loaded: return pv.display_value()
                return "–"

        if role == Qt.EditRole and col == COL_VALUE:
            # KEY FIX: always return a usable value for the editor.
            # If loaded, return actual value. Otherwise return a safe default
            # derived from the definition so the editor is never empty.
            if pv and pv.value is not None:
                return pv.value
            # Sensible defaults when not yet read from device
            t = defn.param_type
            if t == ParameterType.BOOL:  return False
            if t == ParameterType.ENUM:
                return next(iter(defn.enum_values.keys()), 0)
            if t == ParameterType.FLOAT:
                if defn.min_val is not None and defn.min_val > 0:
                    return defn.min_val
                return 0.0
            return 0

        if role == Qt.ForegroundRole and col == COL_VALUE:
            if pv:
                if pv.error:     return C_ERROR
                if pv.is_dirty:  return C_PENDING
                if pv.is_loaded: return C_LOADED
            return C_UNLOADED

        if role == Qt.FontRole and col in (COL_DID, COL_NAME):
            f = QFont("Consolas, Courier New, monospace")
            if col == COL_NAME: f.setPointSize(10)
            return f

        if role == Qt.ToolTipRole:
            tip = f"<b>{defn.name}</b><br>{defn.description}"
            if defn.min_val is not None:
                tip += f"<br>Range: {defn.min_val} … {defn.max_val}"
            if defn.step:
                tip += f"  Step: {defn.step}"
            if defn.unit and defn.unit != "-":
                tip += f"  Unit: {defn.unit}"
            if pv and pv.error:
                tip += f"<br><span style='color:red'>Error: {pv.error}</span>"
            return tip

        return None

    def setData(self, index: QModelIndex, value: Any, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or index.column() != COL_VALUE:
            return False
        if index.row() >= len(self._dids):
            return False
        did = self._dids[index.row()]
        self._store.request_write(did, value)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.ForegroundRole])
        return True

    def _on_changed(self, did: int):
        if did not in self._dids:
            return
        row = self._dids.index(did)
        tl = self.index(row, COL_VALUE)
        self.dataChanged.emit(tl, tl, [Qt.DisplayRole, Qt.ForegroundRole])


class ParameterDelegate(QStyledItemDelegate):
    def __init__(self, store: ParameterStore, parent=None):
        super().__init__(parent)
        self._store = store

    def createEditor(self, parent, option, index: QModelIndex):
        did = index.data(Qt.UserRole)
        if did is None: return None
        defn = self._store.get_definition(did)
        if not defn or defn.read_only: return None

        t = defn.param_type

        if t == ParameterType.BOOL:
            w = QComboBox(parent)
            w.addItem("False", False)
            w.addItem("True",  True)
            return w

        if t == ParameterType.ENUM:
            w = QComboBox(parent)
            for k, v in sorted(defn.enum_values.items()):
                w.addItem(v, k)
            return w

        if t == ParameterType.FLOAT:
            w = QDoubleSpinBox(parent)
            w.setMinimum(defn.min_val if defn.min_val is not None else -1e9)
            w.setMaximum(defn.max_val if defn.max_val is not None else  1e9)
            if defn.step:
                w.setSingleStep(defn.step)
                # Use log10 — str() uses scientific notation for small steps
                # e.g. str(1e-7) = "1e-07" which breaks the old string method
                dec = max(2, -int(math.floor(math.log10(defn.step)))) if defn.step > 0 else 6
                w.setDecimals(min(dec, 10))
            else:
                w.setDecimals(6)
            if defn.unit and defn.unit not in ("-", ""):
                w.setSuffix(f"  {defn.unit}")
            return w

        # Integer types
        w = QSpinBox(parent)
        lo = int(defn.min_val) if defn.min_val is not None else -(2**30)
        hi = int(defn.max_val) if defn.max_val is not None else  (2**30)
        w.setMinimum(max(lo, -(2**31)))
        w.setMaximum(min(hi,   2**31 - 1))
        if defn.step:
            w.setSingleStep(int(defn.step))
        return w

    def setEditorData(self, editor, index: QModelIndex):
        val = index.data(Qt.EditRole)
        if val is None: return
        did  = index.data(Qt.UserRole)
        defn = self._store.get_definition(did) if did else None
        if not defn: return
        t = defn.param_type
        if isinstance(editor, QComboBox):
            idx = editor.findData(bool(val) if t == ParameterType.BOOL else int(val))
            if idx >= 0: editor.setCurrentIndex(idx)
        elif isinstance(editor, QDoubleSpinBox):
            editor.setValue(float(val))
        elif isinstance(editor, QSpinBox):
            editor.setValue(int(val))

    def setModelData(self, editor, model, index: QModelIndex):
        did = index.data(Qt.UserRole)
        if did is None: return
        defn = self._store.get_definition(did)
        if not defn: return

        if isinstance(editor, QComboBox):
            val = editor.currentData()
        elif isinstance(editor, QDoubleSpinBox):
            val = editor.value()
        elif isinstance(editor, QSpinBox):
            val = editor.value()
        else:
            return

        # KEY FIX: call store directly — bypasses Qt's "unchanged" optimisation
        self._store.request_write(did, val)
        # Also update model display immediately
        model.setData(index, val, Qt.EditRole)
