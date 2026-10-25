#!/usr/bin/env python3
"""
team_clv.py -- CLOSING-LINE VALUE for the team lines (spread, moneyline) in NBA, NFL and NHL, from ESPN's opening and closing numbers for every game.

    python bots/team_clv.py nba|nfl|nhl [--cache DIR]

For each game of 2025 (and 2026 so far), our out-of-sample model (team_models.py) against the OPENING line. When our number differs from the open by at least a threshold:
  CLV     how far the line moved toward our side by the close (points of spread for NBA / NFL; points of win probability for the moneyline, all three sports)
  moved   the share of those games where the market moved our way / against us
  cover   how often our side covered the CLOSING spread, and the result of just betting our side at the open (-110 assumed) vs at the close
The NHL book spread is a fixed puck line, so NHL is the moneyline only. Positive CLV over many games is what a real edge looks like long before the win rate settles.
"""
import argparse, math, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team_models as tm            # noqa: E402
import game_lines as G              # noqa: E402


def imp(ml):
    return None if ml is None else (100 / (ml + 100) if ml > 0 else -ml / (-ml + 100))


def novig(h, a):
    ph, pa = imp(h), imp(a)
    return ph / (ph + pa) if ph and pa else None


def espn_id(sport, g, cache):
    return g["id"] if sport != "nfl" else g.get("espn")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("sport", choices=["nba", "nfl", "nhl"]); ap.add_argument("--cache", default=None); a = ap.parse_args()
    sport = a.sport
    cache = a.cache or str(Path.home() / {"nba": ".cache/dash_nba", "nhl": ".cache/dash_nhl", "nfl": ".cache/dash_nfl"}[sport])
    games = {"nba": tm.games_nba, "nhl": tm.games_nhl, "nfl": tm.games_nfl}[sport](cache)
    if sport == "nhl":
        ids = __import__("json").load(open(Path(cache) / "espn_nhl_games.json"))
        for g in games:
            g["espn"] = ids.get(f"{g['date']}|{g['away']}|{g['home']}")
    elif sport == "nba":
        for g in games:
            g["espn"] = g["id"]
    else:
        import nflreadpy as nfl
        emap = {r["game_id"]: str(r["espn"]) for r in nfl.load_schedules([2023, 2024, 2025, 2026]).iter_rows(named=True) if r.get("espn")}
        for g in games:
            g["espn"] = emap.get(g["id"])
    preds = [p for p in tm.predictions(sport, games) if p.get("espn")]
    ld = Path(cache).parent / "lineoc"; ld.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(6) as ex:
        lines = list(ex.map(lambda p: G.open_close_line(sport, p["espn"], ld), preds))
    rows = []
    for p, ln in zip(preds, lines):
        if not ln or p["hs"] == p["as"]:
            continue
        r = dict(p, **ln); rows.append(r)
    print(f"{sport.upper()}: {len(preds)} games predicted out of sample, {len(rows)} with ESPN opening and closing lines")
    for season in (2025, 2026):
        rs = [r for r in rows if r["season"] == season]
        if len(rs) < 30:
            continue
        print(f"\n  {season}{'-' + str(season + 1)[2:] if sport != 'nfl' else ''}: {len(rs)} games")
        if sport != "nhl":
            sp = [r for r in rs if r.get("open_spread_home") is not None and r.get("close_spread_home") is not None]
            mv = np.array([r["close_spread_home"] - r["open_spread_home"] for r in sp])
            print(f"    the spread moved at least a half point open-to-close in {100 * np.mean(np.abs(mv) >= 0.5):.0f}% of games; average absolute move {np.mean(np.abs(mv)):.2f} pts")
            for thr in (1.0, 2.0, 3.0):
                clv, mvd, cov_open, cov_close, n = [], [], [], [], 0
                for r in sp:
                    ours = -r["em"]; gap = ours - r["open_spread_home"]
                    if abs(gap) < thr:
                        continue
                    sign = -1 if gap < 0 else 1                                    # gap < 0: we like the home side
                    c = -sign * (r["close_spread_home"] - r["open_spread_home"]) * -1
                    c = (r["open_spread_home"] - r["close_spread_home"]) if gap < 0 else (r["close_spread_home"] - r["open_spread_home"])
                    clv.append(c); mvd.append(np.sign(c))
                    margin = r["hs"] - r["as"]
                    hc_open = margin + r["open_spread_home"]; hc_close = margin + r["close_spread_home"]
                    cov_open.append(((hc_open > 0) if gap < 0 else (hc_open < 0)) if hc_open != 0 else None)
                    cov_close.append(((hc_close > 0) if gap < 0 else (hc_close < 0)) if hc_close != 0 else None)
                if len(clv) >= 20:
                    co = [x for x in cov_open if x is not None]; cc = [x for x in cov_close if x is not None]
                    pr = lambda w: 100 * (w * 100 / 110 - (len(w) - w.sum() if False else 0)) if False else None
                    wins = sum(co); roi = 100 * (wins * (100 / 110) - (len(co) - wins)) / len(co)
                    print(f"    our number >= {thr:.0f} pt from the OPEN: {len(clv)} games, line moved our way {100 * np.mean(np.array(mvd) > 0):.0f}% / against {100 * np.mean(np.array(mvd) < 0):.0f}%, average CLV {np.mean(clv):+.2f} pts; "
                          f"our side covered the open {100 * np.mean(co):.1f}% (ROI at -110 {roi:+.0f}%), the close {100 * np.mean(cc):.1f}%")
        ml = [r for r in rs if novig(r.get("open_ml_home"), r.get("open_ml_away")) and novig(r.get("close_ml_home"), r.get("close_ml_away"))]
        if ml:
            for thr in (0.03, 0.06):
                clv, wins, n = [], [], 0
                for r in ml:
                    po, pc = novig(r["open_ml_home"], r["open_ml_away"]), novig(r["close_ml_home"], r["close_ml_away"])
                    d = r["pm"] - po
                    if abs(d) < thr:
                        continue
                    s = 1 if d > 0 else -1
                    clv.append(s * (pc - po)); wins.append(((r["hs"] > r["as"]) if d > 0 else (r["hs"] < r["as"])))
                if len(clv) >= 20:
                    print(f"    moneyline, our win probability >= {100 * thr:.0f} pts from the open's fair price: {len(clv)} games, line moved our way {100 * np.mean(np.array(clv) > 0):.0f}% / against {100 * np.mean(np.array(clv) < 0):.0f}%, average CLV {100 * np.mean(clv):+.2f} pts of win probability; our side won {100 * np.mean(wins):.1f}%")


if __name__ == "__main__":
    main()
