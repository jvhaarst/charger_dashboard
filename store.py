"""SQLite history for charge point observations.

One row per poll, keyed by the unix minute, so several workers polling the same
minute cannot double-insert. Payload is the raw FeatureCollection, which keeps
the schema stable if NDW adds fields.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    minute INTEGER PRIMARY KEY,
    body   TEXT NOT NULL
);
"""


class Store:
    """Append-only store of observations, one row per polled minute."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, when: float, collection: dict[str, Any]) -> bool:
        """Record one observation. Returns False if that minute already exists."""
        minute = int(when // 60)
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO snapshots (minute, body) VALUES (?, ?)",
                (minute, json.dumps(collection, separators=(",", ":"))),
            )
            return cursor.rowcount > 0

    def latest_minute(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(minute) FROM snapshots").fetchone()
        return row[0] if row and row[0] is not None else None

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    def history(self, since_minute: int, limit: int = 5000) -> list[tuple[int, dict]]:
        """Observations at or after a minute, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT minute, body FROM snapshots WHERE minute >= ? "
                "ORDER BY minute LIMIT ?",
                (since_minute, limit),
            ).fetchall()
        return [(minute, json.loads(body)) for minute, body in rows]

    def prune(self, before_minute: int) -> int:
        """Drop observations older than a minute. Returns rows removed."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM snapshots WHERE minute < ?", (before_minute,)
            )
            return cursor.rowcount
