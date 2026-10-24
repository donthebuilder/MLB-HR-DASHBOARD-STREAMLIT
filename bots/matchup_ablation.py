#!/usr/bin/env python3
"""
matchup_ablation.py -- how much do the DEFENSIVE terms actually add to each lane? Refit without them and compare on the same holdout.

    python bots/matchup_ablation.py nhl|nba|nbalanes [--cache DIR]

For each model: the full tuned fit, the fit with the DEFENCE terms removed (opponent shots against / goalie save% in NHL; opponent points / rebounds /
assists / threes allowed in NBA), and the fit with defence AND game context removed (pace, book spread, total). Same train seasons, same holdout,
same rows. Reported: best pick per game, top 10 per night, log loss (lower is better; it scores the whole probability, not just the top).
A defensive term earns its place only if taking it out makes the holdout worse.
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tune_models import fit_logistic, topk   # noqa: E402

DEF = {"nhl": {"goalie_sv", "opp_sa"}, "pts": {"opp_allowed"}, "lane": {"opp_allowed"}}
CTX = {"nhl": {"is_home"}, "pts": {"pace", "pace_pts", "strength_gap", "fav_gap", "dog_gap", "book_fav", "book_blowout_loss", "book_total"},
       "lane": {"pace", "book_fav", "book_total"}}


def logloss(rs, key):
    s = 0.0
    for r in rs:
        p = min(1 - 1e-6, max(1e-6, 1 / (1 + math.exp(-r[key]))))
        s -= math.log(p) if r["y"] else math.log(1 - p)
    return s / len(rs)


def run(label, rows, getf, gety, train_seasons, test_season, kind, group_key="game"):
    tr = [r for r in rows if r["season"] in train_seasons]
    te = [r for r in rows if r["season"] == test_season]
    names = sorted(getf(tr[0]))
    out = []
    for variant, drop in (("full", set()), ("no defence", DEF[kind]), ("no defence, no context", DEF[kind] | CTX[kind])):
        use = [n for n in names if n not in drop]
        X = lambda rs: np.array([[getf(r)[n] for n in use] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([gety(r) for r in tr], float))
        for r, z in zip(te, ((X(te) - mu) / sd) @ w[1:] + w[0]):
            r["_z"], r["y"] = float(z), gety(r)
            r["_g"] = (r["night"], r["game"])
        out.append((variant, topk(te, "_z", "y", 1, "_g"), topk(te, "_z", "y", 10, "night"), logloss(te, "_z")))
    print(f"{label}  (graded on {len(te)} player-games)")
    base = out[0]
    for v, t1, t10, ll in out:
        print(f"   {v:<24} best/game {t1:5.1f}%   top10/night {t10:5.1f}%   log loss {ll:.4f}" + ("" if v == "full" else f"   (vs full: best/game {t1 - base[1]:+.1f}, top10 {t10 - base[2]:+.1f}, loss {ll - base[3]:+.4f})"))
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=["nhl", "nba", "nbalanes"])
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()
    if a.sport == "nhl":
        import nhl_backtest as m
        cache = a.cache or str(Path.home() / ".cache/dash_nhl")
        rows = [r for s in (2023, 2024, 2025) for r in m.run(s, cache)]
        run("NHL anytime goal", rows, lambda r: r["f"], lambda r: r["goal"], [2023, 2024], 2025, "nhl")
    else:
        import buckets_backtest as bb
        cache = a.cache or str(Path.home() / ".cache/dash_nba")
        if a.sport == "nba":
            rows = [r for s in (2023, 2024) for r in bb.run(s, cache)]
            run("NBA points 25+", rows, lambda r: r["f"], lambda r: r["y25"], [2023], 2024, "pts")
        else:
            rows = [r for s in (2023, 2024, 2025) for r in bb.run(s, cache) if r.get("lanes")]
            for lane, label in (("reb", "NBA rebounds 10+"), ("ast", "NBA assists 8+"), ("3pm", "NBA 3PM 4+"), ("pra", "NBA PRA 35+")):
                run(label, rows, lambda r, l=lane: r["lanes"][l]["f"], lambda r, l=lane: r["lanes"][l]["y"], [2023, 2024], 2025, "lane")
    return 0


if __name__ == "__main__":
    sys.exit(main())
