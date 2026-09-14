#!/usr/bin/env python3
"""nfl_fantasy_stats.py — the box scores FRANCHISE scores a week on.

WHY THIS EXISTS (2026-09-14). FRANCHISE scored every player ZERO for all of
Week 1. The site's lib/fantasy/nflFeed.js reads a per-player `game_stats`
block off the published slate and sync_nfl_week_feed writes it to
nfl_player_week_stats, where fantasy_points_for_stats() sums it -- and
nothing in this repo ever published that block. 535 players on the live
nfl_week.json, zero of them carrying a stat line. The whole downstream
pipeline (scheduler, sync, SQL scorer, matchup page) was working and summing
an empty object. Flagged in the site repo on 09-04 as the one piece of
football infrastructure blocking real scoring; never built.

WHY IT IS ITS OWN FILE AND ITS OWN WORKFLOW rather than a field on
nfl_week.json: the full NFL bot (nfl_bot.py) rebuilds the slate ~5 times on a
Sunday. A fantasy score that moves five times a day is not live scoring.
This script does one thing -- ESPN summary -> per-player fantasy stat line
-- takes seconds, needs only `requests`, and runs every 15 minutes in game
windows (.github/workflows/nfl-fantasy-stats.yml). The site merges it over
the slate on load.

OUTPUT: public/data/current/nfl_fantasy_stats.json

  {
    "season": 2026, "week": 1, "built_at": "...",
    "games":   [{game_id, week, state, completed, home, away,
                 home_score, away_score, kickoff}],
    "players": {"<gsis_id>": {passing_yards, passing_touchdowns,
                 interceptions, rushing_yards, rushing_touchdowns,
                 receptions, receiving_yards, receiving_touchdowns,
                 fumbles_lost, two_point_conversions, extra_points,
                 field_goals_0_39, field_goals_40_49, field_goals_50_plus,
                 return_touchdowns}},
    "defense": {"<TEAM>": {points_allowed, def_sacks, def_interceptions,
                 def_fumble_recoveries, def_touchdowns, def_safeties}}
  }

The player keys are exactly the canonical names in the site's
normalizeStats() / fantasy_points_for_stats(), so nothing is remapped
downstream. Only games that are LIVE or FINAL contribute a line: a game that
has not kicked off has no box score and its players are simply absent, which
the site already renders as "no points yet" rather than 0.

JOIN. ESPN keys athletes by its own id; FRANCHISE keys players by gsis id.
The published nfl_week.json already carries `espn_id` on 533 of 535 rows
(nfl_injuries.attach_espn_ids), so the crosswalk is read straight off the
slate this run is scoring -- no nflreadpy download, no second source of
truth. An ESPN line with no slate match is dropped and COUNTED in the log,
never name-matched.

WHAT IS PARSED BY KEY, NEVER BY INDEX. ESPN ships `keys` beside `labels` per
category ("passingYards", "rushingTouchdowns", ...). A fixed column offset
would silently start reading yards-per-carry as yards when ESPN adds a
column; a missing key is a loud 0 in the log instead.

FIELD GOALS BY DISTANCE come from `scoringPlays` text ("Jake Bates 51 Yd
Field Goal"), not the box score (which only has made/att). A made FG whose
distance line could not be found is counted in the 0-39 bucket -- the
LOWEST tier, 3 points -- so a kicker is never over-credited and never loses
a kick that the box score says he made. The log says when this happens.

TWO-POINT CONVERSIONS are also only in scoringPlays text: "(Bucky Irving
Run for Two-Point Conversion)" / "(Chris Godwin Pass From Baker Mayfield for
Two-Point Conversion)". The runner/receiver is credited; the passer is not
(the site's formula has one 2-pt term, the standard one).

D/ST: sacks and INTs from the defensive/interceptions categories summed per
team; fumble recoveries are the OPPONENT's fumbles_lost (a lost fumble is by
definition recovered by the other side -- ESPN's own `fumblesRecovered`
column mixes own and opponent recoveries); defensive and special-teams TDs
and safeties from scoringPlays; points_allowed is the opponent's score.

Never invents a number: anything ESPN did not say is absent or 0 with a
log line, never estimated.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

import requests

import nfl_espn

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
WEEK_JSON = ("https://raw.githubusercontent.com/donthebuilder/"
             "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current/nfl_week.json")
TIMEOUT = 30

PLAYER_KEYS = ("passing_yards", "passing_touchdowns", "interceptions",
               "rushing_yards", "rushing_touchdowns", "receptions",
               "receiving_yards", "receiving_touchdowns", "fumbles_lost",
               "two_point_conversions", "extra_points", "field_goals_0_39",
               "field_goals_40_49", "field_goals_50_plus", "return_touchdowns")

# ESPN category -> {ESPN key: our key}. Parsed by KEY (see module doc).
_WANT: dict[str, dict[str, str]] = {
    "passing":   {"passingYards": "passing_yards",
                  "passingTouchdowns": "passing_touchdowns",
                  "interceptions": "interceptions"},
    "rushing":   {"rushingYards": "rushing_yards",
                  "rushingTouchdowns": "rushing_touchdowns"},
    "receiving": {"receptions": "receptions",
                  "receivingYards": "receiving_yards",
                  "receivingTouchdowns": "receiving_touchdowns"},
    "fumbles":   {"fumblesLost": "fumbles_lost"},
    "kicking":   {"fieldGoalsMade/fieldGoalAttempts": "_fg_made",
                  "extraPointsMade/extraPointAttempts": "extra_points"},
    "kickreturns": {"kickReturnTouchdowns": "return_touchdowns"},
    "puntreturns": {"puntReturnTouchdowns": "return_touchdowns"},
}
_DEF_WANT: dict[str, dict[str, str]] = {
    "defensive":     {"sacks": "def_sacks"},
    "interceptions": {"interceptions": "def_interceptions"},
}

_FG_RE = re.compile(r"^(?P<name>.+?)\s+(?P<yds>\d+)\s+Yd\s+Field\s+Goal", re.I)
_TWO_PT_RE = re.compile(r"\((?P<name>[^()]+?)\s+(?:Run|Rush|Pass)\b[^()]*Two-Point", re.I)
_DEF_TD_RE = re.compile(r"(Interception|Fumble|Blocked|Punt|Kickoff|Kick)\s+(Return|Recovery)", re.I)


def _num(v: Any) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return 0.0


def _made(v: Any) -> float:
    """ESPN publishes kicking as MADE/ATT ("2/3"). Only made counts."""
    s = str(v or "")
    return _num(s.split("/")[0]) if "/" in s else _num(s)


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower().replace("jr", "").replace("sr", "").replace("iii", "").replace("ii", ""))


def fetch_summary(game_id: str) -> dict:
    try:
        r = requests.get(SUMMARY, params={"event": game_id}, timeout=TIMEOUT)
        if not r.ok:
            return {}
        return r.json() or {}
    except Exception:
        return {}


def parse_game(summary: dict, home: str, away: str, home_score: int, away_score: int) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """(players_by_espn_id, defense_by_team, notes) for one game.

    Pure: takes the summary dict, never fetches. Tested against a real
    completed Week 1 summary in tests/test_nfl_fantasy_stats.py.
    """
    notes: list[str] = []
    players: dict[str, dict] = {}
    # (team, normalized name) -> espn id, for the scoringPlays joins.
    by_name: dict[tuple[str, str], str] = {}
    defense: dict[str, dict] = {
        home: {"points_allowed": int(away_score), "def_sacks": 0.0, "def_interceptions": 0.0,
               "def_fumble_recoveries": 0.0, "def_touchdowns": 0.0, "def_safeties": 0.0},
        away: {"points_allowed": int(home_score), "def_sacks": 0.0, "def_interceptions": 0.0,
               "def_fumble_recoveries": 0.0, "def_touchdowns": 0.0, "def_safeties": 0.0},
    }
    fumbles_lost_by_team: dict[str, float] = {home: 0.0, away: 0.0}
    fg_made_by_id: dict[str, float] = {}

    for team_blk in ((summary.get("boxscore") or {}).get("players") or []):
        team = nfl_espn._abbr(((team_blk.get("team") or {}).get("abbreviation")) or "")
        for cat in (team_blk.get("statistics") or []):
            cname = str(cat.get("name") or "").lower()
            keys = [str(k) for k in (cat.get("keys") or [])]
            idx = {k: i for i, k in enumerate(keys)}
            want = _WANT.get(cname)
            dwant = _DEF_WANT.get(cname)
            if not want and not dwant:
                continue
            for a in (cat.get("athletes") or []):
                ath = a.get("athlete") or {}
                eid = str(ath.get("id") or "")
                if not eid:
                    continue
                stats = a.get("stats") or []
                if want:
                    row = players.setdefault(eid, {"_team": team, "_name": ath.get("displayName") or "",
                                                   **{k: 0.0 for k in PLAYER_KEYS}})
                    by_name[(team, _norm_name(ath.get("displayName") or ""))] = eid
                    for ek, col in want.items():
                        i = idx.get(ek)
                        if i is None or i >= len(stats):
                            notes.append(f"{team} {cname}: key {ek} missing from ESPN")
                            continue
                        if col == "_fg_made":
                            fg_made_by_id[eid] = _made(stats[i])
                        elif col == "extra_points":
                            row[col] = _made(stats[i])
                        else:
                            # A man can appear in two categories; the keys are
                            # disjoint per category, so plain assignment is
                            # right -- except return TDs, which come from two
                            # categories and SUM.
                            if col == "return_touchdowns":
                                row[col] += _num(stats[i])
                            else:
                                row[col] = _num(stats[i])
                    if cname == "fumbles":
                        i = idx.get("fumblesLost")
                        if i is not None and i < len(stats):
                            fumbles_lost_by_team[team] = fumbles_lost_by_team.get(team, 0.0) + _num(stats[i])
                if dwant and team in defense:
                    for ek, col in dwant.items():
                        i = idx.get(ek)
                        if i is None or i >= len(stats):
                            continue
                        defense[team][col] += _num(stats[i])

    # A lost fumble is the other side's recovery.
    if home in defense:
        defense[home]["def_fumble_recoveries"] = fumbles_lost_by_team.get(away, 0.0)
    if away in defense:
        defense[away]["def_fumble_recoveries"] = fumbles_lost_by_team.get(home, 0.0)

    # scoringPlays: FG distances, two-point conversions, defensive TDs, safeties.
    fg_seen: dict[str, float] = {}
    for p in (summary.get("scoringPlays") or []):
        text = str(p.get("text") or "")
        team = nfl_espn._abbr(((p.get("team") or {}).get("abbreviation")) or "")
        stype = str((p.get("scoringType") or {}).get("displayName") or "").lower()
        if stype == "field goal":
            m = _FG_RE.match(text)
            if not m:
                notes.append(f"FG text not parsed: {text!r}")
                continue
            eid = by_name.get((team, _norm_name(m.group("name"))))
            if not eid:
                notes.append(f"FG kicker not in box: {text!r}")
                continue
            yds = int(m.group("yds"))
            col = "field_goals_0_39" if yds <= 39 else "field_goals_40_49" if yds <= 49 else "field_goals_50_plus"
            players[eid][col] += 1
            fg_seen[eid] = fg_seen.get(eid, 0.0) + 1
        elif stype == "safety":
            if team in defense:
                defense[team]["def_safeties"] += 1
        elif stype == "touchdown":
            if _DEF_TD_RE.search(text) and team in defense:
                defense[team]["def_touchdowns"] += 1
            m2 = _TWO_PT_RE.search(text)
            if m2:
                eid = by_name.get((team, _norm_name(m2.group("name"))))
                if eid:
                    players[eid]["two_point_conversions"] += 1
                else:
                    notes.append(f"2-pt player not in box: {text!r}")

    # Reconcile FG counts: the box score's made total is the source of truth
    # for HOW MANY; scoringPlays only tells us how far. Unplaced kicks land
    # in the lowest tier, never dropped, never promoted.
    for eid, made in fg_made_by_id.items():
        placed = fg_seen.get(eid, 0.0)
        if made > placed:
            players[eid]["field_goals_0_39"] += made - placed
            notes.append(f"{players[eid]['_name']}: {int(made - placed)} FG without a distance line -> 0-39 tier")
        elif placed > made:
            notes.append(f"{players[eid]['_name']}: scoringPlays has {int(placed)} FG, box says {int(made)} -- keeping the plays")

    return players, defense, notes


def _slate_crosswalk(week_json: dict) -> tuple[dict[str, str], int, int]:
    """(espn_id -> gsis player_id, season, week) off the published slate."""
    xw: dict[str, str] = {}
    for p in (week_json.get("players") or []):
        e, g = str(p.get("espn_id") or ""), str(p.get("player_id") or "")
        if e and g:
            xw[e] = g
    return xw, int(week_json.get("season") or 0), int(week_json.get("week") or 0)


def build(week_json: dict, year: int, week_override: int | None = None) -> dict:
    xw, slate_season, slate_week = _slate_crosswalk(week_json)
    week = week_override or nfl_espn.current_week(year) or slate_week
    season = year or slate_season
    games = nfl_espn.fetch(seasontype=2, week=week, year=season)
    print(f"  season {season} week {week}: {len(games)} game(s) on ESPN, {len(xw)} slate ids to join")

    out_games, players_out, defense_out = [], {}, {}
    joined = unjoined = 0
    for g in games:
        state = g.get("state")
        out_games.append({k: g.get(k) for k in ("game_id", "week", "kickoff", "home", "away",
                                                 "home_score", "away_score", "state", "completed")})
        if state not in ("in", "post"):
            continue
        summary = fetch_summary(g["game_id"])
        if not summary:
            print(f"  {g['away']}@{g['home']}: summary unavailable -- no line this run")
            continue
        players, defense, notes = parse_game(summary, g["home"], g["away"],
                                             int(g.get("home_score") or 0), int(g.get("away_score") or 0))
        for team, line in defense.items():
            defense_out[team] = {k: (int(v) if k == "points_allowed" else v) for k, v in line.items()}
        for eid, row in players.items():
            gsis = xw.get(eid)
            if not gsis:
                unjoined += 1
                continue
            joined += 1
            players_out[gsis] = {k: row[k] for k in PLAYER_KEYS}
        for n in notes[:8]:
            print(f"    note {g['away']}@{g['home']}: {n}")
        print(f"  {g['away']}@{g['home']} [{state}] {len(players)} lines")
    print(f"  joined {joined} line(s) to slate ids; {unjoined} ESPN line(s) had no slate match (dropped, not name-matched)")
    return {
        "season": season, "week": week,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "espn",
        "games": out_games,
        "players": players_out,
        "defense": defense_out,
        "counts": {"games": len(out_games), "scored_games": sum(1 for g in out_games if g["state"] in ("in", "post")),
                   "players": len(players_out), "unjoined": unjoined},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--out", default="public/data/current")
    ap.add_argument("--slate", default=None, help="local nfl_week.json instead of the data branch")
    a = ap.parse_args()

    if a.slate:
        week_json = json.loads(Path(a.slate).read_text())
    else:
        try:
            r = requests.get(WEEK_JSON, timeout=TIMEOUT)
            r.raise_for_status()
            week_json = r.json()
        except Exception as exc:
            print(f"slate unavailable ({type(exc).__name__}: {exc}); nothing to join against")
            return 1
    payload = build(week_json, a.season, a.week)
    if not payload["games"]:
        print("no games from ESPN -- refusing to publish an empty file over a good one")
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "nfl_fantasy_stats.json").write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {out / 'nfl_fantasy_stats.json'}: {payload['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
