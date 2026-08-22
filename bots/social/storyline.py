"""Builds the Storyline content object — "why the model likes this pick,"
in the model's own words. Every field comes straight off today_slim.json's
top_pick_reason (a short tag) and top_board_rank_reason (a comma-separated
list of the signals that actually drove the ranking) — nothing here is
newly written narrative, it's the model's existing reasoning surfaced as
its own card instead of buried in a tooltip.
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


def build(*, date_str: str | None = None) -> dict[str, Any] | None:
    """Returns a Storyline `data` dict for the single highest-ranked pick
    that actually has a model-written reason attached, or None if
    today_slim.json isn't available or nobody qualifies yet."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    candidates = [
        r for r in rows
        if r.get("game_pick_role") and (r.get("top_pick_reason") or r.get("top_board_rank_reason"))
    ]
    if not candidates:
        print("  · no rows on today_slim.json have a model reason attached yet")
        return None

    candidates.sort(key=lambda r: -(float(r.get("top_board_score_v2") or 0)))
    r = candidates[0]

    reasons_raw = str(r.get("top_board_rank_reason") or "")
    reasons = [s.strip() for s in reasons_raw.split(",") if s.strip()] or None

    data: dict[str, Any] = {
        "date": date_str,
        "name": r.get("name"),
        "team": r.get("team"),
        "opponent": r.get("opponent"),
        "role": r.get("game_pick_role"),
        "tag": r.get("top_pick_reason"),
        "reasons": reasons,
        "score": round(float(r.get("top_board_score_v2") or 0), 1) if r.get("top_board_score_v2") else None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
