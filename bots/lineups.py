"""
lineups.py -- who is actually in the game tonight, and the lines that go with it.

    python bots/lineups.py mlb|nhl|nba|nfl [YYYY-MM-DD]

Per sport, what the public feeds really give us:
  MLB  posted batting orders (statsapi), with the spot, and the probable starters.
  NHL  ESPN's probable starting goalie per team (it is a probable, not a confirmation).
  NBA  no pregame starting-five feed we can reach; availability comes from injury_status.py.
  NFL  no pregame inactives feed we can reach; same.
  All  start time, status, and the book's spread / total where ESPN carries it (the input the
       pregame forecast will use).

CHANCE HE STARTS (MLB, measured on 7,557 graded board players, 2026):
    lineup posted and he is in it   plays 98%  (2.0% never came to the plate)
    lineup NOT posted at lock       plays 76%  (23.9% never came to the plate, n=213)
    lineup posted and he is not in it   0%
apply_mlb() multiplies these into p_play so an unposted bat is discounted instead of trusted.

LOCK LATER: lock_decision() says WAIT while a lineup is still unposted and first pitch is more than
LOCK_FLOOR_MIN away, LOCK once it is posted, and LOCK_UNCONFIRMED at the floor (flagged, with the
76% already in the number). pick_lock.py still freezes at first pitch; this is the rule it would call.
"""
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

P_IN_LINEUP = 0.98
P_UNPOSTED = 0.76
P_NOT_IN_POSTED = 0.0
LOCK_FLOOR_MIN = 20

MLB = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}&hydrate=lineups,probablePitcher,team"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{}/scoreboard?dates={}"
LEAGUE = {"nhl": "hockey/nhl", "nba": "basketball/nba", "nfl": "football/nfl", "mlb": "baseball/mlb"}


def _get(url: str) -> Dict[str, Any]:
    raw = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True).stdout
    return json.loads(raw)


def mlb(day: str) -> List[Dict[str, Any]]:
    out = []
    for dte in _get(MLB.format(d=day)).get("dates", []):
        for g in dte.get("games", []):
            lu = g.get("lineups") or {}
            sides = {}
            for side, key in (("away", "awayPlayers"), ("home", "homePlayers")):
                team = g["teams"][side]["team"]
                players = lu.get(key) or []
                sides[side] = {"team": team.get("abbreviation") or team.get("name"), "posted": len(players) >= 9,
                               "order": {str(p["id"]): i + 1 for i, p in enumerate(players)},
                               "probable": (g["teams"][side].get("probablePitcher") or {}).get("fullName")}
            out.append({"sport": "mlb", "game_id": g["gamePk"], "start_utc": g.get("gameDate"),
                        "status": g["status"]["detailedState"], "sides": sides,
                        "lineup_posted": all(s["posted"] for s in sides.values())})
    return out


def espn(sport: str, day: str) -> List[Dict[str, Any]]:
    out = []
    for e in _get(ESPN.format(LEAGUE[sport], day.replace("-", ""))).get("events", []):
        c = e["competitions"][0]
        odds = (c.get("odds") or [{}])[0]
        sides = {}
        for x in c["competitors"]:
            pr = x.get("probables") or []
            sides[x["homeAway"]] = {"team": x["team"]["abbreviation"],
                                    "probable_goalie": next((p["athlete"]["displayName"] for p in pr
                                                             if p.get("name") == "probableStartingGoalie"), None)}
        out.append({"sport": sport, "game_id": e["id"], "start_utc": e.get("date"),
                    "status": e["status"]["type"]["description"], "sides": sides, "lineup_posted": False,
                    "spread": odds.get("details"), "total": odds.get("overUnder")})
    return out


def fetch(sport: str, day: Optional[str] = None) -> List[Dict[str, Any]]:
    day = day or date.today().isoformat()
    return mlb(day) if sport == "mlb" else espn(sport, day)


