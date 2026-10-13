#!/usr/bin/env python3
"""DASH model lab: formula audit for EVERY NFL market (nfl_scoring.MODELS), plus selection-policy tests.

    python model_lab/nfl_formula_audit.py selection     # folds 2022-2024; weights fitted on earlier seasons only
    python model_lab/nfl_formula_audit.py holdout       # 2025, run once after selection

For each market (TD, REC_YDS, REC, RUSH_YDS, RUSH_ATT, PASS_YDS, KICK_PTS):
  * the published formula as a within-week percentile blend of its own components (the convention
    bots/nfl/nfl_td_lab.py uses; production additionally maps to a league reference and the MLB scale,
    which is monotone in this blend per week, so ranks are unchanged),
  * leave-one-component-out ablation and weight sensitivity (each weight x0.5 / x1.5, renormalised),
  * fitted non-negative weights (regularised logistic on the same component percentiles, train seasons only),
  * an unrestricted standardised logistic on the same components and a wider logistic on every trailing feature,
  * ranking and selection-policy diagnostics for the published top-5 card.
Outcome = the market's own grade: OUTCOME[market] >= bar. Metrics: log loss and AUC of a probability obtained
by a logistic map fitted on train seasons only (the published score is not a probability), top-5 / top-15 per
week, paired cluster bootstrap against the published formula.
"""
from __future__ import annotations

import json
import os
import sys
import warnings

import numpy as np
import polars as pl
from scipy.optimize import minimize
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model_lab"))
sys.path.insert(0, os.path.join(ROOT, "bots", "nfl"))
import nfl_td_walkforward as W  # noqa: E402  (metrics, bootstrap, loaders)
from build_nfl_tables import build as load_season  # noqa: E402
from nfl_scoring import MODELS, OUTCOME  # noqa: E402

SEASONS = W.SEASONS
SEL, HOLD = W.SELECTION_FOLDS, W.HOLDOUT
EPS = 1e-4
MARKETS = list(MODELS)


def load_all() -> pl.DataFrame:
    fr = [load_season(s).with_columns(pl.lit(s).alias("sea")) for s in SEASONS]
    d = pl.concat(fr, how="diagonal_relaxed")
    return d.with_columns((pl.col("sea") * 100 + pl.col("week")).alias("sw"))


def market_frame(d: pl.DataFrame, key: str) -> pl.DataFrame:
    m = MODELS[key]
    f = d.filter(pl.col("position").is_in(m["pos"]))
    return f.with_columns((OUTCOME[key] >= m["bar"]).cast(pl.Int8).alias("y"))


def comp_cols(key):
    return [(raw.lstrip("-"), raw.startswith("-"), w) for raw, w in MODELS[key]["w"].items()]


def percentiles(f: pl.DataFrame, key: str) -> pl.DataFrame:
    """Within-week percentile of each component (inverted where the weight name starts with '-')."""
    cols = []
    for c, inv, _ in comp_cols(key):
        if c not in f.columns:
            continue
        e = pl.col(c).fill_null(0)
        e = -e if inv else e
        cols.append((e.rank("average") / pl.len()).over("sw").alias(f"p_{c}"))
    return f.with_columns(cols)


def available(f: pl.DataFrame, key: str):
    """Components the production pipeline would actually use here: present and not constant (nfl_bot.score_all)."""
    out = []
    for c, inv, w in comp_cols(key):
        if c in f.columns and f[c].null_count() < f.height and f[c].n_unique() > 1:
            out.append((c, w))
    tot = sum(w for _, w in out)
    return [(c, w / tot) for c, w in out]


def blend(f: pl.DataFrame, weights: dict) -> np.ndarray:
    s = np.zeros(f.height)
    for c, w in weights.items():
        s += w * f[f"p_{c}"].to_numpy()
    return s * 100


def platt(train_s, train_y, test_s):
    m = LogisticRegression(C=1e6, max_iter=1000).fit(((np.asarray(train_s) - 50) / 15).reshape(-1, 1), train_y)
    return m.predict_proba(((np.asarray(test_s) - 50) / 15).reshape(-1, 1))[:, 1]


