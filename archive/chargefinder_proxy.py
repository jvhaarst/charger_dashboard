"""Tiny local proxy so a browser page can read the ChargeFinder API.

The public API answers 403 to any browser request whose Origin is not
chargefinder.com. A server-side request sends no Origin header at all and is
accepted, so this forwards the two endpoints the page needs and serves the page
itself from the same origin (no CORS involved).

    uv run chargefinder_proxy.py

Then open http://localhost:8787/ in a browser.
"""

from __future__ import annotations

import argparse
import http.server
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM = "https://api.chargefinder.com"
PAGE = Path(__file__).with_name("chargefinder-akkermaalsbos.html")
ALLOWED_PREFIXES = ("/station/", "/status/")
TIMEOUT_SECONDS = 15
USER_AGENT = "chargefinder-local-proxy/1.0"


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve the local page and forward /api/* to the ChargeFinder API."""

    server_version = "ChargefinderProxy/1.0"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path in ("/", "/index.html"):
            self._serve_page()
        elif self.path.startswith("/api/"):
            self._serve_api(self.path[len("/api") :])
        else:
            self.send_error(404, "Not found")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _serve_page(self) -> None:
        try:
            body = PAGE.read_bytes()
        except OSError:
            self.send_error(500, f"Cannot read {PAGE.name}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_api(self, path: str) -> None:
        if not path.startswith(ALLOWED_PREFIXES):
            self.send_error(403, "Only /station/ and /status/ are proxied")
            return
        request = urllib.request.Request(  # noqa: S310 - fixed https upstream
            UPSTREAM + path,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed https upstream
                request, timeout=TIMEOUT_SECONDS
            ) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as err:
            self.send_error(err.code, f"Upstream said {err.code}")
            return
        except (urllib.error.URLError, TimeoutError) as err:
            self.send_error(502, f"Upstream unreachable: {err}")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        print(f"Serving {PAGE.name} on http://{args.host}:{args.port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
