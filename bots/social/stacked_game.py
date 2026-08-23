"""Builds the Stacked Game content object — the single game on tonight's
board carrying the most model picks, i.e. "this is the game to watch."

Same source as daily_board.py (today_slim.json's game_pick_role rows), just
grouped by game_pk instead of ranked flat. A game only qualifies once it
clears MIN_PICKS picks (Donovan asked for "top game with 3 picks" —
3 is the fixed, documented threshold, same pattern as watchlist.py's
MIN_LAST5_HR), so this is reproducible from the published file, not a vibe.
"""

from __future__ import annotations

import json
import urllib.request
import datetime as dt
from typing import Any

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

MIN_PICKS = 3  # threshold for "stacked" — 3+ board picks in the same game


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


def build(*, date_str: str | None = None, min_picks: int = MIN_PICKS) -> dict[str, Any] | None:
    """Returns a Stacked Game `data` dict, or None if today_slim.json isn't
    available or no single game clears min_picks board picks yet."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    picks = [r for r in rows if _is_real_pick(r.get("game_pick_role")) and r.get("game_pk")]
    if not picks:
        print("  · no rows on today_slim.json have an assigned pick role yet")
        return None

    by_game: dict[Any, list[dict[str, Any]]] = {}
    for r in picks:
        by_game.setdefault(r.get("game_pk"), []).append(r)

    game_pk, members = max(by_game.items(), key=lambda kv: len(kv[1]))
    if len(members) < min_picks:
        print(f"  · the most-stacked game today only has {len(members)} pick(s), below {min_picks}")
        return None

    members.sort(key=lambda r: -(float(r.get("top_board_score_v2") or 0)))
    team = members[0].get("team")
    opponent = members[0].get("opponent")

    game_picks: list[dict[str, Any]] = []
    for r in members:
        game_picks.append({
            "name": r.get("name"),
            "team": r.get("team"),
            "role": r.get("game_pick_role"),
            "score": round(float(r.get("top_board_score_v2") or 0), 1) if r.get("top_board_score_v2") else None,
        })

    data: dict[str, Any] = {
        "date": date_str,
        "game_pk": game_pk,
        "team": team,
        "opponent": opponent,
        "game_time": members[0].get("game_time"),
        "pick_count": len(members),
        "picks": game_picks or None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