def fit_nonneg_weights(P, y, l2=1.0):
    """Non-negative logistic on component percentiles (an interpretable weight vector); L2-regularised; train only."""
    n, k = P.shape
    X = (P - 0.5) * 2.0
    def f(v):
        b, w = v[0], v[1:]
        z = b + X @ w
        ll = np.mean(np.logaddexp(0, z) - y * z)
        return ll + l2 * 0.5 * np.sum(w ** 2) / n * 50
    r = minimize(f, np.r_[np.log(y.mean() / (1 - y.mean())), np.full(k, 0.1)], method="L-BFGS-B", bounds=[(None, None)] + [(0, None)] * k)
    w = np.clip(r.x[1:], 0, None)
    return w / w.sum() if w.sum() > 0 else np.full(k, 1 / k), r.x


def wide_cols(f: pl.DataFrame):
    ctx = ["implied_total", "total_line", "spread", "is_home", "wind_mph", "indoors", "is_carryover", "f_gp"]
    cols = [c for c in f.columns if c.startswith("f_") and not c.startswith("f_ngs") and not c.startswith("f_opp")
            and not c.startswith("f_oppr") and c not in ("f_gp",)] + [c for c in ctx if c in f.columns]
    keep = []
    for c in cols:
        if f[c].dtype in (pl.Float64, pl.Float32, pl.Int64, pl.Int32, pl.Int8, pl.UInt32, pl.Boolean) and f[c].null_count() < f.height * 0.5 and f[c].n_unique() > 1:
            keep.append(c)
    return keep


def lr_features(train, test, cols, C=0.1):
    def X(d):
        a = d.select(cols).to_numpy().astype(float)
        return a
    Xtr = X(train); med = np.nanmedian(Xtr, axis=0); med = np.where(np.isnan(med), 0, med)
    def imp(a):
        a = a.copy(); bad = ~np.isfinite(a); a[bad] = np.take(med, np.where(bad)[1]); return a
    sc = StandardScaler().fit(imp(Xtr))
    m = LogisticRegression(C=C, max_iter=3000).fit(sc.transform(imp(Xtr)), train["y"].to_numpy())
    return m.predict_proba(sc.transform(imp(X(test))))[:, 1], dict(zip(cols, m.coef_[0]))


