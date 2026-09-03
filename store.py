"""SQLite history for charge point observations.

Two tables, written at different rates because their sources move at different
rates:

`observations` — one row per power group per poll, from the fast bbox feed. This
is what occupancy is computed from: `in_use` summed over five-minute intervals
gives occupied socket-hours. `in_use` and `dead` are NULL when the socket detail
was unavailable, so a gap is visible as a gap rather than counted as idle.

`socket_states` — one row per socket per OCPI refresh, roughly hourly. Socket
identity exists only in the bulk OCPI file, so this is as fine-grained as it
gets; it answers which socket does the work and which is dead, not how long an
individual session lasted.

Storing counts rather than the raw FeatureCollection costs about a tenth of the
disk (3.3 MB per month against 33.9) and is queryable directly in SQL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    minute    INTEGER NOT NULL,
    station   TEXT    NOT NULL,
    power_kw  REAL    NOT NULL,
    available INTEGER NOT NULL,
    total     INTEGER NOT NULL,
    in_use    INTEGER,
    dead      INTEGER,
    PRIMARY KEY (minute, station, power_kw)
);
CREATE INDEX IF NOT EXISTS observations_by_station
    ON observations (station, minute);

CREATE TABLE IF NOT EXISTS socket_states (
    minute  INTEGER NOT NULL,
    station TEXT    NOT NULL,
    evse    TEXT    NOT NULL,
    status  TEXT    NOT NULL,
    PRIMARY KEY (minute, evse)
);
CREATE INDEX IF NOT EXISTS socket_states_by_evse
    ON socket_states (evse, minute);

-- The old schema stored a whole FeatureCollection per poll and could not
-- express occupancy at all. Dropped rather than left behind unread.
DROP TABLE IF EXISTS snapshots;
"""


class Store:
    """Append-only history: group counts often, socket states occasionally."""

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
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, when: float, stations: list[dict[str, Any]]) -> int:
        """Record one observation per power group. Returns rows written.

        A minute already recorded is ignored, so several workers polling the
        same minute collapse instead of piling up.
        """
        minute = int(when // 60)
        rows = [
            (
                minute,
                station["id"],
                group["power_kw"],
                group["available"],
                group["total"],
                group.get("in_use"),
                group.get("dead"),
            )
            for station in stations
            for group in station["groups"]
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO observations "
                "(minute, station, power_kw, available, total, in_use, dead) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return cursor.rowcount

    def add_socket_states(self, when: float, sockets: list[dict[str, Any]]) -> int:
        """Record each socket's state. Returns rows written."""
        minute = int(when // 60)
        rows = [
            (minute, s["station"], s["evse_id"], s["status"])
            for s in sockets
            if s.get("evse_id") and s.get("status")
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO socket_states (minute, station, evse, status) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            return cursor.rowcount

    def latest_minute(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(minute) AS m FROM observations").fetchone()
        return row["m"] if row and row["m"] is not None else None

    def count(self) -> int:
        """Distinct minutes observed — one 'observation' as the API means it."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT minute) AS n FROM observations"
            ).fetchone()
        return int(row["n"])

    def history(self, since_minute: int, limit: int = 20000) -> list[dict[str, Any]]:
        """Group rows at or after a minute, reassembled per station per minute."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT minute, station, power_kw, available, total, in_use, dead "
                "FROM observations WHERE minute >= ? "
                "ORDER BY minute, station, power_kw LIMIT ?",
                (since_minute, limit),
            ).fetchall()
        samples: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            sample = samples.setdefault(
                (row["minute"], row["station"]),
                {
                    "t": row["minute"] * 60,
                    "station": row["station"],
                    "available": 0,
                    "total": 0,
                    "groups": [],
                },
            )
            sample["available"] += row["available"]
            sample["total"] += row["total"]
            sample["groups"].append(
                {
                    "power_kw": row["power_kw"],
                    "available": row["available"],
                    "total": row["total"],
                    "in_use": row["in_use"],
                    "dead": row["dead"],
                }
            )
        return list(samples.values())

    def occupancy(
        self, since_minute: int, station: str | None = None, poll_minutes: int = 5
    ) -> dict[str, Any]:
        """Occupied socket-hours and utilisation, plus a profile by hour of day.

        Only rows that carry `in_use` count: without the socket detail we cannot
        tell an occupied socket from a silent one, and guessing would be worse
        than reporting a smaller sample.
        """
        # Both queries are written out in full rather than sharing a built-up
        # WHERE clause: no SQL here is ever assembled from a variable. The
        # optional station filter rides on a named parameter.
        params = {"since": since_minute, "station": station or None}
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(DISTINCT minute) AS minutes, SUM(in_use) AS in_use, "
                "SUM(total) AS total, SUM(dead) AS dead FROM observations "
                "WHERE minute >= :since AND in_use IS NOT NULL "
                "AND (:station IS NULL OR station = :station)",
                params,
            ).fetchone()
            by_hour = conn.execute(
                "SELECT CAST(strftime('%H', minute * 60, 'unixepoch', 'localtime') "
                "AS INTEGER) AS hour, SUM(in_use) AS in_use, SUM(total) AS total "
                "FROM observations "
                "WHERE minute >= :since AND in_use IS NOT NULL "
                "AND (:station IS NULL OR station = :station) "
                "GROUP BY hour ORDER BY hour",
                params,
            ).fetchall()
        hours = poll_minutes / 60
        socket_hours = (totals["in_use"] or 0) * hours
        capacity = (totals["total"] or 0) * hours
        return {
            "samples": totals["minutes"] or 0,
            "socket_hours_in_use": round(socket_hours, 2),
            "socket_hours_capacity": round(capacity, 2),
            "utilisation": round(socket_hours / capacity, 4) if capacity else None,
            "socket_hours_dead": round((totals["dead"] or 0) * hours, 2),
            "by_hour": [
                {
                    "hour": row["hour"],
                    "utilisation": round(row["in_use"] / row["total"], 4)
                    if row["total"]
                    else None,
                }
                for row in by_hour
            ],
        }

    def prune(self, before_minute: int) -> int:
        """Drop history older than a minute, from both tables. Rows removed."""
        with self._connect() as conn:
            removed = conn.execute(
                "DELETE FROM observations WHERE minute < ?", (before_minute,)
            ).rowcount
            removed += conn.execute(
                "DELETE FROM socket_states WHERE minute < ?", (before_minute,)
            ).rowcount
            return removed
