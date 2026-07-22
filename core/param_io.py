"""
Parameter Import / Export
==========================
Export: device values → JSON or CSV
Import: JSON or CSV → staged in BatchWriter for review before write

JSON format:
{
  "exported":    "2024-03-15T10:30:00",
  "device":      "0xA0",
  "parameters": [
    {"did": "0x1001", "name": "Motor.PolePairs",
     "value": 4, "unit": "-", "type": "uint8"},
    ...
  ]
}

CSV format:
did,name,value,unit,type
0x1001,Motor.PolePairs,4,-,uint8
0x1002,Motor.PhaseResistance,0.185,Ohm,float
...
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.parameter_model import ParameterStore

log = logging.getLogger(__name__)


class ExportFormat:
    JSON = "json"
    CSV  = "csv"


# ------------------------------------------------------------------ #
#  Export                                                              #
# ------------------------------------------------------------------ #

def export_to_json(store: ParameterStore,
                   device_address: Optional[int] = None,
                   only_loaded: bool = True) -> str:
    """
    Export current parameter values to JSON string.
    only_loaded=True skips parameters not yet read from device.
    """
    params = []
    for did, pv in store.values.items():
        if only_loaded and not pv.is_loaded:
            continue
        defn = store.get_definition(did)
        if defn is None:
            continue
        params.append({
            "did":   f"0x{did:04X}",
            "name":  defn.name,
            "value": pv.value,
            "unit":  defn.unit,
            "type":  defn.param_type.value,
        })

    doc = {
        "exported":   datetime.now().isoformat(timespec="seconds"),
        "device":     f"0x{device_address:02X}" if device_address else "unknown",
        "parameters": params,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


def export_to_csv(store: ParameterStore,
                  only_loaded: bool = True) -> str:
    """Export current parameter values to CSV string."""
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["did", "name", "value", "unit", "type", "description"])

    for did, pv in store.values.items():
        if only_loaded and not pv.is_loaded:
            continue
        defn = store.get_definition(did)
        if defn is None:
            continue
        writer.writerow([
            f"0x{did:04X}",
            defn.name,
            pv.value if pv.value is not None else "",
            defn.unit,
            defn.param_type.value,
            defn.description,
        ])
    return buf.getvalue()


def save_export(store: ParameterStore, path: Path,
                device_address: Optional[int] = None) -> Tuple[int, str]:
    """
    Save to file. Format determined by extension (.json or .csv).
    Returns (count, error_message). error_message is "" on success.
    """
    fmt = ExportFormat.CSV if path.suffix.lower() == ".csv" else ExportFormat.JSON
    only_loaded = True

    try:
        if fmt == ExportFormat.JSON:
            content = export_to_json(store, device_address, only_loaded)
        else:
            content = export_to_csv(store, only_loaded)

        path.write_text(content, encoding="utf-8")
        n = content.count('"did"') if fmt == ExportFormat.JSON else content.count('\n') - 1
        log.info("Exported %d parameters to %s", n, path)
        return n, ""
    except Exception as e:
        log.error("Export failed: %s", e)
        return 0, str(e)


# ------------------------------------------------------------------ #
#  Import                                                              #
# ------------------------------------------------------------------ #

class ImportResult:
    def __init__(self):
        self.entries:  List[Dict[str, Any]] = []  # {did, name, value, unit, type}
        self.errors:   List[str] = []
        self.warnings: List[str] = []

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        return len(self.entries) > 0 and len(self.errors) == 0


def import_from_json(content: str, store: ParameterStore) -> ImportResult:
    """Parse JSON export and validate against current parameter definitions."""
    result = ImportResult()
    try:
        doc = json.loads(content)
    except json.JSONDecodeError as e:
        result.errors.append(f"JSON parse error: {e}")
        return result

    params = doc.get("parameters", [])
    if not params:
        result.errors.append("No parameters found in JSON")
        return result

    for item in params:
        did_str = item.get("did", "")
        try:
            did = int(did_str, 0)
        except (ValueError, TypeError):
            result.warnings.append(f"Skipped: invalid DID '{did_str}'")
            continue

        defn = store.get_definition(did)
        if defn is None:
            result.warnings.append(
                f"Skipped: DID {did_str} not in parameter definitions")
            continue

        if defn.read_only:
            result.warnings.append(f"Skipped read-only: {defn.name}")
            continue

        value = item.get("value")
        if value is None:
            result.warnings.append(f"Skipped: no value for {defn.name}")
            continue

        # Validate range
        try:
            if defn.param_type.value == "float":
                v = float(value)
            elif defn.param_type.value == "bool":
                v = bool(value)
            else:
                v = int(value)

            if defn.min_val is not None and v < defn.min_val:
                result.warnings.append(
                    f"Warning: {defn.name}={v} below min {defn.min_val}, clamped")
                v = type(v)(defn.min_val)
            if defn.max_val is not None and v > defn.max_val:
                result.warnings.append(
                    f"Warning: {defn.name}={v} above max {defn.max_val}, clamped")
                v = type(v)(defn.max_val)
        except (ValueError, TypeError) as e:
            result.warnings.append(f"Skipped: {defn.name} bad value '{value}': {e}")
            continue

        result.entries.append({
            "did":   did,
            "name":  defn.name,
            "value": v,
            "unit":  defn.unit,
            "type":  defn.param_type.value,
        })

    log.info("Import: %d params, %d warnings, %d errors",
             result.count, len(result.warnings), len(result.errors))
    return result


def import_from_csv(content: str, store: ParameterStore) -> ImportResult:
    """Parse CSV export and validate."""
    result = ImportResult()
    try:
        reader = csv.DictReader(StringIO(content))
    except Exception as e:
        result.errors.append(f"CSV parse error: {e}")
        return result

    for row in reader:
        did_str = row.get("did", "").strip()
        try:
            did = int(did_str, 0)
        except (ValueError, TypeError):
            result.warnings.append(f"Skipped: invalid DID '{did_str}'")
            continue

        defn = store.get_definition(did)
        if defn is None:
            result.warnings.append(f"Skipped: DID {did_str} not in definitions")
            continue

        if defn.read_only:
            continue

        raw_val = row.get("value", "").strip()
        if not raw_val:
            continue

        try:
            if defn.param_type.value == "float":
                v = float(raw_val)
            elif defn.param_type.value == "bool":
                v = raw_val.lower() in ("1", "true", "yes")
            else:
                v = int(float(raw_val))  # int(float()) handles "4.0"
        except (ValueError, TypeError):
            result.warnings.append(f"Skipped: {defn.name} bad value '{raw_val}'")
            continue

        result.entries.append({
            "did":   did,
            "name":  defn.name,
            "value": v,
            "unit":  defn.unit,
            "type":  defn.param_type.value,
        })

    return result


def load_import(path: Path, store: ParameterStore) -> ImportResult:
    """Load from file, auto-detect format by extension."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        r = ImportResult()
        r.errors.append(f"Cannot read file: {e}")
        return r

    if path.suffix.lower() == ".csv":
        return import_from_csv(content, store)
    else:
        return import_from_json(content, store)
