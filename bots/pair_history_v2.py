#!/usr/bin/env python3
"""
pairhist_v2 -- four seasons of pair history, ACTIVE players only, for MLB (home
runs), NHL (goals) and NFL (touchdowns).

A NEW MODEL VERSION, NEW FILES. This does not touch bots/pair_history_cache.py
(the existing one-season same-day pair file, pair_history_summary.json), the
pair generator, or any pool. It writes three files the site reads:

    pairhist_v2_mlb.json   pairhist_v2_nhl.json   pairhist_v2_nfl.json

WHAT A PAIR IS (kept from the existing file): two players who each did the thing
(homered / scored a goal / scored a touchdown) on the SAME SLATE DAY, whatever
team they play for. The slate day is the calendar date for MLB and NHL and the
NFL week for football (one slate, as a Sunday is). "Same game" is the rarer
subset where both did it in one game. v2 adds what v1 never had, the denominator:

    joint_days        days BOTH played (both appeared in a game that day)
    joint_event_days  of those, days BOTH did it
    same_game_event_days  of those, days both did it in the same game
    rate              joint_event_days / joint_days
    expected_joint    what the two players' own rates would give if the two were
                      unrelated: joint_days * rate_a * rate_b (window-wide rates)
    lift              joint_event_days / expected_joint

ACTIVE (the thresholds are the named constants below; tests pin them):
  1. he is on a current roster (the sport's own roster feed), AND
  2. he clears ONE of
       SEASON_GATE        games played >= 25% of the season's games in the CURRENT
                          season (so a guy who is playing now counts), or
       WINDOW_GATE        games played >= 37.5% of all the games in the window, or
       CONSISTENT         >= 3 of the 4 seasons each with >= 15% of the season's
                          games (a part-timer who does it every year).
A season's games = the most games any one team has played that season in the data.

Nothing here is invented: every row is a real game log line (MLB StatsAPI
boxscores, NHL stats API per-game skater report, nflverse weekly player stats via
nflreadpy). A sport with fewer seasons of data publishes what exists and says how
many in `seasons_covered`.

    python3 bots/pair_history_v2.py --sport mlb|nhl|nfl|all [--out DIR] [--cache DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

MODEL_VERSION = "pairhist_v2"
SCHEMA = "pairhist_v2"

# ---- named thresholds (the owner's call, 10-07: a quarter to 3/8 of the season) ----
WINDOW_SEASONS = 4
SEASON_GATE = 0.25            # games played / season games, in the CURRENT season
WINDOW_GATE = 0.375           # games played / season games, across the whole window
CONSISTENT_MIN_SEASONS = 3    # "consistently" = at least this many seasons ...
CONSISTENT_SEASON_FRAC = 0.15  # ... each with at least this share of the season
PAIR_CAP = 300                # most pairs written per sport
# a pair needs both: enough shared days to mean something, and the thing done together at least twice
MIN_JOINT_DAYS = {"mlb": 40, "nhl": 40, "nfl": 15}
MIN_JOINT_EVENT_DAYS = 2
LAST_DATES_KEPT = 5

SPORT_WORDS = {
    "mlb": {"unit": "home run", "unit_plural": "home runs", "short": "HR", "day": "game day"},
    "nhl": {"unit": "goal", "unit_plural": "goals", "short": "G", "day": "game day"},
    "nfl": {"unit": "touchdown", "unit_plural": "touchdowns", "short": "TD", "day": "week"},
}

DEFINITION = {
    "mlb": ("A pair is two hitters who each homered on the same date, on any team. Joint games are the dates both "
            "played. Same game is the subset where both homered in one game."),
    "nhl": ("A pair is two skaters who each scored a goal on the same date, on any team. Joint games are the dates both "
            "played. Same game is the subset where both scored in one game."),
    "nfl": ("A pair is two players who each scored a rushing or receiving touchdown in the same week, on any team. Joint "
            "games are the weeks both had a stat line. Same game is the subset where both scored in one game. "
            "Passing touchdowns are not counted."),
}


# ------------------------------------------------------------------ records
# One record per player per game he appeared in. Plain dicts, so tests build them by hand:
#   {season: int, day: str, date: 'YYYY-MM-DD', game: str, team: str, pid: int|str, name: str, events: int}
# `day` is the slate key (the date for MLB/NHL, 'YYYY-wNN' for the NFL); `events` is the count that game.


def season_game_counts(records: Iterable[Dict[str, Any]]) -> Dict[int, int]:
    """{season: games in it} = the most distinct games any single team has played that season."""
    per_team: Dict[Tuple[int, str], Set[str]] = defaultdict(set)
    for r in records:
        per_team[(r["season"], r["team"])].add(r["game"])
    out: Dict[int, int] = defaultdict(int)
    for (season, _team), games in per_team.items():
        out[season] = max(out[season], len(games))
    return dict(out)


def games_played(records: Iterable[Dict[str, Any]]) -> Dict[Any, Dict[int, int]]:
    """{pid: {season: distinct games he appeared in}}"""
    seen: Dict[Any, Dict[int, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in records:
        seen[r["pid"]][r["season"]].add(r["game"])
    return {pid: {s: len(g) for s, g in by.items()} for pid, by in seen.items()}


def qualifies(gp_by_season: Dict[int, int], season_games: Dict[int, int], current_season: int) -> Tuple[bool, str]:
    """Does this player clear the games-played gate? (the roster check is separate)
    Returns (ok, which rule let him in)."""
    def share(season: int) -> float:
        total = season_games.get(season, 0)
        return (gp_by_season.get(season, 0) / total) if total else 0.0

    if share(current_season) >= SEASON_GATE:
        return True, "season"
    window_games = sum(season_games.values())
    if window_games and sum(gp_by_season.values()) / window_games >= WINDOW_GATE:
        return True, "window"
    steady = sum(1 for s in season_games if share(s) >= CONSISTENT_SEASON_FRAC)
    if steady >= CONSISTENT_MIN_SEASONS:
        return True, "consistent"
    return False, ""


def active_players(records: List[Dict[str, Any]], current_ids: Set[Any], current_season: int) -> Dict[Any, str]:
    """{pid: rule} for every player who is on a current roster AND clears the games gate."""
    sg = season_game_counts(records)
    gp = games_played(records)
    out: Dict[Any, str] = {}
    for pid, by in gp.items():
        if pid not in current_ids:
            continue
        ok, rule = qualifies(by, sg, current_season)
        if ok:
            out[pid] = rule
    return out


def pair_key(a: Any, b: Any) -> Tuple[Any, Any]:
    return (a, b) if str(a) <= str(b) else (b, a)


def build_pairs(records: List[Dict[str, Any]], active: Dict[Any, str], sport: str,
                min_joint_days: Optional[int] = None, min_event_days: int = MIN_JOINT_EVENT_DAYS,
                cap: int = PAIR_CAP) -> Dict[str, Any]:
    """Count every active pair. Returns {pairs: [...capped, ranked...], considered, ...}."""
    floor = MIN_JOINT_DAYS[sport] if min_joint_days is None else min_joint_days
    recs = [r for r in records if r["pid"] in active]

    # who played which slate day, who did it, in which game
    played: Dict[Any, Set[str]] = defaultdict(set)
    did: Dict[Any, Set[str]] = defaultdict(set)
    by_day_scorers: Dict[str, Dict[Any, Set[str]]] = defaultdict(lambda: defaultdict(set))  # day -> pid -> games he did it in
    day_date: Dict[str, str] = {}
    info: Dict[Any, Dict[str, Any]] = {}
    season_of_day: Dict[str, int] = {}
    for r in recs:
        pid = r["pid"]
        played[pid].add(r["day"])
        day_date[r["day"]] = min(day_date.get(r["day"], r["date"]), r["date"])
        season_of_day[r["day"]] = r["season"]
        prev = info.get(pid)
        if prev is None or r["date"] >= prev["date"]:
            info[pid] = {"name": r["name"], "team": r["team"], "date": r["date"]}
        if r["events"] > 0:
            did[pid].add(r["day"])
            by_day_scorers[r["day"]][pid].add(r["game"])

    # joint event days: only scorers can pair, so this stays small
    joint_events: Dict[Tuple[Any, Any], List[str]] = defaultdict(list)
    same_game: Dict[Tuple[Any, Any], int] = defaultdict(int)
    for day, scorers in by_day_scorers.items():
        for a, b in itertools.combinations(sorted(scorers, key=str), 2):
            k = pair_key(a, b)
            joint_events[k].append(day)
            if scorers[a] & scorers[b]:
                same_game[k] += 1

    seasons_in = sorted(set(season_of_day.values()))
    rows: List[Dict[str, Any]] = []
    considered = len(joint_events)
    for (a, b), event_days in joint_events.items():
        if len(event_days) < min_event_days:
            continue
        shared = played[a] & played[b]
        if len(shared) < floor:
            continue
        jd, je = len(shared), len(set(event_days) & shared)
        ra = len(did[a]) / len(played[a])
        rb = len(did[b]) / len(played[b])
        expected = jd * ra * rb
        by_season: Dict[str, List[int]] = {}
        for s in seasons_in:
            sj = sum(1 for d in shared if season_of_day[d] == s)
            se = sum(1 for d in event_days if season_of_day.get(d) == s)
            if sj:
                by_season[str(s)] = [sj, se]
        last = sorted((day_date[d] for d in event_days), reverse=True)
        rows.append({
            "pair_key": f"{a}|{b}",
            "players": [
                {"player_id": a, "name": info[a]["name"], "team": info[a]["team"]},
                {"player_id": b, "name": info[b]["name"], "team": info[b]["team"]},
            ],
            "joint_days": jd,
            "joint_event_days": je,
            "same_game_event_days": same_game.get((a, b), 0),
            "rate": round(je / jd, 4),
            "expected_joint": round(expected, 2),
            "lift": round(je / expected, 2) if expected > 0 else None,
            "seasons": by_season,
            "last_joint_date": last[0],
            "last_dates": last[:LAST_DATES_KEPT],
        })
    rows.sort(key=lambda p: (-p["joint_event_days"], -p["same_game_event_days"], -p["rate"], p["pair_key"]))
    return {"pairs": rows[:cap], "pairs_qualifying": len(rows), "pairs_considered": considered,
            "floor_joint_days": floor, "floor_joint_event_days": min_event_days}


def build_file(sport: str, records: List[Dict[str, Any]], current_ids: Set[Any], current_season: int,
               generated_at: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    active = active_players(records, current_ids, current_season)
    built = build_pairs(records, active, sport)
    sg = season_game_counts(records)
    out = {
        "schema": SCHEMA,
        "model_version": MODEL_VERSION,
        "sport": sport,
        "generated_at": generated_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": SPORT_WORDS[sport],
        "definition": DEFINITION[sport],
        "seasons_covered": sorted(sg),
        "season_games": {str(s): g for s, g in sorted(sg.items())},
        "current_season": current_season,
        "thresholds": {
            "season_gate": SEASON_GATE, "window_gate": WINDOW_GATE,
            "consistent_min_seasons": CONSISTENT_MIN_SEASONS, "consistent_season_frac": CONSISTENT_SEASON_FRAC,
            "min_joint_days": built["floor_joint_days"], "min_joint_event_days": built["floor_joint_event_days"],
            "pair_cap": PAIR_CAP,
        },
        "active_players": len(active),
        "active_rule_counts": {k: sum(1 for v in active.values() if v == k) for k in ("season", "window", "consistent")},
        "pairs_considered": built["pairs_considered"],
        "pairs_qualifying": built["pairs_qualifying"],
        "pairs": built["pairs"],
    }
    if extra:
        out.update(extra)
    return out


# ------------------------------------------------------------------ fetchers (real data only)
def _get_json(session: Any, url: str, params: Optional[Dict[str, Any]] = None, tries: int = 4) -> Any:
    last: Optional[Exception] = None
    for t in range(tries):
        try:
            r = session.get(url, params=params or {}, timeout=40)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # network flake: back off and retry
            last = exc
            time.sleep(4.0 * (t + 1))
    raise RuntimeError(f"failed {url}: {last}")


def fetch_mlb(seasons: List[int], cache_dir: Optional[Path]) -> Tuple[List[Dict[str, Any]], Set[Any], Dict[str, Any]]:
    import requests
    base = "https://statsapi.mlb.com/api/v1"
    s = requests.Session()
    records: List[Dict[str, Any]] = []
    games_seen = 0
    for season in seasons:
        cache_f = (cache_dir / f"mlb_{season}.json") if cache_dir else None
        cache: Dict[str, Any] = json.loads(cache_f.read_text()) if cache_f and cache_f.exists() else {}
        sched = _get_json(s, f"{base}/schedule", {"sportId": 1, "gameType": "R",
                          "startDate": f"{season}-02-15", "endDate": min(dt.date.today(), dt.date(season, 12, 31)).isoformat()})
        # a Postponed game keeps its original slot as abstract state Final with no box score: only played games count
        played_states = {"Final", "Completed Early", "Game Over"}
        finals = list({g["gamePk"]: (g["gamePk"], g.get("officialDate") or d["date"]) for d in sched.get("dates", [])
                       for g in d.get("games", [])
                       if (g.get("status") or {}).get("detailedState") in played_states}.values())
        todo = [pk for pk, _ in finals if str(pk) not in cache]

        def one(pk: int) -> Tuple[int, Any]:
            box = _get_json(s, f"{base}/game/{pk}/boxscore")
            rows = []
            for side in ("away", "home"):
                tm = (box.get("teams", {}).get(side) or {})
                abbr = ((tm.get("team") or {}).get("abbreviation") or (tm.get("team") or {}).get("name") or "")
                for p in (tm.get("players") or {}).values():
                    b = ((p.get("stats") or {}).get("batting") or {})
                    if int(b.get("plateAppearances") or b.get("atBats") or 0) <= 0:
                        continue
                    per = p.get("person") or {}
                    rows.append([per.get("id"), per.get("fullName"), abbr, int(b.get("homeRuns") or 0)])
            return pk, rows

        with ThreadPoolExecutor(max_workers=8) as ex:
            for pk, rows in ex.map(one, todo):
                cache[str(pk)] = rows
        if cache_f and todo:
            cache_f.parent.mkdir(parents=True, exist_ok=True)
            cache_f.write_text(json.dumps(cache))
        for pk, date in finals:
            games_seen += 1
            for pid, name, team, hr in cache.get(str(pk), []):
                records.append({"season": season, "day": date, "date": date, "game": str(pk), "team": team,
                                "pid": pid, "name": name, "events": hr})
        print(f"mlb {season}: {len(finals)} final games ({len(todo)} fetched)", file=sys.stderr)
    # current roster: every 40-man
    ids: Set[Any] = set()
    teams = _get_json(s, f"{base}/teams", {"sportId": 1, "season": seasons[-1]}).get("teams", [])
    for t in teams:
        ro = _get_json(s, f"{base}/teams/{t['id']}/roster", {"rosterType": "40Man"})
        ids.update(p["person"]["id"] for p in ro.get("roster", []))
    return records, ids, {"games_checked": games_seen, "roster_source": "MLB StatsAPI 40-man rosters"}


NHL_ROW_CAP = 10000   # the stats API silently stops at 10,000 rows (limit=-1 included), so windows are split below it


def _nhl_window(session: Any, url: str, lo: dt.date, hi: dt.date) -> List[Dict[str, Any]]:
    cay = f'gameDate>="{lo.isoformat()}" and gameDate<="{hi.isoformat()}" and gameTypeId=2'
    body = _get_json(session, url, {"isAggregate": "false", "isGame": "true", "start": 0, "limit": -1, "cayenneExp": cay})
    rows = body.get("data", [])
    time.sleep(1.0)   # the stats API rate-limits (429) a fast reader
    if len(rows) < NHL_ROW_CAP or lo >= hi:
        return rows
    mid = lo + (hi - lo) // 2
    return _nhl_window(session, url, lo, mid) + _nhl_window(session, url, mid + dt.timedelta(days=1), hi)


def fetch_nhl(seasons: List[int], cache_dir: Optional[Path]) -> Tuple[List[Dict[str, Any]], Set[Any], Dict[str, Any]]:
    """`seasons` are the START years (2025 = 2025-26)."""
    import requests
    s = requests.Session()
    stats = "https://api.nhle.com/stats/rest/en/skater/summary"
    records: List[Dict[str, Any]] = []
    games: Set[str] = set()
    for y in seasons:
        cache_f = (cache_dir / f"nhl_{y}.json") if cache_dir else None
        if cache_f and cache_f.exists():
            rows = json.loads(cache_f.read_text())
        else:
            rows = _nhl_window(s, stats, dt.date(y, 9, 1), dt.date(y + 1, 8, 31))
            rows = [[r["playerId"], r["skaterFullName"], r["teamAbbrev"], r["gameId"], r["gameDate"], int(r.get("goals") or 0)] for r in rows]
            # a season still being played is not cached whole
            if cache_f and y < seasons[-1]:
                cache_f.parent.mkdir(parents=True, exist_ok=True)
                cache_f.write_text(json.dumps(rows))
        for pid, name, team, gid, date, goals in rows:
            games.add(str(gid))
            records.append({"season": y, "day": date, "date": date, "game": str(gid), "team": team,
                            "pid": pid, "name": name, "events": goals})
        print(f"nhl {y}-{str(y + 1)[2:]}: {len(rows)} skater-games", file=sys.stderr)
    ids: Set[Any] = set()
    for team in sorted({r["team"] for r in records if r["season"] >= seasons[-2]}):
        try:
            ro = _get_json(s, f"https://api-web.nhle.com/v1/roster/{team}/current")
        except Exception:
            continue
        for grp in ("forwards", "defensemen"):
            ids.update(p["id"] for p in ro.get(grp, []))
    return records, ids, {"games_checked": len(games), "roster_source": "NHL api-web current rosters"}


def fetch_nfl(seasons: List[int], cache_dir: Optional[Path]) -> Tuple[List[Dict[str, Any]], Set[Any], Dict[str, Any]]:
    import nflreadpy as nfl
    import polars as pl
    records: List[Dict[str, Any]] = []
    games: Set[str] = set()
    for season in seasons:
        try:
            wk = nfl.load_player_stats(seasons=[season], summary_level="week").filter(pl.col("season_type") == "REG")
            sc = nfl.load_schedules(seasons=[season]).filter(pl.col("game_type") == "REG")
        except Exception as exc:
            print(f"nfl {season}: not available ({exc})", file=sys.stderr)
            continue
        date_of = {(g["week"], t): str(g["gameday"]) for g in sc.iter_rows(named=True) for t in (g["home_team"], g["away_team"])}
        for r in wk.iter_rows(named=True):
            w, team = int(r["week"]), r["team"]
            date = date_of.get((w, team))
            if not date or not r.get("player_id"):
                continue
            td = int((r.get("rushing_tds") or 0) + (r.get("receiving_tds") or 0))
            gid = f"{season}-w{w:02d}-" + "-".join(sorted([team, r.get("opponent_team") or ""]))
            games.add(gid)
            records.append({"season": season, "day": f"{season}-w{w:02d}", "date": date, "game": gid, "team": team,
                            "pid": r["player_id"], "name": r.get("player_display_name") or r.get("player_name"), "events": td})
        print(f"nfl {season}: {wk.height} player-weeks", file=sys.stderr)
    ids: Set[Any] = set()
    try:
        ro = nfl.load_rosters(seasons=[seasons[-1]])
        ids = {x for x in ro.filter(pl.col("status") == "ACT")["gsis_id"].to_list() if x}
    except Exception as exc:
        print(f"nfl roster failed: {exc}", file=sys.stderr)
    return records, ids, {"games_checked": len(games), "roster_source": "nflverse rosters (status ACT)"}


FETCHERS = {"mlb": fetch_mlb, "nhl": fetch_nhl, "nfl": fetch_nfl}


def season_window(sport: str, today: Optional[dt.date] = None) -> Tuple[List[int], int]:
    """The last WINDOW_SEASONS seasons, ending at the current one. NHL seasons are keyed by START year."""
    today = today or dt.date.today()
    if sport == "mlb":
        cur = today.year if today.month >= 3 else today.year - 1
    elif sport == "nhl":
        cur = today.year if today.month >= 9 else today.year - 1
    else:
        cur = today.year if today.month >= 9 else today.year - 1
    return list(range(cur - WINDOW_SEASONS + 1, cur + 1)), cur


def main() -> int:
    ap = argparse.ArgumentParser(description="pairhist_v2: four seasons of active-player pair history")
    ap.add_argument("--sport", default="all", choices=["mlb", "nhl", "nfl", "all"])
    ap.add_argument("--out", default="public/data")
    ap.add_argument("--cache", default="")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache) if args.cache else None
    for sport in (["mlb", "nhl", "nfl"] if args.sport == "all" else [args.sport]):
        seasons, cur = season_window(sport)
        records, ids, extra = FETCHERS[sport](seasons, cache)
        payload = build_file(sport, records, ids, cur, extra=extra)
        path = out / f"pairhist_v2_{sport}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        print(f"{sport}: {len(payload['pairs'])} pairs, {payload['active_players']} active, "
              f"seasons {payload['seasons_covered']} -> {path} ({path.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
