"""Builds the Player Card content object — a general-purpose profile card
for any hitter currently on today's slate: season line, recent form, and
today's model score. Unlike Player Spotlight (which only exists for
whoever actually hit a HR on an already-graded night), this works for
anyone on today_slim.json, any time before or during the game.

Same rule as every other builder here: every field is read straight off
today_slim.json — nothing recomputed, nothing invented, and a player not
currently on the slate simply isn't available (no season-only fallback
file exists to build this from once they roll off today's board).
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


def build(*, date_str: str | None = None, name: str, team: str | None = None) -> dict[str, Any] | None:
    """Returns a Player Card `data` dict for the named hitter (case-
    insensitive exact match against today_slim.json's `name` field), or
    None if they aren't on today's slate.

    Real MLB has genuine same-name collisions on a given slate (e.g. two
    active players named "Max Muncy" — this isn't a data bug), so a bare
    name match is resolved by team if given, else by preferring whichever
    match actually has a game_pick_role (the one on today's board), else
    the higher top_board_score_v2. Pass `team` to disambiguate explicitly
    when neither tiebreak applies."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    target = name.strip().lower()
    matches = [r for r in rows if str(r.get("name", "")).strip().lower() == target]
    if team:
        t = team.strip().upper()
        matches = [r for r in matches if str(r.get("team", "")).upper() == t]
    if not matches:
        print(f"  · {name!r} is not on today's slate" + (f" for team {team!r}" if team else ""))
        return None
    if len(matches) > 1:
        print(f"  · {len(matches)} players on today's slate are named {name!r} — "
              f"picking the one on the board (or highest score) unless --team disambiguates")
        matches.sort(key=lambda r: (bool(r.get("game_pick_role")),
                                     float(r.get("top_board_score_v2") or 0)), reverse=True)
    row = matches[0]

    def pct(v):
        return round(float(v) * 100, 1) if isinstance(v, (int, float)) else None

    data: dict[str, Any] = {
        "date": date_str,
        "name": row.get("name"),
        "team": row.get("team"),
        "opponent": row.get("opponent"),
        "role": row.get("game_pick_role"),
        "season_avg": row.get("season_avg"),
        "season_hr": row.get("season_hr"),
        "season_ops": row.get("season_ops"),
        "season_slg": row.get("season_slg"),
        "season_rbi": row.get("season_rbi"),
        "last5_avg": round(float(row["last5_avg"]), 3) if row.get("last5_avg") is not None else None,
        "last5_hr": row.get("last5_hr"),
        "last7_avg": round(float(row["last7_avg"]), 3) if row.get("last7_avg") is not None else None,
        "last7_hr": row.get("last7_hr"),
        "top_board_score": round(float(row["top_board_score_v2"]), 1) if row.get("top_board_score_v2") else None,
        "hr_score": round(float(row["hr_score"]), 1) if row.get("hr_score") else None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
