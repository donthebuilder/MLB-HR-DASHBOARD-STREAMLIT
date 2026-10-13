#!/usr/bin/env python3
"""NFL TD lab, part 2: what do the winners actually owe their gain to, where are they wrong, and what else is there?

    python model_lab/nfl_td_discovery.py            # selection folds only (2022-2024); never reads 2025

1. LEAVE-ONE-GROUP-OUT from the full logistic model (incremental value of each feature family, paired bootstrap).
2. SEGMENTS: position, carryover (stale inputs), opportunity tier, rank bucket of the published v2 score.
3. SIGN STABILITY of the logistic coefficients across folds (a signal that flips is not a signal).
4. PRE-REGISTERED DISCOVERY TESTS: each extra candidate feature is added once to the full model; p-values are
   cluster-bootstrap, corrected for the number of tests (Holm). The list is fixed in HYPOTHESES below BEFORE running.
5. QB sub-population.
"""
import json
import os
import sys

import numpy as np
import polars as pl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nfl_td_walkforward as L  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

FOLDS = L.SELECTION_FOLDS

# Pre-registered (fixed before the run). feature -> mechanism.
HYPOTHESES = {
    "f_ngs_rush_pct_over_expected": "backs who beat their blocking score more near the goal line",
    "f_ngs_avg_separation": "separation -> catchable red-zone targets",
    "f_ngs_avg_cushion": "soft coverage -> easier touchdowns",
    "f_receiving_40": "big-play receivers score on long plays, a different route to a TD",
    "f_ngs_avg_yac_above_expectation": "receivers who create after the catch convert short-field targets",
    "div_game": "division games are lower-scoring / more familiar defences",
    "f_snap_pct_sq": "non-linear snap-share effect (starters vs committee)",
    "touch_x_implied": "opportunity matters more on high-scoring teams (interaction)",
    "xtd_x_opp_soft": "expected TDs matter more against soft red-zone defences (interaction)",
    "td_over_exp": "his TDs above expected last window (regression-to-the-mean test)",
}


def prepare():
    d = L.with_pos(L.add_scores(L.load()))
    d = d.with_columns(
        (pl.col("f_snap_pct").fill_null(0) ** 2).alias("f_snap_pct_sq"),
        (pl.col("f_touches").fill_null(0) * pl.col("implied_total").fill_null(22)).alias("touch_x_implied"),
        (pl.col("f_xtd").fill_null(0) * (pl.col("f_opp_d_rec_td").fill_null(0) + pl.col("f_opp_d_rush_td").fill_null(0))).alias("xtd_x_opp_soft"),
        (pl.col("f_td_actual").fill_null(0) - pl.col("f_xtd").fill_null(0)).alias("td_over_exp"),
        pl.col("div_game").cast(pl.Float64).alias("div_game"),
    )
    return d


def fold_preds(d, cols, fold, target="y", C=0.1):
    tr = d.filter(pl.col("sea") < fold); te = d.filter(pl.col("sea") == fold)
    f, _ = L.fit_lr(tr, cols, target=target, C=C)
    return te, f(te)


def pooled(d, cols, **kw):
    sw, p, y = [], [], []
    for s in FOLDS:
        te, pr = fold_preds(d, cols, s, **kw)
        sw.append(te["sw"].to_numpy()); p.append(pr); y.append(te["y"].to_numpy())
    return np.concatenate(sw), np.concatenate(p), np.concatenate(y)


