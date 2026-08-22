"""Builds the Game Card content object — an on-demand, share-anytime card
for one specific matchup: which picks are in it, and (once the game has
been graded) how each of them actually did.

Reads today_slim.json for the pre-game picks in the named game, and — only
if a graded_results_<date>.json for that same date already has rows for
that game_pk — overlays each pick's real actual_hr/actual_hits. Never
invents a score or a result; a game with no graded file yet is reported
"upcoming" with picks only, exactly like the site itself would show it
before first pitch.
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


def build(*, date_str: str | None = None, game_pk: int | str | None = None,
          team: str | None = None) -> dict[str, Any] | None:
    """Returns a Game Card `data` dict for one matchup, identified either by
    `game_pk` or by `team` (either side's abbreviation, case-insensitive,
    matched against today_slim.json). Returns None if neither identifies a
    real game on today's slate, or the game has no assigned picks yet."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    if game_pk is not None:
        game_rows = [r for r in rows if str(r.get("game_pk")) == str(game_pk)]
    elif team:
        t = team.strip().upper()
        matches = [r for r in rows if str(r.get("team", "")).upper() == t
                   or str(r.get("opponent", "")).upper() == t]
        gp = matches[0].get("game_pk") if matches else None
        game_rows = [r for r in rows if r.get("game_pk") == gp] if gp is not None else []
    else:
        print("  · game_card.build() needs either game_pk or team")
        return None

    if not game_rows:
        print(f"  · no rows on today_slim.json match that game (game_pk={game_pk!r}, team={team!r})")
        return None

    picks_rows = [r for r in game_rows if r.get("game_pick_role")]
    if not picks_rows:
        print("  · that game has no assigned picks yet")
        return None
    picks_rows.sort(key=lambda r: -(float(r.get("top_board_score_v2") or 0)))

    home_row = picks_rows[0]
    resolved_game_pk = home_row.get("game_pk")
    team_a = home_row.get("team")
    team_b = home_row.get("opponent")

    graded = _fetch(f"{RAW}/graded_results_{date_str}.json")
    graded_by_pid: dict[Any, dict[str, Any]] = {}
    status = "upcoming"
    if graded and isinstance(graded.get("graded_slots"), list):
        for g in graded["graded_slots"]:
            if str(g.get("game_pk")) == str(resolved_game_pk):
                graded_by_pid[g.get("player_id")] = g
        if graded_by_pid:
            gs = next(iter(graded_by_pid.values())).get("game_status") or {}
            state = str(gs.get("detailed_state") or "").lower()
            status = "final" if "final" in state else ("live" if state else "graded")

    picks: list[dict[str, Any]] = []
    for r in picks_rows:
        row: dict[str, Any] = {
            "name": r.get("name"),
            "team": r.get("team"),
            "role": r.get("game_pick_role"),
            "score": round(float(r.get("top_board_score_v2") or 0), 1) if r.get("top_board_score_v2") else None,
        }
        g = graded_by_pid.get(r.get("player_id"))
        if g:
            row["actual_hr"] = int(g.get("actual_hr") or 0)
            row["actual_hits"] = int(g.get("actual_hits") or 0)
        picks.append(row)

    data: dict[str, Any] = {
        "date": date_str,
        "game_pk": resolved_game_pk,
        "team": team_a,
        "opponent": team_b,
        "game_time": home_row.get("game_time"),
        "status": status,
        "pick_count": len(picks),
        "picks": picks or None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
