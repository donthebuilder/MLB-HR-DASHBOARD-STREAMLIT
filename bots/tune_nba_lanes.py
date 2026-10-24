#!/usr/bin/env python3
"""
tune_nba_lanes.py -- the other BUCKETS lanes (REB 10+, AST 8+, 3PM 4+, PRA 35+) through the same fit-early / grade-late test.

    python bots/tune_nba_lanes.py [--cache DIR] [--lane reb|ast|3pm|pra]

Fit on 2023-24 and 2024-25, graded once on 2025-26. Each lane: the plain stat (his season average) is the baseline to beat, the tuned
logistic uses his recent rate, minutes, shots, the opponent's allowed number, pace, home, back-to-back, teammates out and the book's spread/total
where the closing lines exist. Reports the best pick per game and the top 10 per night, calibration by tenth, and the model-view picks, then saves
models/nba_<lane>_v1.json (with isotonic calibration from the holdout) for the live board.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb          # noqa: E402
from tune_models import fit_logistic, topk   # noqa: E402

LABEL = {"reb": "REB 10+", "ast": "AST 8+", "3pm": "3PM 4+", "pra": "PRA 35+"}
TRAIN, TEST = [2023, 2024], 2025


def pav(pr):
    step = max(150, len(pr) // 40)
    blocks = [[sum(t[0] for t in pr[i:i + step]) / len(pr[i:i + step]), sum(t[1] for t in pr[i:i + step]) / len(pr[i:i + step]), len(pr[i:i + step])]
              for i in range(0, len(pr), step)]
    out = []
    for b in blocks:
        out.append(b)
        while len(out) > 1 and out[-2][1] > out[-1][1]:
            y, x = out.pop(), out.pop()
            n_ = x[2] + y[2]
            out.append([(x[0] * x[2] + y[0] * y[2]) / n_, (x[1] * x[2] + y[1] * y[2]) / n_, n_])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    ap.add_argument("--lane", choices=list(LABEL), default=None)
    a = ap.parse_args()
    rows = []
    for s in TRAIN + [TEST]:
        rows += bb.run(s, a.cache)
    rows = [r for r in rows if r.get("lanes")]
    print(f"{len(rows)} player-games with the lane data; fit on {TRAIN}, graded on {TEST}\n")
    for lane in ([a.lane] if a.lane else list(LABEL)):
        tr = [r for r in rows if r["season"] in TRAIN]
        te = [r for r in rows if r["season"] == TEST]
        names = sorted(tr[0]["lanes"][lane]["f"])
        X = lambda rs: np.array([[r["lanes"][lane]["f"][n] for n in names] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([r["lanes"][lane]["y"] for r in tr], float))
        for r, z in zip(te, ((X(te) - mu) / sd) @ w[1:] + w[0]):
            r["tuned"] = float(z); r["y"] = r["lanes"][lane]["y"]; r["base"] = r["lanes"][lane]["avg"]; r["game"] = (r["night"], r["game"]) if not isinstance(r["game"], tuple) else r["game"]
        base = 100 * sum(r["y"] for r in te) / len(te)
        print(f"{LABEL[lane]}   (graded on {len(te)} player-games, base rate {base:.1f}%)")
        for label, key in (("his season average", "base"), ("tuned", "tuned")):
            print(f"   {label:<20} top1/game {topk(te, key, 'y', 1, 'game'):5.1f}%   top3/game {topk(te, key, 'y', 3, 'game'):5.1f}%   top10/night {topk(te, key, 'y', 10, 'night'):5.1f}%")
        print("   weights: " + ", ".join(f"{n} {c:+.2f}" for n, c in sorted(zip(names, w[1:]), key=lambda t: -abs(t[1]))[:6]))
        pr = sorted(((1 / (1 + math.exp(-r["tuned"])), r["y"]) for r in te), key=lambda t: t[0])
        cal = " ".join(f"{100 * sum(t[0] for t in pr[i * len(pr) // 10:(i + 1) * len(pr) // 10]) / max(1, len(pr[i * len(pr) // 10:(i + 1) * len(pr) // 10])):.0f}>{100 * sum(t[1] for t in pr[i * len(pr) // 10:(i + 1) * len(pr) // 10]) / max(1, len(pr[i * len(pr) // 10:(i + 1) * len(pr) // 10])):.0f}" for i in range(10))
        print(f"   calibration, predicted>actual % by tenth: {cal}")
        night = defaultdict(list)
        for r in te:
            night[r["night"]].append(r)
        mv = ob = mvn = 0
        for v in night.values():
            T = {id(r) for r in sorted(v, key=lambda r: r["tuned"], reverse=True)[:10]}
            N = {id(r) for r in sorted(v, key=lambda r: r["base"], reverse=True)[:10]}
            for r in v:
                if id(r) in T and id(r) not in N:
                    mv += r["y"]; mvn += 1
                if id(r) in N and id(r) not in T:
                    ob += r["y"]
        print(f"   nightly top 10, where tuned and the season average disagree: tuned-only {100 * mv / max(1, mvn):.1f}% vs average-only {100 * ob / max(1, mvn):.1f}%  ({mvn} picks each side)\n")
        # production fit on everything, calibrated from the holdout, saved for the live board
        allr = tr + te
        Xa = X(allr); amu, asd = Xa.mean(0), Xa.std(0) + 1e-9
        aw = fit_logistic((Xa - amu) / asd, np.array([r["lanes"][lane]["y"] for r in allr], float))
        out = Path(__file__).resolve().parent / "models"
        out.mkdir(exist_ok=True)
        k = pav(pr)
        (out / f"nba_{lane}_v1.json").write_text(json.dumps({"model_version": f"nba_{lane}_v1", "target": LABEL[lane], "features": names, "mu": amu.tolist(), "sd": asd.tolist(),
                                                              "intercept": float(aw[0]), "weights": aw[1:].tolist(),
                                                              "fit_on": f"{len(allr)} player-games, seasons {TRAIN + [TEST]}",
                                                              "calibration": {"x": [round(b[0], 5) for b in k], "y": [round(b[1], 5) for b in k]}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