def evaluate_market(d: pl.DataFrame, key: str, folds: list[int]) -> dict:
    f0 = market_frame(d, key)
    f0 = percentiles(f0, key)
    avail = available(f0, key)
    weights = dict(avail)
    res = {"market": key, "label": MODELS[key]["label"], "bar": MODELS[key]["bar"], "positions": MODELS[key]["pos"],
           "published_weights": {c: w for c, _, w in comp_cols(key)}, "weights_used_after_availability": weights,
           "dropped_constant_or_missing": [c for c, _, _ in comp_cols(key) if c not in weights]}
    per = {}
    preds = {}
    store = lambda name, s, p, te: preds.setdefault(name, []).append((te["sw"].to_numpy(), np.asarray(s), np.asarray(p), te["y"].to_numpy()))
    fit_w_by_fold = {}
    for fold in folds:
        tr = f0.filter(pl.col("sea") < fold); te = f0.filter(pl.col("sea") == fold)
        if tr.height < 500 or te.height < 200:
            continue
        ytr = tr["y"].to_numpy()
        # published formula
        s_tr = blend(tr, weights); s_te = blend(te, weights)
        store("published", s_te, platt(s_tr, ytr, s_te), te)
        # leave one component out (renormalised) and +-50% on one weight
        for c in weights:
            w2 = {k: v for k, v in weights.items() if k != c}
            if not w2:
                continue
            t = sum(w2.values()); w2 = {k: v / t for k, v in w2.items()}
            a, b = blend(tr, w2), blend(te, w2); store(f"drop:{c}", b, platt(a, ytr, b), te)
            for mult in (0.5, 1.5):
                w3 = dict(weights); w3[c] = w3[c] * mult; t = sum(w3.values()); w3 = {k: v / t for k, v in w3.items()}
                a, b = blend(tr, w3), blend(te, w3); store(f"x{mult}:{c}", b, platt(a, ytr, b), te)
        # equal weights (a transparent simple formula)
        wq = {c: 1 / len(weights) for c in weights}
        a, b = blend(tr, wq), blend(te, wq); store("equal_weights", b, platt(a, ytr, b), te)
        # fitted non-negative weights on the same components
        P = tr.select([f"p_{c}" for c in weights]).to_numpy()
        wfit, _ = fit_nonneg_weights(P, ytr)
        fw = dict(zip(weights, wfit)); fit_w_by_fold[fold] = {c: round(float(v), 3) for c, v in fw.items()}
        a, b = blend(tr, fw), blend(te, fw); store("fitted_weights", b, platt(a, ytr, b), te)
        # unrestricted logistic on the same components
        pc = [f"p_{c}" for c in weights]
        p, _ = lr_features(tr, te, pc, C=0.3); store("lr_same_components", p * 100, p, te)
        # wider logistic on every trailing feature + context
        wc = wide_cols(tr)
        p, coefs = lr_features(tr, te, wc, C=0.05); store("lr_wide_features", p * 100, p, te)
    base_name = "published"
    sw0 = np.concatenate([x[0] for x in preds[base_name]]); y0 = np.concatenate([x[3] for x in preds[base_name]])
    p0 = np.concatenate([x[2] for x in preds[base_name]])
    for name, parts in preds.items():
        sw = np.concatenate([x[0] for x in parts]); s = np.concatenate([x[1] for x in parts]); p = np.concatenate([x[2] for x in parts])
        m = W.metrics(sw, p, y0)
        m["auc_score"] = float(W.roc_auc_score(y0, s))
        m["top5"] = W.topk_hit(sw, s, y0, 5)
        diff, lo, hi = W.paired_bootstrap(sw, p, p0, y0, n_boot=600)
        m.update({"d_logloss_vs_published": diff, "lo": lo, "hi": hi,
                  "folds_better": int(sum(1 for i, fo in enumerate([x for x in folds if x in {int(str(a[0][0])[:4]) for a in preds[base_name]}]) if
                                          W.log_loss(parts[i][3], W._clip(parts[i][2])) < W.log_loss(preds[base_name][i][3], W._clip(preds[base_name][i][2]))))
                            if name != base_name else None})
        per[name] = m
    res["results"] = per
    res["fitted_weights_by_fold"] = fit_w_by_fold
    res["n_total"] = int(len(y0)); res["base_rate"] = float(y0.mean()); res["folds"] = folds
    return res


