#!/usr/bin/env python3
"""
🏏 BAT TRACKING PROBE — do Bat Spd, Blast%, SqUp% and Comp% exist here, and can
they be asked for AS OF A DATE?

WHY A PROBE AND NOT A FETCHER
=============================
Donovan's HR Matchups highlights lean on four metrics — Bat Spd, Comp%, Blast%,
SqUp% — that could not be backtested at all, because none of them is in
StatsAPI's game feed and none is on the data branch (checked: only
`l25pa_avg_bat_speed` exists anywhere in the slate, and Blast/SqUp/Comp appear
in no file). They are Statcast BAT-TRACKING metrics, which live on Savant's own
leaderboard rather than in the play feed.

Claude's sandbox cannot reach baseballsavant.mlb.com (CONNECT tunnel blocked,
re-verified 2026-08-23), so the column names could only be GUESSED from here —
and this repo already has the receipt for what guessing an endpoint's shape
costs: the odds pipeline needed "eight round trips of failure" to learn one
provider's response. bots/savant_feeds.py states the rule outright: every column
name was read off the live endpoint before a line was written.

So this writes nothing, publishes no model term, and changes no score. It asks
the endpoint and PRINTS WHAT CAME BACK.

THE TWO QUESTIONS IT ANSWERS
----------------------------
1. WHICH COLUMNS ARE THERE. The full header row, verbatim, so a fetcher can be
   written against real names instead of plausible ones.

2. CAN IT BE ASKED FOR A DATE RANGE — and this is the one that decides how much
   is possible. Bat tracking is served as a season-to-date leaderboard. If the
   endpoint honours a start/end date, every one of these metrics can be rebuilt
   as-of any past date and Donovan's highlight rules become fully backtestable
   over the whole season. If it does not, they can only ever be captured going
   FORWARD, one night at a time, and the four terms stay untested until enough
   nights accumulate. Same shape of answer as the odds probe: it changes what is
   worth building next.

It tries several candidate URLs because the leaderboard's parameter names are
not something to assert from memory. Each is reported with its status and the
first line of what it returned — a 404 is an answer too.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 30
UA = {"User-Agent": "Mozilla/5.0 (moonshot-probe)"}

# The metrics the highlights need, in the words the site uses for them. The
# probe reports which of these it can find a plausible column for — WITHOUT
# committing to a mapping, because that is the fetcher's job after a human has
# read the header.
WANTED = ["bat speed", "swing length", "blast", "squared", "competitive",
          "fast swing", "swings", "contact"]

CANDIDATES = [
    ("season leaderboard, csv",
     "https://baseballsavant.mlb.com/leaderboard/bat-tracking?type=batter&minSwings=q&csv=true"),
    ("season leaderboard, explicit season",
     "https://baseballsavant.mlb.com/leaderboard/bat-tracking?type=batter&year=2026&minSwings=q&csv=true"),
    ("DATE RANGE — the question that matters",
     "https://baseballsavant.mlb.com/leaderboard/bat-tracking?type=batter&minSwings=1"
     "&startDate=2026-05-01&endDate=2026-05-31&csv=true"),
    ("date range, alternate parameter spelling",
     "https://baseballsavant.mlb.com/leaderboard/bat-tracking?type=batter&minSwings=1"
     "&start_date=2026-05-01&end_date=2026-05-31&csv=true"),
    ("swing-take leaderboard, in case bat tracking moved",
     "https://baseballsavant.mlb.com/leaderboard/bat-tracking-swing-path?type=batter&csv=true"),
]


def fetch(url: str):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            return r.status, body
    except urllib.error.HTTPError as ex:
        return ex.code, (ex.read() or b"")[:400]
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}".encode()


def main() -> int:
    print("=" * 78)
    print("BAT TRACKING PROBE — reading, not assuming")
    print("=" * 78)
    header_seen = None
    for label, url in CANDIDATES:
        print(f"\n── {label}")
        print(f"   {url}")
        status, body = fetch(url)
        print(f"   status: {status}   bytes: {len(body)}")
        if not body:
            continue
        text = body.decode("utf-8", "replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        looks_csv = ("," in first and "<" not in first[:40])
        if not looks_csv:
            print(f"   NOT CSV — first 200 chars: {text[:200]!r}")
            continue
        cols = [c.strip().strip('"') for c in first.split(",")]
        print(f"   CSV with {len(cols)} columns, {len(text.splitlines()) - 1} data rows")
        print("   HEADER, VERBATIM:")
        for i in range(0, len(cols), 6):
            print("      " + ", ".join(cols[i:i + 6]))
        hits = {w: [c for c in cols if w in c.lower().replace("_", " ")] for w in WANTED}
        print("   what the highlights are asking for:")
        for w, found in hits.items():
            mark = "✓" if found else "✗"
            print(f"      {mark} {w:14} {found if found else '(no column matches)'}")
        rows = text.splitlines()[1:3]
        for r in rows:
            vals = r.split(",")
            print("   sample: " + ", ".join(f"{c}={v}" for c, v in list(zip(cols, vals))[:8]))
        if header_seen is None:
            header_seen = cols
        elif cols != header_seen:
            print("   ⚠ header DIFFERS from the first responding endpoint — note which one you fetch")
    print("\n" + "=" * 78)
    print("READ THIS BEFORE WRITING A FETCHER")
    print("=" * 78)
    print("""
If the DATE RANGE candidate returned a different row set than the season
leaderboard, these metrics can be rebuilt as-of any past date and every
highlight rule that uses them becomes backtestable over the whole season.

If it returned the same rows as the season call, the parameter was ignored:
bat tracking is season-to-date only, it CANNOT be backfilled, and the four
terms can only be captured one night at a time from here forward. In that case
the right move is a nightly snapshot into the slate and the archive, and the
rules stay unproven until ~30 nights accumulate.

Either way: copy the header row above into the fetcher's docstring before
writing it, the way bots/savant_feeds.py does.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
