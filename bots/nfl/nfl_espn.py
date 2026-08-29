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
            row = {
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
            }
            # WEATHER (2026-08-28, B7). ESPN already ships this on the event
            # itself (event.weather, a sibling of event.competitions — NOT
            # nested under the competition), confirmed against a real live
            # response: {"displayValue": "Intermittent clouds",
            # "temperature": 82, ...}. Never fabricated, never a separate API
            # call — just a field this parser wasn't reading before. Indoor
            # games and games ESPN hasn't priced weather for simply carry no
            # "weather" key; `.get()` leaves both fields None rather than
            # guessing a fallback value.
            wx = ev.get("weather") or {}
            row["weather_temp_f"] = wx.get("temperature")
            row["weather_condition"] = wx.get("displayValue")
            # DRIVE STATE (2026-08-28, B7). ESPN's scoreboard competition
            # object carries a "situation" block ONLY while a game is
            # actually live (down/distance/possession) — confirmed absent on
            # every pregame event checked directly. Best-effort and
            # defensive on purpose: this field set is not in ESPN's public
            # docs (there are none) and has not yet been observed on a real
            # live game from this codebase, only inferred from the same
            # publicly-documented shape other ESPN scoreboard integrations
            # report. Every read is `.get()`, nothing here raises, and if
            # the shape is wrong or missing this silently yields
            # down_distance=None — Games.js already has its own honest
            # fallback caveat for exactly that case, so a wrong guess here
            # degrades to today's behavior, it doesn't break it. Treat the
            # first live game of the season as the real verification step,
            # not this comment.
            situation = comp.get("situation") or {}
            row["down_distance"] = situation.get("downDistanceText") or situation.get("shortDownDistanceText")
            row["red_zone"] = bool(situation.get("isRedZone")) if situation else False
            out.append(row)
        except Exception:
            continue
    return out


# REST DAYS / SHORT WEEK (2026-08-28, B7). "Tired defense" was asked for;
# real snap-count/fatigue data doesn't exist anywhere in this codebase's NFL
# layer (checked directly — no snap_count, days_rest, or fatigue field
# anywhere in lib/nfl or bots/nfl). What DOES exist for free, with zero new
# dependency and zero risk of being wrong, is the schedule itself: how many
# days since a team's last game is pure date arithmetic over data already
# fetched. This isn't the same signal DVP already covers (matchup softness,
# not workload) — it's a real, if blunt, fatigue proxy: a short week
# (Thursday off a Sunday, 4 days) is a genuinely different rest state than a
# normal 7-day turnaround, independent of who's on defense.
#
# Call with the FULL season-type schedule (not just the current week's
# slice) — rest days needs to see a team's PRIOR game, which a single-week
# fetch doesn't carry.
SHORT_WEEK_MAX_DAYS = 5


def attach_rest_days(all_games: list[dict[str, Any]], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate `target` games with home_rest_days/away_rest_days/
    home_short_week/away_short_week, computed from the FULL `all_games`
    schedule (which may be a superset of `target`, or the same list).
    Returns new dicts; does not mutate the input games.
    """
    by_team: dict[str, list[dt.date]] = {}
    for g in all_games:
        raw = str(g.get("kickoff") or "")[:10]
        if not raw:
            continue
        try:
            day = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        for team in (g.get("home"), g.get("away")):
            if team:
                by_team.setdefault(team, []).append(day)
    for team in by_team:
        by_team[team] = sorted(set(by_team[team]))

    def _rest(team: str | None, kickoff_day: dt.date | None) -> int | None:
        if not team or kickoff_day is None or team not in by_team:
            return None
        prior = [d for d in by_team[team] if d < kickoff_day]
        if not prior:
            return None
        return (kickoff_day - max(prior)).days

    out = []
    for g in target:
        row = dict(g)
        raw = str(g.get("kickoff") or "")[:10]
        try:
            kickoff_day = dt.date.fromisoformat(raw) if raw else None
        except ValueError:
            kickoff_day = None
        home_rest = _rest(g.get("home"), kickoff_day)
        away_rest = _rest(g.get("away"), kickoff_day)
        row["home_rest_days"] = home_rest
        row["away_rest_days"] = away_rest
        row["home_short_week"] = home_rest is not None and home_rest <= SHORT_WEEK_MAX_DAYS
        row["away_short_week"] = away_rest is not None and away_rest <= SHORT_WEEK_MAX_DAYS
        out.append(row)
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


# ── box scores, so preseason can be graded at all ────────────────────────────
#
# The stat lines come back as parallel `labels` and `stats` arrays per athlete,
# grouped by category. PARSED BY LABEL, NEVER BY INDEX: ESPN reorders and adds
# columns without notice, and a fixed offset would silently start reading
# rushing average as rushing yards rather than failing loudly.
#
# The keys emitted here are nflverse's, not ESPN's, so the same OUTCOME
# expressions the backtest grades on apply unchanged to a preseason line.
_WANT = {
    "passing":   {"YDS": "passing_yards"},
    "rushing":   {"CAR": "carries", "YDS": "rushing_yards", "TD": "rushing_tds"},
    "receiving": {"REC": "receptions", "YDS": "receiving_yards", "TD": "receiving_tds"},
    "kicking":   {"XP": "_xp", "FG": "_fg"},
}
_STATS = ("passing_yards", "carries", "rushing_yards", "rushing_tds",
          "receptions", "receiving_yards", "receiving_tds", "fg_made", "pat_made")


def _num(v: Any) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return 0.0


def _made(v: Any) -> float:
    """ESPN publishes kicking as MADE/ATT ("2/3"). Only made counts."""
    s = str(v or "")
    return _num(s.split("/")[0]) if "/" in s else _num(s)


def box_score(game_id: str) -> list[dict[str, Any]]:
    """Per-player stat lines for one game, in nflverse column names.

    Returns [] on any failure — the grader treats an unreachable game as
    ungraded, never as a slate of zeros.
    """
    url = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
           f"?event={game_id}")
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if not r.ok:
            return []
        data = r.json()
    except Exception:
        return []

    rows: dict[str, dict[str, Any]] = {}
    for team_blk in ((data.get("boxscore") or {}).get("players") or []):
        team = _abbr(((team_blk.get("team") or {}).get("abbreviation")) or "")
        for cat in (team_blk.get("statistics") or []):
            want = _WANT.get(str(cat.get("name") or "").lower())
            if not want:
                continue
            labels = [str(x).upper() for x in (cat.get("labels") or [])]
            idx = {lab: i for i, lab in enumerate(labels)}
            for a in (cat.get("athletes") or []):
                ath = a.get("athlete") or {}
                eid = str(ath.get("id") or "")
                if not eid:
                    continue
                row = rows.setdefault(eid, {
                    "espn_id": eid,
                    "name": ath.get("displayName") or "",
                    "team": team,
                    **{k: 0.0 for k in _STATS},
                })
                stats = a.get("stats") or []
                for lab, col in want.items():
                    i = idx.get(lab)
                    if i is None or i >= len(stats):
                        continue
                    if col == "_xp":
                        row["pat_made"] = _made(stats[i])
                    elif col == "_fg":
                        row["fg_made"] = _made(stats[i])
                    else:
                        row[col] = _num(stats[i])
    return list(rows.values())


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
