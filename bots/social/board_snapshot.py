"""Builds the Board Snapshot content object — an on-demand, share-anytime
capture of the site's current Board ranking, wider than the automated Daily
Board post (top 10 instead of top 5) since this one isn't trying to fit a
single pre-game announcement, just "here's the board right now."

Same source and shape as daily_board.py (today_slim.json's game_pick_role
rows, ranked by top_board_score_v2) — kept as its own module rather than a
thin daily_board wrapper so it can carry its own default top_n and, later,
its own framing without touching the automated daily post's behaviour.
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


def build(*, date_str: str | None = None, top_n: int = 10) -> dict[str, Any] | None:
    """Returns a Board Snapshot `data` dict, or None if today_slim.json
    isn't published yet / has no rows with an assigned pick role."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    picks = [r for r in rows if r.get("game_pick_role")]
    if not picks:
        print("  · no rows on today_slim.json have an assigned pick role yet")
        return None

    picks.sort(key=lambda r: -(float(r.get("top_board_score_v2") or 0)))
    top = picks[:top_n]

    board: list[dict[str, Any]] = []
    for r in top:
        board.append({
            "name": r.get("name"),
            "team": r.get("team"),
            "opponent": r.get("opponent"),
            "role": r.get("game_pick_role"),
            "score": round(float(r.get("top_board_score_v2") or 0), 1) if r.get("top_board_score_v2") else None,
        })

    games = len(set(r.get("game_pk") for r in rows if r.get("game_pk")))
    hitters = len(rows)

    data: dict[str, Any] = {
        "date": date_str,
        "games": games or None,
        "hitters": hitters or None,
        "board_size": len(picks),
        "board": board or None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
