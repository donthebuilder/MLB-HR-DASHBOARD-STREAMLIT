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
import json
import math
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

    # production fit: the same recipe on ALL seasons, saved for the live board (the holdout numbers above stay the honest ones)
    allX = np.array([[r["f"][n] for n in names] for r in rows], float)
    amu, asd = allX.mean(0), allX.std(0) + 1e-9
    aw = fit_logistic((allX - amu) / asd, np.array([r[primary] for r in rows], float))
    out = Path(__file__).resolve().parent / "models"
    out.mkdir(exist_ok=True)
    version = {"nhl": "nhl_goal_v2", "nba": "nba_pts_v2"}[a.sport]
    (out / f"{version}.json").write_text(json.dumps({
        "model_version": version, "target": cfg["target"][primary], "features": names,
        "mu": amu.tolist(), "sd": asd.tolist(), "intercept": float(aw[0]), "weights": aw[1:].tolist(),
        "fit_on": f"{len(rows)} player-games, seasons {cfg['train'] + [cfg['test']]}",
        "holdout_note": f"honest numbers come from the fit on {cfg['train']} graded on {cfg['test']} (tune_models.py output)"}, indent=1))
    print(f"saved bots/models/{version}.json")

    print(f"\nWhat carries weight (standardised coefficients, fitted on '{cfg['target'][primary]}'):")
    for n, c in sorted(zip(names, w[1:]), key=lambda t: -abs(t[1])):
        print(f"   {n:<16}{c:+.3f}")

    for tgt, tl in cfg["target"].items():
        base = 100 * sum(r[tgt] for r in test) / len(test)
        print(f"\nHOLDOUT {cfg['test']}, {tl}  (base rate {base:.1f}%)")
        for label, key in (("shipped v1 score", cfg["shipped"]), (cfg["naive_label"], cfg["naive"]), ("tuned", "tuned")):
            print(f"   {label:<18} top1/game {topk(test, key, tgt, 1, 'game'):5.1f}%   top3/game {topk(test, key, tgt, 3, 'game'):5.1f}%"
                  f"   top10/night {topk(test, key, tgt, 10, 'night'):5.1f}%")

    # isotonic calibration from the HOLDOUT predictions (model fitted on train seasons only), saved beside the model:
    # live boards map raw -> calibrated so a "50%" means what 50% meant when it was graded.
    pr0 = sorted(((1 / (1 + math.exp(-r["tuned"])), r[primary]) for r in test), key=lambda t: t[0])
    step = max(150, len(pr0) // 40)
    blocks = [[sum(t[0] for t in pr0[i:i + step]) / len(pr0[i:i + step]), sum(t[1] for t in pr0[i:i + step]) / len(pr0[i:i + step]), len(pr0[i:i + step])]
              for i in range(0, len(pr0), step)]
    pav = []
    for b in blocks:
        pav.append(b)
        while len(pav) > 1 and pav[-2][1] > pav[-1][1]:
            y, x = pav.pop(), pav.pop()
            n_ = x[2] + y[2]
            pav.append([(x[0] * x[2] + y[0] * y[2]) / n_, (x[1] * x[2] + y[1] * y[2]) / n_, n_])
    cal = json.loads((out / f"{version}.json").read_text())
    cal["calibration"] = {"x": [round(b[0], 5) for b in pav], "y": [round(b[1], 5) for b in pav],
                          "note": f"isotonic fit of actual vs predicted on the {cfg['test']} holdout; kept for reference"}
    zz = np.array([[r["tuned"]] for r in test], float)
    ab = fit_logistic(zz, np.array([r[primary] for r in test], float), l2=0.01)
    cal["platt"] = {"a": float(ab[0]), "b": float(ab[1]),
                    "note": f"p = sigmoid(a + b * raw logit), fitted on the {cfg['test']} holdout. Smooth and monotone: it corrects the over-confidence at the top without flattening it, "
                            f"so the order of the top names is the model's own."}
    pp = sorted(1 / (1 + math.exp(-(ab[0] + ab[1] * z[0]))) for z in zz)
    t = pp[int(len(pp) * 0.99)]                # above the 99th percentile of what the holdout saw, there are too few games to check a probability
    cal["platt"]["tail"] = {"t": t, "slope": 0.25,
                            "note": "past t, the probability is compressed (t + 0.25 x the excess) instead of extrapolated: strictly increasing, so no ties and no plateau, but not claiming precision the data cannot check"}
    print(f"Platt recalibration on the {cfg['test']} holdout: a {ab[0]:+.3f}, b {ab[1]:.3f}  (b < 1 = the raw model was over-confident); tail compression above {100 * t:.1f}%")
    (out / f"{version}.json").write_text(json.dumps(cal, indent=1))

    # calibration: does "40%" mean 40%? (holdout, by tenths of the fitted probability)
    pr = sorted(((1 / (1 + math.exp(-r["tuned"])), r[primary]) for r in test), key=lambda t: t[0])
    print(f"\nCALIBRATION on {cfg['test']} ({cfg['target'][primary]}): predicted vs actual, by tenth of the model's probability")
    for i in range(10):
        ch = pr[i * len(pr) // 10:(i + 1) * len(pr) // 10]
        print(f"   tenth {i + 1:>2}: predicted {100 * sum(c[0] for c in ch) / len(ch):5.1f}%   actual {100 * sum(c[1] for c in ch) / len(ch):5.1f}%   (n={len(ch)})")

    hi = [(1 / (1 + math.exp(-r["tuned"])), r[primary]) for r in test]
    print("   the very top of the range:")
    for lo_p, hi_p in ((0.30, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 1.0)) if a.sport == "nhl" else ((0.40, 0.50), (0.50, 0.60), (0.60, 1.0)):
        ch = [t for t in hi if lo_p <= t[0] < hi_p]
        if len(ch) >= 20:
            print(f"      predicted {int(lo_p * 100)}-{int(hi_p * 100) if hi_p < 1 else '+'}%: n={len(ch):5}  predicted {100 * sum(c[0] for c in ch) / len(ch):5.1f}%   actual {100 * sum(c[1] for c in ch) / len(ch):5.1f}%")
    if a.sport == "nhl":
        print("\nCALIBRATION, EARLY vs LATE SEASON (top two tenths of the model's probability): is the live board's October situation safe?")
        for label, sel in (("player has 5-11 games this season", lambda r: r["gp"] < 12), ("12-29 games", lambda r: 12 <= r["gp"] < 30), ("30+ games", lambda r: r["gp"] >= 30)):
            rs = sorted((r for r in test if sel(r)), key=lambda r: r["tuned"])
            top = rs[int(len(rs) * 0.8):]
            if top:
                print(f"   {label:<34} n={len(top):5}  predicted {100 * sum(1 / (1 + math.exp(-r['tuned'])) for r in top) / len(top):5.1f}%   actual {100 * sum(r[primary] for r in top) / len(top):5.1f}%")

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
