"""Flask dashboard for public EV charge point availability (NDW DOT-NL).

The browser cannot call NDW directly — it sends no CORS headers — so this app
fetches server-side, keeps a history in SQLite, and serves both to a single
dashboard page from its own origin.

    uv sync
    uv run flask --app app run --port 8000

Configuration is environment-only:

    NDW_BBOX           minLon,minLat,maxLon,maxLat (default: Akkermaalsbos 2)
    NDW_POLL_SECONDS   how often to record an observation (default 300)
    NDW_CACHE_SECONDS  how long a fetched response stays fresh (default 60)
    NDW_MERGE_METRES   fold one operator's stations this close into one site
                       (default 10; 0 shows every registered station)
    NDW_DB             SQLite path (default data/history.sqlite3)
    NDW_CACHE_DIR      response cache directory (default data/cache)
    NDW_RETAIN_DAYS    history retention, 0 keeps everything (default 90)
    NDW_FIXTURE        read this JSON file instead of calling NDW (offline demo)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

import ndw
from store import Store

LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        LOGGER.warning("%s is not a number; using %s", name, default)
        return default


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        BBOX=ndw.default_bbox(),
        POLL_SECONDS=_int_env("NDW_POLL_SECONDS", 300),
        CACHE_SECONDS=_int_env("NDW_CACHE_SECONDS", 60),
        MERGE_METRES=_int_env("NDW_MERGE_METRES", 10),
        RETAIN_DAYS=_int_env("NDW_RETAIN_DAYS", 90),
        CACHE_DIR=Path(os.environ.get("NDW_CACHE_DIR", "data/cache")),
        FIXTURE=os.environ.get("NDW_FIXTURE") or None,
    )
    try:
        ndw.validate_bbox(app.config["BBOX"])
    except ValueError as err:
        raise SystemExit(f"NDW_BBOX is invalid: {err}") from err

    store = Store(Path(os.environ.get("NDW_DB", "data/history.sqlite3")))
    app.extensions["store"] = store
    state: dict[str, Any] = {"last_fetch": None, "last_error": None}
    app.extensions["state"] = state

    def observe() -> dict[str, Any]:
        """Fetch (cached) and record the observation. Returns the collection."""
        collection, from_cache = ndw.fetch(
            app.config["BBOX"],
            cache_dir=app.config["CACHE_DIR"],
            ttl=app.config["CACHE_SECONDS"],
            fixture=app.config["FIXTURE"],
        )
        now = time.time()
        latest = store.latest_minute()
        due = latest is None or (now // 60 - latest) * 60 >= app.config["POLL_SECONDS"]
        if due:
            store.add(now, collection)
        if not from_cache:
            state["last_fetch"] = now
            state["last_error"] = None
        return collection

    app.extensions["observe"] = observe

    @app.route("/")
    def index() -> str:
        return render_template("index.html", bbox=app.config["BBOX"])

    @app.route("/api/current")
    def api_current():
        try:
            collection = observe()
        except ndw.NdwError as err:
            state["last_error"] = str(err)
            return jsonify({"error": str(err)}), 503
        return jsonify(
            {
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "bbox": app.config["BBOX"],
                "stations": ndw.parse(collection, app.config["MERGE_METRES"]),
            }
        )

    @app.route("/api/history")
    def api_history():
        hours = min(max(request.args.get("hours", default=48, type=int), 1), 24 * 90)
        station_id = request.args.get("station")
        since = int(time.time() // 60) - hours * 60
        series = []
        for minute, collection in store.history(since):
            for station in ndw.parse(collection, app.config["MERGE_METRES"]):
                if station_id and station["id"] != station_id:
                    continue
                series.append(
                    {
                        "t": minute * 60,
                        "station": station["id"],
                        "available": station["available"],
                        "total": station["total"],
                        "last_updated": station["last_updated"],
                        "groups": [
                            {
                                "power_kw": g["power_kw"],
                                "available": g["available"],
                                "total": g["total"],
                            }
                            for g in station["groups"]
                        ],
                    }
                )
        return jsonify({"hours": hours, "station": station_id, "samples": series})

    @app.route("/healthz")
    def healthz():
        last = state["last_fetch"]
        return jsonify(
            {
                "ok": state["last_error"] is None,
                "observations": store.count(),
                "last_fetch_age_seconds": None
                if last is None
                else int(time.time() - last),
                "last_error": state["last_error"],
                "bbox": app.config["BBOX"],
                "fixture": bool(app.config["FIXTURE"]),
            }
        )

    _start_poller(app)
    return app


def _start_poller(app: Flask) -> None:
    """Poll in the background so history accrues with nobody watching.

    Several workers may run this; the store ignores a minute it already has, so
    duplicates collapse rather than pile up.
    """
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    store: Store = app.extensions["store"]
    observe = app.extensions["observe"]
    state = app.extensions["state"]

    def loop() -> None:
        while True:
            try:
                observe()
                retain = app.config["RETAIN_DAYS"]
                if retain:
                    cutoff = int(time.time() // 60) - retain * 24 * 60
                    removed = store.prune(cutoff)
                    if removed:
                        LOGGER.info(
                            "pruned %s observations older than %sd", removed, retain
                        )
            except Exception as err:  # keep the loop alive whatever happens
                state["last_error"] = str(err)
                LOGGER.warning("poll failed: %s", err)
            time.sleep(max(app.config["POLL_SECONDS"], 60))

    threading.Thread(target=loop, name="ndw-poller", daemon=True).start()


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_int_env("PORT", 8000))  # noqa: S104
