"""
Parameter model: typed dataclasses representing FOC drive parameters
loaded from JSON, with change notification via Qt signals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from PySide6.QtCore import QObject, Signal


class ParameterType(str, Enum):
    UINT8  = "uint8"
    UINT16 = "uint16"
    UINT32 = "uint32"
    INT8   = "int8"
    INT16  = "int16"
    INT32  = "int32"
    FLOAT  = "float"
    BOOL   = "bool"
    ENUM   = "enum"


@dataclass
class ParameterDefinition:
    """Static definition loaded from JSON – never mutated after init."""
    did:         int          # e.g. 0x1001
    name:        str          # e.g. "Motor.PolePairs"
    description: str
    category:    str
    param_type:  ParameterType
    unit:        str
    read_only:   bool
    visible:     bool
    # numeric constraints (None for bool/enum)
    min_val:     Optional[float] = None
    max_val:     Optional[float] = None
    step:        Optional[float] = None
    # enum specific
    enum_values: Dict[int, str] = field(default_factory=dict)

    @property
    def did_str(self) -> str:
        return f"0x{self.did:04X}"

    @property
    def short_name(self) -> str:
        """Return the part after the last dot, e.g. 'PolePairs'"""
        return self.name.split(".")[-1]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ParameterDefinition":
        did_raw = d["did"]
        did = int(did_raw, 16) if isinstance(did_raw, str) else int(did_raw)
        ptype = ParameterType(d["type"])
        enum_values: Dict[int, str] = {}
        if ptype == ParameterType.ENUM:
            enum_values = {int(k): v for k, v in d.get("values", {}).items()}
        return cls(
            did=did,
            name=d["name"],
            description=d.get("description", ""),
            category=d.get("category", ""),
            param_type=ptype,
            unit=d.get("unit", ""),
            read_only=d.get("readOnly", False),
            visible=d.get("visible", True),
            min_val=d.get("min"),
            max_val=d.get("max"),
            step=d.get("step"),
            enum_values=enum_values,
        )


class ParameterValue:
    """Mutable, typed wrapper around a parameter's current value."""

    def __init__(self, definition: ParameterDefinition):
        self.definition = definition
        self._value: Any = None
        self.is_dirty: bool = False       # written locally, not yet confirmed
        self.is_loaded: bool = False      # successfully read from device
        self.error: Optional[str] = None  # last read/write error

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, v: Any) -> None:
        self._value = self._coerce(v)
        self.error = None

    def _coerce(self, v: Any) -> Any:
        t = self.definition.param_type
        if t == ParameterType.BOOL:
            if isinstance(v, bool):
                return v
            return bool(int(v))
        if t == ParameterType.FLOAT:
            return float(v)
        if t in (ParameterType.UINT8, ParameterType.UINT16, ParameterType.UINT32,
                 ParameterType.INT8, ParameterType.INT16, ParameterType.INT32,
                 ParameterType.ENUM):
            return int(v)
        return v

    def display_value(self) -> str:
        if not self.is_loaded:
            return "–"
        if self._value is None:
            return "–"
        t = self.definition.param_type
        if t == ParameterType.BOOL:
            return "True" if self._value else "False"
        if t == ParameterType.ENUM:
            return self.definition.enum_values.get(int(self._value), str(self._value))
        if t == ParameterType.FLOAT:
            step = self.definition.step
            if step and step > 0:
                import math
                dec = max(2, -int(math.floor(math.log10(step))))
                dec = min(dec, 10)
                return f"{self._value:.{dec}f}"
            return f"{self._value:.6g}"
        return str(self._value)

    def __repr__(self) -> str:
        return f"<ParameterValue {self.definition.name}={self._value}>"


class ParameterStore(QObject):
    """
    Central store for all parameters.
    Signals:
        parameter_changed(did: int)   – a value was updated from device
        parameter_write_requested(did: int, value: Any) – UI wants to write
    """
    parameter_changed         = Signal(int)
    parameter_write_requested = Signal(int, object)
    all_parameters_loaded     = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._definitions: Dict[int, ParameterDefinition] = {}
        self._values:      Dict[int, ParameterValue]      = {}

    # ------------------------------------------------------------------ #
    #  Loading                                                             #
    # ------------------------------------------------------------------ #

    def load_from_json(self, path: Union[str, Path]) -> None:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
        for item in items:
            defn = ParameterDefinition.from_dict(item)
            self._definitions[defn.did] = defn
            self._values[defn.did] = ParameterValue(defn)

    def load_from_list(self, items: List[Dict[str, Any]]) -> None:
        for item in items:
            defn = ParameterDefinition.from_dict(item)
            self._definitions[defn.did] = defn
            self._values[defn.did] = ParameterValue(defn)

    # ------------------------------------------------------------------ #
    #  Access                                                              #
    # ------------------------------------------------------------------ #

    @property
    def definitions(self) -> Dict[int, ParameterDefinition]:
        return self._definitions

    @property
    def values(self) -> Dict[int, ParameterValue]:
        return self._values

    def get_definition(self, did: int) -> Optional[ParameterDefinition]:
        return self._definitions.get(did)

    def get_value(self, did: int) -> Optional[ParameterValue]:
        return self._values.get(did)

    def categories(self) -> List[str]:
        seen, result = set(), []
        for defn in self._definitions.values():
            if defn.category not in seen:
                seen.add(defn.category)
                result.append(defn.category)
        return result

    def parameters_in_category(self, category: str) -> List[ParameterDefinition]:
        return [d for d in self._definitions.values() if d.category == category]

    # ------------------------------------------------------------------ #
    #  Updates (called by UDS layer)                                       #
    # ------------------------------------------------------------------ #

    def update_from_device(self, did: int, raw_value: Any) -> None:
        pv = self._values.get(did)
        if pv is None:
            return
        pv.value = raw_value
        pv.is_loaded = True
        pv.is_dirty = False
        pv.error = None
        self.parameter_changed.emit(did)

    def set_error(self, did: int, message: str) -> None:
        pv = self._values.get(did)
        if pv is None:
            return
        pv.error = message
        pv.is_loaded = False
        self.parameter_changed.emit(did)

    def request_write(self, did: int, value: Any) -> None:
        """Called by UI when user edits a value."""
        pv = self._values.get(did)
        if pv is None:
            return
        pv.value = value
        pv.is_dirty = True
        self.parameter_write_requested.emit(did, pv.value)

    def all_dids(self) -> List[int]:
        return list(self._definitions.keys())
