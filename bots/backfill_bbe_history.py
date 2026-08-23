#!/usr/bin/env python3
"""
BACKFILL the batted-ball history the archive never kept (2026-08-23).

WHY THIS EXISTS
---------------
The slate publishes 424 fields a row. `slot_snapshot` keeps 55 and the graded
archive keeps 149 -- and of the 52 fields the site's own filter menu exposes,
only 22 exist on 26+ of the 28 graded nights. Every distance and exit-velocity
field the menu offers (Max EV, Avg dist, Max dist, Air %, Pull %, Sweet-spot %,
BBE) has ZERO rows in the archive. Avg EV has 13 nights. So "check these against
the history" has, until now, had no history to check.

Nothing about that is fixable going forward alone: a slate is rebuilt every
night and the old values are gone. But the raw material is not gone. Every
batted ball of the season is still in StatsAPI's game feeds, with launch speed,
launch angle, trajectory, distance and spray coordinates on it -- the same
`hitData` block `bots/backfill_hr_events.py` already walks for homers, and the
same numbers `score_hitter()` turns into `recent_barrel_rate` and friends.

This script harvests EVERY batted ball (not just homers), then rebuilds, for any
date, the exact feature set the live bot computes -- using only games played
BEFORE that date.

LEAK-FREE BY CONSTRUCTION
-------------------------
`build_features()` filters events with `game_date < as_of` before it computes
anything. There is no window, no flag and no fallback that can see the day it is
predicting. This is the property step 9 spent a month failing to get from the
live pipeline, and here it is a one-line invariant that `tests/` can assert.

DEFINITIONS ARE COPIED, NOT INVENTED
------------------------------------
Every threshold below is lifted verbatim from `bots/mlb_dashboard.py` so a
backfilled column means precisely what the live column means:

    barrel        launch_speed >= 98 and 24 <= launch_angle <= 32   (line 2858/4007)
    hard hit      launch_speed >= 95                                (line 3701)
    sweet spot    8 <= launch_angle <= 32                           (line 3702)
    ideal HR      launch_speed >= 97 and 18 <= launch_angle <= 36   (line 3705)
    fb/gb/ld/pu   hitData.trajectory, the StatsAPI name for bb_type  (line 3699)

If a threshold ever moves in the dashboard, move it here in the same commit.

USAGE
-----
    # harvest -- one JSONL per date, skips dates already on disk
    python3 bots/backfill_bbe_history.py harvest --start 2026-03-26 --end 2026-08-23

    # prove it on three days first
    python3 bots/backfill_bbe_history.py harvest --start 2026-08-01 --end 2026-08-03 --dry-run

    # build the as-of feature table (leak-free) for every date that has events
    python3 bots/backfill_bbe_history.py features --start 2026-04-15 --end 2026-08-23

Outputs, under --out-dir (default public/data/current/bbe_history):
    bbe_<YYYY-MM-DD>.jsonl        one row per batted ball
    features_<YYYY-MM-DD>.jsonl   one row per batter, as of that morning
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests

SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
FEED = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"

# Same whitelist shape backfill_hr_events.py uses, plus the batted-ball extras.
FEED_FIELDS = (
    "gameData,teams,home,away,abbreviation,players,batSide,pitchHand,code,"
    "liveData,plays,allPlays,result,event,eventType,rbi,about,inning,isTopInning,"
    "matchup,batter,pitcher,id,fullName,batSide,pitchHand,playEvents,details,"
    "type,description,code,isInPlay,hitData,launchSpeed,launchAngle,"
    "totalDistance,trajectory,hardness,coordinates,coordX,coordY"
)
TIMEOUT = 30
DEFAULT_OUT = Path("public/data/current/bbe_history")

# ── thresholds, copied from bots/mlb_dashboard.py ────────────────────────────
BARREL_EV, BARREL_LA_LO, BARREL_LA_HI = 98.0, 24.0, 32.0
HARD_HIT_EV = 95.0
SWEET_LA_LO, SWEET_LA_HI = 8.0, 32.0
IDEAL_EV, IDEAL_LA_LO, IDEAL_LA_HI = 97.0, 18.0, 36.0

# The rolling windows the live bot publishes, so a backfilled column lines up
# with the live one by name as well as by definition.
WINDOWS = (20, 25)


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


def daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    """Never truncate a finished file: write a sibling, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


# ── harvest ──────────────────────────────────────────────────────────────────

