"""
Write History & Undo
====================
Tracks every parameter write with timestamp.
Supports undo (single-level and multi-level).

History is kept in-memory during the session.
Optionally persisted to a session log file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

MAX_HISTORY = 200


@dataclass
class HistoryEntry:
    timestamp:  str
    did:        int
    name:       str
    old_value:  Any
    new_value:  Any
    unit:       str = ""

    @property
    def timestamp_short(self) -> str:
        return self.timestamp[11:19]  # HH:MM:SS

    @property
    def old_str(self) -> str:
        return "—" if self.old_value is None else str(self.old_value)

    @property
    def new_str(self) -> str:
        return "—" if self.new_value is None else str(self.new_value)


class WriteHistory(QObject):
    """
    Records parameter writes and supports undo.

    Usage:
        history.record(did, name, old_value, new_value)
        history.undo(write_fn)   # calls write_fn(did, old_value)
    """

    history_changed = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._entries: List[HistoryEntry] = []
        self._undo_stack: List[HistoryEntry] = []  # entries that were undone

    def record(self, did: int, name: str, old_value: Any,
               new_value: Any, unit: str = "") -> None:
        entry = HistoryEntry(
            timestamp=datetime.now().isoformat(timespec="milliseconds"),
            did=did, name=name,
            old_value=old_value, new_value=new_value,
            unit=unit,
        )
        self._entries.append(entry)
        self._undo_stack.clear()  # new write clears redo stack
        if len(self._entries) > MAX_HISTORY:
            self._entries.pop(0)
        self.history_changed.emit()
        log.debug("History: %s = %s → %s", name, old_value, new_value)

    def undo(self, write_fn: Callable[[int, Any], None]) -> Optional[HistoryEntry]:
        """Undo last write. Calls write_fn(did, old_value)."""
        if not self._entries:
            return None
        entry = self._entries.pop()
        self._undo_stack.append(entry)
        write_fn(entry.did, entry.old_value)
        self.history_changed.emit()
        log.info("Undo: %s = %s → %s", entry.name, entry.new_value, entry.old_value)
        return entry

    def redo(self, write_fn: Callable[[int, Any], None]) -> Optional[HistoryEntry]:
        """Redo last undone write."""
        if not self._undo_stack:
            return None
        entry = self._undo_stack.pop()
        self._entries.append(entry)
        write_fn(entry.did, entry.new_value)
        self.history_changed.emit()
        log.info("Redo: %s = %s", entry.name, entry.new_value)
        return entry

    @property
    def entries(self) -> List[HistoryEntry]:
        return list(reversed(self._entries))  # newest first

    @property
    def can_undo(self) -> bool:
        return len(self._entries) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def last_entry(self) -> Optional[HistoryEntry]:
        return self._entries[-1] if self._entries else None

    def clear(self) -> None:
        self._entries.clear()
        self._undo_stack.clear()
        self.history_changed.emit()
