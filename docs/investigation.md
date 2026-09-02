# How the data source was chosen

A record of what was tested and what it returned, so nobody repeats it. Everything
below was run on 2 September 2026.

## The original ask

Parse `https://chargefinder.com/nl/laadpaal-wageningen-akkermaalsbos-2-wageningen/djx7rq`
into a self-contained browser page.

## 1. ChargeFinder — works, but fragile and origin-locked

The page is a Vue SPA; its HTML contains no data. Two endpoints supply it:

- `https://api.chargefinder.com/station/<slug>` — static station record
- `https://api.chargefinder.com/status/<realtimeId>` — live per-socket status

Both return `{i, e, a}`: AES-GCM over zlib-deflated JSON. Key, IV and ciphertext:

- key = UTF-8 bytes of `9ac6af64f912e44291c7989bb7da774a`, taken from their
  `/js/app.js` bundle beside `crypto.subtle.importKey`
- iv = hex-decoded `i`; ciphertext+tag = hex-decoded `e + a`
- plaintext starts `0x78` (zlib) → inflate → JSON

Decrypted, it gives per-socket identity (`NL*505*E1234792938*1`), status codes
(0 unknown, 2 available, 3 charging) and "Available since / Charging since" text.

**The wall:** the API returns `403 {"message":"Forbidden"}` to any request whose
`Origin` is not chargefinder.com. Confirmed from a sandboxed iframe (origin `null`,
what a `file://` page gets). Note CORS itself is permissive there — the 403 body was
readable cross-origin — so it is an application allowlist, not a CORS failure.

Workable escape hatches, all of which were built (see `archive/`): bookmarklet on
their own page, userscript via `GM_xmlhttpRequest`, or a server-side relay.

## 2. The operator's own platform — authenticated, account-scoped

50five runs on **EVC-Net** (Last Mile Solutions). Its client is a PHP portal:
`POST /Login/Login` → `PHPSESSID` cookie → `POST /api/ajax`, on hosts like
`50five-snl.evc-net.com` (also `-sbelux`, `-sde`, `-suk`). Confirmed by reading the
Home Assistant integration `Platzii/homeassistant-evcnet`.

Everything there is scoped to charge points on *your* account, so it cannot answer
for a campus charger you do not manage. Their consumer app requires a login too. No
public map behind it.

## 3. Shell Recharge roaming feed — dead

`ui-map.shellrecharge.com/api/map/v2/locations/search/{id}` was the obvious
unauthenticated roaming source (`cyberjunky/python-shellrecharge`). The host no
longer resolves. Shell sold ~100,000 private charge points to **50five** in December
2024 and wound the service down — the successor of the feed is the operator itself.

## 4. Open Charge Map — CORS-friendly, needs a key, static

`api.openchargemap.io` is readable cross-origin (a 403 body came back fine from a
foreign origin), but requires a free API key and carries no real-time availability.
Fine for metadata, not for this.

## 5. TomTom EV Charging Stations Availability — licensed, coarse

`https://api.tomtom.com/search/2/chargingAvailability.json?key=…&chargingAvailability=<id>`,
where the id comes from the `dataSources` block of a TomTom POI search. Free
developer key required. Returns counts per connector type and power level. Its Dutch
data comes from the operator via Eco-Movement — the same underlying source as NDW,
through a commercial intermediary.

## 6. NDW DOT-NL — chosen

The Dutch national access point, mandated by AFIR (EU 2023/1804): static and dynamic
charge point data, free of charge, no key, 39 CPOs connected, dynamic data updated
within one minute of a change.

```
GET https://dotnl.ndw.nu/api/rest/geojson/dynamic-road-status/charge-point-data/v1/features?bbox=5.6580,51.9810,5.6620,51.9840
```

returns exactly our station:

```json
{"type":"Feature","id":"NL-LMS-91107050",
 "geometry":{"type":"Point","coordinates":[5.660022,51.982319]},
 "properties":{"address":"Akkermaalsbos 2","last_updated":"2026-09-02T14:03:30Z",
   "cpo_id":"LMS","operator_name":"50five",
   "availabilities":[
     {"available":3,"total":5,"power_max":7360.0,"power_type":"AC1","tariff_ids":["417371606"]},
     {"available":1,"total":1,"power_max":22080.0,"power_type":"AC3","tariff_ids":["417371606"]}]}}
```

