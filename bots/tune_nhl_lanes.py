#!/usr/bin/env python3
"""
tune_nhl_lanes.py -- the NHL lanes (points 1+, assists 1+, shots on goal 3+) through the same fit-early / grade-late test.

    python bots/tune_nhl_lanes.py [--cache DIR] [--lane pts|ast|sog]

Fit on 2023-24 and 2024-25, graded once on 2025-26. Each lane: the plain stat (his season average) is the baseline to beat, the tuned
logistic uses his recent rate, minutes, shots, the opponent's allowed number, pace, home, back-to-back, teammates out and the book's spread/total
. Reports the best pick per game and the top 10 per night, calibration by tenth, and the model-view picks, then saves
models/nhl_<lane>_v1.json (with isotonic calibration from the holdout) for the live board.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as bb          # noqa: E402
from tune_models import fit_logistic, topk   # noqa: E402

LABEL = {"pts": "NHL POINTS 1+", "ast": "NHL ASSISTS 1+", "sog": "NHL SHOTS 3+"}
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
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nhl"))
    ap.add_argument("--lane", choices=list(LABEL), default=None)
    a = ap.parse_args()
    rows = []
    for s in TRAIN + [TEST]:
        rows += bb.run(s, a.cache)
    rows = [r for r in rows if r.get("lanes")]
    try:                                                    # the current season to date: graded out-of-sample, then included in the saved fit
        from collections import defaultdict as _dd
        d25, d26 = bb.load_season(2025, a.cache), bb.load_season(2026, a.cache)
        comb = _dd(list)
        for k in set(d25) | set(d26):
            comb[k] = list(d25.get(k, [])) + list(d26.get(k, []))
        cur = [r for r in bb.run_data(comb, 2026) if r["night"] >= "2026-10-01" and r.get("lanes")]       # history carries over from last season
    except Exception as exc:                                # noqa: BLE001
        print(f"2026 not available ({type(exc).__name__}); saved models use 2023-25 only"); cur = []
    print(f"{len(rows)} player-games with the lane data; fit on {TRAIN}, graded on {TEST}\n")
    for lane in ([a.lane] if a.lane else list(LABEL)):
        tr = [r for r in rows if r["season"] in TRAIN]
        te = [r for r in rows if r["season"] == TEST]
        names = sorted(tr[0]["lanes"][lane]["f"])
        X = lambda rs: np.array([[r["lanes"][lane]["f"][n] for n in names] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([r["lanes"][lane]["y"] for r in tr], float))
        for r, z in zip(te, ((X(te) - mu) / sd) @ w[1:] + w[0]):
            r["tuned"] = float(z); r["y"] = r["lanes"][lane]["y"]; r["base"] = r["lanes"][lane]["avg"]; pass
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
        if cur:
            zc = ((X(cur) - amu) / asd) @ aw[1:] + aw[0]
            yc = np.array([r["lanes"][lane]["y"] for r in cur], float); pc = np.clip(1 / (1 + np.exp(-zc)), 1e-6, 1 - 1e-6)
            for r, z_ in zip(cur, zc):
                r["_z"] = float(z_); r["_y"] = r["lanes"][lane]["y"]; r["_b"] = r["lanes"][lane]["avg"]
            print(f"   2026 to date ({len(cur)} player-games, fit on 2023-25 never saw them): predicted {100 * pc.mean():.1f}% vs actual {100 * yc.mean():.1f}%; "
                  f"top1/game tuned {topk(cur, '_z', '_y', 1, 'game'):.1f}% vs his average {topk(cur, '_b', '_y', 1, 'game'):.1f}%")
            allr = allr + cur
            Xa = X(allr); amu, asd = Xa.mean(0), Xa.std(0) + 1e-9
            aw = fit_logistic((Xa - amu) / asd, np.array([r["lanes"][lane]["y"] for r in allr], float))
        out = Path(__file__).resolve().parent / "models"
        out.mkdir(exist_ok=True)
        k = pav(pr)
        zs = np.array([[math.log(p / (1 - p))] for p, _ in pr], float)
        ab = fit_logistic(zs, np.array([y for _, y in pr], float), l2=0.01)
        qs = sorted(1 / (1 + math.exp(-(ab[0] + ab[1] * z[0]))) for z in zs)
        t = qs[int(len(qs) * 0.99)]
        (out / f"nhl_{lane}_v1.json").write_text(json.dumps({"model_version": f"nhl_{lane}_v1", "target": LABEL[lane], "features": names, "mu": amu.tolist(), "sd": asd.tolist(),
                                                              "intercept": float(aw[0]), "weights": aw[1:].tolist(),
                                                              "fit_on": f"{len(allr)} player-games, seasons {TRAIN + [TEST]} + 2026 to date",
                                                              "calibration": {"x": [round(b[0], 5) for b in k], "y": [round(b[1], 5) for b in k]},
                                                              "platt": {"a": float(ab[0]), "b": float(ab[1]), "tail": {"t": t, "slope": 0.25}}}, indent=1))
        print(f"   Platt: a {ab[0]:+.3f}, b {ab[1]:.3f}; tail compression above {100 * t:.1f}%\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
