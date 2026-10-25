#!/usr/bin/env python3
"""
comeback_prices.py -- how low does the live moneyline go before a team comes back, and how often does a team at that price actually win?

    python bots/comeback_prices.py [--cache DIR]

Every NBA game of 2025-26 (so far) and NFL game of 2023-25, ESPN's live win-probability curve for every play (the live "price"; it is ESPN's model, not a sportsbook line, so read it as the level a
live moneyline sits at). Per game we keep each side's lowest point and the eventual result. Reported: for a team whose live win probability fell to X% or lower at any moment, how often it
still won (with the American-odds equivalent of X), the average and median LOW POINT of the teams that came back from 50% or less to win, and the same for teams that led big and lost.
NHL has no ESPN curve, so it uses the score state instead: how often a team trailing by k goals after each period (or at any moment) won, from the play-by-play of 2023-25.
"""
import argparse, json, subprocess, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
LEVELS = [0.40, 0.30, 0.20, 0.10, 0.05, 0.02]


def curl(url):
    for _ in range(3):
        raw = subprocess.run(["curl", "-s", "-m", "60", url], capture_output=True, text=True).stdout
        if raw.startswith("{"):
            return json.loads(raw)
    return {}


def amer(p):
    return f"+{round(100 * (1 - p) / p):,}" if p < 0.5 else f"{round(-100 * p / (1 - p)):,}"


def game_curve(sport, eid, cache):
    f = Path(cache) / f"wp_{sport.replace('/', '_')}_{eid}.json"
    if f.exists():
        return json.load(open(f))
    d = curl(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/summary?event={eid}")
    w = [x["homeWinPercentage"] for x in d.get("winprobability", []) if x.get("homeWinPercentage") is not None]
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    home_win = None
    for c in comp.get("competitors", []):
        if c.get("homeAway") == "home" and c.get("winner") is not None:
            home_win = bool(c["winner"])
    out = {"id": eid, "n": len(w), "home_min": min(w) if w else None, "home_max": max(w) if w else None, "home_win": home_win}
    Path(cache).mkdir(parents=True, exist_ok=True)
    json.dump(out, open(f, "w"))
    return out


def summarize(label, games):
    games = [g for g in games if g["n"] > 20 and g["home_win"] is not None]
    lows = []                                        # (low point, did that team win) for each side of each game
    for g in games:
        lows.append((g["home_min"], g["home_win"])); lows.append((1 - g["home_max"], not g["home_win"]))
    print(f"\n{label}: {len(games)} games")
    print("  a team whose live win probability fell to X% or lower at any point:")
    for x in LEVELS:
        sel = [w for lo, w in lows if lo <= x]
        if sel:
            print(f"     <= {100 * x:>3.0f}% (price {amer(x):>7}): {len(sel):>4} teams, {100 * sum(sel) / len(sel):4.1f}% of them won   (a fair price would be {100 * x:.0f}% or less)")
    winners = [lo for lo, w in lows if w and lo <= 0.5]
    big = [lo for lo, w in lows if w and lo <= 0.10]
    print(f"  teams that won after being at or below 50%: {len(winners)}; their lowest point averaged {100 * np.mean(winners):.1f}% (median {100 * np.median(winners):.1f}%), i.e. a typical low-point price of {amer(float(np.median(winners)))}")
    if big:
        print(f"  teams that won after being at or below 10%: {len(big)}, lowest point averaged {100 * np.mean(big):.1f}%, deepest {100 * min(big):.2f}%")
    blown = [1 - hi if False else hi for hi, w in [(g["home_max"], not g["home_win"]) for g in games] + [(1 - g["home_min"], g["home_win"]) for g in games] if w]
    if blown:
        print(f"  leaders that lost: {len(blown)} teams, their highest point averaged {100 * np.mean(blown):.1f}% (those were priced {amer(1 - float(np.median(blown)))} to LOSE at the median peak)")


def nhl_states(cache):
    """NHL: how often a team trailing by k goals after P periods (or ever by k) won. Goals from ESPN's play-by-play (scoring plays carry the running score and period)."""
    import buckets_backtest  # noqa: F401
    ids = []
    for day_file in sorted(Path(cache).glob("espn_nhl_games.json")):
        ids = list(json.load(open(day_file)).values())
    def one(eid):
        f = Path(cache) / f"nhlplays_{eid}.json"
        if f.exists():
            return json.load(open(f))
        d = curl(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={eid}")
        plays = [(p["period"]["number"], p["homeScore"], p["awayScore"]) for p in d.get("plays", []) if p.get("scoringPlay")]
        comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
        hw = next((c["winner"] for c in comp.get("competitors", []) if c.get("homeAway") == "home" and c.get("winner") is not None), None)
        out = {"plays": plays, "home_win": hw}
        json.dump(out, open(f, "w")); return out
    with ThreadPoolExecutor(12) as ex:
        games = list(ex.map(one, ids))
    rows = defaultdict(lambda: [0, 0])               # key -> [teams, wins]
    for g in games:
        if g["home_win"] is None:
            continue
        hw = g["home_win"]
        for side in ("home", "away"):
            won = hw if side == "home" else not hw
            sign = 1 if side == "home" else -1
            worst = 0
            for per in (1, 2):
                ps = [p for p in g["plays"] if p[0] <= per]
                diff = sign * (ps[-1][1] - ps[-1][2]) if ps else 0
                rows[f"after {per}{'st' if per == 1 else 'nd'} period, trailing by {-diff}"][0] += diff < 0 and 1 or 0
                rows[f"after {per}{'st' if per == 1 else 'nd'} period, trailing by {-diff}"][1] += (diff < 0 and won) and 1 or 0
            for p in g["plays"]:
                worst = min(worst, sign * (p[1] - p[2]))
            for k in (2, 3):
                if worst <= -k:
                    rows[f"at any point trailing by {k}+"][0] += 1; rows[f"at any point trailing by {k}+"][1] += int(won)
    n = sum(1 for g in games if g["home_win"] is not None)
    print(f"\nNHL: {n} games (score-state comeback rates; no live price curve for hockey)")
    for k in sorted(rows):
        t, w = rows[k]
        if t >= 20:
            print(f"     {k:<44}{t:>5} teams, {100 * w / t:4.1f}% won  (price {amer(w / t) if 0 < w / t < 1 else 'n/a'})")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_comeback")); ap.add_argument("--nhl-cache", default=str(Path.home() / ".cache/dash_nhl")); ap.add_argument("--nba-cache", default=str(Path.home() / ".cache/dash_nba")); a = ap.parse_args()
    import buckets_backtest as bb
    nba_cache = a.nba_cache
    ids = [g["id"] for s in (2025, 2026) for g in bb.load(s, nba_cache)]
    with ThreadPoolExecutor(12) as ex:
        nba = list(ex.map(lambda e: game_curve("basketball/nba", e, a.cache), ids))
    summarize("NBA 2025-26", nba)
    import nflreadpy as nfl
    eids = [str(r["espn"]) for r in nfl.load_schedules([2023, 2024, 2025]).iter_rows(named=True) if r["game_type"] == "REG" and r.get("espn") and r["home_score"] is not None]
    with ThreadPoolExecutor(12) as ex:
        nfls = list(ex.map(lambda e: game_curve("football/nfl", e, a.cache), eids))
    summarize("NFL 2023-25", nfls)
    nhl_states(a.nhl_cache)


if __name__ == "__main__":
    main()
