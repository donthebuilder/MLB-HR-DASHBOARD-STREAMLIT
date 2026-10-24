#!/usr/bin/env python3
"""
tune_models.py -- fit the scorers' parts and angles on early seasons, report on a season they never saw.

    python bots/tune_models.py nhl   [--cache DIR]     fit 2023-24 + 2024-25, test 2025-26
    python bots/tune_models.py nba   [--cache DIR]     fit 2023-24,           test 2024-25

A ridge-penalised logistic regression over the scorer's own components plus the
angles the data supports (home/road, back-to-back, rest, opponent, strength gap).
One fit, one look at the holdout -- nothing is re-tuned after seeing it.

Reports, on the holdout season only:
  - the shipped v1 score, the plain-stat baseline, and the tuned score
  - the coefficients, so you can see which parts and which angles carry weight
  - MODEL-VIEW PICKS: where the tuned score and the obvious pick disagree, who scored.
    "Obvious" = the plain stat (season shots/gm for NHL, season points/gm for NBA).
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

SPORTS = {
    "nhl": dict(label="NHL anytime goal", target={"goal": "1+ goal"}, shipped="lamp", naive="base_shots",
                naive_label="season shots/gm", train=[2023, 2024], test=2025),
    "nba": dict(label="NBA points", target={"y25": "25+ pts", "y30": "30+ pts"}, shipped="bk", naive="ppg",
                naive_label="season points/gm", train=[2023], test=2024),
}


def fit_logistic(X, y, l2=5.0, iters=40):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(Xb.shape[1])
    pen = np.eye(len(w)) * l2
    pen[0, 0] = 0.0
    for _ in range(iters):
        z = np.clip(Xb @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = Xb.T @ (p - y) + pen @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + pen
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-7:
            break
    return w


def topk(rows, key, y, k, group):
    g = defaultdict(list)
    for r in rows:
        g[r[group]].append(r)
    hit = n = 0
    for v in g.values():
        for r in sorted(v, key=lambda r: r[key], reverse=True)[:k]:
            hit += r[y]; n += 1
    return 100 * hit / n if n else float("nan")


def rows_for(sport, cache, seasons):
    if sport == "nhl":
        import nhl_backtest as m
    else:
        import buckets_backtest as m
    out = []
    for s in seasons:
        out += m.run(s, cache)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=SPORTS)
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()
    cfg = SPORTS[a.sport]
    cache = a.cache or str(Path.home() / (".cache/dash_nhl" if a.sport == "nhl" else ".cache/dash_nba"))
    rows = rows_for(a.sport, cache, cfg["train"] + [cfg["test"]])
    train = [r for r in rows if r["season"] in cfg["train"]]
    test = [r for r in rows if r["season"] == cfg["test"]]
    names = sorted(rows[0]["f"])
    Xtr = np.array([[r["f"][n] for n in names] for r in train], float)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    Xte = np.array([[r["f"][n] for n in names] for r in test], float)
    Xtr, Xte = (Xtr - mu) / sd, (Xte - mu) / sd
    print(f"{cfg['label']}: fit on seasons {cfg['train']} ({len(train)} player-games), tested on {cfg['test']} ({len(test)})")

    primary = next(iter(cfg["target"]))
    w = fit_logistic(Xtr, np.array([r[primary] for r in train], float))
    for r, z in zip(test, Xte @ w[1:] + w[0]):
        r["tuned"] = float(z)

    print(f"\nWhat carries weight (standardised coefficients, fitted on '{cfg['target'][primary]}'):")
    for n, c in sorted(zip(names, w[1:]), key=lambda t: -abs(t[1])):
        print(f"   {n:<16}{c:+.3f}")

    for tgt, tl in cfg["target"].items():
        base = 100 * sum(r[tgt] for r in test) / len(test)
        print(f"\nHOLDOUT {cfg['test']}, {tl}  (base rate {base:.1f}%)")
        for label, key in (("shipped v1 score", cfg["shipped"]), (cfg["naive_label"], cfg["naive"]), ("tuned", "tuned")):
            print(f"   {label:<18} top1/game {topk(test, key, tgt, 1, 'game'):5.1f}%   top3/game {topk(test, key, tgt, 3, 'game'):5.1f}%"
                  f"   top10/night {topk(test, key, tgt, 10, 'night'):5.1f}%")

    # model-view picks: tuned vs the obvious pick, game by game and night by night
    tgt = primary
    by_game = defaultdict(list)
    for r in test:
        by_game[r["game"]].append(r)
    same = diff = th = nh = 0
    for v in by_game.values():
        t = max(v, key=lambda r: r["tuned"]); n = max(v, key=lambda r: r[cfg["naive"]])
        if t is n:
            same += 1
        else:
            diff += 1; th += t[tgt]; nh += n[tgt]
    print(f"\nMODEL-VIEW PICKS (tuned vs {cfg['naive_label']}), {cfg['target'][tgt]}")
    print(f"   per game: same #1 in {same}, different in {diff} ({100 * diff / (same + diff):.0f}%)."
          f" When different: tuned's pick scored {th} ({100 * th / diff:.1f}%), the obvious pick {nh} ({100 * nh / diff:.1f}%)")
    by_night = defaultdict(list)
    for r in test:
        by_night[r["night"]].append(r)
    mv = ob = mvn = obn = 0
    for v in by_night.values():
        T = {id(r) for r in sorted(v, key=lambda r: r["tuned"], reverse=True)[:10]}
        N = {id(r) for r in sorted(v, key=lambda r: r[cfg["naive"]], reverse=True)[:10]}
        for r in v:
            if id(r) in T and id(r) not in N:
                mv += r[tgt]; mvn += 1
            if id(r) in N and id(r) not in T:
                ob += r[tgt]; obn += 1
    print(f"   nightly top 10: model-only picks scored {100 * mv / mvn:.1f}% ({mvn} picks), "
          f"obvious-only picks {100 * ob / obn:.1f}% ({obn} picks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
