"""
game_lines.py -- the book's line for a game, and what a lopsided line means. One module for every sport.

This is the bot-side home of the site's blowout / moneyline work (site: lib/blowout.js, lib/nba/favourites.js,
scripts/measure-*.mjs). The rules are Donovan's, carried over unchanged:

  BLOWOUT_SPREAD   a game is a BLOWOUT WATCH when the book has one club favoured by at least this many points
                   (NBA 10, NFL 10). NHL has no pregame watch: the puck line is always 1.5 and a -300 favourite wins
                   by 4+ no more often than a -150 one (13-14%, 2,722 games).
  BLOWOUT_FINAL    the final margin that counts as a blowout in a player's own history (NBA 15, NFL 21, NHL 4).

And one thing the site could not do: HISTORY. ESPN's core odds feed keeps each game's CLOSING spread and moneylines,
so every past game can be paired with the line it actually closed at (close_line / fetch_closing). That is what turns
"the book has them by 12" into a measured probability for a star's night (see blowout_lines_nba.py).
"""
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional

BLOWOUT_SPREAD = {"nba": 10.0, "nfl": 10.0}
BLOWOUT_FINAL = {"nba": 15, "nfl": 21, "nhl": 4}
LEAGUE_PATH = {"nba": "basketball/leagues/nba", "nfl": "football/leagues/nfl", "nhl": "hockey/leagues/nhl", "mlb": "baseball/leagues/mlb"}
ODDS = "https://sports.core.api.espn.com/v2/sports/{p}/events/{e}/competitions/{e}/odds"


def american(v) -> Optional[float]:
    s = str(v if v is not None else "").strip().upper()
    if s in ("EVEN", "EV"):
        return 100.0
    try:
        return float(s.lstrip("+")) if s else None
    except ValueError:
        return None


def implied(ml: Optional[float]) -> Optional[float]:
    """American moneyline -> raw win probability (vig still in)."""
    if ml is None:
        return None
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def novig(ml_home: Optional[float], ml_away: Optional[float]) -> Optional[float]:
    """The home club's win probability with the book's margin taken out."""
    h, a = implied(ml_home), implied(ml_away)
    return h / (h + a) if h and a else None


def blowout_watch(sport: str, spread_home: Optional[float]) -> Optional[Dict]:
    """None if the sport has no watch number or there is no line; else {watch, spread, favourite: 'home'|'away'|None}."""
    at = BLOWOUT_SPREAD.get(sport)
    if not at or spread_home is None:
        return None
    fav = "home" if spread_home < 0 else "away" if spread_home > 0 else None
    return {"watch": abs(spread_home) >= at, "spread": abs(spread_home), "favourite": fav, "at": at}


def close_line(sport: str, event_id: str, cache: Optional[Path] = None) -> Optional[Dict]:
    """The provider-priority-first NON-live line: {spread_home, ml_home, ml_away, total, provider}."""
    f = cache / f"line_{sport}_{event_id}.json" if cache else None
    if f and f.exists():
        return json.loads(f.read_text()) or None
    out, fetched = None, False
    for _ in range(3):
        raw = subprocess.run(["curl", "-s", "-m", "30", ODDS.format(p=LEAGUE_PATH[sport], e=event_id)],
                             capture_output=True, text=True).stdout
        if raw.startswith("{"):
            fetched = True
            items = [i for i in json.loads(raw).get("items", []) if "live" not in (i.get("provider", {}).get("name", "")).lower()]
            items.sort(key=lambda i: i.get("provider", {}).get("priority", 99))
            if items:
                it = items[0]
                h, a = it.get("homeTeamOdds", {}), it.get("awayTeamOdds", {})
                sp = american((h.get("close", {}).get("pointSpread") or {}).get("american"))
                if sp is None:
                    asp = american((a.get("close", {}).get("pointSpread") or {}).get("american"))
                    sp = -asp if asp is not None else it.get("spread")
                out = {"spread_home": sp, "ml_home": american((h.get("close", {}).get("moneyLine") or {}).get("american")) or american(h.get("moneyLine")),
                       "ml_away": american((a.get("close", {}).get("moneyLine") or {}).get("american")) or american(a.get("moneyLine")),
                       "total": it.get("overUnder"), "provider": it.get("provider", {}).get("name")}
            break
    if f and fetched:                           # never cache a failed request (a 403 / timeout) as "no line": that poisoned the cache once
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(out))
    return out


def open_close_line(sport: str, event_id: str, cache: Optional[Path] = None) -> Optional[Dict]:
    """OPENING and CLOSING team lines from ESPN's first non-live provider: {open_spread_home, open_ml_home, open_ml_away, open_total, close_spread_home, close_ml_home, close_ml_away, close_total, provider}.
    Cached once fetched (lineoc_<sport>_<id>.json); a failed request is not cached."""
    f = cache / f"lineoc_{sport}_{event_id}.json" if cache else None
    if f and f.exists():
        return json.loads(f.read_text()) or None
    out, fetched = None, False
    for _ in range(3):
        raw = subprocess.run(["curl", "-s", "-m", "30", "-A", "Mozilla/5.0", ODDS.format(p=LEAGUE_PATH[sport], e=event_id)], capture_output=True, text=True).stdout
        if raw.startswith("{"):
            fetched = True
            items = [i for i in json.loads(raw).get("items", []) if "live" not in (i.get("provider", {}).get("name", "")).lower()]
            items.sort(key=lambda i: i.get("provider", {}).get("priority", 99))
            if items:
                it = items[0]
                h, a = it.get("homeTeamOdds", {}), it.get("awayTeamOdds", {})
                def side(which):
                    sp = american(((h.get(which) or {}).get("pointSpread") or {}).get("american"))
                    if sp is None:
                        asp = american(((a.get(which) or {}).get("pointSpread") or {}).get("american"))
                        sp = -asp if asp is not None else None
                    tot = ((it.get(which) or {}).get("total") or {}) if isinstance(it.get(which), dict) else {}
                    return {f"{which}_spread_home": sp, f"{which}_ml_home": american(((h.get(which) or {}).get("moneyLine") or {}).get("american")),
                            f"{which}_ml_away": american(((a.get(which) or {}).get("moneyLine") or {}).get("american")),
                            f"{which}_total": american_total(h, a, it, which)}
                out = {**side("open"), **side("close"), "provider": it.get("provider", {}).get("name")}
            break
    if f and fetched:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(out))
    return out


def american_total(h, a, it, which):
    """the total's opening / closing number: ESPN puts it in event.<open|close>.total.american as a string ("221.5")."""
    t = ((it.get(which) or {}).get("total") or {}) if isinstance(it.get(which), dict) else {}
    for k in ("american", "alternateDisplayValue"):
        try:
            return float(str(t.get(k)).lstrip("ou"))
        except (TypeError, ValueError):
            pass
    return it.get("overUnder") if which == "close" else None


def fetch_closing(sport: str, event_ids: Iterable[str], cache: Path, workers: int = 12) -> Dict[str, Optional[Dict]]:
    ids = list(event_ids)
    with ThreadPoolExecutor(workers) as ex:
        return dict(zip(ids, ex.map(lambda e: close_line(sport, e, cache), ids)))
