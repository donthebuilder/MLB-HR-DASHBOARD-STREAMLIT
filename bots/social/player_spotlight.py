"""Builds the Player Spotlight content object — one standout performer from
an already-graded slate, given the full single-card treatment instead of
being one line in the Night Recap's top_cashes list.

Same rule as night_recap.py: every field comes from a file this repo
already publishes (graded_results_<date>.json, odds_history.json) — this
module does not recompute the model or invent a "why" for the pick beyond
what the graded row already states.
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


def _odds_price_for(player_id: int, odds_history: dict[str, Any] | None, date_str: str) -> int | None:
    """Same best-effort lookup as night_recap._odds_price_for — kept
    duplicated rather than imported so this module stays independently
    fetchable/testable the same way night_recap.py is."""
    if not odds_history:
        return None
    try:
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


def build(*, date_str: str | None = None, graded: dict[str, Any] | None = None,
          odds_history: bool = True) -> dict[str, Any] | None:
    """Returns a Player Spotlight `data` dict for the single highest-scoring
    HR of the given (already-graded, already-complete) slate, or None if
    nobody on the sheet actually homered that night. `graded` may be passed
    in by a caller (social_night_recap.py) that already fetched it this run
    to avoid a second HTTP round trip; otherwise this fetches it itself."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    if graded is None:
        graded = _fetch(f"{RAW}/graded_results_{date_str}.json")
        if not graded:
            graded = _fetch(f"{RAW}/results_final.json")
            if graded and str(graded.get("date")) != date_str:
                graded = None
    if not graded:
        print(f"  · no graded results available for {date_str}")
        return None

    slots = graded.get("graded_slots") or []
    scorers: dict[int, dict[str, Any]] = {}
    for r in slots:
        if not r.get("got_hr"):
            continue
        pid = r.get("player_id")
        if pid is None:
            continue
        # keep the highest-scoring row per player if they appear more than once
        cur = scorers.get(pid)
        if cur is None or float(r.get("hr_score") or 0) > float(cur.get("hr_score") or 0):
            scorers[pid] = r

    if not scorers:
        return None

    top = max(scorers.values(), key=lambda r: float(r.get("hr_score") or 0))
    odds_hist = _fetch(f"{RAW}/odds_history.json") if odds_history else None
    price = _odds_price_for(top.get("player_id"), odds_hist, date_str)

    longest_ft = None
    for m in (graded.get("merged_homers") or []):
        if str(m.get("name", "")).strip().lower() == str(top.get("name", "")).strip().lower() and m.get("longest_ft"):
            v = int(m.get("longest_ft") or 0)
            longest_ft = max(longest_ft or 0, v)

    hr_count = sum(1 for s in slots if s.get("player_id") == top.get("player_id") and s.get("got_hr"))

    data: dict[str, Any] = {
        "date": date_str,
        "name": top.get("name"),
        "team": top.get("team"),
        "opponent": top.get("opponent"),
        "pick_type": top.get("pick_type"),
        "hr_score": round(float(top.get("hr_score") or 0), 1) if top.get("hr_score") else None,
        "odds_price": price,
        "longest_hr_feet": longest_ft,
        "multi_hr": hr_count if hr_count and hr_count > 1 else None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
