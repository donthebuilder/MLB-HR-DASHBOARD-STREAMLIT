#!/usr/bin/env python3
"""OFFSEASON GUARD (2026-09-27, BOT-WORKFLOWS-CLEANUP).

After the World Series the MLB workflows would keep firing on empty days.
Rather than delete them (they come back in March on their own), each one asks
this first: is there an MLB game -- regular season OR postseason -- from
BACK days ago to AHEAD days ahead? The look-back keeps grading alive for the
nights after the last game (final grades, the earlier-slates sweep).

Writes `active=true|false` to $GITHUB_OUTPUT. Fails OPEN: if the schedule
can't be read, active=true -- a missed guard costs one empty run, a wrong
"off" would cost a real night.

    python3 bots/mlb_season_guard.py [--back 3] [--ahead 3]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--back", type=int, default=3)
    ap.add_argument("--ahead", type=int, default=3)
    a = ap.parse_args()
    today = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).date()   # Phoenix, like every MLB workflow
    start, end = today - dt.timedelta(days=a.back), today + dt.timedelta(days=a.ahead)
    url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={start}&endDate={end}"
           "&gameType=R,F,D,L,W")   # regular season + every postseason round
    active, why = True, "schedule unreadable -- failing open"
    # OFF DAYS ARE A SKIP, NOT A FAILURE (2026-10-04, ops audit: 25 of 71 Today
    # runs went red, nearly all on days with no game -- the slate guard in
    # make_slim refuses an empty slate, as it should). The same response says
    # whether TODAY and TOMORROW each have a game; the Today / Tomorrow jobs
    # skip on a day with none. "unknown" (unreadable) means run: fail open.
    today_games = tomorrow_games = "unknown"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            body = json.load(r)
        games = int(body.get("totalGames") or 0)
        active = games > 0
        per_day = {d.get("date"): len(d.get("games") or []) for d in body.get("dates") or []}
        today_games = str(per_day.get(str(today), 0))
        tomorrow_games = str(per_day.get(str(today + dt.timedelta(days=1)), 0))
        why = f"{games} MLB game(s) {start}..{end}; today {today_games}, tomorrow {tomorrow_games}"
    except Exception as e:  # noqa: BLE001 -- fail open on anything
        why = f"{why} ({type(e).__name__})"
    print(f"season guard: active={str(active).lower()} -- {why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"active={str(active).lower()}\n")
            fh.write(f"today_games={today_games}\n")
            fh.write(f"tomorrow_games={tomorrow_games}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
