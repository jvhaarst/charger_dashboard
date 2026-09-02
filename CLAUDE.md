# WUR charger dashboard — project brief

Live availability of the public EV charge points at **Akkermaalsbos 2, Wageningen**
(WUR Business & Science Park), plus whatever else falls in the configured bounding
box. Flask app, deployed as a container.

This file is the handover from the session that built it. The investigation trail
is in `docs/investigation.md`; read it before proposing a different data source,
because most of the obvious alternatives have already been tested and ruled out.

## The station

| | |
|---|---|
| NDW id | `NL-LMS-91107050` |
| Operator | 50five (`cpo_id: LMS` = Last Mile Solutions, the EVC-Net platform) |
| Coordinates | 51.982319, 5.660022 |
| Sockets | 6 × Type 2: 5 × 7.4 kW (AC1) + 1 × 22 kW (AC3) |
| ChargeFinder slug | `djx7rq`, realtimeId `kw2qpmhren2xy` (legacy source, see archive) |

## Data source

**NDW DOT-NL** — the Dutch national access point, free and keyless, published under
AFIR (EU 2023/1804). Dynamic data must be updated within one minute of a change.

```
https://dotnl.ndw.nu/api/rest/geojson/dynamic-road-status/charge-point-data/v1/features?bbox=minLon,minLat,maxLon,maxLat
```

Limits: bounding box ≤ 1.0 degree², ≤ 1000 features, ≤ 10 requests/second (429 beyond).
Also published as bulk gzipped OCPI on `opendata.ndw.nu`.

**Why this app exists at all:** `dotnl.ndw.nu` sends **no CORS headers**, for any
origin — verified with a real `Origin` header, an `OPTIONS` preflight, and a
null-origin browser test with a known-good control. So a browser page cannot read
it and something server-side has to. That is the app's entire reason for being; if
NDW ever adds `Access-Control-Allow-Origin: *`, a static page could replace it.
Asking their service desk (mail@servicedeskndw.nu) for that header is a legitimate
request given the AFIR mandate, and worth doing.

## Layout

```
app.py               Flask: routes, background poller, config from env
ndw.py               DOT-NL client — TTL cache, stale fallback, bbox validation
store.py             SQLite history, one row per polled minute
templates/index.html the dashboard (no build step, no CDN, inline CSS/JS)
seed_demo.py         synthetic history, for looking at charts before data accrues
test_app.py          14 tests, no network (NDW_FIXTURE stands in)
fixture.json         a real recorded NDW response
archive/             the browser-only attempts that preceded this — see below
docs/investigation.md how each data source was tested and what it returned
```

Configuration is environment-only and documented in `README.md` and the `app.py`
docstring. Nothing is hardcoded except the NDW base URL.

## Conventions

- **uv** for packaging and environments — `uv sync`, `uv run`. Never pip/venv.
- **ruff** clean and formatted (88 cols) before anything is called done:
  `uv run ruff check . && uv run ruff format --check .`
- **Each functional change is its own git commit**, not one combined commit.
- The repo has no git history yet — this is a drop of files, `git init` when ready.
- Docker + GHCR multi-arch publish mirrors the `zeildashboard` setup, so the image
  runs on the Raspberry Pi k3s nodes. `VERSION` + run number is the tag.
- `jq` over Python one-liners for JSON in shell.

## State: verified vs not

Verified in the building session:
- 14 tests green; ruff clean; a real gunicorn run driven through a headless browser
  (hero count, socket pips, 96 bucketed history bars from 288 observations, the
  12h/48h/7d switch, table view, the by-hour panel appearing only past 24h of span).
- The NDW response shape, from a live call and from Jan's own `curl`.

**Not verified: the live HTTP call from the app itself.** The building container's
egress was restricted to package registries and GitHub, so every test ran against
`fixture.json` via `NDW_FIXTURE`. First real run on a machine that can reach NDW is
the moment that path is exercised. `/healthz` reports it honestly.

## Known data quirks

- `last_updated` is when the operator last reported a **change**, not when we asked.
  It sat unchanged for 78 minutes across two observations on 2 Sep 2026 — that is
  not necessarily staleness, and the dashboard deliberately shows it as the
  operator's timestamp rather than disguising it as freshness.
- Availability is grouped by power level, not per socket. "3 of 5 free at 7.4 kW" is
  the finest grain available; individual sockets are not identified in this feed.
- NDW and ChargeFinder disagreed on the 22 kW socket at the same moment (NDW: free,
  ChargeFinder: charging), and ChargeFinder had been blind to two sockets for days.
  Which is right is an open question that accumulated history should settle.

## Next steps, in rough order of value

1. **Run it against the real API** and watch `/healthz`.
2. **Tariffs.** Each group carries `tariff_ids` (e.g. `417371606`) that resolve
   against `https://opendata.ndw.nu/charging_point_tariffs_ocpi.json.gz`. That gives
   real prices — something no earlier approach produced. Biggest single win.
3. **Widen the bbox** to include the Allego chargers on Tarthorst; the station
   picker already handles multiple stations.
4. Deploy to k3s (volume at `/data`, else history dies with the container).
5. Ask NDW for a CORS header — a long shot that would obsolete the server.

## archive/

The browser-only attempts, kept because they still work and because they document
why the server exists. All of these read **ChargeFinder**, not NDW.

- `chargefinder.html` — single self-contained page: viewer, localStorage history,
  and it hands you the grabber bookmarklet and userscript from inside itself.
- `chargefinder-bookmarklet.txt` — the grabber, minified, as a bookmark URL.
  Verified working on the live station page.
- `chargefinder-grabber.js` — readable source of that bookmarklet.
- `chargefinder.user.js` — Tampermonkey bridge (`GM_xmlhttpRequest` carries no
  `Origin`, so the API answers). Never verified — no extension available to test.
- `chargefinder_proxy.py` — stdlib local proxy, if a server-side hop is wanted.
- `chargefinder-akkermaalsbos.html` — first attempt, with a baked-in snapshot.

ChargeFinder's API decrypts with an **AES-GCM key hardcoded in their frontend
bundle** (`/js/app.js`, next to `crypto.subtle.importKey`), and 403s any foreign
`Origin`. It gives per-socket detail and "available since" durations that NDW does
not — but it can break on any deploy of theirs. NDW is the better foundation; this
is kept for the per-socket angle only.
