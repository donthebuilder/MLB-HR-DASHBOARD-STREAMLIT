#!/usr/bin/env python3
"""nfl_tuned_td.py -- a fitted, calibrated anytime-TD model on the same player-weeks as nfl_td_v2 (hand-weighted percentiles).

    python bots/nfl/nfl_tuned_td.py [--cache DIR]

Fit on 2023-24, graded once on 2025 (v2's weights were swept on 2024/25, so v2 has the edge on that season; if the fit still
matches or beats it, that is conservative). Logistic over trailing-only features from nfl_features.build: red-zone/goal-line
opportunities, xTD, targets/carries, snap %, touchdowns, target share, the game line (implied total, spread), home, the opponent's
TDs allowed by type, team red-zone TD rate, position. Reports top-5 and top-15 per week and best per game vs v2, log loss and
calibration by tenth.
"""
import argparse, math, os, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
from nfl_scoring import MODELS, derive, _pctile     # noqa: E402
from nfl_td_v3_backtest import _prep, _score, _topk, _auc, VARIANTS   # noqa: E402
from tune_models import fit_logistic                # noqa: E402

FEATS = ["f_rz_opp", "f_gl_opp", "f_xtd", "f_targets", "f_carries", "f_snap_pct", "f_td_actual", "f_target_share", "f_air_yards_share",
         "implied_total", "spread", "is_home", "f_opp_d_rec_td", "f_opp_d_rush_td", "f_tm_rz_td_rate", "f_gp"]
POSD = ["QB", "RB", "WR", "TE"]


def table(s, cache):
    from nfl_features import build
    p = Path(cache) / f"build_{s}.parquet"
    if p.exists():
        return pl.read_parquet(p)
    t = build(s); p.parent.mkdir(parents=True, exist_ok=True); t.write_parquet(p); return t


def with_out(d):
    """Regulars who did NOT play this week (the NHL/NBA 'teammates out' idea). The table drops listed-out players, so absence is read
    from the table itself: a player with real trailing usage (3+ targets or 5+ carries a game) who has a row in the team's previous game but none now.
    Sums their trailing targets, carries and red-zone opportunity."""
    rows = d.select(["team", "week", "player_id", "f_targets", "f_carries", "f_rz_opp", "season"]).to_dicts()
    by_tw = {}
    for r in rows:
        by_tw.setdefault((r["season"], r["team"]), {}).setdefault(r["week"], {})[r["player_id"]] = r
    out = {}
    for (se, tm), wk in by_tw.items():
        ws = sorted(wk)
        for i, w in enumerate(ws):
            t = c = z = 0.0
            if i:
                for pid, r in wk[ws[i - 1]].items():
                    if pid not in wk[w] and ((r["f_targets"] or 0) >= 3 or (r["f_carries"] or 0) >= 5):
                        t += r["f_targets"] or 0; c += r["f_carries"] or 0; z += r["f_rz_opp"] or 0
            out[(se, tm, w)] = (t, c, z)
    return d.with_columns([pl.Series("tm_out_tgt", [out.get((r["season"], r["team"], r["week"]), (0, 0, 0))[0] for r in d.select(["season", "team", "week"]).to_dicts()]),
                           pl.Series("tm_out_car", [out.get((r["season"], r["team"], r["week"]), (0, 0, 0))[1] for r in d.select(["season", "team", "week"]).to_dicts()]),
                           pl.Series("tm_out_rz", [out.get((r["season"], r["team"], r["week"]), (0, 0, 0))[2] for r in d.select(["season", "team", "week"]).to_dicts()])])


def matrix(d):
    cols = [d[c].cast(pl.Float64).fill_null(0.0).to_numpy() if c in d.columns else np.zeros(d.height) for c in FEATS]
    for pz in POSD:
        cols.append((d["position"] == pz).cast(pl.Float64).to_numpy())
    return np.column_stack(cols)


