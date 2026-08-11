#!/usr/bin/env python3
"""
BACKFILL hr_events onto the graded archive (JOB 2, 2026-08-11).

Every archived homer says `actual_hr: 1` and nothing about the ball. The
grader now records launch speed / angle / distance going forward
(live_results_tracker.get_player_batting_line), but the 1,883 homers already
on disk carry none of it -- so "line drive HR vs moonshot" is unanswerable
for the whole season to date.

13,698 of 13,714 rows carry game_pk and the league still serves finished
games, so this walks the archive and attaches the same hr_events shape the
grader now writes.

SAFE BY CONSTRUCTION:
  · additive -- only ever ADDS an hr_events key, never edits an existing one
  · idempotent -- a row that already has hr_events is skipped, so re-running
    costs API calls and changes nothing
  · shape-tolerant -- the archive has four schemas (bare list, graded_slots,
    results, rows); rows_of() handles all of them, same as bots/archive.py
  · writes atomically via a temp file, so a crash mid-write cannot truncate
    a night that took a season to accumulate
  · --dry-run reports exactly what would change and writes nothing

USAGE
  python3 bots/backfill_hr_events.py --dry-run          # look first
  python3 bots/backfill_hr_events.py --limit 3          # prove it on 3 nights
  python3 bots/backfill_hr_events.py                    # the real run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
FEED = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"
# Same whitelist the site's fetchHrContext uses, plus what the row shape needs.
FIELDS = ("liveData,plays,allPlays,result,event,eventType,about,inning,matchup,"
          "batter,pitcher,id,fullName,playEvents,details,type,description,code,"
          "hitData,launchSpeed,launchAngle,totalDistance")
TIMEOUT = 30


def num(v):
    try:
        if v in (None, "", "--"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def as_int(v):
    f = num(v)
    return int(f) if f is not None else None


def rows_of(payload: Any) -> list[dict]:
    """The graded rows, whatever shape the file is. Mirrors bots/archive.py."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


def homers_from_feed(pk: int, session: requests.Session) -> dict[int, list[dict]]:
    """{batter_id: [hr_event, ...]} for one game. {} if the feed won't answer."""
    try:
        r = session.get(FEED.format(pk=pk), params={"fields": FIELDS}, timeout=TIMEOUT)
        if not r.ok:
            return {}
        plays = ((r.json().get("liveData") or {}).get("plays") or {}).get("allPlays") or []
    except Exception:
        return {}

    out: dict[int, list[dict]] = {}
    for pl in plays:
        res = pl.get("result") or {}
        if str(res.get("eventType") or res.get("event") or "").lower() not in ("home_run", "home run"):
            continue
        pid = as_int(((pl.get("matchup") or {}).get("batter") or {}).get("id"))
        if not pid:
            continue
        # hitData sits on the last playEvent of the at-bat; the same event
        # carries the pitch that was hit.
        hit, evt = {}, {}
        for e in reversed(pl.get("playEvents") or []):
            if e.get("hitData"):
                hit, evt = e["hitData"], e
                break
        det = (evt.get("details") or {}).get("type") or {}
        out.setdefault(pid, []).append({
            "inning": as_int((pl.get("about") or {}).get("inning")),
            "pitcher_id": as_int(((pl.get("matchup") or {}).get("pitcher") or {}).get("id")),
            "pitcher_name": ((pl.get("matchup") or {}).get("pitcher") or {}).get("fullName", ""),
            "launch_speed": num(hit.get("launchSpeed")),
            "launch_angle": num(hit.get("launchAngle")),
            "total_distance": num(hit.get("totalDistance")),
            "pitch_type": det.get("description") or det.get("code") or "",
            "event": res.get("event", ""),
        })
    return out


def do_night(path: Path, session: requests.Session, dry: bool, pause: float) -> tuple[int, int, str]:
    """Returns (rows_filled, rows_wanted, note)."""
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        return 0, 0, f"unreadable ({e.__class__.__name__})"

    rows = rows_of(payload)
    if not rows:
        return 0, 0, "no rows"

    # Only homer rows that don't already have the key and do have a game_pk.
    want = [r for r in rows
            if as_int(r.get("actual_hr") or r.get("got_hr")) and not r.get("hr_events")
            and as_int(r.get("game_pk"))]
    if not want:
        have = sum(1 for r in rows if r.get("hr_events"))
        return 0, 0, f"nothing to do ({have} rows already carry hr_events)" if have else "no homers"

    by_pk: dict[int, dict[int, list[dict]]] = {}
    for pk in sorted({as_int(r.get("game_pk")) for r in want}):
        by_pk[pk] = homers_from_feed(pk, session)
        if pause:
            time.sleep(pause)

    filled = 0
    for r in want:
        ev = by_pk.get(as_int(r.get("game_pk")), {}).get(as_int(r.get("player_id")))
        if ev:
            r["hr_events"] = ev
            filled += 1

    if filled and not dry:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)          # atomic; never a half-written archive

    miss = len(want) - filled
    return filled, len(want), f"{filled}/{len(want)} filled" + (f", {miss} had no tracking data" if miss else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--archive-dir", type=Path, default=Path.home() / "Desktop" / "results")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--pause", type=float, default=0.2, help="seconds between game feeds (be kind to the API)")
    a = ap.parse_args()

    d = a.archive_dir.expanduser()
    if not d.is_dir():
        print(f"archive dir not found: {d}", file=sys.stderr)
        return 1

    files = sorted((p for p in d.glob("graded_results_*.json") if DATE_RE.search(p.name)), reverse=True)
    if a.limit:
        files = files[:a.limit]
    if not files:
        print(f"no graded_results_*.json in {d}", file=sys.stderr)
        return 1

    print(f"{len(files)} nights in {d}" + ("   [DRY RUN — nothing will be written]" if a.dry_run else ""))
    tot_f = tot_w = 0
    with requests.Session() as s:
        for p in files:
            f, w, note = do_night(p, s, a.dry_run, a.pause)
            tot_f += f
            tot_w += w
            print(f"  {'*' if f else ' '} {DATE_RE.search(p.name).group(1)}  {note}")

    print(f"\n{tot_f} of {tot_w} homer rows filled across {len(files)} nights."
          + ("  (dry run — nothing written)" if a.dry_run else ""))
    if tot_w and tot_f < tot_w:
        print("Rows left empty are homers Statcast never tracked; they stay absent rather than zero.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
