"""Builds the Moonshot/MLB Night Recap content object — the first content
type end to end (spec section 22's "first publishing milestone").

Every number in the returned `data` dict is read from a file this repo
already publishes (results_final.json / graded_results_<date>.json,
odds_history.json) — nothing here recomputes the model or duplicates
scoring logic. This module owns exactly one thing: turning already-graded
results into the small, labeled dict Claude and the asset renderer are
allowed to see.

The SAME shape of function (fetch → tally by pick_type → build `data`) is
what a future Tuddy/NFL week-recap builder would follow; nothing below
reads an MLB-only field name without a comment saying so.
"""

from __future__ import annotations

import json
import urllib.request
import datetime as dt
from typing import Any

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


def _fetch(url: str, timeout: int = 20) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  · fetch failed for {url}: {e}")
        return None


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def is_night_complete(graded: dict[str, Any]) -> bool:
    """True once every game on the slate has a Final status — the gate
    spec section 15 asks for ("once grading is complete"). A slate with
    zero graded rows (games haven't started, or the file hasn't published
    yet) is NOT complete."""
    slots = graded.get("graded_slots") or []
    if not slots:
        return False
    by_game: dict[str, int] = {}
    for r in slots:
        gp = str(r.get("game_pk") or "")
        if not gp:
            continue
        by_game[gp] = max(by_game.get(gp, 0), int(r.get("is_final") or 0))
    return bool(by_game) and all(v == 1 for v in by_game.values())


def _odds_price_for(player_id: int, odds_history: dict[str, Any] | None, date_str: str) -> int | None:
    """Best-effort closing HR-prop price for a player on this date, from
    odds_history.json. Returns None (never a guess) if the file, the date,
    the player or the market isn't present — see dataSource.js's own
    comment: a missing odds file is a normal state, not an error."""
    if not odds_history:
        return None
    try:
        # odds_history.json's exact per-date/per-market shape hasn't been
        # pinned down here — this looks for the most common shapes
        # (date-keyed, then player-id-keyed, then a flat list) and returns
        # None rather than guess if none match.
        by_date = odds_history.get(date_str) if isinstance(odds_history, dict) else None
        bucket = by_date if isinstance(by_date, dict) else odds_history
        row = (bucket or {}).get(str(player_id)) if isinstance(bucket, dict) else None
        if isinstance(row, dict):
            market = row.get("batter_home_runs") or row.get("hr")
            if isinstance(market, dict):
                price = market.get("best_over") or market.get("over")
                return int(price) if price is not None else None
    except Exception:
        pass
    return None


def fetch_graded(date_str: str) -> dict[str, Any] | None:
    """Fetch + validate the graded payload for date_str, or None. Split out
    of build() so a caller (social_night_recap.py) can fetch it once and
    reuse it for a same-run player_spotlight.build(graded=...) instead of a
    second HTTP round trip for the same file."""
    graded = _fetch(f"{RAW}/graded_results_{date_str}.json")
    if not graded:
        # Same-day slate before the per-date archive file exists yet.
        graded = _fetch(f"{RAW}/results_final.json")
        if graded and str(graded.get("date")) != date_str:
            graded = None
    return graded


def build(*, date_str: str | None = None, odds_history: bool = True,
          graded: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Returns a Night Recap `data` dict, or None if the slate isn't graded
    complete yet / no data is available (the caller should not queue a post
    in either case). Pass `graded` to reuse an already-fetched payload."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    if graded is None:
        graded = fetch_graded(date_str)
    if not graded:
        print(f"  · no graded results available for {date_str}")
        return None

    if not is_night_complete(graded):
        print(f"  · {date_str} is not fully graded yet — skipping recap")
        return None

    slots = graded.get("graded_slots") or []
    top15 = [r for r in slots if r.get("pick_type") == "TOP15"]
    hr_picks = [r for r in slots if r.get("pick_type") == "HR"]

    board_hits = sum(1 for r in top15 if r.get("got_hr"))
    board_total = len(top15)
    hr_pick_hits = sum(1 for r in hr_picks if r.get("got_hr"))
    hr_pick_total = len(hr_picks)

    hcr = graded.get("hr_capture_report") or {}
    total_slate_hrs = int(hcr.get("total_hrs_on_slate") or 0)
    caught_slate_hrs = int(hcr.get("caught_hrs_on_sheet") or 0)

    # Unique players on the sheet who actually homered — derived here from
    # graded_slots rather than duplicating bots/live_results_tracker.py's
    # own (unpublished) unique_player_report.
    scorers: dict[int, dict[str, Any]] = {}
    for r in slots:
        if not r.get("got_hr"):
            continue
        pid = r.get("player_id")
        if pid is None or pid in scorers:
            continue
        scorers[pid] = r

    odds_hist = _fetch(f"{RAW}/odds_history.json") if odds_history else None

    top_cashes: list[str] = []
    for r in sorted(scorers.values(), key=lambda x: float(x.get("hr_score") or 0), reverse=True)[:5]:
        price = _odds_price_for(r.get("player_id"), odds_hist, date_str)
        line = r.get("name") or "Unknown"
        if price is not None:
            line += f" {'+' if price > 0 else ''}{price}"
        top_cashes.append(line)

    longest = None
    merged = graded.get("merged_homers") or []
    dist_rows = [m for m in merged if m.get("longest_ft")]
    if dist_rows:
        top = max(dist_rows, key=lambda m: float(m.get("longest_ft") or 0))
        longest = {"name": top.get("name"), "feet": int(top.get("longest_ft") or 0)}

    data: dict[str, Any] = {
        "date": date_str,
        "board_record": f"{board_hits}/{board_total}" if board_total else None,
        "board_hit_rate": _pct(board_hits, board_total) if board_total else None,
        "hr_pick_record": f"{hr_pick_hits}/{hr_pick_total}" if hr_pick_total else None,
        "hr_pick_hit_rate": _pct(hr_pick_hits, hr_pick_total) if hr_pick_total else None,
        "hr_scorers": len(scorers),
        "slate_hr_coverage": f"{caught_slate_hrs}/{total_slate_hrs}" if total_slate_hrs else None,
        "slate_hr_coverage_pct": hcr.get("hr_capture_pct"),
        "top_cashes": top_cashes or None,
        "longest_hr": longest,
    }
    # Never hand Claude or the asset renderer a null placeholder — omit
    # anything we couldn't compute rather than pass a misleading key.
    return {k: v for k, v in data.items() if v not in (None, [], "")}
