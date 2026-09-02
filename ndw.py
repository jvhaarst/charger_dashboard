"""Client for the NDW DOT-NL public charge point API.

DOT-NL is the Dutch national access point for charge point data, published free
of charge under AFIR (EU 2023/1804). It needs no key, but it sends no CORS
headers, which is why this has to be fetched server-side rather than from the
browser.

Responses are cached on disk with a TTL. A failed fetch falls back to the last
good cached value at any age, so an upstream hiccup never blanks the dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

BASE_URL = (
    "https://dotnl.ndw.nu/api/rest/geojson/dynamic-road-status"
    "/charge-point-data/v1/features"
)
USER_AGENT = "ndw-charger-dashboard/1.0 (+https://github.com/)"

# The API caps a bounding box at 1.0 degree² and 1000 features, and rate-limits
# to 10 requests per second.
MAX_BBOX_DEGREES_SQUARED = 1.0

# Connect and read budgets are kept apart on purpose. dotnl.ndw.nu publishes an
# AAAA record that silently drops packets, and socket.create_connection tries
# each resolved address in turn applying the whole timeout to each — so a single
# scalar spends its entire budget on the dead IPv6 address before trying IPv4,
# which then answers in milliseconds. A short connect timeout caps that waste.
# 3.05 rather than 3 so it lands just past the 3-second TCP retransmission
# window. See docs/investigation.md.
CONNECT_TIMEOUT = 3.05
READ_TIMEOUT = 15.0


class NdwError(RuntimeError):
    """Raised when NDW cannot be reached and no cached value is available."""


def _cache_path(cache_dir: Path, key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return cache_dir / f"{safe}.json"


def _cache_read(path: Path, ttl: float | None) -> dict[str, Any] | None:
    """Read a cached payload. ttl None accepts any age (the stale fallback)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if ttl is not None and time.time() - raw.get("ts", 0) > ttl:
        return None
    return raw


def _cache_write(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"ts": time.time(), "body": body})
    # Write-then-rename so a reader never sees a half-written file.
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(payload)
        temp = Path(handle.name)
    temp.replace(path)


def validate_bbox(bbox: str) -> str:
    """Check a "minLon,minLat,maxLon,maxLat" string against the API's limits."""
    parts = bbox.split(",")
    if len(parts) != 4:
        raise ValueError("bbox must be minLon,minLat,maxLon,maxLat")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as err:
        raise ValueError("bbox values must be numbers") from err
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox minimums must be smaller than maximums")
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > MAX_BBOX_DEGREES_SQUARED:
        raise ValueError(f"bbox area {area:.3f} exceeds the API limit of 1.0 deg²")
    return bbox


def fetch(
    bbox: str,
    cache_dir: Path,
    ttl: float = 60.0,
    timeout: float | tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT),
    fixture: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return (GeoJSON FeatureCollection, from_cache).

    Serves a fresh cache hit without touching the network. On a network or HTTP
    failure, falls back to the last good cached value of any age.
    """
    if fixture:
        # Offline/demo mode: read a saved response instead of calling NDW.
        return json.loads(Path(fixture).read_text(encoding="utf-8")), True

    path = _cache_path(cache_dir, bbox)
    fresh = _cache_read(path, ttl)
    if fresh is not None:
        return fresh["body"], True

    try:
        response = requests.get(
            BASE_URL,
            params={"bbox": bbox},
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as err:
        stale = _cache_read(path, None)
        if stale is not None:
            LOGGER.warning("NDW fetch failed (%s); serving cached data", err)
            return stale["body"], True
        raise NdwError(f"NDW unreachable and nothing cached: {err}") from err

    _cache_write(path, body)
    return body, False


def parse(collection: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a FeatureCollection into the shape the dashboard renders."""
    stations = []
    for feature in collection.get("features", []):
        props = feature.get("properties", {}) or {}
        coords = (feature.get("geometry", {}) or {}).get("coordinates") or [None, None]
        groups = []
        for group in props.get("availabilities", []) or []:
            total = int(group.get("total", 0) or 0)
            available = int(group.get("available", 0) or 0)
            groups.append(
                {
                    "available": available,
                    "total": total,
                    "power_kw": round(float(group.get("power_max", 0) or 0) / 1000, 1),
                    "power_type": group.get("power_type"),
                    "connector": group.get("connector_type"),
                    "format": group.get("connector_format"),
                    "tariff_ids": group.get("tariff_ids") or [],
                }
            )
        stations.append(
            {
                "id": feature.get("id"),
                "address": props.get("address"),
                "operator": props.get("operator_name") or props.get("owner_name"),
                "cpo_id": props.get("cpo_id"),
                "country": props.get("country"),
                "longitude": coords[0],
                "latitude": coords[1],
                "last_updated": props.get("last_updated"),
                "groups": groups,
                "available": sum(g["available"] for g in groups),
                "total": sum(g["total"] for g in groups),
            }
        )
    stations.sort(key=lambda s: (s["address"] or "", s["id"] or ""))
    return stations


def default_bbox() -> str:
    """Bounding box from the environment.

    The default covers roughly a square kilometre of the Wageningen Business &
    Science Park: Akkermaalsbos 2, the two Qwello points on Bornsesteeg 4, and
    Droevendaalsesteeg 1. The dashboard grows a station picker past one station.
    """
    return os.environ.get("NDW_BBOX", "5.655835,51.980796,5.670791,51.989109")
