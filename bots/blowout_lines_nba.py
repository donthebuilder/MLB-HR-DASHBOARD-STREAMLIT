#!/usr/bin/env python3
"""
blowout_lines_nba.py -- what the BOOK'S closing spread says about a star's night, and how our forecast compares to it.

    python bots/blowout_lines_nba.py [--cache DIR] [--lines DIR]

Every 2023-24 and 2024-25 regular-season game is paired with its closing spread and moneylines
(game_lines.py, ESPN's core odds feed). Then:

  1. How often each spread band ends in a blowout (15+, the site's NBA number), for the favourite and the underdog.
  2. A star's night by his team's spread: minutes, points against his own last-10 average, and 25+ rate.
  3. Our NBA forecast against the book on the same holdout games: margin error and winner accuracy.
  4. Does the spread separate star nights better than our forecast or the old rating gap? (tercile spread, same test as before)

Star = top-2 scorer on his team over his last 10 games, averaging 28+ minutes; all of it read from games before tip.
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402
import forecast_nba as fc       # noqa: E402
import game_lines as G          # noqa: E402

BANDS = [(-99, -13, "heavy underdog (13+)"), (-13, -10, "underdog 10-13"), (-10, -5, "underdog 5-10"), (-5, -0.5, "underdog 0.5-5"),
         (-0.5, 0.5, "pick'em"), (0.5, 5, "favourite 0.5-5"), (5, 10, "favourite 5-10"), (10, 13, "favourite 10-13"), (13, 99, "heavy favourite (13+)")]


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    ap.add_argument("--lines", default=None)
    a = ap.parse_args()
    lines_dir = Path(a.lines) if a.lines else Path(a.cache).parent / "nba_lines"

    rows, games_meta = [], {}
    for season in (2023, 2024):
        hist = defaultdict(list)
        for g in sorted(bb.load(season, a.cache), key=lambda g: (g["date"], g["id"])):
            t = g["teams"]
            pts = [sum(p["pts"] for p in x["players"]) for x in t]
            ln = G.close_line("nba", g["id"], lines_dir)
            for i, x in enumerate(t):
                ro = []
                for p in x["players"]:
                    h = hist[p["pid"]][-10:]
                    if len(h) >= 6 and sum(m for m, _ in h) / len(h) >= 28:
                        ro.append((p, sum(m for m, _ in h) / len(h), sum(q for _, q in h) / len(h)))
                if ln and ln.get("spread_home") is not None:
                    fav_pts = -ln["spread_home"] if x["home"] else ln["spread_home"]       # + = this club favoured by that many
                    margin = pts[i] - pts[1 - i]
                    games_meta[(g["id"], i)] = (fav_pts, margin, season)
                    for p, m10, q10 in sorted(ro, key=lambda r: r[2], reverse=True)[:2]:
                        rows.append({"fav": fav_pts, "margin": margin, "min": p["min"], "min10": m10, "pts": p["pts"], "pts10": q10,
                                     "y25": int(p["pts"] >= 25), "season": season, "id": g["id"]})
            for x in t:
                for p in x["players"]:
                    if p["min"] > 0:
                        hist[p["pid"]].append((p["min"], p["pts"]))
    with_line = len({k[0] for k in games_meta})
    print(f"{with_line} games with a closing spread, {len(rows)} star player-games\n")

    print("1. HOW A SPREAD BAND ENDS (this club's side; blowout = margin 15+)")
    print(f"   {'this club is':<26}{'club-games':>11}{'wins':>8}{'wins by 15+':>13}{'loses by 15+':>14}")
    for lo, hi, label in BANDS:
        gs = [m for (f, m, s) in games_meta.values() if lo <= f < hi]
        if gs:
            print(f"   {label:<26}{len(gs):>11}{100 * mean([m > 0 for m in gs]):>7.0f}%{100 * mean([m >= 15 for m in gs]):>12.0f}%{100 * mean([m <= -15 for m in gs]):>13.0f}%")

    print("\n2. A STAR'S NIGHT BY HIS CLUB'S SPREAD")
    print(f"   {'his club is':<26}{'n':>6}{'minutes':>9}{'points':>8}{'vs usual':>9}{'25+':>7}")
    for lo, hi, label in BANDS:
        rs = [r for r in rows if lo <= r["fav"] < hi]
        if len(rs) >= 30:
            print(f"   {label:<26}{len(rs):>6}{mean([r['min'] for r in rs]):>9.1f}{mean([r['pts'] for r in rs]):>8.1f}{mean([r['pts'] - r['pts10'] for r in rs]):>+9.2f}{100 * mean([r['y25'] for r in rs]):>6.1f}%")

    print("\n3. OUR FORECAST vs THE BOOK, 2024-25 holdout (same games)")
    games, _ = fc.collect(a.cache)
    tr = [g for g in games if g["season"] == 2023]; te = [g for g in games if g["season"] == 2024]
    wm = fc.fit(np.array([g["f"] for g in tr]), np.array([g["margin"] for g in tr], float))
    pm = fc.predict(wm, np.array([g["f"] for g in te]))
    book, ours, act = [], [], []
    for g, p in zip(te, pm):
        ln = G.close_line("nba", g["id"], lines_dir)
        if ln and ln.get("spread_home") is not None:
            book.append(-ln["spread_home"]); ours.append(p); act.append(g["margin"])
    book, ours, act = map(np.array, (book, ours, act))
    print(f"   {len(act)} games   margin error:  book {np.mean(np.abs(act - book)):.2f}   ours {np.mean(np.abs(act - ours)):.2f}"
          f"   |   picks the winner:  book {100 * np.mean((book > 0) == (act > 0)):.1f}%   ours {100 * np.mean((ours > 0) == (act > 0)):.1f}%")

    print("\n4. DOES IT SEPARATE STAR NIGHTS? (2024-25, tercile of his club's expected margin; same rows for all three)")
    te_ids = {g["id"]: (b, o, g["gap"]) for g, b, o in zip([g for g in te if G.close_line("nba", g["id"], lines_dir)
                                                          and G.close_line("nba", g["id"], lines_dir).get("spread_home") is not None], book, ours)}
    sel = [(r, te_ids[r["id"]]) for r in rows if r["season"] == 2024 and r["id"] in te_ids]
    for label, k in (("book spread", 0), ("our forecast", 1), ("old rating gap", 2)):
        # sign: rows carry the club's own favourite points; the other two are home-based, flip for road clubs
        v, y = [], []
        for r, vals in sel:
            home_fav = vals[0]
            side = 1 if abs(r["fav"] - home_fav) < 1e-6 else -1        # r["fav"] equals home_fav when the star is on the home club
            v.append(vals[k] * side); y.append(r["y25"])
        v, y = np.array(v), np.array(y)
        lo_, hi_ = np.quantile(v, [1 / 3, 2 / 3])
        t = [y[v <= lo_].mean(), y[(v > lo_) & (v <= hi_)].mean(), y[v > hi_].mean()]
        print(f"   {label:<16} club likely to LOSE {100 * t[0]:5.1f}%   toss-up {100 * t[1]:5.1f}%   likely to WIN {100 * t[2]:5.1f}%   spread {100 * (t[2] - t[0]):4.1f} pts  (n={len(y)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
