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


@pytest.mark.parametrize(
    "bbox",
    ["5.0,51.0", "a,b,c,d", "5.7,52.0,5.6,52.1", "0,0,50,50"],
)
def test_validate_bbox_rejects_bad_input(bbox):
    with pytest.raises(ValueError):
        ndw.validate_bbox(bbox)


def test_validate_bbox_accepts_the_default():
    assert ndw.validate_bbox("5.6580,51.9810,5.6620,51.9840")


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