### The CORS tests, in order

1. Null-origin fetch (sandboxed iframe) → `TypeError: Failed to fetch`.
2. Same test against `api.github.com` as a control → 200. So the harness was sound
   and the failure was NDW's.
3. `curl` with no `Origin` → 200, and the response headers carry no
   `access-control-allow-origin`.
4. `curl -H 'Origin: https://example.org'` → empty. Nothing echoed for a real origin.
5. `curl -X OPTIONS` preflight with `Access-Control-Request-Method: GET` → empty.

Conclusion: no CORS for any origin, no preflight support. A browser page cannot read
it; a server can. Hence this app.

`opendata.ndw.nu` (the bulk gzipped OCPI files) behaves the same way.

## 7. Running it live — verified, and one defect found

First run against the real API from a machine with unrestricted egress, on
2 September 2026. The app's own client path works: `/api/current`, `/api/history`,
`/healthz` and the dashboard all served real data (`2 of 6 free`, `last_updated`
five minutes old), and `/healthz` reported `fixture: false, last_error: null`.

Incidentally this settles one of the open questions: `last_updated` does move. The
78-minute freeze recorded earlier was the operator not reporting a *change*, not a
dead feed.

### The 15-second fetch

Every cache-miss fetch took 15.08s, while `curl` to the same URL took 0.25s.

Root cause: `dotnl.ndw.nu` (CNAME `rt.ndw.nu`) publishes both an A record,
`128.251.236.183`, and an AAAA record, `2603:1020:203:14::74`. **The IPv6 address
silently drops packets on 443** — the connect never completes and never refuses.
`getaddrinfo` returns it first, per RFC 6724, and `socket.create_connection` —
which urllib3 and therefore `requests` sit on — tries each address in turn
applying the full timeout to *each* one. So the whole budget was spent on the
dead address before IPv4 was tried, which then answered in ~50ms.

`curl` escapes this by implementing Happy Eyeballs (RFC 8305), racing both
families with a 200ms head start. Python has no equivalent in the stdlib.

Evidence:

- Total time tracked the timeout parameter exactly — `timeout=3.0` → 3.06s,
  `timeout=8.0` → 8.05s, `timeout=15.0` → 15.08s.
- `curl -6` → connect never completes; `curl -4` → 200 in 0.035s.
- `nc -6 -z -w 5 2603:1020:203:14::74 443` → timeout.
- IPv6 from the same machine to `ipv6.google.com`, `api.github.com` and
  `cloudflare.com` all connect in ~27ms, so local IPv6 is healthy. The fault is
  NDW's, and any IPv6-capable host will hit it — including the Pi k3s nodes.

Fixed by splitting the scalar timeout into `(CONNECT_TIMEOUT, READ_TIMEOUT)` =
`(3.05, 15.0)` in `ndw.py`. Because the connect timeout applies per address, the
dead attempt is capped at ~3s rather than 15, IPv6 keeps working if NDW ever fixes
the record, and no IPv4 pinning is baked in. Live cold fetch went 15.08s → 3.11s,
reproducible across runs.

Pinning `AF_INET` through a `requests` adapter would remove the remaining 3s
entirely, at the cost of disabling IPv6 permanently. Not done — the record is
NDW's bug to fix, and worth raising with mail@servicedeskndw.nu alongside the
CORS ask.

## Sources

- <https://docs.ndw.nu/data-uitwisseling/interface-beschrijvingen/dafne-api/>
- <https://docs.ndw.nu/en/data-uitwisseling/interface-beschrijvingen/dafne-api/dafne_api_consumer_pull/>
- <https://www.ndw.nu/producten-en-diensten/dataportalen/dot-nl>
- <https://docs.ndw.nu/faq/DOT-NL/>
- <https://github.com/Platzii/homeassistant-evcnet>
- <https://github.com/cyberjunky/python-shellrecharge>
- <https://www.fleet-mobility.nl/fleet-mobility/internationaal/2024/12/50five-neemt-private-laadpalen-shell-recharge-solutions-over/>
- <https://docs.tomtom.com/ev-charging-stations-availability-api/documentation/ev-charging-stations-availability-api/ev-charging-stations-availability>
