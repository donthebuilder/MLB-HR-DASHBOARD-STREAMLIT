#!/usr/bin/env python3
"""THE TRUE PRICE — what a player actually does, against what he was priced at.

Donovan, 2026-08-15:
  "then i gues we can havbe page where it track players who go at what price
   for certain props that way we can find the true price of a player to do
   certian things."

A single night's price tells you what the book thinks. A hundred nights of
prices next to a hundred outcomes tells you whether the book is RIGHT about
this hitter — and that gap is the only durable edge a card like this can have.

WHAT THIS DOES

  1. Reads every dated odds snapshot the data branch holds (odds_YYYY-MM-DD.json,
     written by odds_fetch.py BEFORE first pitch — a closing-ish price, not a
     post-hoc one).
  2. Reads the graded file for each of those same dates.
  3. Settles every priced prop against the actual box line, at the exact number
     the book was offering that night.
  4. Publishes odds_history.json: per player, per market, per line —
     how often he does it, the average price he goes at, and the price his
     own rate says he's worth.

REBUILT FROM SCRATCH EVERY RUN. There is no accumulator to corrupt and no
double-count to guard against: the archives are the state, this is a pure
function of them. A grading bug found in November can be fixed and the whole
history recomputed, which would not be true of a running total.

WHAT COUNTS, AND WHAT DOESN'T

  · A game where he never came up (0 AB and 0 BB) is a VOID, not a miss. Same
    rule the rest of this product uses — lib/myPicks.js on the site, the
    live tracker here. Books void those too.
  · A price with no graded outcome for that date is dropped, not assumed.
  · Prices are averaged as IMPLIED PROBABILITY and converted back. American
    odds are not linear — the mean of -400 and +300 is not +/-50, and averaging
    them directly would quietly overstate every longshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ODDS_RE = re.compile(r"odds_(\d{4}-\d{2}-\d{2})\.json$")
GRADED_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

# How the box score settles each market. The line is always the book's number
# and the over needs to BEAT it, which is why this is `>` and not `>=`: at a
# whole-number line (rare, but books post them) equalling it is a push, and a
# push is not a win.
SETTLE = {
    "batter_hits": lambda a: a["h"],
    "batter_total_bases": lambda a: a["tb"],
    "batter_home_runs": lambda a: a["hr"],
    "batter_hits_runs_rbis": lambda a: a["h"] + a["r"] + a["rbi"],
    "batter_runs_scored": lambda a: a["r"],
    "batter_rbis": lambda a: a["rbi"],
}

LABEL = {
    "batter_hits": "Hits",
    "batter_total_bases": "Total bases",
    "batter_home_runs": "Home runs",
    "batter_hits_runs_rbis": "H+R+RBI",
    "batter_runs_scored": "Runs",
    "batter_rbis": "RBIs",
}


def implied_raw(american) -> float | None:
    """American price -> break-even percentage, UNROUNDED.

    Everything that gets averaged and converted back uses this. The dated
    archive stores `implied` already rounded to a tenth, and folding hundreds
    of those together then converting drifted +500 to +499 in testing — the
    error is tiny but it lands on the one number this file is named after.
    """
    try:
        n = float(american)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return 100 * ((-n / (-n + 100)) if n < 0 else (100 / (n + 100)))


def american(pct) -> int | None:
    """Break-even percentage -> the American price that pays exactly that."""
    try:
        p = float(pct) / 100
    except (TypeError, ValueError):
        return None
    if not (0 < p < 1):
        return None
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def rows_of(payload) -> list[dict]:
    """The graded rows, whatever shape the file is. Mirrors bots/archive.py."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


def as_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def box_by_pid(payload) -> dict[int, dict]:
    """{player_id: box line} for one graded night.

    The graded file carries ONE ROW PER PICK CATEGORY, so a hitter who was both
    the HR and the CONTACT pick appears twice with identical actual_* fields.
    First row wins; the rest are the same numbers wearing a different label.
    """
    out: dict[int, dict] = {}
    for r in rows_of(payload):
        pid = as_int(r.get("player_id") or r.get("id"))
        if not pid or pid in out:
            continue
        ab, bb = as_int(r.get("actual_ab")), as_int(r.get("actual_bb"))
        # He never came up. Not a miss — nothing happened. Same filter the
        # site's game log uses (lib/gamelogs.js: ab > 0 || bb > 0), so a rare
        # all-walks night still counts as a real chance he didn't convert.
        if ab == 0 and bb == 0:
            continue
        out[pid] = {
            "h": as_int(r.get("actual_hits")), "tb": as_int(r.get("actual_tb")),
            "hr": as_int(r.get("actual_hr")), "r": as_int(r.get("actual_runs")),
            "rbi": as_int(r.get("actual_rbi")), "ab": ab, "bb": bb,
            "name": r.get("player_name") or r.get("name") or "",
            "team": r.get("team") or "",
        }
    return out


def snapshot_rows(payload) -> dict[str, dict]:
    """{player_id: {market: [line, over, implied]}} from a dated odds archive.

    Accepts the slim archive odds_fetch.py writes AND the full odds_latest.json
    shape, so a snapshot that was only ever kept as `latest` can still be fed
    in by hand with --extra.
    """
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("rows")
    if isinstance(rows, dict) and rows:
        return {str(k): v for k, v in rows.items() if isinstance(v, dict)}
    full = payload.get("by_player_id")
    if isinstance(full, dict):
        out = {}
        for pid, mkts in full.items():
            if not isinstance(mkts, dict):
                continue
            slim = {}
            for m, q in mkts.items():
                if isinstance(q, dict) and q.get("over") is not None:
                    slim[m] = [q.get("line"), q.get("over"), q.get("implied")]
            if slim:
                out[str(pid)] = slim
        return out
    return {}


