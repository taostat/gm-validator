"""Durable record of which finalized epochs the validator has processed.

Without this, ``_processed`` is in-memory only: a restart re-discovers
every finalized epoch still in S3 and re-submits weights for all of them.
The state file is a small JSON document holding the sorted list of
processed epoch ids; it is loaded at startup and rewritten atomically
after each newly-processed epoch.
"""

from __future__ import annotations

import json
import logging
import os

LOGGER = logging.getLogger(__name__)


class ProcessedState:
    """Loads and persists the set of processed epoch ids."""

    def __init__(self, path: str) -> None:
        """Open the state file at *path*, creating its parent directory.

        A missing or unreadable file is treated as an empty state — the
        validator must never crash because a fresh deploy has no marker.
        """
        self._path = path
        self._epochs: set[int] = _load(path)

    @property
    def epochs(self) -> set[int]:
        """The set of epoch ids recorded as processed."""
        return self._epochs

    def __contains__(self, epoch_id: int) -> bool:
        return epoch_id in self._epochs

    def mark(self, epoch_id: int) -> None:
        """Record *epoch_id* as processed and persist immediately.

        Persisting after every epoch (rather than at shutdown) means a
        crash mid-loop cannot lose progress.
        """
        if epoch_id in self._epochs:
            return
        self._epochs.add(epoch_id)
        self._persist()

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".part"
        payload = {"schema_version": "1", "processed_epochs": sorted(self._epochs)}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self._path)


def _load(path: str) -> set[int]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {int(e) for e in data.get("processed_epochs", [])}
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        LOGGER.warning(
            "processed-state file %s unreadable (%s); starting with empty state",
            path,
            exc,
        )
        return set()
