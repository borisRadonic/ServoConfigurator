"""
Preset Manager
==============
Handles named parameter presets — save, load, compare, export/import.

Preset file format (JSON):
{
  "name": "Motor_A_tuned",
  "description": "Tuned for Motor A, 3kW, Hall encoder",
  "created": "2024-03-15T10:30:00",
  "modified": "2024-03-15T11:00:00",
  "values": {
    "0x1001": 4,
    "0x1002": 0.185,
    ...
  }
}

Presets are stored in:
  <app_dir>/presets/<name>.json
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from core.parameter_model import ParameterStore

log = logging.getLogger(__name__)

PRESETS_DIR_NAME = "presets"


@dataclass
class Preset:
    name:        str
    description: str = ""
    created:     str = ""
    modified:    str = ""
    values:      Dict[str, Any] = field(default_factory=dict)  # "0x1001" → value

    @property
    def did_values(self) -> Dict[int, Any]:
        """Values keyed by int DID."""
        result = {}
        for k, v in self.values.items():
            try:
                result[int(k, 0)] = v
            except (ValueError, TypeError):
                pass
        return result

    def to_dict(self) -> dict:
        return {
            "name":        self.name,
            "description": self.description,
            "created":     self.created,
            "modified":    self.modified,
            "values":      self.values,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        return cls(
            name=d.get("name", "unnamed"),
            description=d.get("description", ""),
            created=d.get("created", ""),
            modified=d.get("modified", ""),
            values=d.get("values", {}),
        )

    @classmethod
    def from_store(cls, store: ParameterStore, name: str,
                   description: str = "") -> "Preset":
        """Capture current device values from store into a preset."""
        values = {}
        for did, pv in store.values.items():
            if pv.is_loaded and pv.value is not None:
                values[f"0x{did:04X}"] = pv.value
        now = datetime.now().isoformat(timespec="seconds")
        return cls(name=name, description=description,
                   created=now, modified=now, values=values)


@dataclass
class DiffEntry:
    did:          int
    name:         str
    left_value:   Any    # None if not present
    right_value:  Any    # None if not present
    unit:         str = ""

    @property
    def differs(self) -> bool:
        if self.left_value is None or self.right_value is None:
            return True
        # Float comparison with tolerance
        try:
            return abs(float(self.left_value) - float(self.right_value)) > 1e-9
        except (TypeError, ValueError):
            return self.left_value != self.right_value

    @property
    def left_str(self) -> str:
        return "—" if self.left_value is None else str(self.left_value)

    @property
    def right_str(self) -> str:
        return "—" if self.right_value is None else str(self.right_value)


class PresetManager(QObject):
    """
    Manages preset files on disk.
    Emits signals when presets change.
    """
    presets_changed = Signal()

    def __init__(self, app_dir: Optional[Path] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        if app_dir is None:
            app_dir = Path(__file__).parent.parent
        self._presets_dir = app_dir / PRESETS_DIR_NAME
        self._presets_dir.mkdir(exist_ok=True)

    @property
    def presets_dir(self) -> Path:
        return self._presets_dir

    def list_presets(self) -> List[str]:
        """Return sorted list of preset names."""
        return sorted(
            p.stem for p in self._presets_dir.glob("*.json")
        )

    def load(self, name: str) -> Optional[Preset]:
        path = self._presets_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return Preset.from_dict(json.load(f))
        except Exception as e:
            log.error("Failed to load preset %s: %s", name, e)
            return None

    def save(self, preset: Preset) -> bool:
        preset.modified = datetime.now().isoformat(timespec="seconds")
        if not preset.created:
            preset.created = preset.modified
        path = self._presets_dir / f"{preset.name}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(preset.to_dict(), f, indent=2, ensure_ascii=False)
            self.presets_changed.emit()
            log.info("Preset saved: %s (%d values)", preset.name, len(preset.values))
            return True
        except Exception as e:
            log.error("Failed to save preset %s: %s", preset.name, e)
            return False

    def delete(self, name: str) -> bool:
        path = self._presets_dir / f"{name}.json"
        try:
            path.unlink()
            self.presets_changed.emit()
            return True
        except Exception as e:
            log.error("Failed to delete preset %s: %s", name, e)
            return False

    def rename(self, old_name: str, new_name: str) -> bool:
        preset = self.load(old_name)
        if not preset:
            return False
        preset.name = new_name
        if self.save(preset):
            self.delete(old_name)
            return True
        return False

    def compare(self, store: ParameterStore,
                left: Preset, right: Preset) -> List[DiffEntry]:
        """
        Compare two presets against the parameter definitions in store.
        Returns only entries that differ.
        """
        entries = []
        all_dids = set(left.did_values.keys()) | set(right.did_values.keys())

        for did in sorted(all_dids):
            defn = store.get_definition(did)
            name = defn.name if defn else f"0x{did:04X}"
            unit = defn.unit if defn else ""
            left_val  = left.did_values.get(did)
            right_val = right.did_values.get(did)
            entry = DiffEntry(did=did, name=name, unit=unit,
                              left_value=left_val, right_value=right_val)
            if entry.differs:
                entries.append(entry)
        return entries

    def diff_vs_device(self, store: ParameterStore,
                       preset: Preset) -> List[DiffEntry]:
        """Compare preset against current device values in store."""
        device_preset = Preset.from_store(store, "__device__")
        return self.compare(store, preset, device_preset)

    def import_from_file(self, path: Path) -> Optional[Preset]:
        try:
            with open(path, encoding="utf-8") as f:
                preset = Preset.from_dict(json.load(f))
            self.save(preset)
            return preset
        except Exception as e:
            log.error("Import failed: %s", e)
            return None

    def export_to_file(self, name: str, path: Path) -> bool:
        preset = self.load(name)
        if not preset:
            return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(preset.to_dict(), f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            log.error("Export failed: %s", e)
            return False