def final_game_pks(session: requests.Session, day: date) -> list[int]:
    try:
        r = session.get(
            SCHEDULE,
            params={"sportId": 1, "date": day.isoformat(),
                    "fields": "dates,games,gamePk,status,abstractGameState,detailedState"},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return []
        out = []
        for dd in (r.json().get("dates") or []):
            for g in (dd.get("games") or []):
                st = (g.get("status") or {})
                if str(st.get("abstractGameState") or "").lower() == "final":
                    pk = as_int(g.get("gamePk"))
                    if pk:
                        out.append(pk)
        return sorted(set(out))
    except Exception:
        return []


def bbe_from_feed(session: requests.Session, pk: int, day: date) -> list[dict]:
    """Every batted ball in one game. [] if the feed won't answer."""
    try:
        r = session.get(FEED.format(pk=pk), params={"fields": FEED_FIELDS}, timeout=TIMEOUT)
        if not r.ok:
            return []
        payload = r.json()
    except Exception:
        return []
    return bbe_from_payload(payload, pk, day)


def bbe_from_payload(payload: dict, pk: int, day: date) -> list[dict]:
    """The parse, split out so it can be fixture-tested without a network."""
    plays = ((payload.get("liveData") or {}).get("plays") or {}).get("allPlays") or []
    rows: list[dict] = []
    for pl in plays:
        res = pl.get("result") or {}
        mu = pl.get("matchup") or {}
        # hitData sits on the last playEvent of the at-bat -- same rule as
        # backfill_hr_events.py, which has been right about this all season.
        hit, evt = {}, {}
        for e in reversed(pl.get("playEvents") or []):
            if e.get("hitData"):
                hit, evt = e["hitData"], e
                break
        if not hit:
            continue  # strikeout, walk, or an untracked ball -- not a BBE

        ev = num(hit.get("launchSpeed"))
        la = num(hit.get("launchAngle"))
        dist = num(hit.get("totalDistance"))
        coords = hit.get("coordinates") or {}
        event = str(res.get("event") or "")
        etype = str(res.get("eventType") or "").lower()
        is_hr = etype in ("home_run", "home run")
        rows.append({
            "game_date": day.isoformat(),
            "game_pk": pk,
            "batter_id": as_int((mu.get("batter") or {}).get("id")),
            "batter_name": (mu.get("batter") or {}).get("fullName", ""),
            "pitcher_id": as_int((mu.get("pitcher") or {}).get("id")),
            "stand": ((mu.get("batSide") or {}).get("code") or ""),
            "p_throws": ((mu.get("pitchHand") or {}).get("code") or ""),
            "event": event,
            "event_type": etype,
            "launch_speed": ev,
            "launch_angle": la,
            "total_distance": dist,
            "bb_type": (hit.get("trajectory") or ""),
            "hc_x": num(coords.get("coordX")),
            "hc_y": num(coords.get("coordY")),
            "is_hr": bool(is_hr),
            "is_xbh": etype in ("double", "triple") or is_hr,
            "is_barrel": bool(ev is not None and la is not None
                              and ev >= BARREL_EV and BARREL_LA_LO <= la <= BARREL_LA_HI),
            "is_hard_hit": bool(ev is not None and ev >= HARD_HIT_EV),
            "is_sweet_spot": bool(la is not None and SWEET_LA_LO <= la <= SWEET_LA_HI),
            "is_ideal_hr": bool(ev is not None and la is not None
                                and ev >= IDEAL_EV and IDEAL_LA_LO <= la <= IDEAL_LA_HI),
            "is_350_plus": bool(dist is not None and dist >= 350),
            "is_375_plus": bool(dist is not None and dist >= 375),
            "is_400_plus": bool(dist is not None and dist >= 400),
        })
    return [r for r in rows if r["batter_id"]]


def cmd_harvest(args) -> int:
    out_dir = Path(args.out_dir)
    session = requests.Session()
    session.headers.update({"User-Agent": "moonshot-backfill/1.0"})
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    total_days = total_rows = skipped = 0

    for day in daterange(start, end):
        path = out_dir / f"bbe_{day.isoformat()}.jsonl"
        if path.exists() and not args.force:
            skipped += 1
            continue
        pks = final_game_pks(session, day)
        if not pks:
            print(f"{day}  no final games")
            continue
        rows: list[dict] = []
        for pk in pks:
            rows.extend(bbe_from_feed(session, pk, day))
            if args.pause:
                time.sleep(args.pause)
        if args.dry_run:
            print(f"{day}  {len(pks):2} games  {len(rows):4} batted balls  (dry run, nothing written)")
        else:
            atomic_write_jsonl(path, rows)
            print(f"{day}  {len(pks):2} games  {len(rows):4} batted balls -> {path.name}")
        total_days += 1
        total_rows += len(rows)
        if args.limit and total_days >= args.limit:
            break

    print(f"\nharvested {total_days} days, {total_rows} batted balls, skipped {skipped} already on disk")
    return 0


# ── features ─────────────────────────────────────────────────────────────────

def load_events(out_dir: Path, upto: date) -> dict[int, list[dict]]:
    """{batter_id: [events...]} for every game strictly BEFORE `upto`, oldest first."""
    by_batter: dict[int, list[dict]] = {}
    for path in sorted(out_dir.glob("bbe_*.jsonl")):
        try:
            d = date.fromisoformat(path.stem.replace("bbe_", ""))
        except ValueError:
            continue
        if d >= upto:            # <-- THE INVARIANT. Nothing from the day itself.
            continue
        with path.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                by_batter.setdefault(r["batter_id"], []).append(r)
    for pid in by_batter:
        by_batter[pid].sort(key=lambda r: (r["game_date"], r["game_pk"]))
    return by_batter


def rate(rows: list[dict], key: str) -> float:
    return (sum(1 for r in rows if r.get(key)) / len(rows)) if rows else 0.0


def mean_of(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return (sum(vals) / len(vals)) if vals else None


def max_of(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return max(vals) if vals else None


def window_features(rows: list[dict], tag: str) -> dict:
    """rows are the most recent N batted balls, oldest first."""
    n = len(rows)
    f = {
        f"{tag}_bbe": n,
        f"{tag}_barrel_rate": rate(rows, "is_barrel"),
        f"{tag}_hard_hit_rate": rate(rows, "is_hard_hit"),
        f"{tag}_sweet_spot_rate": rate(rows, "is_sweet_spot"),
        f"{tag}_ideal_hr_contact": rate(rows, "is_ideal_hr"),
        f"{tag}_fb_rate": (sum(1 for r in rows if r.get("bb_type") == "fly_ball") / n) if n else 0.0,
        f"{tag}_gb_rate": (sum(1 for r in rows if r.get("bb_type") == "ground_ball") / n) if n else 0.0,
        f"{tag}_ld_rate": (sum(1 for r in rows if r.get("bb_type") == "line_drive") / n) if n else 0.0,
        f"{tag}_popup_rate": (sum(1 for r in rows if r.get("bb_type") == "popup") / n) if n else 0.0,
        f"{tag}_hr_rate": rate(rows, "is_hr"),
        f"{tag}_350_plus": sum(1 for r in rows if r.get("is_350_plus")),
        f"{tag}_375_plus": sum(1 for r in rows if r.get("is_375_plus")),
        f"{tag}_400_plus": sum(1 for r in rows if r.get("is_400_plus")),
        f"{tag}_avg_ev": mean_of(rows, "launch_speed"),
        f"{tag}_max_ev": max_of(rows, "launch_speed"),
        f"{tag}_avg_la": mean_of(rows, "launch_angle"),
        f"{tag}_avg_distance": mean_of(rows, "total_distance"),
        f"{tag}_max_distance": max_of(rows, "total_distance"),
    }
    f[f"{tag}_air_rate"] = f[f"{tag}_fb_rate"] + f[f"{tag}_ld_rate"]
    return f


def cmd_features(args) -> int:
    out_dir = Path(args.out_dir)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    made = 0
    for day in daterange(start, end):
        path = out_dir / f"features_{day.isoformat()}.jsonl"
        if path.exists() and not args.force:
            continue
        hist = load_events(out_dir, day)
        if not hist:
            continue
        rows = []
        for pid, evs in hist.items():
            row = {"as_of": day.isoformat(), "batter_id": pid,
                   "batter_name": evs[-1].get("batter_name", ""),
                   "season_bbe": len(evs)}
            row.update(window_features(evs, "season"))
            for w in WINDOWS:
                row.update(window_features(evs[-w:], f"l{w}"))
            rows.append(row)
        if args.min_bbe:
            rows = [r for r in rows if r["season_bbe"] >= args.min_bbe]
        if args.dry_run:
            print(f"{day}  {len(rows)} batters (dry run)")
        else:
            atomic_write_jsonl(path, rows)
            print(f"{day}  {len(rows)} batters -> {path.name}")
        made += 1
    print(f"\nbuilt {made} feature days")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("harvest", cmd_harvest), ("features", cmd_features)):
        p = sub.add_parser(name)
        p.add_argument("--start", required=True)
        p.add_argument("--end", required=True)
        p.add_argument("--out-dir", default=str(DEFAULT_OUT))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--force", action="store_true", help="rebuild days already on disk")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--pause", type=float, default=0.15)
        p.add_argument("--min-bbe", type=int, default=10)
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
