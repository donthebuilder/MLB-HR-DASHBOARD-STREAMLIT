"""Builds the Daily Board content object — a morning/afternoon preview of
today's slate before any game has started, for the "here's tonight's board"
post rather than the after-the-fact recap.

Reads today_slim.json, the exact file streamlit_app.py's own Board/Games
tabs read (see streamlit_app.py's use of `top_board_score_v2` /
`game_pick_role`) — this module does not recompute a ranking, it reads the
one the site already publishes and shows the same picks a visitor sees.
"""

from __future__ import annotations

import json
import urllib.request
import datetime as dt
from typing import Any

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


# WATCH-AWARE (2026-08-23): WATCH is a coverage marker, not a pick --
# build_game_pick_role_map stamps the next 3 power bats per game so the
# coverage report can count them. A row whose ONLY role is WATCH must not
# appear on a social board as "the bot's pick".
def _is_real_pick(role) -> bool:
    toks = {t.strip().upper() for t in str(role or "").split("/") if t.strip()}
    return bool(toks - {"WATCH"})


def _fetch(url: str, timeout: int = 20) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  · fetch failed for {url}: {e}")
        return None


def build(*, date_str: str | None = None, top_n: int = 5) -> dict[str, Any] | None:
    """Returns a Daily Board `data` dict, or None if today_slim.json isn't
    published yet / has no rows with an assigned pick role. date_str is used
    only for the post id / fingerprint — today_slim.json itself is always
    "the current slate," there is no historical per-date archive of it."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    picks = [r for r in rows if _is_real_pick(r.get("game_pick_role"))]
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
