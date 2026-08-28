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
    # THE CONTROL (added 2026-08-28). The first run of this probe (2026-08-23)
    # compared the season leaderboard (minSwings=q) against the date-range
    # call below (minSwings=1) and read the row-count difference (202 vs 637)
    # as proof the date params work. That comparison changes TWO variables at
    # once -- minSwings AND the date scope -- so the row-count gap could come
    # entirely from minSwings=1 pulling in low-swing-count batters the season
    # call's minSwings=q (a qualified-batter threshold) excludes, regardless
    # of whether startDate/endDate did anything at all. Confirmed unresolved
    # in moonshot-verification-status-2026-08-24.md: "needs a rerun with
    # minSwings=1 and no date params... before trusting either conclusion."
    # This candidate is that control -- same minSwings=1 as the date-range
    # call below, no date restriction. If DATE RANGE returns a materially
    # different row count/set than THIS one, the date param is real. If they
    # match, minSwings alone explained the original 202-vs-637 gap and bat
    # tracking is season-to-date only -- not backfillable, capture-forward
    # only, exactly the "if it returned the same rows" branch this probe's
    # own closing guidance already describes, just against the right baseline.
    ("season leaderboard, minSwings=1 — the CONTROL for the date-range test",
     "https://baseballsavant.mlb.com/leaderboard/bat-tracking?type=batter&minSwings=1&csv=true"),
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


CONTROL_LABEL = "season leaderboard, minSwings=1 — the CONTROL for the date-range test"
DATE_RANGE_LABEL = "DATE RANGE — the question that matters"


def main() -> int:
    print("=" * 78)
    print("BAT TRACKING PROBE — reading, not assuming")
    print("=" * 78)
    header_seen = None
    id_sets: dict[str, frozenset] = {}
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
        data_lines = text.splitlines()[1:]
        print(f"   CSV with {len(cols)} columns, {len(data_lines)} data rows")
        print("   HEADER, VERBATIM:")
        for i in range(0, len(cols), 6):
            print("      " + ", ".join(cols[i:i + 6]))
        hits = {w: [c for c in cols if w in c.lower().replace("_", " ")] for w in WANTED}
        print("   what the highlights are asking for:")
        for w, found in hits.items():
            mark = "✓" if found else "✗"
            print(f"      {mark} {w:14} {found if found else '(no column matches)'}")
        for r in data_lines[:2]:
            vals = r.split(",")
            print("   sample: " + ", ".join(f"{c}={v}" for c, v in list(zip(cols, vals))[:8]))
        if header_seen is None:
            header_seen = cols
        elif cols != header_seen:
            print("   ⚠ header DIFFERS from the first responding endpoint — note which one you fetch")
        # First column is the id/name key on every candidate this endpoint has
        # returned so far (id, name, ...) -- capture it for the CONTROL and
        # DATE RANGE candidates specifically so the verdict below is a real
        # set comparison, not an eyeballed row count (a matched row count
        # could still hide a fully different set of players; an identical
        # SET is the only thing that actually proves the date param did
        # nothing).
        if label in (CONTROL_LABEL, DATE_RANGE_LABEL) and cols:
            id_sets[label] = frozenset(r.split(",", 1)[0] for r in data_lines if r)
    print("\n" + "=" * 78)
    print("THE VERDICT — computed, not eyeballed")
    print("=" * 78)
    if CONTROL_LABEL in id_sets and DATE_RANGE_LABEL in id_sets:
        control, date_range = id_sets[CONTROL_LABEL], id_sets[DATE_RANGE_LABEL]
        only_control = control - date_range
        only_date_range = date_range - control
        print(f"   CONTROL (minSwings=1, no date):     {len(control)} players")
        print(f"   DATE RANGE (minSwings=1, May 2026):  {len(date_range)} players")
        print(f"   only in CONTROL:    {len(only_control)}")
        print(f"   only in DATE RANGE: {len(only_date_range)}")
        if control == date_range:
            print("""
   IDENTICAL PLAYER SETS with minSwings matched between both calls.
   The date parameter did NOTHING -- bat tracking is season-to-date only.
   It CANNOT be backfilled. The four terms can only be captured one night
   at a time from here forward: a nightly snapshot into the slate and the
   archive, with the highlight rules unproven until ~30 nights accumulate.""")
        else:
            print("""
   DIFFERENT player sets with minSwings matched between both calls -- the
   date parameter is real. These metrics CAN be rebuilt as-of any past date,
   and every highlight rule that uses them becomes backtestable over the
   whole season. (The original 2026-08-23 run of this probe also saw a row-
   count difference, but with minSwings unmatched between the two calls --
   this is the same conclusion reached the right way, matched control
   included.)""")
    else:
        print("""
   Could not compute the verdict -- the CONTROL and/or DATE RANGE candidate
   above did not return usable CSV. Read their individual sections above for
   why (status code, non-CSV body, etc.) before re-running.""")
    print("""
Either way: copy the header row above into the fetcher's docstring before
writing it, the way bots/savant_feeds.py does.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