def selection_policy(d: pl.DataFrame, key: str, folds: list[int], k: int = 5) -> dict:
    """Diagnostics on the published top-k card (within-week blend), pooled over `folds`."""
    f0 = percentiles(market_frame(d, key), key)
    weights = dict(available(f0, key))
    f0 = f0.with_columns(pl.Series("score", blend(f0, weights)))
    f0 = f0.filter(pl.col("sea").is_in(folds))
    f0 = f0.with_columns(pl.col("score").rank("ordinal", descending=True).over("sw").alias("rk"),
                         pl.len().over("sw").alias("pool"))
    top = f0.filter(pl.col("rk") <= k)
    out = {"market": key, "k": k, "weeks": int(f0["sw"].n_unique()), "picks": top.height}
    out["hit_by_rank"] = {str(r): float(top.filter(pl.col("rk") == r)["y"].mean()) for r in range(1, k + 1)}
    out["hit_by_rank_n"] = int(top.filter(pl.col("rk") == 1).height)
    # gap between #1 and #2
    wide = top.filter(pl.col("rk") <= 2).sort("sw", "rk").group_by("sw", maintain_order=True).agg(
        pl.col("score").first().alias("s1"), pl.col("score").last().alias("s2"), pl.col("y").first().alias("y1"))
    gap = (wide["s1"] - wide["s2"]).to_numpy(); y1 = wide["y1"].to_numpy()
    if len(gap) > 10:
        med = np.median(gap)
        out["rank1_hit_when_gap_large"] = float(y1[gap >= med].mean()); out["rank1_hit_when_gap_small"] = float(y1[gap < med].mean())
        out["gap_median_points"] = float(med)
    # concentration: weeks where >=2 of the top-k share a game / a team
    g = top.group_by("sw").agg(pl.col("game_id").n_unique().alias("games"), pl.col("team").n_unique().alias("teams"), pl.len().alias("n"))
    out["weeks_with_two_or_more_same_game"] = float((g["games"] < g["n"]).mean())
    out["weeks_with_two_or_more_same_team"] = float((g["teams"] < g["n"]).mean())
    # outcome dependence: pairs of top-k picks in the SAME game vs different games (phi coefficient)
    import itertools
    same, diff = [], []
    for _, grp in top.group_by("sw"):
        rows = list(zip(grp["game_id"].to_list(), grp["y"].to_list()))
        for (g1, y1_), (g2, y2_) in itertools.combinations(rows, 2):
            (same if g1 == g2 else diff).append((y1_, y2_))
    def phi(pairs):
        a = np.array(pairs, float)
        return float(np.corrcoef(a[:, 0], a[:, 1])[0, 1]) if len(a) > 30 and a[:, 0].std() > 0 and a[:, 1].std() > 0 else None
    out["pair_counts"] = {"same_game": len(same), "different_game": len(diff)}
    out["outcome_phi_same_game"] = phi(same); out["outcome_phi_different_game"] = phi(diff)
    # slate size dependence
    q = f0.filter(pl.col("rk") <= k).group_by("sw").agg(pl.col("y").mean().alias("hit"), pl.col("pool").first().alias("pool"))
    cut = float(q["pool"].median())
    out["top_hit_small_pool"] = float(q.filter(pl.col("pool") < cut)["hit"].mean()); out["top_hit_large_pool"] = float(q.filter(pl.col("pool") >= cut)["hit"].mean())
    out["pool_median"] = cut
    # the card's low-sample filter (touches < 3) -- does dropping those rows change who is picked and how they do
    if key in ("TD", "REC_YDS", "REC", "RUSH_YDS", "RUSH_ATT"):
        solid = f0.filter((pl.col("f_targets").fill_null(0) + pl.col("f_carries").fill_null(0)) >= 3.0)
        s2 = solid.with_columns(pl.col("score").rank("ordinal", descending=True).over("sw").alias("rk2")).filter(pl.col("rk2") <= k)
        out["top_hit_with_low_sample_filter"] = float(s2["y"].mean()); out["top_hit_without_filter"] = float(top["y"].mean())
    return out


def main(mode):
    d = load_all()
    folds = SEL if mode == "selection" else [HOLD]
    out = {"mode": mode, "folds": folds, "markets": {}, "selection_policy": {}}
    for key in MARKETS:
        r = evaluate_market(d, key, folds)
        out["markets"][key] = r
        out["selection_policy"][key] = selection_policy(d, key, folds)
        pub = r["results"]["published"]
        print(f"\n== {key} ({r['label']}, bar {r['bar']}) n={r['n_total']} base {r['base_rate']:.3f} folds {folds}")
        print(f"  published: LL {pub['logloss']:.4f} AUC {pub['auc']:.3f} top5 {pub['top5']*100:.1f}% top15 {pub['top15']*100:.1f}%  dropped components: {r['dropped_constant_or_missing']}")
        for name, m in sorted(r["results"].items(), key=lambda kv: kv[1]["logloss"])[:6]:
            if name.startswith(("drop:", "x0", "x1")) and name not in ():
                continue
            print(f"  {name:22} LL {m['logloss']:.4f} AUC {m['auc']:.3f} top5 {m['top5']*100:5.1f}%  dLL {m['d_logloss_vs_published']:+.4f} [{m['lo']:+.4f},{m['hi']:+.4f}]")
    path = os.path.join(ROOT, "model_lab", "results", f"nfl_formula_audit_{mode}.json")
    json.dump(out, open(path, "w"), indent=1, default=float)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "selection")
