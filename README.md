# Charge point availability dashboard

A small Flask app showing live availability of public EV charge points, from
**NDW DOT-NL** — the Dutch national access point, published free of charge under
AFIR (EU 2023/1804). No API key, no scraping, no reverse-engineered payloads.

It exists because that API sends **no CORS headers**, so a browser page cannot
call it directly. This app fetches server-side, keeps a history in SQLite, and
serves both to a single dashboard page from its own origin.

The default bounding box covers about a square kilometre of the Wageningen
Business & Science Park — four stations across three addresses, 50five and Qwello.
Point `NDW_BBOX` anywhere in the Netherlands.

## Run it

```
uv sync
uv run flask --app app run --port 8000
```

Then open <http://localhost:8000>. To see the charts before real data has piled
up, seed some plausible history first:

```
uv run python seed_demo.py --days 3
```

## Configuration

All environment variables, all optional:

| Variable | Default | What it does |
|---|---|---|
| `NDW_BBOX` | `5.655835,51.980796,5.670791,51.989109` | `minLon,minLat,maxLon,maxLat`. The API caps a box at 1.0 degree² and 1000 features. |
| `NDW_POLL_SECONDS` | `300` | How often an observation is recorded. |
| `NDW_CACHE_SECONDS` | `60` | How long a fetched response counts as fresh. NDW updates within a minute of a change, so going below this only adds load. |
| `NDW_MERGE_METRES` | `10` | Stations of the *same* operator closer than this are shown as one site. Operators register each post separately, so one location can arrive as several features. `0` shows every registered station. |
| `NDW_DB` | `data/history.sqlite3` | History database. |
| `NDW_CACHE_DIR` | `data/cache` | On-disk response cache. |
| `NDW_RETAIN_DAYS` | `90` | History retention; `0` keeps everything. |
| `NDW_FIXTURE` | – | Read this JSON file instead of calling NDW. Offline demos and tests. |

A wider box picks up more stations and the dashboard grows a station picker —
the Allego chargers on Tarthorst are a few hundred metres further out.

## Endpoints

- `GET /` — the dashboard
- `GET /api/current` — parsed stations as they are now
- `GET /api/history?hours=48&station=<id>` — recorded observations
- `GET /healthz` — observation count, age of last successful fetch, last error

## Deploying

```
docker build -t ndw-charger-dashboard .
docker run -p 8000:8000 -v ndw-data:/data ndw-charger-dashboard
```

`.github/workflows/docker-publish.yaml` lints, tests, then publishes a
multi-arch image (amd64 + arm64) to GHCR on push to `main`, versioned from
`VERSION` plus the run number.

Mount a volume at `/data` — otherwise the history dies with the container.

One worker is the intended shape: a single poller thread and a single SQLite
writer. Scaling out still behaves, because the store ignores a minute it already
holds, but there's no reason to.

## How it behaves when NDW is down

Responses are cached on disk with a TTL. A failed fetch falls back to the last
good cached value at any age, so the dashboard shows slightly stale numbers
rather than blanking. `/healthz` reports the real state, and `last_updated` on
the page is the operator's own "last change" timestamp — not when the app last
asked — so a stale feed is visible rather than disguised.

## Development

```
uv run pytest -q          # 15 tests, no network needed
uv run ruff check .
uv run ruff format .
```

The tests run against `fixture.json`, a real recorded response.

## Data notes

- `last_updated` is when the operator last reported a *change*. It can sit
  unchanged for a long time; that's not necessarily staleness.
- Availability arrives grouped by power level, not per socket — you get "3 of 5
  free at 7.4 kW", not which specific socket. The bulk OCPI file *does* identify
  sockets; see `docs/investigation.md` §9.
- Co-located stations of one operator are merged into a single site
  (`NDW_MERGE_METRES`). Different operators are never merged — metres-apart
  Allego and Vattenfall listings may be one post registered twice, and merging
  those would double-count sockets.
- `tariff_ids` reference NDW's bulk OCPI tariff file
  (`https://opendata.ndw.nu/charging_point_tariffs_ocpi.json.gz`), which is not
  wired up here. That's the obvious next feature: real prices per station.
