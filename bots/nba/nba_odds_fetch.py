#!/usr/bin/env python3
"""nba_odds_fetch.py — the book's price next to the NBA (BUCKETS) board.

    python bots/nba/nba_odds_fetch.py --probe
    python bots/nba/nba_odds_fetch.py --out public/data/current

Sibling of bots/nhl/nhl_odds_fetch.py, bots/nfl/nfl_odds_fetch.py (and, through it, bots/odds_fetch.py):
same provider (odds-api.io, Fanatics + DraftKings, the repo's ODDSAPI_IO_KEY),
same consensus maths, same freeze-at-start rule. NBA differs in two ways:

  * No slate file to join to. The NBA board is built on the site from the
    league data, so the bot publishes by normalised NAME (plus the game), and
    the site joins on name + game exactly as it does for MLB odds.
  * Games straddle UTC midnight, so the dated snapshot is keyed on the
    game's own date (its start in US/Eastern, which is how the NBA dates its
    own schedule), never the date the fetch ran.

Writes (public/data/current):
  nba_odds_latest.json   consensus line / over / under / implied per player
  nba_odds_status.json   what happened this run, every run, including nothing
  nba_odds_<date>.json   HISTORY: one file per game date, {name: {market:
                         [line, over, implied]}}, a player's row frozen once
                         his game starts so history is the pregame price

UNVERIFIED LIVE (the sandbox cannot reach the host). The first probe prints
every market name the books serve; BASKETBALL_HINTS is one line per fix.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "nfl"))      # nfl_odds_fetch (shared maths)
sys.path.insert(0, str(HERE.parent))              # odds_fetch

import nfl_odds_fetch as base   # noqa: E402
import odds_fetch as mlb_odds   # noqa: E402

SPORT = "basketball_nba"
ET = ZoneInfo("America/New_York")

# pick market -> our market key (what the site reads). DD and TD are the
# double-double / triple-double markets the NBA record grades; the rest are
# the stat lines a play is built from.
CATEGORY_MARKET = {
    "DD":   "player_double_double",
    "TD":   "player_triple_double",
    "PTS":  "player_points",
    "REB":  "player_rebounds",
    "AST":  "player_assists",
    "3PM":  "player_threes",
    "PRA":  "player_pra",
    "PR":   "player_pts_reb",
    "PA":   "player_pts_ast",
    "RA":   "player_reb_ast",
}
MARKETS = sorted(set(CATEGORY_MARKET.values()))

# ORDER IS LOAD-BEARING (same rule as PROP_HINTS / FOOTBALL_HINTS): not-ours
# first, then the COMPOUND stat lines ahead of the single they contain, so
# "Points + Rebounds + Assists" is never filed as points. "_other" is matched
# only to be thrown away.
BASKETBALL_HINTS = [
    ("_other", ("first basket", "first field goal", "1st quarter", "2nd quarter", "3rd quarter",
                "4th quarter", "quarter", "1st half", "2nd half", "half", "alt ", "alternate",
                "team ", "steals", "blocks", "turnovers", "fouls", "minutes", "free throw",
                "field goals", "stocks", "overtime", "race to", "fantasy")),
    ("player_triple_double", ("triple double", "triple-double")),
    ("player_double_double", ("double double", "double-double")),
    ("player_pra", ("points + rebounds + assists", "points, rebounds, assists",
                    "points rebounds assists", "pts + reb + ast", "pts+reb+ast", "pra",
                    "points+rebounds+assists", "points & rebounds & assists")),
    ("player_pts_reb", ("points + rebounds", "points+rebounds", "pts + reb", "pts+reb",
                        "points and rebounds", "points & rebounds")),
    ("player_pts_ast", ("points + assists", "points+assists", "pts + ast", "pts+ast",
                        "points and assists", "points & assists")),
    ("player_reb_ast", ("rebounds + assists", "rebounds+assists", "reb + ast", "reb+ast",
                        "rebounds and assists", "rebounds & assists")),
    ("player_threes", ("three", "3-pointer", "3 pointer", "3pt", "3-point", "3 point")),
    ("player_rebounds", ("rebound",)),
    ("player_assists", ("assist",)),
    ("player_points", ("points",)),
]


def et_date(stamp) -> str | None:
    try:
        t = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(ET).date().isoformat()
    except Exception:
        return None


def fetch(key: str, books: str = "fanatics,draftkings") -> list[dict]:
    """NBA props from odds-api.io; [] on any failure (reported, not raised)."""
    if base._SSL is not None:
        mlb_odds._SSL = base._SSL
    try:
        rows = mlb_odds.fetch_oddsapiio(
            key, books, slugs=("basketball", "nba"),
            league_words=("nba", "national basketball"), days=2, hints=BASKETBALL_HINTS)
    except Exception as e:
        print(f"  odds-api.io: {type(e).__name__}: {e}")
        rows = []
    for r in mlb_odds.FORENSICS:
        if len(base.FORENSICS) < 60:
            base.FORENSICS.append(r)
    base.RAW_DUMP.extend(mlb_odds.RAW_DUMP)
    keep = set(MARKETS)
    dropped = sum(1 for r in rows if r["market"] not in keep)
    if dropped:
        print(f"  odds-api.io: dropped {dropped} quote(s) in markets we do not publish")
    return [r for r in rows if r["market"] in keep]


def slim_by_date(board: dict) -> dict[str, dict]:
    """{game_date: {"rows": {name: {market: [line, over, implied]}},
    "commence": {name: iso}, "game": {name: "A @ H"}}} from consensus()."""
    out: dict[str, dict] = {}
    for norm, mkts in board.items():
        for m, q in mkts.items():
            if q.get("over") is None or q.get("line") is None:
                continue
            day = et_date(q.get("commence"))
            if not day:
                continue
            d = out.setdefault(day, {"rows": {}, "commence": {}, "game": {}})
            d["rows"].setdefault(norm, {})[m] = [q["line"], q["over"], q.get("implied")]
            d["commence"][norm] = q.get("commence")
            d["game"][norm] = q.get("game")
    return out


def write_status(out: Path, **kw) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    (out / "nba_odds_status.json").write_text(json.dumps(
        {"checked_at": now.isoformat(), **kw}, separators=(",", ":")))
    print(f"status: {kw.get('state')} — {kw.get('reason', '')}")


def probe(key: str) -> int:
    print("probing odds-api.io (Fanatics + DraftKings) for NBA …")
    rows = fetch(key)
    if not rows:
        print("no quotes came back — see the forensics above for the real reason")
        return 1
    print(f"\n{len(rows)} quote(s), books {sorted({r['book'] for r in rows})}")
    for m in MARKETS:
        n = sum(1 for r in rows if r["market"] == m)
        print(f"  {m:<24}: {n} quote(s)" + ("" if n else "  — NOT PRICED"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--out", default="public/data/current")
    ap.add_argument("--force", action="store_true", help="ignore the request lock")
    ap.add_argument("--dump", default="", help="write the raw provider payload here")
    a = ap.parse_args()
    out = Path(a.out)

    key = os.environ.get("ODDSAPI_IO_KEY", "").strip()
    if not key:
        write_status(out, state="no_key", reason="ODDSAPI_IO_KEY is not set, nothing fetched.")
        return 0
    if a.probe:
        rc = probe(key)
        base._write_dump(a.dump)
        return rc

    # light lock so a manual re-run can't burn quota (the cron sets the pace)
    try:
        min_gap = int(os.environ.get("NBA_ODDS_MIN_INTERVAL_MIN", "") or 45)
    except ValueError:
        min_gap = 45
    prev = None if a.force else base.published("nba_odds_latest.json")
    if isinstance(prev, dict) and not prev.get("empty"):
        try:
            last = dt.datetime.fromisoformat(str(prev.get("fetched_at")).replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
        except Exception:
            age = 1e9
        if age < min_gap:
            write_status(out, state="skipped",
                         reason=f"A snapshot from {age:.0f} min ago is still fresh (lock {min_gap} min).")
            return 0

    rows = fetch(key, os.environ.get("ODDSAPI_IO_BOOKMAKERS", "").strip() or "fanatics,draftkings")
    base._write_dump(a.dump)
    now = dt.datetime.now(dt.timezone.utc)
    if not rows:
        reason = ("odds-api.io returned no NBA player-prop quote. Usually no props posted yet, "
                  "a spent quota, or a market name FOOTBALL/BASKETBALL_HINTS does not know.")
        write_status(out, state="empty", reason=reason, forensics=base.FORENSICS)
        # keep any good latest on disk: only publish an empty marker if none exists
        if not (out / "nba_odds_latest.json").exists():
            (out / "nba_odds_latest.json").write_text(json.dumps(
                {"source": "", "sport": SPORT, "fetched_at": now.isoformat(), "empty": True,
                 "reason": reason, "by_name": {}, "category_market": CATEGORY_MARKET},
                separators=(",", ":")))
        return 0

    board = base.consensus(rows)
    (out / "nba_odds_latest.json").write_text(json.dumps({
        "source": "oddsapiio", "sport": SPORT, "fetched_at": now.isoformat(),
        "category_market": CATEGORY_MARKET, "by_name": board,
        "note": ("Consensus line is the one the most books post; over/under are median prices at "
                 "that line; implied is the break-even %. Joined on the site by normalised name + "
                 "game. A live quote can appear once a game starts; the dated history files are "
                 "frozen at game start."),
    }, separators=(",", ":")))

    written = 0
    for day, d in slim_by_date(board).items():
        snap = out / f"nba_odds_{day}.json"
        rows_, com = base.freeze_started(snap, d["rows"], d["commence"], now)
        game = d["game"]
        try:
            old = json.loads(snap.read_text())
            game = {**(old.get("game") or {}), **game}
        except Exception:
            pass
        snap.write_text(json.dumps({
            "date": day, "fetched_at": now.isoformat(), "source": "oddsapiio",
            "rows": rows_, "commence": com, "game": game,
            "note": "{name: {market: [line, over, implied]}}; frozen per player at his game's start.",
        }, separators=(",", ":")))
        written += 1
    write_status(out, state="ok", reason=f"{len(board)} priced players, {written} game date(s) written.",
                 players=len(board), forensics=base.FORENSICS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
