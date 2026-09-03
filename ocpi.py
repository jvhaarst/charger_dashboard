"""Per-socket state from NDW's bulk OCPI locations file.

The bbox feed says how many sockets are free and nothing else, so a socket that
is merely silent is indistinguishable from one with a car on it. This module
supplies the missing half: which sockets are *dead* — not reporting, out of
order, or inoperative. Everything else follows by arithmetic from the live free
count, so the volatile state (CHARGING) is never fetched.

That matters, because the only source is a national file:

    https://opendata.ndw.nu/charging_point_locations_ocpi.json.gz

18 MB gzipped, ~198 MB raw, 80k locations. It is streamed and discarded a record
at a time — `json.load` on it peaks at 1.23 GB, five times the container's limit.
Being dead is a slow-moving property (half of OUTOFORDER sockets nationally have
been that way over a week), so this is polled far more slowly than the bbox feed.
See docs/investigation.md §9.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

BULK_URL = "https://opendata.ndw.nu/charging_point_locations_ocpi.json.gz"

# A socket in one of these is not occupied — nobody can charge on it, and nobody
# is. Everything not free and not dead is taken to be in use.
DEAD_STATUSES = frozenset({"UNKNOWN", "OUTOFORDER", "INOPERATIVE"})
# Sockets that do not physically exist yet, or any more, are not counted at all.
IGNORED_STATUSES = frozenset({"REMOVED", "PLANNED"})

# OCPI spells the phase count out; the bbox feed abbreviates it.
POWER_TYPES = {
    "AC_1_PHASE": "AC1",
    "AC_2_PHASE": "AC2",
    "AC_3_PHASE": "AC3",
    "DC": "DC",
}

# Bumped whenever the cached snapshot's shape changes. A cached file without
# this exact version is treated as a miss: when "evses" was added, the previous
# format sat inside its TTL and read as good data, so annotate() quietly did
# nothing and no socket states were recorded until the hour was up.
CACHE_VERSION = 2

_DECODER = json.JSONDecoder()


class OcpiError(RuntimeError):
    """Raised when the bulk file cannot be fetched and nothing is cached."""


def ocpi_id(station_id: str) -> str:
    """`NL-LMS-91107050` -> `91107050`.

    The bbox feed prefixes country and CPO; the OCPI file does not. Qwello ids
    are UUIDs containing the same separator, so only the first two fields are
    stripped.
    """
    parts = (station_id or "").split("-", 2)
    return parts[2] if len(parts) == 3 else (station_id or "")


def _locations(handle, chunk: int = 1 << 20) -> Iterator[dict[str, Any]]:
    """Yield each location from the JSON array without holding the array.

    `raw_decode` walks a buffer by offset; the buffer is compacted only when a
    record straddles a chunk boundary, which keeps this linear rather than
    quadratic.
    """
    buffer = handle.read(chunk)
    position = buffer.find("[") + 1
    while True:
        while position < len(buffer) and buffer[position] in ", \n\r\t":
            position += 1
        if position < len(buffer) and buffer[position] == "]":
            return
        try:
            record, position = _DECODER.raw_decode(buffer, position)
        except ValueError:
            more = handle.read(chunk)
            if not more:
                return
            buffer = buffer[position:] + more
            position = 0
            continue
        yield record


def extract(path: Path, wanted: set[str]) -> dict[str, dict[str, Any]]:
    """Read the sockets of the wanted stations out of the bulk file.

    Returns `{station_id: {"groups": {power_type: {"dead": n, "total": n}},
    "evses": [{"evse_id", "status", "power_type"}]}}` — the counts drive the
    dashboard's free/in-use/dead split, the list is what gets recorded as
    per-socket history.
    """
    found: dict[str, dict[str, Any]] = {}
    if not wanted:
        return found
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for record in _locations(handle):
            station = record.get("id")
            if station not in wanted:
                continue
            groups: dict[str, dict[str, int]] = {}
            evses: list[dict[str, Any]] = []
            for evse in record.get("evses") or []:
                status = evse.get("status")
                if status in IGNORED_STATUSES:
                    continue
                connectors = evse.get("connectors") or [{}]
                power_type = POWER_TYPES.get(
                    connectors[0].get("power_type"),
                    connectors[0].get("power_type") or "?",
                )
                group = groups.setdefault(power_type, {"dead": 0, "total": 0})
                group["total"] += 1
                if status in DEAD_STATUSES:
                    group["dead"] += 1
                evses.append(
                    {
                        "evse_id": evse.get("evse_id") or evse.get("uid"),
                        "status": status,
                        "power_type": power_type,
                    }
                )
            found[station] = {"groups": groups, "evses": evses}
            if len(found) == len(wanted):
                break
    return found


def annotate(
    stations: list[dict[str, Any]], snapshot: dict[str, dict[str, Any]]
) -> None:
    """Split each group's not-free sockets into in-use and dead, in place.

    Stations with nothing in the snapshot are left exactly as they were, so the
    dashboard falls back to plain free/not-free when this data is unavailable.
    """
    for station in stations:
        dead_by_type: dict[str, int] = {}
        for member in station.get("members") or [station.get("id")]:
            station_snapshot = snapshot.get(ocpi_id(member)) or {}
            for power_type, counts in (station_snapshot.get("groups") or {}).items():
                dead_by_type[power_type] = dead_by_type.get(power_type, 0) + counts.get(
                    "dead", 0
                )
        if not dead_by_type:
            continue
        for group in station["groups"]:
            not_free = group["total"] - group["available"]
            # The snapshot is up to an hour old: a socket recorded as dead may
            # already be free or charging again. Never claim more dead sockets
            # than there are not-free ones, or in_use would go negative.
            dead = max(0, min(dead_by_type.get(group["power_type"], 0), not_free))
            group["dead"] = dead
            group["in_use"] = not_free - dead
        station["dead"] = sum(g.get("dead", 0) for g in station["groups"])
        station["in_use"] = sum(g.get("in_use", 0) for g in station["groups"])


def fetch(
    station_ids: set[str],
    cache_dir: Path,
    ttl: float = 3600.0,
    timeout: float | tuple[float, float] = (3.05, 120.0),
    fixture: str | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return (snapshot, from_cache), refetching the bulk file only past `ttl`.

    A failed fetch falls back to the last good snapshot at any age: a stale idea
    of which sockets are dead is far better than none, because `annotate` clamps
    it against the live free count anyway.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = cache_dir / "ocpi-dead.json"
    try:
        cached = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if cached is not None and cached.get("v") != CACHE_VERSION:
        LOGGER.info("ignoring an OCPI cache written in an older format")
        cached = None
    if cached and time.time() - cached.get("ts", 0) <= ttl:
        return cached["snapshot"], True

    source = Path(fixture) if fixture else None
    temp = None
    try:
        if source is None:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".gz") as handle:
                temp = Path(handle.name)
            with requests.get(BULK_URL, timeout=timeout, stream=True) as response:
                response.raise_for_status()
                with temp.open("wb") as out:
                    shutil.copyfileobj(response.raw, out)
            source = temp
        snapshot = extract(source, station_ids)
    except (requests.RequestException, OSError, ValueError) as err:
        if cached:
            LOGGER.warning("OCPI fetch failed (%s); using the cached snapshot", err)
            return cached["snapshot"], True
        raise OcpiError(f"OCPI bulk file unavailable: {err}") from err
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)

    snapshot_path.write_text(
        json.dumps({"v": CACHE_VERSION, "ts": time.time(), "snapshot": snapshot}),
        encoding="utf-8",
    )
    return snapshot, False
