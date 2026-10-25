#!/usr/bin/env python3
"""
nfl_continuous.py -- passing yards and kicking points as a FORECAST OF THE NUMBER (a ridge regression of the stat on trailing form and context), not a yes/no at a fixed bar.

    python bots/nfl/nfl_continuous.py [--cache DIR] [--save]

The threshold models for these two markets were weak (AUC 0.69 and 0.59): 300+ yards happens in 12-17% of QB games and 6+ kicking points in two thirds, so a fixed bar throws away the information.
Here the model forecasts the stat, and the chance of going over ANY line follows from the size of its misses at that forecast. Judged on games it never saw (fit 2023-24 graded 2025; fit 2023-25
graded 2026 weeks to date) against the plain trailing average:
   mean absolute error of the forecast   (model vs trailing average)
   skill at a book-style line: line = the trailing average rounded to a half; log loss of P(over line) against the 0.693 of a coin; calibration by band
--save writes models/nfl_<market>_cont_v1.json (coefficients, residual spread by forecast size) for the line-edge tools.
"""
import argparse, json, math, sys
from pathlib import Path
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import nfl_lanes as L                 # noqa: E402
import nfl_more as NM                 # noqa: E402

CFG = {"PASS_YDS": {"stat": "passing_yards", "trail": "f_passing_yards", "extra": ["f_passing_tds", "f_completions", "f_attempts", "f_passing_cpoe", "f_sacks", "total_line", "wind_mph", "indoors", "spread", "implied_total"]},
       "KICK_PTS": {"stat": "kp", "trail": "f_kp", "extra": ["implied_total", "total_line", "spread", "wind_mph", "indoors"]}}      # kicker: the game environment alone beat adding his own form (pooled log loss 0.6396 vs 0.6414 with form, 0.6546 trailing only)
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prep(T, m):
    cfg = CFG[m]; out = {}
    for s, t in T.items():
        d = t.filter(pl.col("season_type") == "REG")
        if m == "KICK_PTS":
            d = d.with_columns((pl.col("fg_made").fill_null(0) * 3 + pl.col("pat_made").fill_null(0)).alias("kp"))
            d = d.with_columns(pl.col("kp").shift(1).rolling_mean(8, min_samples=2).over("player_id").alias("f_kp"))
        d = d.filter(pl.col("position") == ("QB" if m == "PASS_YDS" else "K"))
        d = d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d
        if m == "KICK_PTS":
            d = d.filter(pl.col("f_kp").is_not_null() & (pl.col("f_kp") > 0))               # a kicker with no trailing games has no baseline: leaving him in made the line 0.5 and the "skill" trivial
        else:
            d = d.filter(pl.col(cfg["trail"]).is_not_null() & (pl.col(cfg["trail"]) > 50))   # a real starter's trailing average, not a backup's two throws
        out[s] = d
    return out


def matrix(d, cols):
    return np.column_stack([d[c].cast(pl.Float64).fill_null(0.0).to_numpy() if c in d.columns else np.zeros(d.height) for c in cols])


def ridge(X, y, lam=10.0):
    A = np.hstack([np.ones((len(X), 1)), X]); P = np.eye(A.shape[1]) * lam; P[0, 0] = 0
    return np.linalg.solve(A.T @ A + P, A.T @ y)


def fit_predict(m, tr, te):
    cfg = CFG[m]; cols = [cfg["trail"]] + cfg["extra"]
    Xtr, Xte = matrix(tr, cols), matrix(te, cols)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    y = tr[cfg["stat"]].cast(pl.Float64).fill_null(0).to_numpy()
    w = ridge((Xtr - mu) / sd, y)
    pred_tr = np.hstack([np.ones((len(Xtr), 1)), (Xtr - mu) / sd]) @ w
    pred_te = np.hstack([np.ones((len(Xte), 1)), (Xte - mu) / sd]) @ w
    # residual spread by forecast size (terciles of the training forecasts)
    q = np.quantile(pred_tr, [1 / 3, 2 / 3]); res = y - pred_tr
    sds = [float(np.std(res[pred_tr <= q[0]])), float(np.std(res[(pred_tr > q[0]) & (pred_tr <= q[1])])), float(np.std(res[pred_tr > q[1]]))]
    return pred_te, q, sds, (w, mu, sd, cols)


def spread_for(p, q, sds):
    return np.where(p <= q[0], sds[0], np.where(p <= q[1], sds[1], sds[2]))


def evaluate(m, tr, te, label):
    cfg = CFG[m]
    pred, q, sds, _ = fit_predict(m, tr, te)
    y = te[cfg["stat"]].cast(pl.Float64).fill_null(0).to_numpy()
    base = te[cfg["trail"]].cast(pl.Float64).fill_null(0).to_numpy()
    mae_m, mae_b = np.mean(np.abs(y - pred)), np.mean(np.abs(y - base))
    line = np.floor(base) + 0.5
    over = (y > line).astype(float)
    sd = spread_for(pred, q, sds)
    p_model = np.array([1 - Phi((l - m_) / s) for l, m_, s in zip(line, pred, sd)])
    sd_b = float(np.std(y - base))
    p_base = np.array([1 - Phi((l - b) / sd_b) for l, b in zip(line, base)])
    ll = lambda p: -np.mean(over * np.log(np.clip(p, 1e-4, 1 - 1e-4)) + (1 - over) * np.log(np.clip(1 - p, 1e-4, 1 - 1e-4)))
    o = np.argsort(p_model); k = max(1, len(o) // 6)
    cal = " ".join(f"{100 * p_model[o[i * k:(i + 1) * k if i < 5 else None]].mean():.0f}>{100 * over[o[i * k:(i + 1) * k if i < 5 else None]].mean():.0f}" for i in range(6))
    corr = float(np.corrcoef(pred, y)[0, 1]); corr_b = float(np.corrcoef(base, y)[0, 1])
    print(f"   {label}: {len(y)} player-games, mean {y.mean():.1f}  MAE model {mae_m:.2f} vs trailing average {mae_b:.2f}  correlation {corr:.3f} vs {corr_b:.3f}\n"
          f"      line = trailing average rounded to a half: P(over) log loss model {ll(p_model):.4f} vs average-only {ll(p_base):.4f} vs coin 0.6931; calibration by sixth (pred>actual %): {cal}")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl")); ap.add_argument("--save", action="store_true"); a = ap.parse_args()
    NM.register(); T = NM.augment(a.cache)
    for m in CFG:
        D = prep(T, m)
        print(f"\n{m}  (forecast of {CFG[m]['stat']})")
        evaluate(m, pl.concat([D[2023], D[2024]], how="diagonal_relaxed"), D[2025], "graded 2025 (fit 2023-24)")
        if D[2026].height:
            evaluate(m, pl.concat([D[2023], D[2024], D[2025]], how="diagonal_relaxed"), D[2026], "graded 2026 W1-7 (fit 2023-25)")
        if a.save:
            allr = pl.concat([D[s] for s in D if D[s].height], how="diagonal_relaxed")
            pred, q, sds, (w, mu, sd, cols) = fit_predict(m, allr, allr.head(5))
            (HERE.parent / "models" / f"nfl_{m.lower()}_cont_v1.json").write_text(json.dumps({"model_version": f"nfl_{m.lower()}_cont_v1", "stat": CFG[m]["stat"], "features": cols, "mu": mu.tolist(), "sd": sd.tolist(), "weights": w.tolist(), "q": q.tolist(), "resid_sd": sds}, indent=1))
            print(f"   saved models/nfl_{m.lower()}_cont_v1.json")


if __name__ == "__main__":
    main()