def save_model(D, te_z, te_y, path):
    """Production fit on every season, Platt (+tail) calibration from the holdout run above. Read by td_fit.py."""
    import json
    allr = pl.concat([D[2023], D[2024], D[2025]], how="diagonal_relaxed")
    X = matrix(allr); mu, sd = X.mean(0), X.std(0) + 1e-9
    w = fit_logistic((X - mu) / sd, allr["hit"].to_numpy().astype(float), l2=20.0)
    ab = fit_logistic(te_z.reshape(-1, 1), te_y, l2=0.01)
    qs = np.sort(1 / (1 + np.exp(-(ab[0] + ab[1] * te_z))))
    t = float(qs[int(len(qs) * 0.95)])
    Path(path).write_text(json.dumps({"model_version": "nfl_td_fit_v1", "target": "anytime TD", "features": FEATS + POSD, "mu": mu.tolist(), "sd": sd.tolist(),
                                      "intercept": float(w[0]), "weights": w[1:].tolist(), "fit_on": f"{allr.height} player-weeks, 2023-25",
                                      "platt": {"a": float(ab[0]), "b": float(ab[1]), "tail": {"t": t, "slope": 0.25}}}, indent=1))
    print(f"  saved {path}  (Platt a {ab[0]:+.3f} b {ab[1]:.3f}, tail above {100 * t:.1f}%)")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl"))
    ap.add_argument("--save", default=None); ap.add_argument("--out-feats", action="store_true"); ap.add_argument("--fit", nargs="+", default=["2023", "2024"]); ap.add_argument("--test", default="2025"); a = ap.parse_args()
    if a.out_feats:
        FEATS.extend(["tm_out_tgt", "tm_out_car", "tm_out_rz"])
    D = {s: _prep(table(s, a.cache)).filter(pl.col("f_gp") >= 3) if "f_gp" in table(s, a.cache).columns else _prep(table(s, a.cache)) for s in (2023, 2024, 2025)}
    if a.out_feats:
        D = {k: with_out(v) for k, v in D.items()}
    tr_seasons, te_season = [int(x) for x in a.fit], int(a.test)
    tr = pl.concat([D[x] for x in tr_seasons], how="diagonal_relaxed"); te = D[te_season]
    Xtr, Xte = matrix(tr), matrix(te)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = fit_logistic((Xtr - mu) / sd, tr["hit"].to_numpy().astype(float), l2=20.0)
    z = ((Xte - mu) / sd) @ w[1:] + w[0]
    p = 1 / (1 + np.exp(-z)); y = te["hit"].to_numpy().astype(float)
    te = te.with_columns(pl.Series("tuned", z))
    v2 = _score(te, VARIANTS["v2 (published)"])
    tu = te.with_columns(pl.col("tuned").alias("score"))
    print(f"{te_season}: {te.height} player-weeks, base {100 * y.mean():.1f}%   (fit on {tr.height} player-weeks, {tr_seasons})")
    for name, x in (("v2 published", v2), ("tuned logistic", tu)):
        (h5, n5), (h15, n15) = _topk(x, 5), _topk(x, 15)
        print(f"  {name:<16} top-5/wk {100 * h5 / n5:5.1f}%   top-15/wk {100 * h15 / n15:5.1f}%   AUC {_auc(x):.4f}")
    # best per game
    for name, x, key in (("v2 published", v2, "score"), ("tuned logistic", tu, "score")):
        hit = n = 0
        for _, g in x.group_by("game_id"):
            hit += int(g.sort([key, "player_id"], descending=[True, False]).head(1)["hit"][0]); n += 1
        print(f"  {name:<16} best pick per game {100 * hit / n:5.1f}%  ({n} games)")
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    print(f"  tuned log loss {-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)):.4f} vs base-rate {-np.mean(y * math.log(y.mean()) + (1 - y) * math.log(1 - y.mean())):.4f}")
    o = np.argsort(p); k = len(o) // 10
    print("  calibration by tenth (pred>actual %): " + " ".join(f"{100 * p[o[i * k:(i + 1) * k if i < 9 else None]].mean():.0f}>{100 * y[o[i * k:(i + 1) * k if i < 9 else None]].mean():.0f}" for i in range(10)))
    if a.save:
        save_model(D, z, y, a.save)
    print("  weights: " + ", ".join(f"{n} {c:+.2f}" for n, c in sorted(zip(FEATS + POSD, w[1:]), key=lambda t: -abs(t[1]))[:8]))


if __name__ == "__main__":
    main()