def load(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception as e:
        print(f"  skip {p.name} ({type(e).__name__})", file=sys.stderr)
        return None


def find(dirs: list[Path], pattern: re.Pattern) -> dict[str, Path]:
    """{date: newest path} across every directory, later dirs winning ties."""
    out: dict[str, Path] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            m = pattern.search(p.name)
            if m:
                out[m.group(1)] = p
    return out


def build(odds_dirs: list[Path], graded_dirs: list[Path], log_keep: int) -> dict:
    snaps = find(odds_dirs, ODDS_RE)
    grades = find(graded_dirs, GRADED_RE)
    dates = sorted(set(snaps) & set(grades))
    missing = sorted(set(snaps) - set(grades))
    print(f"{len(snaps)} odds snapshots, {len(grades)} graded days, {len(dates)} joinable")
    if missing:
        print(f"  priced but not yet graded: {', '.join(missing[-6:])}")

    players: dict[str, dict] = {}
    settled = 0
    for date in dates:
        board = snapshot_rows(load(snaps[date]) or {})
        box = box_by_pid(load(grades[date]) or {})
        if not board or not box:
            continue
        for pid_s, mkts in board.items():
            pid = as_int(pid_s)
            actual = box.get(pid)
            if not actual:
                continue  # priced, but he isn't in the graded file — no outcome
            rec = players.setdefault(pid_s, {
                "name": actual["name"], "team": actual["team"], "markets": {}
            })
            if actual["name"]:
                rec["name"] = actual["name"]
            if actual["team"]:
                rec["team"] = actual["team"]
            for market, q in mkts.items():
                fn = SETTLE.get(market)
                if not fn or not isinstance(q, (list, tuple)) or len(q) < 2:
                    continue
                line, over = q[0], q[1]
                # Recomputed from the price, never read from the archive's
                # rounded copy — see implied_raw().
                imp = implied_raw(over)
                if line is None or over is None or imp is None:
                    continue
                got = 1 if fn(actual) > float(line) else 0
                key = f"{market}|{line}"
                b = rec["markets"].setdefault(key, {
                    "market": market, "line": float(line),
                    "n": 0, "hits": 0, "sum_implied": 0.0, "log": [],
                })
                b["n"] += 1
                b["hits"] += got
                b["sum_implied"] += float(imp)
                b["log"].append([date, over, got])
                settled += 1

    # ── finish: the two numbers the page exists to show ──────────────────────
    out_players = {}
    for pid, rec in players.items():
        mkts = {}
        for key, b in rec["markets"].items():
            n = b["n"]
            # Convert from the UNROUNDED rates. Rounding a percentage to one
            # decimal and then turning it into odds moved +500 to +499 in
            # testing — small, but it's the number the whole page is named
            # after, so it rounds once, at the end, for display only.
            rate_raw = 100 * b["hits"] / n
            avg_raw = b["sum_implied"] / n
            mkts[key] = {
                "market": b["market"], "line": b["line"],
                "label": f"{b['line'] + 0.5:g}+ {LABEL.get(b['market'], b['market'])}",
                "n": n, "hits": b["hits"],
                # What he actually does.
                "rate": round(10 * rate_raw) / 10,
                # The price that rate deserves — his TRUE price. null at 0% or
                # 100%: a rate that has never missed has no finite price, and
                # inventing one would be the single most misleading number here.
                "true_price": american(rate_raw),
                # What he's actually been offered, averaged honestly.
                "avg_implied": round(10 * avg_raw) / 10,
                "avg_price": american(avg_raw),
                # Positive = the book has been paying more than he's worth.
                "edge": round(10 * (rate_raw - avg_raw)) / 10,
                # The nights themselves, newest first. Without these the two
                # numbers above are a claim; with them they're checkable.
                "log": b["log"][-log_keep:][::-1],
            }
        if mkts:
            out_players[pid] = {"name": rec["name"], "team": rec["team"], "markets": mkts}

    now = dt.datetime.now(dt.timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "generated_at_human": now.strftime("%b %-d, %-I:%M %p UTC"),
        "days": dates,
        "first_day": dates[0] if dates else None,
        "last_day": dates[-1] if dates else None,
        "priced_not_graded": missing[-14:],
        "settled_props": settled,
        "players": out_players,
        "note": ("Every priced prop settled against that night's box score at the "
                 "exact line the book posted. rate is how often he clears it; "
                 "true_price is the American price that rate breaks even at; "
                 "avg_price is what he has actually been offered, averaged as "
                 "probability rather than as odds. edge = rate - avg_implied, in "
                 "percentage points. A game he never batted in is void, not a miss."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--odds-dir", action="append", default=[],
                    help="directory of odds_YYYY-MM-DD.json (repeatable)")
    ap.add_argument("--graded-dir", action="append", default=[],
                    help="directory of graded_results_YYYY-MM-DD.json (repeatable)")
    ap.add_argument("--log-keep", type=int, default=12,
                    help="nights kept per bucket for the receipts (default 12)")
    ap.add_argument("--out", default="public/data/current/odds_history.json")
    a = ap.parse_args()

    odds_dirs = [Path(d) for d in (a.odds_dir or ["public/data/current", "public/data"])]
    graded_dirs = [Path(d) for d in (a.graded_dir or ["public/data/current", "public/data"])]

    payload = build(odds_dirs, graded_dirs, a.log_keep)
    dest = Path(a.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} — {len(payload['players'])} players, "
          f"{payload['settled_props']} settled props over {len(payload['days'])} days "
          f"({dest.stat().st_size / 1024:.0f} KB)")
    if not payload["days"]:
        print("  NOTE: nothing joined yet. The history starts the first night an "
              "odds snapshot and a graded file exist for the SAME date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
