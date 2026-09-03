"""Tests that run without touching the network (NDW_FIXTURE stands in)."""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

import pytest

import ndw
import ocpi

FIXTURE = Path(__file__).with_name("fixture.json")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NDW_FIXTURE", str(FIXTURE))
    monkeypatch.setenv("NDW_DB", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("NDW_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NDW_POLL_SECONDS", "60")
    # Off unless a test asks for it: enabled, this downloads a national file.
    monkeypatch.setenv("NDW_OCPI_SECONDS", "0")
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


def _evse(status, power_type="AC_1_PHASE"):
    return {
        "evse_id": f"NL505E{status}",
        "status": status,
        "connectors": [{"power_type": power_type, "max_amperage": 32}],
    }


def _ocpi_bulk(tmp_path, locations):
    """A gzipped OCPI locations array, the shape opendata.ndw.nu serves."""
    path = tmp_path / "locations.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(locations, handle)
    return path


def test_ocpi_id_strips_the_country_and_cpo_prefix():
    assert ocpi.ocpi_id("NL-LMS-91107050") == "91107050"
    # Qwello ids are UUIDs, which contain the separator themselves.
    assert (
        ocpi.ocpi_id("NL-QWC-631dc33b-24f8-4fb3-9400-51a9782025f1")
        == "631dc33b-24f8-4fb3-9400-51a9782025f1"
    )


def test_extract_keeps_only_the_wanted_stations(tmp_path):
    path = _ocpi_bulk(
        tmp_path,
        [
            {"id": "aaa", "evses": [_evse("AVAILABLE")]},
            {"id": "91107050", "evses": [_evse("UNKNOWN")]},
            {"id": "zzz", "evses": [_evse("CHARGING")]},
        ],
    )
    snapshot = ocpi.extract(path, {"91107050"})
    assert set(snapshot) == {"91107050"}


def test_extract_counts_dead_sockets_per_power_type(tmp_path):
    path = _ocpi_bulk(
        tmp_path,
        [
            {
                "id": "s1",
                "evses": [
                    _evse("AVAILABLE", "AC_1_PHASE"),
                    _evse("UNKNOWN", "AC_1_PHASE"),
                    _evse("OUTOFORDER", "AC_1_PHASE"),
                    _evse("CHARGING", "AC_3_PHASE"),
                    _evse("INOPERATIVE", "AC_3_PHASE"),
                ],
            }
        ],
    )
    groups = ocpi.extract(path, {"s1"})["s1"]["groups"]
    # UNKNOWN + OUTOFORDER are dead; CHARGING is not.
    assert groups["AC1"]["dead"] == 2
    assert groups["AC3"]["dead"] == 1


def test_extract_ignores_removed_and_planned_sockets(tmp_path):
    path = _ocpi_bulk(
        tmp_path,
        [{"id": "s1", "evses": [_evse("REMOVED"), _evse("PLANNED"), _evse("UNKNOWN")]}],
    )
    assert ocpi.extract(path, {"s1"})["s1"]["groups"]["AC1"]["dead"] == 1


def test_extract_lists_each_socket_with_its_state(tmp_path):
    path = _ocpi_bulk(
        tmp_path,
        [
            {
                "id": "s1",
                "evses": [
                    {
                        "evse_id": "NL505E1*1",
                        "status": "CHARGING",
                        "connectors": [{"power_type": "AC_1_PHASE"}],
                    },
                    {
                        "evse_id": "NL505E2*1",
                        "status": "UNKNOWN",
                        "connectors": [{"power_type": "AC_1_PHASE"}],
                    },
                    # Not built yet: no socket to record a state for.
                    {"evse_id": "NL505E3*1", "status": "PLANNED", "connectors": []},
                ],
            }
        ],
    )
    evses = ocpi.extract(path, {"s1"})["s1"]["evses"]
    assert [(e["evse_id"], e["status"]) for e in evses] == [
        ("NL505E1*1", "CHARGING"),
        ("NL505E2*1", "UNKNOWN"),
    ]


def test_fetch_ignores_a_cache_written_in_an_older_format(tmp_path):
    """A shape change must read as a miss, not as good data.

    The snapshot format changed once already, and the old file stayed within its
    TTL: annotate() found no "groups" key, silently did nothing, and the cache
    hit meant no socket states were recorded either.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "ocpi-dead.json").write_text(
        json.dumps({"ts": time.time(), "snapshot": {"s1": {"AC1": {"dead": 2}}}}),
        encoding="utf-8",
    )
    bulk = _ocpi_bulk(tmp_path, [{"id": "s1", "evses": [_evse("UNKNOWN")]}])
    snapshot, from_cache = ocpi.fetch({"s1"}, cache_dir=cache, fixture=str(bulk))
    assert from_cache is False, "an unversioned cache must not be trusted"
    assert snapshot["s1"]["groups"]["AC1"]["dead"] == 1


def test_annotate_splits_the_not_free_sockets():
    stations = [
        {
            "id": "NL-LMS-x",
            "members": ["NL-LMS-x"],
            "groups": [{"power_type": "AC1", "available": 3, "total": 6}],
        }
    ]
    ocpi.annotate(stations, {"x": {"groups": {"AC1": {"dead": 2}}}})
    group = stations[0]["groups"][0]
    assert (group["available"], group["dead"], group["in_use"]) == (3, 2, 1)


def test_annotate_clamps_when_a_dead_socket_came_back():
    # The hourly snapshot says 2 dead, but the live feed already counts them free.
    stations = [
        {
            "id": "NL-LMS-x",
            "members": ["NL-LMS-x"],
            "groups": [{"power_type": "AC1", "available": 6, "total": 6}],
        }
    ]
    ocpi.annotate(stations, {"x": {"groups": {"AC1": {"dead": 2}}}})
    group = stations[0]["groups"][0]
    assert (group["dead"], group["in_use"]) == (0, 0), "in_use must never go negative"


def test_annotate_sums_across_the_members_of_a_merged_site():
    stations = [
        {
            "id": "NL-QWC-a",
            "members": ["NL-QWC-a", "NL-QWC-b"],
            "groups": [{"power_type": "AC3", "available": 2, "total": 4}],
        }
    ]
    ocpi.annotate(
        stations,
        {"a": {"groups": {"AC3": {"dead": 1}}}, "b": {"groups": {"AC3": {"dead": 1}}}},
    )
    group = stations[0]["groups"][0]
    assert (group["dead"], group["in_use"]) == (2, 0)


def test_annotate_leaves_stations_untouched_without_a_snapshot():
    stations = [
        {
            "id": "NL-LMS-x",
            "members": ["NL-LMS-x"],
            "groups": [{"power_type": "AC1", "available": 3, "total": 6}],
        }
    ]
    ocpi.annotate(stations, {})
    assert "dead" not in stations[0]["groups"][0]


def _stations(available=3, in_use=1, dead=1, total=5):
    return [
        {
            "id": "NL-LMS-x",
            "groups": [
                {
                    "power_kw": 7.4,
                    "available": available,
                    "total": total,
                    "in_use": in_use,
                    "dead": dead,
                }
            ],
        }
    ]


def test_store_ignores_a_minute_it_already_has(tmp_path):
    from store import Store

    store = Store(tmp_path / "h.sqlite3")
    # 1_000_000 // 60 == 16666, and so does 1_000_010 — the same minute.
    assert store.add(1_000_000, _stations()) == 1
    assert store.add(1_000_010, _stations()) == 0
    assert store.add(1_000_100, _stations()) == 1
    assert store.count() == 2


def test_store_prunes_both_tables(tmp_path):
    from store import Store

    store = Store(tmp_path / "h.sqlite3")
    store.add(1_000_000, _stations())
    store.add(2_000_000, _stations())
    store.add_socket_states(
        1_000_000, [{"station": "NL-LMS-x", "evse_id": "e1", "status": "CHARGING"}]
    )
    # One observation row and one socket row are older than the cutoff.
    assert store.prune(int(1_500_000 // 60)) == 2
    assert store.count() == 1


def test_occupancy_totals_socket_hours(tmp_path):
    from store import Store

    store = Store(tmp_path / "h.sqlite3")
    # Three polls, two sockets in use each, at the default five-minute cadence.
    for i in range(3):
        store.add(1_000_000 + i * 300, _stations(available=2, in_use=2, dead=1))
    stats = store.occupancy(0)
    assert stats["samples"] == 3
    # 3 polls x 2 sockets x 5 minutes = 30 socket-minutes = 0.5 socket-hours.
    assert stats["socket_hours_in_use"] == 0.5
    assert stats["socket_hours_capacity"] == 1.25
    assert stats["utilisation"] == 0.4


def test_occupancy_skips_rows_without_socket_detail(tmp_path):
    from store import Store

    store = Store(tmp_path / "h.sqlite3")
    store.add(1_000_000, _stations(in_use=2))
    # No OCPI snapshot: in_use is unknown, and guessing would be worse.
    store.add(1_000_300, _stations(in_use=None, dead=None))
    stats = store.occupancy(0)
    assert stats["samples"] == 1, "a gap must not be counted as idle"


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


def test_current_marks_dead_sockets_when_the_ocpi_snapshot_is_available(
    tmp_path, monkeypatch
):
    """The fixture station is 3/5 free at 7.4kW and 1/1 at 22kW."""
    bulk = tmp_path / "ocpi.json.gz"
    with gzip.open(bulk, "wt", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "id": "91107050",
                    "evses": [
                        _evse("AVAILABLE", "AC_1_PHASE"),
                        _evse("AVAILABLE", "AC_1_PHASE"),
                        _evse("AVAILABLE", "AC_1_PHASE"),
                        _evse("UNKNOWN", "AC_1_PHASE"),
                        _evse("OUTOFORDER", "AC_1_PHASE"),
                        _evse("AVAILABLE", "AC_3_PHASE"),
                    ],
                }
            ],
            handle,
        )
    monkeypatch.setenv("NDW_FIXTURE", str(FIXTURE))
    monkeypatch.setenv("NDW_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("NDW_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NDW_OCPI_SECONDS", "3600")
    monkeypatch.setenv("NDW_OCPI_FIXTURE", str(bulk))
    import app as app_module

    body = app_module.create_app().test_client().get("/api/current").get_json()
    station = body["stations"][0]
    slow = next(g for g in station["groups"] if g["power_kw"] == 7.4)
    # Two of the five are dead, so none of the not-free ones is in use.
    assert (slow["available"], slow["dead"], slow["in_use"]) == (3, 2, 0)
    assert station["dead"] == 2
    assert body["sockets_at"] is not None


def test_livez_does_not_touch_the_history_volume(tmp_path, monkeypatch):
    """Liveness must not depend on storage.

    /healthz reads the store, and on a longhorn rebuild that volume stalled for
    up to 10s at a time — with a 1s probe timeout, Kubernetes killed a healthy
    process 18 times. Liveness answers "is this process serving", nothing more.
    """
    monkeypatch.setenv("NDW_FIXTURE", str(FIXTURE))
    monkeypatch.setenv("NDW_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("NDW_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NDW_OCPI_SECONDS", "0")
    import app as app_module

    application = app_module.create_app()

    def boom(*args, **kwargs):
        raise AssertionError("liveness must not touch the history volume")

    application.extensions["store"].count = boom
    assert application.test_client().get("/livez").status_code == 200


def test_healthz_reports_state(client):
    body = client.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["fixture"] is True


def test_dashboard_page_renders(client):
    page = client.get("/").get_data(as_text=True)
    assert "Charge point availability" in page
    assert os.environ["NDW_BBOX"] in page if "NDW_BBOX" in os.environ else True
