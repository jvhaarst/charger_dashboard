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
    NDW_OCPI_SECONDS   how often to refresh which sockets are dead, from the
                       bulk OCPI file (default 3600; 0 disables the feature)

Endpoints worth knowing apart: /livez is liveness and touches nothing, /healthz
is readiness and reads the history database.
    NDW_DB             SQLite path (default data/history.sqlite3)
    NDW_CACHE_DIR      response cache directory (default data/cache)
    NDW_RETAIN_DAYS    history retention, 0 keeps everything (default 365)
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
import ocpi
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
        OCPI_SECONDS=_int_env("NDW_OCPI_SECONDS", 3600),
        OCPI_FIXTURE=os.environ.get("NDW_OCPI_FIXTURE") or None,
        RETAIN_DAYS=_int_env("NDW_RETAIN_DAYS", 365),
        CACHE_DIR=Path(os.environ.get("NDW_CACHE_DIR", "data/cache")),
        FIXTURE=os.environ.get("NDW_FIXTURE") or None,
    )
    try:
        ndw.validate_bbox(app.config["BBOX"])
    except ValueError as err:
        raise SystemExit(f"NDW_BBOX is invalid: {err}") from err

    store = Store(Path(os.environ.get("NDW_DB", "data/history.sqlite3")))
    app.extensions["store"] = store
    state: dict[str, Any] = {
        "last_fetch": None,
        "last_error": None,
        "ocpi_at": None,
        "ocpi_error": None,
    }
    app.extensions["state"] = state

    def sockets(stations: list[dict[str, Any]]) -> dict[str, Any]:
        """Refresh the dead-socket snapshot and fold it into `stations`.

        Never fatal: without it the dashboard simply shows free/not-free, which
        is what it did before this existed.
        """
        if not app.config["OCPI_SECONDS"]:
            return {}
        wanted = {ocpi.ocpi_id(m) for s in stations for m in s.get("members") or []}
        try:
            snapshot, cached = ocpi.fetch(
                wanted,
                cache_dir=app.config["CACHE_DIR"],
                ttl=app.config["OCPI_SECONDS"],
                fixture=app.config["OCPI_FIXTURE"],
            )
        except ocpi.OcpiError as err:
            state["ocpi_error"] = str(err)
            LOGGER.warning("dead-socket snapshot unavailable: %s", err)
            return {}
        if not cached:
            state["ocpi_at"] = time.time()
            state["ocpi_error"] = None
            # Socket identity only exists here, so this is the one chance to
            # record it. Written per refresh, not per poll, or it would repeat
            # the same states a dozen times an hour.
            rows = [
                {"station": member, "evse_id": e["evse_id"], "status": e["status"]}
                for station in stations
                for member in station.get("members") or [station["id"]]
                for e in (snapshot.get(ocpi.ocpi_id(member)) or {}).get("evses") or []
            ]
            written = store.add_socket_states(time.time(), rows)
            if written:
                LOGGER.info("recorded %s socket states", written)
        ocpi.annotate(stations, snapshot)
        return snapshot

    def observe() -> list[dict[str, Any]]:
        """Fetch (cached), annotate and record. Returns the parsed stations."""
        collection, from_cache = ndw.fetch(
            app.config["BBOX"],
            cache_dir=app.config["CACHE_DIR"],
            ttl=app.config["CACHE_SECONDS"],
            fixture=app.config["FIXTURE"],
        )
        stations = ndw.parse(collection, app.config["MERGE_METRES"])
        # Annotate before storing: in_use is what occupancy is computed from,
        # and it cannot be recovered later from a stored free count alone.
        sockets(stations)
        now = time.time()
        latest = store.latest_minute()
        due = latest is None or (now // 60 - latest) * 60 >= app.config["POLL_SECONDS"]
        if due:
            store.add(now, stations)
        if not from_cache:
            state["last_fetch"] = now
            state["last_error"] = None
        return stations

    app.extensions["observe"] = observe
    app.extensions["sockets"] = sockets

    @app.route("/")
    def index() -> str:
        return render_template("index.html", bbox=app.config["BBOX"])

    @app.route("/api/current")
    def api_current():
        try:
            stations = observe()
        except ndw.NdwError as err:
            state["last_error"] = str(err)
            return jsonify({"error": str(err)}), 503
        return jsonify(
            {
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "bbox": app.config["BBOX"],
                "sockets_at": None
                if state["ocpi_at"] is None
                else datetime.fromtimestamp(state["ocpi_at"], UTC).isoformat(
                    timespec="seconds"
                ),
                "stations": stations,
            }
        )

    @app.route("/api/history")
    def api_history():
        hours = min(max(request.args.get("hours", default=48, type=int), 1), 24 * 400)
        station_id = request.args.get("station")
        since = int(time.time() // 60) - hours * 60
        samples = [
            sample
            for sample in store.history(since)
            if not station_id or sample["station"] == station_id
        ]
        return jsonify({"hours": hours, "station": station_id, "samples": samples})

    @app.route("/api/occupancy")
    def api_occupancy():
        hours = min(max(request.args.get("hours", default=168, type=int), 1), 24 * 400)
        since = int(time.time() // 60) - hours * 60
        stats = store.occupancy(
            since,
            station=request.args.get("station"),
            poll_minutes=max(app.config["POLL_SECONDS"] // 60, 1),
        )
        return jsonify({"hours": hours, **stats})

    @app.route("/livez")
    def livez():
        """Liveness only: is this process serving requests?

        Touches no disk and no network, deliberately. `/healthz` reads the
        history database, and that volume can stall for ten seconds at a time
        while longhorn rebuilds a replica — pointing liveness at it turned a slow
        disk into a restart loop and killed a healthy process 18 times over.
        Readiness may fail during a storage stall; liveness must not.
        """
        return "ok\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

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
                "sockets_age_seconds": None
                if state["ocpi_at"] is None
                else int(time.time() - state["ocpi_at"]),
                "sockets_error": state["ocpi_error"],
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
