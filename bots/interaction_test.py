#!/usr/bin/env python3
"""interaction_test.py -- does letting the features interact (all pairwise products + squares) beat the plain logistic lanes?
Fit 2023-24, graded once on 2025-26. Log loss and best-pick-per-game, plain vs interactions.   python bots/interaction_test.py nhl|nba CACHE"""
import itertools, math, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tune_models import fit_logistic, topk

sport, cache = sys.argv[1], sys.argv[2]
if sport == "nhl":
    import nhl_backtest as m; LANES = ["pts", "ast", "sog"]
else:
    import buckets_backtest as m; LANES = ["reb", "ast", "3pm", "pra"]
rows = []
for s in (2023, 2024, 2025):
    rows += m.run(s, cache)
rows = [r for r in rows if r.get("lanes")]
tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
def ll(p, y): p = np.clip(p, 1e-6, 1 - 1e-6); return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
for lane in LANES:
    names = sorted(tr[0]["lanes"][lane]["f"])
    X = lambda rs: np.array([[r["lanes"][lane]["f"][n] for n in names] for r in rs], float)
    Xtr, Xte = X(tr), X(te)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    A, B = (Xtr - mu) / sd, (Xte - mu) / sd
    def expand(Z): return np.hstack([Z] + [Z[:, [i]] * Z[:, [j]] for i, j in itertools.combinations_with_replacement(range(Z.shape[1]), 2)])
    ytr = np.array([r["lanes"][lane]["y"] for r in tr], float); yte = np.array([r["lanes"][lane]["y"] for r in te], float)
    res = {}
    for label, f, l2 in (("plain", lambda Z: Z, 5.0), ("interactions", expand, 50.0)):
        FA, FB = f(A), f(B)
        w = fit_logistic(FA, ytr, l2=l2)
        z = FB @ w[1:] + w[0]
        for r, zz in zip(te, z): r["_z"] = float(zz); r["_g"] = (r["night"], r["game"]); r["_y"] = r["lanes"][lane]["y"]
        res[label] = (ll(1 / (1 + np.exp(-z)), yte), topk(te, "_z", "_y", 1, "_g"), topk(te, "_z", "_y", 10, "night"))
    print(f"{sport} {lane:<4} " + "   ".join(f"{k}: logloss {v[0]:.4f} top1/game {v[1]:.1f}% top10/night {v[2]:.1f}%" for k, v in res.items()))
