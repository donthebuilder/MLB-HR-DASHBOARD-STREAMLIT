#!/usr/bin/env python3
"""
forecast_nba.py -- pregame game forecast for the NBA: margin, total, and the chance of a blowout.

    python bots/forecast_nba.py [--cache DIR]

Inputs, all from games BEFORE tip: last-10 and season-to-date net rating, pace, points scored and
allowed, rest days / back-to-back, home court. Linear model on the margin and on the total, fitted on
2023-24 and graded on 2024-25 (chronological; the holdout is looked at once).

The reason it exists is the star table: a star on the losing side of a lopsided game reaches 25+ only
11.9% of the time. So the test that matters is the last one -- does the forecast separate star
outcomes better than the plain rating gap used so far?

There are no historical NBA lines to compare with (ESPN keeps none); tonight's lines are saved from now
on (lineups.py + the snapshot workflow) so the market can be added as an input and a yardstick later.
"""
import argparse
import datetime as dt
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402

FEATS = ["net10", "net_s", "pace", "rest_h", "rest_a", "b2b_h", "b2b_a", "ortg_sum", "drtg_sum"]


def collect(cache):
    games_out, stars = [], []
    for season in (2023, 2024):
        games = sorted(bb.load(season, cache), key=lambda g: (g["date"], g["id"]))
        res = defaultdict(list)             # team -> [(pf, pa, poss)]
        last = {}
        hist = defaultdict(list)            # pid -> [(min, pts)]
        for g in games:
            t = g["teams"]
            h, a = (t[0], t[1]) if t[0]["home"] else (t[1], t[0])
            pts = {x["abbr"]: sum(p["pts"] for p in x["players"]) for x in t}
            poss = {x["abbr"]: sum(p["fga"] + 0.44 * p["fta"] - p["oreb"] + p["to"] for p in x["players"]) for x in t}
            day = dt.date.fromisoformat(g["date"])

            def stat(team):
                r = res[team]
                if len(r) < 8:
                    return None
                l10, allr = r[-10:], r
                net = lambda xs: sum(f - c for f, c, _ in xs) / len(xs)
                return {"net10": net(l10), "net_s": net(allr), "pace": sum(x[2] for x in l10) / len(l10),
                        "ortg": sum(x[0] for x in l10) / len(l10), "drtg": sum(x[1] for x in l10) / len(l10)}
            sh, sa = stat(h["abbr"]), stat(a["abbr"])
            if sh and sa and h["abbr"] in last and a["abbr"] in last:
                rh, ra = (day - last[h["abbr"]]).days, (day - last[a["abbr"]]).days
                f = [sh["net10"] - sa["net10"], sh["net_s"] - sa["net_s"], (sh["pace"] + sa["pace"]) / 2,
                     min(rh, 5), min(ra, 5), float(rh <= 1), float(ra <= 1), sh["ortg"] + sa["ortg"], sh["drtg"] + sa["drtg"]]
                gid = g["id"]
                games_out.append({"id": gid, "season": season, "f": f, "margin": pts[h["abbr"]] - pts[a["abbr"]],
                                  "total": pts[h["abbr"]] + pts[a["abbr"]], "gap": sh["net10"] - sa["net10"] + 3.0})
                for team, sgn in ((h, 1), (a, -1)):
                    ro = []
                    for p in team["players"]:
                        hh = hist[p["pid"]][-10:]
                        if len(hh) >= 6 and sum(m for m, _ in hh) / len(hh) >= 28:
                            ro.append((p, sum(q for _, q in hh) / len(hh)))
                    for p, _ in sorted(ro, key=lambda r: r[1], reverse=True)[:2]:
                        stars.append({"id": gid, "season": season, "side": sgn, "y25": int(p["pts"] >= 25)})
            for x in t:
                res[x["abbr"]].append((pts[x["abbr"]], pts[[y["abbr"] for y in t if y is not x][0]], poss[x["abbr"]]))
                last[x["abbr"]] = day
                for p in x["players"]:
                    if p["min"] > 0:
                        hist[p["pid"]].append((p["min"], p["pts"]))
    return games_out, stars


def fit(X, y):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    return np.linalg.lstsq(Xb, y, rcond=None)[0]


def predict(w, X):
    return np.hstack([np.ones((len(X), 1)), X]) @ w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    a = ap.parse_args()
    games, stars = collect(a.cache)
    tr = [g for g in games if g["season"] == 2023]; te = [g for g in games if g["season"] == 2024]
    Xtr, Xte = np.array([g["f"] for g in tr]), np.array([g["f"] for g in te])
    wm = fit(Xtr, np.array([g["margin"] for g in tr], float)); wt = fit(Xtr, np.array([g["total"] for g in tr], float))
    pm, pt = predict(wm, Xte), predict(wt, Xte)
    ym, yt = np.array([g["margin"] for g in te], float), np.array([g["total"] for g in te], float)
    naive = np.array([g["gap"] for g in te])
    sigma = float(np.std(ym - predict(wm, Xtr[: len(tr)]) if False else np.std(np.array([g['margin'] for g in tr]) - predict(wm, Xtr))))
    print(f"fit on {len(tr)} games (2023-24), graded on {len(te)} (2024-25)\n")
    print("HOLDOUT                          forecast    naive (rating gap + 3)")
    print(f"  margin MAE (points)            {np.mean(np.abs(ym - pm)):8.2f}    {np.mean(np.abs(ym - naive)):8.2f}")
    print(f"  picks the winner               {100 * np.mean((pm > 0) == (ym > 0)):7.1f}%    {100 * np.mean((naive > 0) == (ym > 0)):7.1f}%")
    print(f"  total MAE (points)             {np.mean(np.abs(yt - pt)):8.2f}    {np.mean(np.abs(yt - np.mean([g['total'] for g in tr]))):8.2f}  (naive = last season's average)")

    print("\nBLOWOUT RISK (final margin 20+ either way) -- predicted vs what happened, by predicted |margin|")
    ab = np.abs(pm)
    for lo, hi in ((0, 3), (3, 6), (6, 9), (9, 99)):
        m = (ab >= lo) & (ab < hi)
        if m.sum():
            print(f"  |predicted margin| {lo:>2}-{hi if hi < 99 else '+':<3} games {int(m.sum()):4}   blowout {100 * np.mean(np.abs(ym[m]) >= 20):5.1f}%   favourite wins {100 * np.mean((pm[m] > 0) == (ym[m] > 0)):5.1f}%")

    print("\nDOES IT SEPARATE STAR NIGHTS? star = top-2 scorer, 28+ min; split his team's predicted margin into thirds")
    idx = {g["id"]: i for i, g in enumerate(te)}
    rows = [(s, idx[s["id"]]) for s in stars if s["season"] == 2024 and s["id"] in idx]
    for label, arr in (("forecast margin", pm), ("rating gap (old)", naive)):
        v = np.array([arr[i] * s["side"] for s, i in rows]); y = np.array([s["y25"] for s, _ in rows])
        lo_, hi_ = np.quantile(v, [1 / 3, 2 / 3])
        t = [y[v <= lo_].mean(), y[(v > lo_) & (v <= hi_)].mean(), y[v > hi_].mean()]
        print(f"  {label:<18} star 25+ rate: team likely to LOSE {100 * t[0]:5.1f}%   toss-up {100 * t[1]:5.1f}%   likely to WIN {100 * t[2]:5.1f}%   spread {100 * (t[2] - t[0]):4.1f} pts   (n={len(y)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
