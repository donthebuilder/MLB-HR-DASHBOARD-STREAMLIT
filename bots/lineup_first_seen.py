"""
lineup_first_seen.py -- WHEN a lineup, a goalie or a starting five first showed up (and when it changed).

Why: the Card locks an hour before the first game. To know how early it can safely lock, we need to know how
early these things are actually confirmed. injury-snapshot.yml saves a snapshot twice a day, too coarse to time it.
This logs the first time each fact was seen, so the time can be measured from our own clock.

    python bots/lineup_first_seen.py observe <store_dir> [YYYY-MM-DD]

One file per day, <store_dir>/<day>.json, APPEND ONLY: an event is never rewritten. A new value (a goalie swap, a
changed batting order, "Expected" turning "Confirmed") is a NEW event. mins_before_start is first_seen vs the game's start.
  MLB  the batting order, once 9 are posted (statsapi)      NHL  ESPN's probable goalie
  NBA  the starting five and its Expected / Confirmed flag    NFL  no feed we can reach (inactives): not logged
A dead feed costs only its own sport. Reads nothing the board reads; writes nothing a pick or score reads.
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lineups  # noqa: E402


def _mins(start_utc: Optional[str], now: datetime) -> Optional[int]:
    try:
        return round((datetime.fromisoformat(start_utc.replace("Z", "+00:00")) - now).total_seconds() / 60)
    except Exception:
        return None


def _facts(sport: str, games: List[Dict[str, Any]], starters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The facts visible right now: [{sport, game_id, start_utc, team, what, value}]."""
    out = []
    for g in games:
        for s in g["sides"].values():
            base = {"sport": sport, "game_id": str(g["game_id"]), "start_utc": g.get("start_utc"), "team": s["team"]}
            if sport == "mlb" and s.get("posted"):
                out.append({**base, "what": "lineup", "value": hashlib.sha1(json.dumps(s["order"], sort_keys=True).encode()).hexdigest()[:10]})
            elif sport == "nhl" and s.get("probable_goalie"):
                out.append({**base, "what": "goalie", "value": s["probable_goalie"]})
            elif sport == "nba" and starters.get(s["team"]):
                st = starters[s["team"]]
                out.append({**base, "what": "starters", "value": f"{st.get('status')}:{'|'.join(st.get('players') or [])}"})
    return out


def observe(day: str, store_dir: str, now: Optional[datetime] = None, fetch: Optional[Dict[str, Callable]] = None) -> List[Dict[str, Any]]:
    """Log what is new since the last look. Returns the new events (empty when nothing changed). `fetch` is the test seam."""
    now = now or datetime.now(timezone.utc)
    f = fetch or {"mlb": lambda d: lineups.mlb(d), "nhl": lambda d: lineups.espn("nhl", d), "nba": lambda d: lineups.espn("nba", d),
                  "nba_starters": lambda d: lineups.nba_starters(d)}
    path = Path(store_dir) / f"{day}.json"
    have = json.loads(path.read_text()) if path.exists() else {"day": day, "events": []}
    seen = {(e["sport"], e["game_id"], e["team"], e["what"], e["value"]) for e in have["events"]}
    new = []
    for sport in ("mlb", "nhl", "nba"):
        try:
            games = f[sport](day)
            starters = f["nba_starters"](day) if sport == "nba" else {}
            for x in _facts(sport, games, starters):
                if x["start_utc"] and _mins(x["start_utc"], now) is not None and _mins(x["start_utc"], now) < -30:
                    continue                                    # a game long under way: nothing left to learn about when it was confirmed
                k = (x["sport"], x["game_id"], x["team"], x["what"], x["value"])
                if k not in seen:
                    seen.add(k)
                    new.append({**x, "first_seen_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mins_before_start": _mins(x["start_utc"], now)})
        except Exception as e:                                  # one dead feed must not cost the others
            print(f"{sport}: failed ({e})", file=sys.stderr)
    if new:
        have["events"].extend(new)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(have, indent=1))
    return new


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "observe":
        from zoneinfo import ZoneInfo
        day = sys.argv[3] if len(sys.argv) > 3 else datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        got = observe(day, sys.argv[2])
        print(f"{len(got)} new" + "".join(f"\n  {e['sport']} {e['team']} {e['what']} {e['mins_before_start']} min before start" for e in got))
        sys.exit(0)
    print(__doc__)