def main():
    d = prepare()
    full = L.group_cols(*L.FEATURE_GROUPS)
    sw, p_full, y = pooled(d, full)
    base_ll = L.metrics(sw, p_full, y)["logloss"]
    out = {"full": L.metrics(sw, p_full, y)}

    print("== 1. LEAVE-ONE-GROUP-OUT (full LR minus one family); positive d = the family helps ==")
    logo = {}
    for g in L.FEATURE_GROUPS:
        cols = L.group_cols(*[x for x in L.FEATURE_GROUPS if x != g])
        _, p, _ = pooled(d, cols)
        diff, lo, hi = L.paired_bootstrap(sw, p_full, p, y)      # full - without: negative = family helps
        logo[g] = {"d_logloss_full_minus_without": diff, "lo": lo, "hi": hi, "without_logloss": L.metrics(sw, p, y)["logloss"]}
        print(f"  without {g:8} logloss {logo[g]['without_logloss']:.4f}  full-minus-without {diff:+.4f} [{lo:+.4f},{hi:+.4f}]")
    out["logo"] = logo

    print("== 2. SEGMENTS (full LR vs published v2 mapped to a probability) ==")
    v2 = []
    for s in FOLDS:
        tr = d.filter(pl.col("sea") < s); te = d.filter(pl.col("sea") == s)
        v2.append(L.fit_platt(tr, "score_v2")(te))
    p_v2 = np.concatenate(v2)
    te_all = pl.concat([d.filter(pl.col("sea") == s) for s in FOLDS])
    seg = {}
    def segment(name, mask):
        m = np.asarray(mask)
        if m.sum() < 200:
            return
        a = L.metrics(sw[m], p_full[m], y[m]); b = L.metrics(sw[m], p_v2[m], y[m])
        diff, lo, hi = L.paired_bootstrap(sw[m], p_full[m], p_v2[m], y[m], n_boot=800)
        seg[name] = {"n": int(m.sum()), "base_rate": a["base_rate"], "ll_full": a["logloss"], "ll_v2": b["logloss"], "d": diff, "lo": lo, "hi": hi, "auc_full": a["auc"], "auc_v2": b["auc"]}
        print(f"  {name:26} n={m.sum():5} base {a['base_rate']:.3f}  LL full {a['logloss']:.4f} v2 {b['logloss']:.4f}  d {diff:+.4f} [{lo:+.4f},{hi:+.4f}]  AUC {a['auc']:.3f} vs {b['auc']:.3f}")
    pos = te_all["position"].to_numpy()
    for pname in L.POS:
        segment(f"position {pname}", pos == pname)
    segment("carryover inputs (stale)", te_all["is_carryover"].fill_null(0).to_numpy() == 1)
    segment("own-season inputs", te_all["is_carryover"].fill_null(0).to_numpy() == 0)
    t = te_all["f_touches"].fill_null(0).to_numpy()
    for lo_, hi_, nm in [(0, 4, "touches/g < 4"), (4, 8, "touches/g 4-8"), (8, 14, "touches/g 8-14"), (14, 99, "touches/g >= 14")]:
        segment(nm, (t >= lo_) & (t < hi_))
    wk = te_all["week"].to_numpy()
    segment("weeks 1-4", wk <= 4); segment("weeks 5-12", (wk >= 5) & (wk <= 12)); segment("weeks 13+", wk >= 13)
    out["segments"] = seg

    print("== 2b. HIT RATE BY RANK BUCKET OF THE PUBLISHED v2 SCORE (within week) ==")
    rk = te_all.with_columns(pl.col("score_v2").rank("ordinal", descending=True).over("sw").alias("rk"))
    rb = {}
    for lo_, hi_ in [(1, 5), (6, 15), (16, 30), (31, 60), (61, 120), (121, 999)]:
        m = rk.filter(pl.col("rk").is_between(lo_, hi_))
        rb[f"{lo_}-{hi_}"] = {"n": m.height, "hit": float(m["y"].mean())}
        print(f"  rank {lo_:>3}-{hi_:<3} n={m.height:5} hit {m['y'].mean()*100:5.1f}%")
    out["rank_buckets_v2"] = rb

    print("== 3. SIGN STABILITY of standardised LR coefficients across folds ==")
    coefs = {}
    for s in FOLDS:
        tr = d.filter(pl.col("sea") < s)
        cols = full + [f"pos_{p}" for p in L.POS]
        pr = L.Prep(cols).fit(tr)
        m = LogisticRegression(C=0.1, max_iter=2000).fit(pr.X(tr), tr["y"].to_numpy())
        for c, w in zip(cols, m.coef_[0]):
            coefs.setdefault(c, []).append(float(w))
    stab = {c: {"coefs": [round(x, 3) for x in v], "same_sign": bool(all(np.sign(v) == np.sign(v[0])))} for c, v in coefs.items()}
    for c, v in sorted(stab.items(), key=lambda kv: -abs(np.mean(kv[1]["coefs"])))[:14]:
        print(f"  {c:26} {v['coefs']} {'stable' if v['same_sign'] else 'FLIPS'}")
    out["coef_stability"] = stab

    print("== 4. PRE-REGISTERED DISCOVERY TESTS (added once to the full model; Holm over", len(HYPOTHESES), "tests) ==")
    res = {}
    for feat, why in HYPOTHESES.items():
        if feat in full:
            raise AssertionError(f"{feat} is already in the full model; a hypothesis must add something new")
        if feat not in d.columns:
            res[feat] = {"status": "not available", "why": why}; print(f"  {feat:30} not available"); continue
        _, p, _ = pooled(d, full + [feat])
        diff, lo, hi = L.paired_bootstrap(sw, p, p_full, y, n_boot=2000)
        # two-sided bootstrap p from the CI: approximate via normal on the bootstrap SE
        se = (hi - lo) / (2 * 1.96)
        z = diff / se if se > 0 else 0.0
        from math import erf, sqrt
        pval = 1 - erf(abs(z) / sqrt(2))
        res[feat] = {"why": why, "d_logloss": diff, "lo": lo, "hi": hi, "p": pval, "coverage": float(1 - d[feat].null_count() / d.height)}
    live = {k: v for k, v in res.items() if "p" in v}
    order = sorted(live, key=lambda k: live[k]["p"])
    m = len(order)
    holm_ok = True
    for i, k in enumerate(order):
        thr = 0.05 / (m - i)
        holm_ok = holm_ok and live[k]["p"] <= thr
        live[k]["holm_threshold"] = thr
        live[k]["significant_after_holm"] = bool(holm_ok)
    for k in order:
        v = live[k]
        print(f"  {k:30} d {v['d_logloss']:+.5f} [{v['lo']:+.5f},{v['hi']:+.5f}] p={v['p']:.3f} holm<= {v['holm_threshold']:.4f} {'SIGNIFICANT' if v['significant_after_holm'] else 'not significant'} coverage {v['coverage']:.0%}")
    out["discovery"] = res

    print("== 5. QB SUB-POPULATION (anytime TD, rush+rec) ==")
    out["qb"] = qb_section()
    path = os.path.join(L.ROOT, "model_lab", "results", "nfl_td_discovery.json")
    json.dump(out, open(path, "w"), indent=1, default=float)


