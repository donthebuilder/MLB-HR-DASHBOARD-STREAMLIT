#!/usr/bin/env python3
"""nfl_espn.py — schedule and live scores, including preseason.

nflverse carries NO preseason at all (verified 2026-08-14: `load_schedules()`
game_type is REG/WC/DIV/CON/SB and nothing else, and player stats only go
REG/POST). So anything before Week 1 has to come from somewhere else, and
ESPN's public scoreboard JSON is the only free source that has it.

seasontype: 1 = preseason, 2 = regular, 3 = postseason.

This is an unofficial endpoint. Everything here fails soft: no network, a
schema change, a 500 — the caller gets an empty list and the bot falls back
to nflverse for the regular season. Nothing on the site is allowed to depend
on ESPN being up.
"""
from __future__ import annotations
import datetime as dt
from typing import Any

import requests

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
TIMEOUT = 30

# nflverse abbreviations differ from ESPN's in three places.
FIX = {"WSH": "WAS", "LAR": "LA", "JAX": "JAX"}


def _abbr(x: str) -> str:
    return FIX.get(x, x)


def fetch(seasontype: int = 1, week: int | None = None,
          year: int | None = None) -> list[dict[str, Any]]:
    """Games for a scoreboard slice. Returns [] on any failure, never raises."""
    params: dict[str, Any] = {"seasontype": seasontype, "limit": 100}
    if year:
        params["dates"] = year
    if week:
        params["week"] = week
    try:
        r = requests.get(SCOREBOARD, params=params, timeout=TIMEOUT)
        if not r.ok:
            return []
        payload = r.json()
    except Exception:
        return []

    out = []
    for ev in payload.get("events", []):
        try:
            comp = ev["competitions"][0]
            sides = {c["homeAway"]: c for c in comp["competitors"]}
            home, away = sides["home"], sides["away"]
            status = ev.get("status", {}).get("type", {})
            out.append({
                "game_id": str(ev.get("id")),
                "kickoff": ev.get("date"),
                "home": _abbr(home["team"]["abbreviation"]),
                "away": _abbr(away["team"]["abbreviation"]),
                "home_name": home["team"].get("displayName"),
                "away_name": away["team"].get("displayName"),
                "home_score": int(home.get("score") or 0),
                "away_score": int(away.get("score") or 0),
                "state": status.get("state"),          # pre | in | post
                "detail": status.get("shortDetail"),
                "completed": bool(status.get("completed")),
                "venue": comp.get("venue", {}).get("fullName"),
                "indoors": bool(comp.get("venue", {}).get("indoor")),
                "week": (payload.get("week") or {}).get("number"),
                "season_type": seasontype,
                "source": "espn",
            })
        except Exception:
            continue
    return out


def scoring_plays(game_id: str) -> list[dict[str, Any]]:
    """Who scored, for the live wire. Empty on any failure."""
    url = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
           f"?event={game_id}")
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if not r.ok:
            return []
        data = r.json()
    except Exception:
        return []
    out = []
    for p in data.get("scoringPlays", []):
        out.append({
            "quarter": (p.get("period") or {}).get("number"),
            "clock": (p.get("clock") or {}).get("displayValue"),
            "team": _abbr(((p.get("team") or {}).get("abbreviation")) or ""),
            "type": (p.get("scoringType") or {}).get("displayName"),
            "text": p.get("text"),
            "away_score": p.get("awayScore"),
            "home_score": p.get("homeScore"),
        })
    return out


def slate_for(date: dt.date, year: int | None = None) -> list[dict[str, Any]]:
    """Every game on a given calendar date, preseason or regular."""
    year = year or date.year
    games: list[dict[str, Any]] = []
    for st in (1, 2, 3):
        games.extend(fetch(seasontype=st, year=year))
    want = date.isoformat()
    return [g for g in games if str(g.get("kickoff", ""))[:10] == want]


if __name__ == "__main__":
    import json
    import sys
    d = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    print(json.dumps(slate_for(d), indent=2)[:3000])