def apply_mlb(rows: List[Dict[str, Any]], games: List[Dict[str, Any]]) -> Dict[str, int]:
    """Stamp lineup_status / p_start on scored rows (keyed by game_pk + MLB player_id) and fold p_start into p_play."""
    by_pk = {g["game_id"]: g for g in games}
    n = {"in lineup": 0, "not in posted lineup": 0, "lineup not posted": 0, "game not found": 0}
    for r in rows:
        g = by_pk.get(int(r.get("game_pk") or 0))
        status, p = "game not found", P_UNPOSTED
        if g:
            side = next((s for s in g["sides"].values() if s["team"] == r.get("team")), None)
            if side and side["posted"]:
                spot = side["order"].get(str(r.get("player_id")))
                status, p = ("in lineup", P_IN_LINEUP) if spot else ("not in posted lineup", P_NOT_IN_POSTED)
                if spot:
                    r["lineup_spot_live"] = spot
            elif side:
                status, p = "lineup not posted", P_UNPOSTED
        r["lineup_status"], r["p_start"] = status, p
        r["p_play"] = round(r.get("p_play", 1.0) * p, 4)
        if "final_score" in r:
            r["expected_score"] = round(r["final_score"] * r["p_play"], 2)
        n[status] += 1
    return n


def lock_decision(game: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """WAIT | LOCK | LOCK_UNCONFIRMED | PAST_START"""
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(game["start_utc"].replace("Z", "+00:00"))
    mins = (start - now).total_seconds() / 60.0
    if mins <= 0:
        return "PAST_START"
    if game.get("lineup_posted"):
        return "LOCK"
    return "LOCK_UNCONFIRMED" if mins <= LOCK_FLOOR_MIN else "WAIT"


def snapshot(out_dir: str) -> List[str]:
    """Save today's games with the book's lines, probable goalies and posted lineups, for all four sports.
    Lines cannot be re-fetched after the game, so the night they are not kept is a night of price history lost."""
    from pathlib import Path
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    written = []
    for sport in LEAGUE:
        try:
            games = espn(sport, date.today().isoformat())
            if sport == "mlb":
                pk = {g["game_id"]: g for g in mlb(date.today().isoformat())}
                for g in games:                 # ESPN ids differ from statsapi: attach by team pair + start time
                    for q in pk.values():
                        if {s["team"] for s in q["sides"].values()} >= {s["team"] for s in g["sides"].values()}:
                            g["lineups"] = q["sides"]; g["lineup_posted"] = q["lineup_posted"]
            path = d / f"games_{sport}_{stamp}.json"
            path.write_text(json.dumps(games, indent=1))
            written.append(str(path))
        except Exception as e:                  # one dead feed must not cost the other three
            print(f"{sport}: failed ({e})", file=sys.stderr)
    return written


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "snapshot":
        for w in snapshot(sys.argv[2]):
            print("wrote", w)
        sys.exit(0)
    sport = sys.argv[1] if len(sys.argv) > 1 else "mlb"
    day = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
    for g in fetch(sport, day):
        s = g["sides"]
        bits = " | ".join(f"{v['team']}: " + (f"lineup {'posted' if v.get('posted') else 'NOT posted'}" if sport == "mlb"
                                              else f"goalie {v.get('probable_goalie') or '?'}") for v in s.values())
        extra = f"  spread {g.get('spread')}  total {g.get('total')}" if sport != "mlb" else ""
        print(f"{g['status']:<12} {bits}{extra}   -> {lock_decision(g) if g['status'] not in ('Final', 'In Progress') else ''}")


NBA_TO_ESPN = {"WAS": "WSH", "SAS": "SA", "NOP": "NO", "GSW": "GS", "NYK": "NY", "UTA": "UTAH"}


def nba_starters(day: str) -> Dict[str, Dict[str, Any]]:
    """NBA's own daily lineups feed: {ESPN team abbrev: {"status": "Confirmed"|"Expected", "players": [names], "game_status": int}}.
    The five with a listed position are the starters; bench names carry no position. Empty dict if the feed is not up."""
    raw = subprocess.run(["curl", "-s", "-m", "30", "-A", "Mozilla/5.0", f"https://stats.nba.com/js/data/leaders/00_daily_lineups_{day.replace('-', '')}.json"],
                         capture_output=True, text=True).stdout
    try:
        games = json.loads(raw).get("games", [])
    except ValueError:
        return {}
    out = {}
    for g in games:
        for side in ("homeTeam", "awayTeam"):
            t = g[side]
            ab = NBA_TO_ESPN.get(t["teamAbbreviation"], t["teamAbbreviation"])
            ps = [p for p in t.get("players", []) if p.get("position")]
            if ps:
                out[ab] = {"status": ps[0].get("lineupStatus"), "players": [p["playerName"] for p in ps][:5], "game_status": g.get("gameStatus")}
    return out
