#!/usr/bin/env python3
"""Candidate weight vectors for every NFL market, frozen from the SELECTION folds only, scored once on the 2025 holdout.

    python model_lab/nfl_formula_audit.py selection     # produces the per-fold fitted weights
    python model_lab/nfl_weight_candidates.py           # reads them, freezes, evaluates 2025

Freezing rule (fixed before the holdout was scored): average the three selection-fold non-negative fitted weights,
round to 2 decimals, drop anything under 0.02, renormalise. A candidate is "stable" when every selection fold's fitted
weight is within +/-0.10 of that average. p-values are Holm-corrected over the seven markets.
"""
import json, os, sys
from math import erf, sqrt
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model_lab"))
import nfl_formula_audit as A  # noqa: E402
import nfl_td_walkforward as W  # noqa: E402
import polars as pl  # noqa: E402

def main():
    sel = json.load(open(os.path.join(ROOT, "model_lab", "results", "nfl_formula_audit_selection.json")))["markets"]
    d = A.load_all()
    out, rows = {}, []
    for key, m in sel.items():
        folds = m["fitted_weights_by_fold"]
        comps = list(m["weights_used_after_availability"])
        avg = {c: float(np.mean([folds[f][c] for f in folds])) for c in comps}
        frozen = {c: round(v, 2) for c, v in avg.items() if round(v, 2) >= 0.02}
        t = sum(frozen.values()); frozen = {c: round(v / t, 3) for c, v in frozen.items()}
        spread = {c: max(abs(folds[f][c] - avg[c]) for f in folds) for c in comps}
        stable = all(v <= 0.10 for v in spread.values())
        f0 = A.percentiles(A.market_frame(d, key), key)
        tr = f0.filter(pl.col("sea") < A.HOLD); te = f0.filter(pl.col("sea") == A.HOLD)
        base_w = dict(m["weights_used_after_availability"])
        ytr, yte = tr["y"].to_numpy(), te["y"].to_numpy()
        res = {}
        for name, w in (("published", base_w), ("candidate", frozen)):
            s_tr, s_te = A.blend(tr, w), A.blend(te, w)
            p = A.platt(s_tr, ytr, s_te)
            r = W.metrics(te["sw"].to_numpy(), p, yte); r["top5"] = W.topk_hit(te["sw"].to_numpy(), s_te, yte, 5)
            res[name] = (r, p)
        diff, lo, hi = W.paired_bootstrap(te["sw"].to_numpy(), res["candidate"][1], res["published"][1], yte, n_boot=2000)
        se = (hi - lo) / (2 * 1.96); z = diff / se if se > 0 else 0.0; pv = 1 - erf(abs(z) / sqrt(2))
        out[key] = {"published_weights": base_w, "fold_average_fitted": {c: round(v, 3) for c, v in avg.items()}, "frozen_candidate": frozen,
                    "max_fold_deviation": {c: round(v, 3) for c, v in spread.items()}, "stable": stable,
                    "holdout": {"n": int(len(yte)), "published": {k: res["published"][0][k] for k in ("logloss", "auc", "top5")},
                                "candidate": {k: res["candidate"][0][k] for k in ("logloss", "auc", "top5")},
                                "d_logloss": diff, "lo": lo, "hi": hi, "p": pv}}
        rows.append((key, pv))
    order = sorted(rows, key=lambda r: r[1]); ok = True
    for i, (k, pv) in enumerate(order):
        thr = 0.05 / (len(order) - i); ok = ok and pv <= thr
        out[k]["holm_threshold"] = thr; out[k]["significant_after_holm"] = bool(ok and out[k]["holdout"]["d_logloss"] < 0)
    json.dump(out, open(os.path.join(ROOT, "model_lab", "results", "nfl_weight_candidates.json"), "w"), indent=1, default=float)
    for k, v in out.items():
        h = v["holdout"]
        print(f"{k:9} n={h['n']:5} stable={v['stable']!s:5} LL {h['published']['logloss']:.4f}->{h['candidate']['logloss']:.4f} dLL {h['d_logloss']:+.4f} [{h['lo']:+.4f},{h['hi']:+.4f}] p={h['p']:.3f} holm<={v['holm_threshold']:.4f} sig={v['significant_after_holm']}  AUC {h['published']['auc']:.3f}->{h['candidate']['auc']:.3f}")
        print("          candidate:", v["frozen_candidate"])

if __name__ == "__main__":
    main()
