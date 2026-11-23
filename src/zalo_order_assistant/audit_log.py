"""SQLite audit log (FR-8): every message read that produced no order. Never the message text (NFR-3)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class SqliteAuditLog:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Created on the main thread, used only by the Coordinator thread afterwards.
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audit ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT NOT NULL,"
            " sent_at TEXT NOT NULL,"
            " reason TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " recorded_at TEXT NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS audit_key ON audit (key)")

    def record(self, key: str, sent_at: datetime, reason: str, status: str) -> None:
        self._db.execute(
            "INSERT INTO audit (key, sent_at, reason, status, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (key, sent_at.isoformat(), reason, status, datetime.now(UTC).isoformat(timespec="seconds")),
        )

    def entries(self) -> list[tuple[str, str, str, str]]:
        """(key, sent_at, reason, status), oldest first."""
        return self._db.execute("SELECT key, sent_at, reason, status FROM audit ORDER BY id").fetchall()

    def close(self) -> None:
        self._db.close()
