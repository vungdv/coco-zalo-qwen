"""Local de-duplication store: keys of acked messages, so a restart never re-emits a handled message (FR-7, NFR-2).

A key is stored only after `ack(key)`. Written from the Coordinator thread, read from the Reader thread.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path


class AckedStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # autocommit: each ack is durable at once
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS acked (key TEXT PRIMARY KEY)")
        self._lock = threading.Lock()

    def contains(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM acked WHERE key = ?", (key,)).fetchone() is not None

    def add(self, keys: Iterable[str]) -> None:
        with self._lock:
            self._db.executemany("INSERT OR IGNORE INTO acked (key) VALUES (?)", [(k,) for k in keys])
