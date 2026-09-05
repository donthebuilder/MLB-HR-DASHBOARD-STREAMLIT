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
import re
from typing import Any

import requests

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
TIMEOUT = 30

# nflverse abbreviations differ from ESPN's in three places.
FIX = {"WSH": "WAS", "LAR": "LA", "JAX": "JAX"}


# ── IS THE BALL INSIDE THE TWENTY (2026-09-01) ──────────────────────────────
#
# This used to be one line -- bool(situation.get("isRedZone")) -- and it had
# two problems, one of which was a guess and one of which was a bug.
#
# THE FIELD NAME WAS A GUESS. The comment in fetch() said so honestly: not
# confirmed against a live game, only inferred. The site's own
# lib/nfl/liveSlate.js had independently inferred the SAME name, which is two
# guesses agreeing and not evidence.
#
# ESPN was read directly on 2026-09-01. No football was live anywhere, so the
# live block still could not be seen -- but a COMPLETED game's drive data uses
# the same naming family, and it confirms, spelled exactly this way:
# downDistanceText, shortDownDistanceText, and yardsToEndzone.
#
# THE bool() WAS A REAL BUG. bool("false") is True in Python. So is bool("0")
# and bool("no"). If ESPN ever sent that flag as a string, every drive of every
# game would have been flagged a red zone -- the failure that is worse than not
# firing, because it is loud and wrong rather than quiet and wrong.
#
# Three signals now, any one of which is enough:
#   1. the flag, as an ALLOWLIST (still the inferred one)
#   2. yardsToEndzone <= 20 -- the actual definition of the red zone, a number
#      rather than somebody's boolean, and a confirmed field name
#   3. the down-and-distance text saying "Goal"
_GOAL_TO_GO = re.compile(r"(?:&|\band)\s*goal\b", re.I)


def _yards_to_endzone(situation: dict) -> int | None:
    """Yards to the end zone, or None when the feed did not say.

    None and 0 are different answers and must not be conflated: 0 is the goal
    line. bool is rejected explicitly because float(False) is 0.0, which would
    otherwise arrive as a goal-line stand.
    """
    raw = situation.get("yardsToEndzone")
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 100 else None


def _red_zone(situation: dict, down_distance: str | None) -> bool:
    """True only when something actually says so. Never truthiness."""
    if not situation:
        return False
    flag = situation.get("isRedZone")
    if flag is None:
        flag = situation.get("inRedZone")
    if flag is True or flag == 1 or (isinstance(flag, str) and flag.strip().lower() == "true"):
        return True
    yards = _yards_to_endzone(situation)
    if yards is not None and yards <= 20:
        return True
    return bool(down_distance and _GOAL_TO_GO.search(down_distance))


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
            # docs (there are none). The NAMES were originally inferred
            # rather than seen; two of the three are now confirmed against a
            # real ESPN football payload (a completed game's drive data, same
            # naming family as the live block): downDistanceText,
            # shortDownDistanceText and yardsToEndzone all exist and are
            # spelled exactly this way. See _red_zone() above for why the
            # red-zone call no longer rests on the one still inferred.
            # Every read is `.get()`, nothing here raises, and a shape this
            # does not recognise still yields down_distance=None and
            # red_zone=False rather than a guess.
            situation = comp.get("situation") or {}
            row["down_distance"] = situation.get("downDistanceText") or situation.get("shortDownDistanceText")
            # Published so the site can show it and so a future reader can see
            # what the red-zone call was actually made on.
            row["yards_to_endzone"] = _yards_to_endzone(situation)
            row["red_zone"] = _red_zone(situation, row["down_distance"])
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


def current_week(year: int | None = None) -> int | None:
    """The regular-season week ESPN's scoreboard is on RIGHT NOW, or None.

    Asked with no parameters the scoreboard answers for the week it is on and
    says which in its top-level `week.number`. It rolls to the next week on
    Wednesday 07:00Z (Tuesday midnight Phoenix), the day the bot's weekly
    spine opens -- so the Tuesday build and the Monday-night grade see
    different numbers, and both are right.

    NO `dates` AND NO `seasontype` -- verified against the live endpoint on
    2026-09-05. Asked with dates=2026&seasontype=2 and no week, ESPN answers
    week 18 and a game from January (the 2025 season's last week falls in
    calendar 2026). Asked plainly it answers week 1 with the Sep 10 opener as
    its first event. The year is only used to refuse an answer from another
    season, and the event's own season type refuses a preseason or playoff
    week.

    (Until this existed the scheduled workflow, which passes no --week,
    built a "Week None" slate out of the whole season's schedule in week
    mode and nfl_results.py exited before grading anything.)
    """
    try:
        r = requests.get(SCOREBOARD, params={"limit": 1}, timeout=TIMEOUT)
        if not r.ok:
            return None
        payload = r.json()
        n = (payload.get("week") or {}).get("number")
        ev = (payload.get("events") or [{}])[0]
        season = ev.get("season") or {}
        if not n or int(season.get("type") or 0) != 2:
            return None
        if year and int(season.get("year") or 0) not in (0, int(year)):
            return None
        return int(n)
    except Exception:
        return None


# ESPN's own calendar (read 2026-09-05): Week 1 is Sep 6-15, Week 2 opens
# Sep 16 (Wed 07:00Z). The fallback only matters when ESPN is unreachable,
# and it must roll on the same day ESPN does or a Tuesday build would grade
# the wrong week. Anchor on the first Wednesday.
SEASON_OPEN = {2026: dt.date(2026, 9, 9)}


def week_from_date(year: int, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    start = SEASON_OPEN.get(year, dt.date(year, 9, 8))
    return max(1, min(18, (today - start).days // 7 + 1))


def resolve_week(year: int, asked: int | None) -> int:
    """--week if given, else ESPN's current week, else the calendar."""
    if asked:
        return int(asked)
    w = current_week(year)
    if w:
        print(f"  week {w} (ESPN's current week)")
        return w
    w = week_from_date(year)
    print(f"  week {w} (from the calendar -- ESPN unreachable)")
    return w


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
