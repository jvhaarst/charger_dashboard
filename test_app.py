"""Tests that run without touching the network (NDW_FIXTURE stands in)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import ndw

FIXTURE = Path(__file__).with_name("fixture.json")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NDW_FIXTURE", str(FIXTURE))
    monkeypatch.setenv("NDW_DB", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("NDW_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NDW_POLL_SECONDS", "60")
    import app as app_module

    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


def test_parse_flattens_the_feature_collection():
    collection = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stations = ndw.parse(collection)
    assert len(stations) == 1
    station = stations[0]
    assert station["id"] == "NL-LMS-91107050"
    assert station["operator"] == "50five"
    assert station["available"] == 4
    assert station["total"] == 6
    assert [g["power_kw"] for g in station["groups"]] == [7.4, 22.1]


def test_parse_survives_missing_fields():
    stations = ndw.parse({"features": [{"id": "X", "properties": {}}]})
    assert stations[0]["available"] == 0
    assert stations[0]["total"] == 0


def _feature(fid, operator, lat, lon, groups, address="Somewhere 1"):
    """A minimal NDW feature. groups is [(available, total, power_max, type)]."""
    return {
        "type": "Feature",
        "id": fid,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "address": address,
            "operator_name": operator,
            "last_updated": "2026-09-02T16:00:00Z",
            "availabilities": [
                {
                    "available": a,
                    "total": t,
                    "power_max": p,
                    "power_type": pt,
                    "connector_type": "IEC_62196_T2",
                    "connector_format": "SOCKET",
                    "tariff_ids": ["238"],
                }
                for a, t, p, pt in groups
            ],
        },
    }


# 50m north, comfortably outside any 10m threshold.
FAR_LAT = 51.982121 + 50 / 111320


def test_parse_merges_co_located_stations_of_one_operator():
    collection = {
        "features": [
            _feature("a", "Qwello", 51.982121, 5.665258, [(1, 2, 17250.0, "AC3")]),
            _feature("b", "Qwello", 51.982121, 5.665258, [(1, 2, 17250.0, "AC3")]),
        ]
    }
    stations = ndw.parse(collection)
    assert len(stations) == 1
    assert (stations[0]["available"], stations[0]["total"]) == (2, 4)
    # Identical power, phase, connector and format collapse into one row.
    assert len(stations[0]["groups"]) == 1
    merged = stations[0]["groups"][0]
    assert (merged["available"], merged["total"]) == (2, 4)


def test_parse_keeps_distant_stations_separate():
    collection = {
        "features": [
            _feature("a", "Qwello", 51.982121, 5.665258, [(1, 2, 17250.0, "AC3")]),
            _feature("b", "Qwello", FAR_LAT, 5.665258, [(1, 2, 17250.0, "AC3")]),
        ]
    }
    assert len(ndw.parse(collection)) == 2


def test_parse_keeps_co_located_operators_separate():
    # Allego and Vattenfall sit metres apart all over Wageningen, and whether
    # that is two posts or one double-registered post is not knowable from the
    # feed — so merging them could double-count sockets. See investigation.md §9.
    collection = {
        "features": [
            _feature("a", "Allego", 51.979702, 5.672529, [(1, 2, 11000.0, "AC3")]),
            _feature("b", "Vattenfall", 51.979702, 5.672529, [(1, 2, 11000.0, "AC3")]),
        ]
    }
    assert len(ndw.parse(collection)) == 2


def test_merged_station_keeps_a_stable_id_and_lists_its_members():
    collection = {
        "features": [
            _feature("b-two", "Qwello", 51.982121, 5.665258, [(1, 2, 17250.0, "AC3")]),
            _feature("a-one", "Qwello", 51.982121, 5.665258, [(0, 2, 17250.0, "AC3")]),
        ]
    }
    station = ndw.parse(collection)[0]
    # First id alphabetically, so a member vanishing for one poll cannot rename
    # the merged station and break history filtering.
    assert station["id"] == "a-one"
    assert station["members"] == ["a-one", "b-two"]


def test_merge_can_be_switched_off():
    collection = {
        "features": [
            _feature("a", "Qwello", 51.982121, 5.665258, [(1, 2, 17250.0, "AC3")]),
            _feature("b", "Qwello", 51.982121, 5.665258, [(1, 2, 17250.0, "AC3")]),
        ]
    }
    assert len(ndw.parse(collection, merge_metres=0)) == 2


def test_merging_keeps_unlike_power_levels_apart():
    collection = {
        "features": [
            _feature("a", "50five", 51.982319, 5.660022, [(3, 5, 7360.0, "AC1")]),
            _feature("b", "50five", 51.982319, 5.660022, [(1, 1, 22080.0, "AC3")]),
        ]
    }
    station = ndw.parse(collection)[0]
    assert [g["power_kw"] for g in station["groups"]] == [7.4, 22.1]
    assert (station["available"], station["total"]) == (4, 6)


@pytest.mark.parametrize(
    "bbox",
    ["5.0,51.0", "a,b,c,d", "5.7,52.0,5.6,52.1", "0,0,50,50"],
)
def test_validate_bbox_rejects_bad_input(bbox):
    with pytest.raises(ValueError):
        ndw.validate_bbox(bbox)


def test_validate_bbox_accepts_the_default(monkeypatch):
    # The box we ship has to satisfy the limits we enforce on everyone else.
    monkeypatch.delenv("NDW_BBOX", raising=False)
    assert ndw.validate_bbox(ndw.default_bbox())


def test_fetch_bounds_the_connect_timeout_separately(tmp_path, monkeypatch):
    """A dead address must not be able to spend the whole request budget.

    dotnl.ndw.nu publishes an AAAA record that silently drops packets, and
    socket.create_connection applies the timeout to each address in turn — so a
    single scalar timeout is burned entirely on the dead IPv6 address before
    IPv4 is even tried. A short connect timeout caps that waste. See
    docs/investigation.md.
    """
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"type": "FeatureCollection", "features": []}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(ndw.requests, "get", fake_get)
    ndw.fetch("5.0,51.0,5.1,51.1", cache_dir=tmp_path / "cache")

    connect, read = seen["timeout"]
    assert connect < read, "connect budget must be separate from the read budget"
    assert connect <= 5, f"connect timeout {connect}s lets a dead address stall us"


def test_store_ignores_a_minute_it_already_has(tmp_path):
    from store import Store

    store = Store(tmp_path / "h.sqlite3")
    # 1_000_000 // 60 == 16666, and so does 1_000_010 — the same minute.
    assert store.add(1_000_000, {"a": 1}) is True
    assert store.add(1_000_010, {"a": 2}) is False
    assert store.add(1_000_100, {"a": 3}) is True
    assert store.count() == 2


def test_store_prunes_old_rows(tmp_path):
    from store import Store

    store = Store(tmp_path / "h.sqlite3")
    store.add(1_000_000, {"a": 1})
    store.add(2_000_000, {"a": 2})
    assert store.prune(int(1_500_000 // 60)) == 1
    assert store.count() == 1


def test_current_endpoint_returns_the_station(client):
    body = client.get("/api/current").get_json()
    assert body["stations"][0]["id"] == "NL-LMS-91107050"
    assert body["stations"][0]["available"] == 4


def test_current_records_history_which_history_endpoint_serves(client):
    client.get("/api/current")
    body = client.get("/api/history?hours=1").get_json()
    assert len(body["samples"]) == 1
    assert body["samples"][0]["available"] == 4
    assert body["samples"][0]["total"] == 6


def test_history_filters_by_station(client):
    client.get("/api/current")
    assert client.get("/api/history?station=nope").get_json()["samples"] == []


def test_healthz_reports_state(client):
    body = client.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["fixture"] is True


def test_dashboard_page_renders(client):
    page = client.get("/").get_data(as_text=True)
    assert "Charge point availability" in page
    assert os.environ["NDW_BBOX"] in page if "NDW_BBOX" in os.environ else True
