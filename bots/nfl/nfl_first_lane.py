#!/usr/bin/env python3
"""
nfl_first_lane.py -- NFL FIRST TOUCHDOWN as a calibrated probability for every RB/WR/TE, plus a top-3-per-game flag.

    python bots/nfl/nfl_first_lane.py [--cache DIR] [--save FILE]

Rows from nfl_first.build_rows (candidates with 3+ games, trailing early usage, first-TD history, the opponent's first-TD record, v2 score) plus the game's
implied total and spread. Fit 2023-24, graded once on 2025. One first TD per game, so each game's probabilities are rescaled to sum to the share of games whose
first TD went to a listed RB/WR/TE (the rest: QBs, kickers, defence, returners). Top pick / top 3 / top 5 per game vs nfl_td_v2 and plain touches.
"""
import argparse, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import nfl_first as NF               # noqa: E402
from tune_models import fit_logistic    # noqa: E402

COLS = ["v2", "touches", "early_pg", "early_share", "first_rate", "opp_concedes_first", "is_home", "implied_total", "spread"]


def rescale(rows, z, share):
    p = 1 / (1 + np.exp(-z)); out = np.zeros(len(rows)); by = defaultdict(list)
    for i, r in enumerate(rows):
        by[r["game"]].append(i)
    for ix in by.values():
        out[ix] = p[ix] / p[ix].sum() * share
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl")); ap.add_argument("--save", default=None); ap.add_argument("--test", type=int, default=2025); a = ap.parse_args()
    rows = NF.build_rows(a.cache, (2023, 2024, 2025, 2026))
    tr = [r for r in rows if r["season"] < a.test]; te = [r for r in rows if r["season"] == a.test]
    ngames_tr = len({r["game"] for r in tr})
    share = sum(r["y"] for r in tr) / ngames_tr
    X = lambda rs: np.array([[r[c] for c in COLS] for r in rs], float)
    Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = fit_logistic((Xtr - mu) / sd, np.array([r["y"] for r in tr], float), l2=20.0)
    z = ((X(te) - mu) / sd) @ w[1:] + w[0]
    y = np.array([r["y"] for r in te], float)
    ab = fit_logistic(z.reshape(-1, 1), y, l2=0.01)                 # Platt on the holdout: the raw top end over-predicted
    zc = ab[0] + ab[1] * z
    p = rescale(te, zc, share)
    tail_t = float(np.quantile(p, 0.95))                                   # the top 2% over-predicted: compress above the 95th percentile
    p = np.where(p > tail_t, tail_t + (p - tail_t) * 0.25, p)
    games = defaultdict(list)
    for i, r in enumerate(te):
        games[r["game"]].append(i)
    games = {k: v for k, v in games.items() if len(v) >= 15}
    print(f"{len(te)} candidate-games, {len(games)} games graded ({a.test}); fit {len(tr)} (earlier seasons); first TD went to a listed RB/WR/TE in {100 * share:.0f}% of games")
    def pick(score):
        t = [0, 0, 0]
        for ix in games.values():
            o = sorted(ix, key=lambda i: score[i], reverse=True)
            t[0] += y[o[0]]; t[1] += y[o[:3]].sum(); t[2] += y[o[:5]].sum()
        n = len(games); return [100 * v / n for v in t]
    for label, sc in (("nfl_td_v2 score", np.array([r["v2"] for r in te])), ("touches per game", np.array([r["touches"] for r in te])), ("fitted model", p)):
        t = pick(sc); print(f"  {label:<18} top 1 {t[0]:5.1f}%   top 3 {t[1]:5.1f}%   top 5 {t[2]:5.1f}%")
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    print(f"  fitted log loss {-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)):.4f} vs flat {-np.mean(y * math.log(y.mean()) + (1 - y) * math.log(1 - y.mean())):.4f}")
    o = np.argsort(p); k = len(o) // 8
    print("  calibration by eighth (pred>actual %): " + " ".join(f"{100 * p[o[i * k:(i + 1) * k if i < 7 else None]].mean():.1f}>{100 * y[o[i * k:(i + 1) * k if i < 7 else None]].mean():.1f}" for i in range(8)))
    top = np.argsort(-p)[: len(p) // 50]
    print(f"  top 2% of candidates: predicted {100 * p[top].mean():.1f}% vs actual {100 * y[top].mean():.1f}%")
    print(f"  tail compression above {100 * tail_t:.1f}%")
    print(f"  Platt a {ab[0]:+.3f} b {ab[1]:.3f} (fitted on this holdout, so the calibration rows above are in-sample for it)")
    print("  weights: " + ", ".join(f"{c} {v:+.2f}" for c, v in sorted(zip(COLS, w[1:]), key=lambda t: -abs(t[1]))[:6]))
    if a.save:
        tail_t = float(tail_t)
        Xa = X(rows); amu, asd = Xa.mean(0), Xa.std(0) + 1e-9
        aw = fit_logistic((Xa - amu) / asd, np.array([r["y"] for r in rows], float), l2=20.0)
        ash = sum(r["y"] for r in rows) / len({r["game"] for r in rows})
        Path(a.save).write_text(json.dumps({"model_version": "nfl_first_td_v1", "target": "first TD of the game (RB/WR/TE)", "features": COLS, "mu": amu.tolist(), "sd": asd.tolist(),
                                            "platt": {"a": float(ab[0]), "b": float(ab[1]), "note": "fitted on the 2025 holdout raw logit; applied before the per-game rescale"}, "tail": {"t": tail_t, "slope": 0.25, "note": "applied after the rescale to the top 2%; no renormalisation"}, "intercept": float(aw[0]), "weights": aw[1:].tolist(), "listed_share": ash, "fit_on": f"{len(rows)} candidate-games, 2023-26"}, indent=1))
        print(f"  saved {a.save}")


if __name__ == "__main__":
    main()