def qb_section():
    import nfl_features  # noqa: F401
    frames = []
    for s in L.SEASONS:
        t = L.load_season(s).filter(pl.col("position") == "QB").with_columns(pl.lit(s).alias("sea"))
        frames.append(t)
    d = pl.concat(frames, how="diagonal_relaxed").with_columns(
        ((pl.col("rushing_tds").fill_null(0) + pl.col("receiving_tds").fill_null(0)) >= 1).cast(pl.Int8).alias("y"),
        (pl.col("sea") * 100 + pl.col("week")).alias("sw"))
    d = d.with_columns([pl.lit(0.0).alias(f"pos_{p}") for p in L.POS])
    cols = ["f_rz_opp", "f_gl_opp", "f_touches", "f_xtd", "f_carries", "f_rushing_20", "implied_total", "spread", "f_td_actual"]
    sw, p, pb, y = [], [], [], []
    for s in L.SELECTION_FOLDS:
        tr = d.filter(pl.col("sea") < s); te = d.filter(pl.col("sea") == s)
        f, _ = L.fit_lr(tr, cols, C=0.1)
        sw.append(te["sw"].to_numpy()); p.append(f(te)); y.append(te["y"].to_numpy())
        pb.append(np.full(te.height, float(tr["y"].mean())))
    sw, p, pb, y = map(np.concatenate, (sw, p, pb, y))
    m = L.metrics(sw, p, y); b = L.metrics(sw, pb, y)
    diff, lo, hi = L.paired_bootstrap(sw, p, pb, y, n_boot=800)
    print(f"  QB rows {m['n']} base {m['base_rate']:.3f}: LR logloss {m['logloss']:.4f} AUC {m['auc']:.3f} vs base-rate {b['logloss']:.4f}  d {diff:+.4f} [{lo:+.4f},{hi:+.4f}]")
    return {"n": m["n"], "base_rate": m["base_rate"], "lr": m, "baseline": b, "d": diff, "lo": lo, "hi": hi}


if __name__ == "__main__":
    main()
