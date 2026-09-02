"""Fill the history with plausible demo observations.

Useful for looking at the charts before real data has accrued. It writes only
into the SQLite history, so delete the database to start clean.

    uv run python seed_demo.py --days 3
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import time
from pathlib import Path

from store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--every", type=int, default=600, help="seconds between rows")
    parser.add_argument("--db", default="data/history.sqlite3")
    parser.add_argument("--fixture", default="fixture.json")
    args = parser.parse_args()

    template = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    store = Store(Path(args.db))
    now = time.time()
    written = 0

    for offset in range(int(args.days * 86400), 0, -args.every):
        when = now - offset
        hour = time.localtime(when).tm_hour
        # Busiest mid-morning and late afternoon; quiet overnight.
        busy = 0.5 + 0.5 * math.sin((hour - 8) / 24 * 2 * math.pi)
        collection = copy.deepcopy(template)
        for group in collection["features"][0]["properties"]["availabilities"]:
            total = group["total"]
            occupied = min(total, round(total * busy * random.uniform(0.55, 1.0)))
            group["available"] = total - occupied
        collection["features"][0]["properties"]["last_updated"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)
        )
        written += bool(store.add(when, collection))

    print(f"wrote {written} observations to {args.db}")


if __name__ == "__main__":
    main()
