# WUR charger dashboard — project brief

Live availability of the public EV charge points at **Akkermaalsbos 2, Wageningen**
(WUR Business & Science Park), plus whatever else falls in the configured bounding
box. Flask app, deployed as a container.

This file is the handover from the session that built it. The investigation trail
is in `docs/investigation.md`; read it before proposing a different data source,
because most of the obvious alternatives have already been tested and ruled out.

## The station

The one the dashboard was built for. Three more fall inside the default bounding
box: two Qwello points at Bornsesteeg 4 (`NL-QWC-631dc33b…`, `NL-QWC-8b634978…`,
2 sockets each at 17.25 kW) and 50five's Droevendaalsesteeg 1 (`NL-LMS-91106101`,
6 × 7.4 kW).

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
test_app.py          15 tests, no network (NDW_FIXTURE stands in)
fixture.json         a real recorded NDW response
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
- 15 tests green; ruff clean; a real gunicorn run driven through a headless browser
  (hero count, socket pips, 96 bucketed history bars from 288 observations, the
  12h/48h/7d switch, table view, the by-hour panel appearing only past 24h of span).
- The NDW response shape, from a live call and from Jan's own `curl`.

**The live HTTP call is now verified too** (2 Sep 2026). The building container's
egress was restricted, so everything up to that point ran against `fixture.json`;
the first run from an unrestricted machine exercised the real path end to end —
`/api/current`, `/api/history`, `/healthz` and the dashboard all served live data,
with `/healthz` reporting `fixture: false, last_error: null`.

That run found one defect, since fixed: NDW publishes an AAAA record that
blackholes, which cost 15s on every cache miss. See `docs/investigation.md` §7.

## Known data quirks

- `last_updated` is when the operator last reported a **change**, not when we asked.
  It sat unchanged for 78 minutes across two observations on 2 Sep 2026 — that is
  not necessarily staleness, and the dashboard deliberately shows it as the
  operator's timestamp rather than disguising it as freshness. The live run later
  the same day saw it five minutes fresh, which confirms it does move.
- One site can arrive as several features: operators register each post
  separately. Same-operator stations within `NDW_MERGE_METRES` (default 10) are
  folded into one, with a `members` list naming the originals. Different
  operators are deliberately never merged — see `docs/investigation.md` §9.
- The **bbox endpoint** groups availability by power level, not per socket — "3 of
  5 free at 7.4 kW" is its finest grain. The **bulk OCPI file** does identify
  individual sockets (`evse_id`, `physical_reference`, per-socket status), which
  is not what this brief originally claimed. See `docs/investigation.md` §9.
- "Available" counts only `AVAILABLE`. Sockets reporting `UNKNOWN` are lumped in
  with the occupied ones, so "2 of 6 free" can mean 2 free, 0 occupied and 4
  unknown — 50five's sockets sit at UNKNOWN for days at a time.
- NDW and ChargeFinder once disagreed on the 22 kW socket, and ChargeFinder had
  been blind to two sockets for days. **Settled:** neither is wrong — the operator
  stops reporting individual sockets for days, and the two sources merely present
  that silence differently. See `docs/investigation.md` §9.

## Next steps, in rough order of value

1. **Tariffs.** Each group carries `tariff_ids` (e.g. `417371606`) that resolve
   against `https://opendata.ndw.nu/charging_point_tariffs_ocpi.json.gz`. That gives
   real prices — something no earlier approach produced. Biggest single win.
2. **Widen the bbox further** — the default now covers the Business & Science
   Park (Akkermaalsbos 2, Bornsesteeg 4 ×2, Droevendaalsesteeg 1); the Allego
   chargers on Tarthorst are still outside it.
3. Deploy to k3s (volume at `/data`, else history dies with the container).
4. Mail mail@servicedeskndw.nu about two things: the missing CORS header (a long
   shot that would obsolete this server) and the blackholed AAAA record on
   `dotnl.ndw.nu`, which is a straightforward bug report.

## archive/ — removed

The browser-only ChargeFinder attempts (a self-contained viewer page, a grabber
bookmarklet and userscript, and a stdlib proxy) lived here until they were
deleted. They were kept for one reason: per-socket detail that NDW was believed
not to publish. NDW's bulk OCPI file does publish it, with EVSE ids matching
ChargeFinder's exactly (`docs/investigation.md` §9), so the reason expired.

Only ChargeFinder's "available since / charging since" durations were ever
exclusive to it, and they came at the price of an AES-GCM key lifted from their
frontend bundle and an API that 403s any foreign `Origin` — something that could
break on any deploy of theirs.

`git log -- archive/` recovers the files if that changes.
