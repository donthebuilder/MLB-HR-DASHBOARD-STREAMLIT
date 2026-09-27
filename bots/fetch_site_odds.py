#!/usr/bin/env python3
"""Our own prices into the True Price archive (2026-09-27).

The props providers odds_fetch.py uses stopped carrying player props on
2026-09-14 (odds_status.json: "Player props require a Business plan"), so
odds_history.json -- the True Price page -- froze there. The site now reads
SportsGameOdds itself and serves any past MLB date's pregame prices in the
EXACT slim shape odds_history.py already reads:

    GET https://dashnetwork.vercel.app/api/odds/past?date=YYYY-MM-DD
    -> {"date": ..., "source": ..., "rows": {mlbam_id: {market: [line, price, implied]}}}

This writes each missing date as public/data/current/odds_<date>.json, which
publish_data.sh already archives (ODDS_GLOB), so each date is fetched once and
kept like every snapshot before it. A date already on the branch is never
re-fetched or overwritten. Nothing is fetched for today: a slate still being
played is not an archive.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import requests

SITE = "https://dashnetwork.vercel.app"
FIRST = dt.date(2026, 9, 26)          # the first day the site holds prices
OUT = Path("public/data/current")
HAVE_DIRS = [Path("/tmp/histout/public/data/current"), OUT, Path("public/data")]


def main() -> int:
    today = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).date()   # Phoenix, like the rest of CI
    OUT.mkdir(parents=True, exist_ok=True)
    d = FIRST
    wrote = 0
    while d < today:
        name = f"odds_{d.isoformat()}.json"
        if any((p / name).exists() for p in HAVE_DIRS):
            d += dt.timedelta(days=1)
            continue
        try:
            r = requests.get(f"{SITE}/api/odds/past", params={"date": d.isoformat()}, timeout=60)
        except Exception as e:
            print(f"::warning::site odds {d}: {type(e).__name__}: {e}", file=sys.stderr)
            d += dt.timedelta(days=1)
            continue
        if r.status_code != 200:
            print(f"::warning::site odds {d}: HTTP {r.status_code}", file=sys.stderr)
        else:
            body = r.json()
            rows = body.get("rows") or {}
            if rows:
                (OUT / name).write_text(json.dumps(body, separators=(",", ":")))
                wrote += 1
                print(f"site odds {d}: {len(rows)} players -> {OUT / name}")
            else:
                print(f"site odds {d}: no prices (no slate, or none priced)")
        d += dt.timedelta(days=1)
    print(f"site odds: {wrote} new date file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
