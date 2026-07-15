"""
Batch Writer
============
Collects parameter changes and writes them all at once on confirm.
Prevents write-per-edit behaviour.

Usage:
    batch = BatchWriter(store)
    batch.stage(did, new_value)     # collect changes
    batch.commit(write_fn)          # write all at once
    batch.discard()                 # throw away staged changes
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from core.parameter_model import ParameterStore

log = logging.getLogger(__name__)


@dataclass
class StagedChange:
    did:       int
    name:      str
    old_value: Any
    new_value: Any
    unit:      str = ""


class BatchWriter(QObject):
    """
    Stages parameter changes without writing to device.
    On commit(), writes all staged changes.
    """

    staged_changed  = Signal()    # staged list changed
    commit_started  = Signal(int) # number of params being written
    commit_done     = Signal()

    def __init__(self, store: ParameterStore,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._store = store
        self._staged: Dict[int, StagedChange] = {}  # did → change

    @property
    def staged(self) -> List[StagedChange]:
        return list(self._staged.values())

    @property
    def count(self) -> int:
        return len(self._staged)

    def stage(self, did: int, new_value: Any) -> None:
        """Stage a change. Overwrites previous staged value for same DID."""
        defn = self._store.get_definition(did)
        pv   = self._store.get_value(did)
        if defn is None:
            return
        old_value = pv.value if pv else None
        self._staged[did] = StagedChange(
            did=did,
            name=defn.name,
            old_value=old_value,
            new_value=new_value,
            unit=defn.unit,
        )
        self.staged_changed.emit()

    def unstage(self, did: int) -> None:
        """Remove a staged change."""
        self._staged.pop(did, None)
        self.staged_changed.emit()

    def discard(self) -> None:
        """Discard all staged changes."""
        self._staged.clear()
        self.staged_changed.emit()

    def commit(self, write_fn: Callable[[int, Any], None],
               history: Optional[Any] = None) -> int:
        """
        Write all staged changes via write_fn(did, value).
        Records each write in history if provided.
        Returns number of writes initiated.
        """
        if not self._staged:
            return 0

        changes = list(self._staged.values())
        self.commit_started.emit(len(changes))

        for change in changes:
            write_fn(change.did, change.new_value)
            if history is not None:
                history.record(
                    change.did, change.name,
                    change.old_value, change.new_value,
                    change.unit,
                )
            log.info("Batch write: %s = %s", change.name, change.new_value)

        n = len(changes)
        self._staged.clear()
        self.staged_changed.emit()
        self.commit_done.emit()
        return n
