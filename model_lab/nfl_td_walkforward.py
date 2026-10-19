#!/usr/bin/env python3
"""DASH model lab: NFL anytime-touchdown backtest (rushing + receiving TD >= 1), walk-forward.

    python model_lab/build_nfl_tables.py 2021 2022 2023 2024 2025     # once (cached parquet)
    python model_lab/nfl_td_walkforward.py selection                          # folds 2022, 2023, 2024  (model selection)
    python model_lab/nfl_td_walkforward.py holdout                            # 2025, the FINAL holdout: run once, after selection

POPULATION. One row per player-week of an RB/WR/TE who recorded a stat line that week, not Out on the injury
report, with at least MIN_GP games of history (the production builder's own filter). A player with no stat line
(inactive) is absent, exactly as the ledger voids him. Production scores the whole upcoming slate; a backtest can
only grade the men who played, so every number here is "among players who played" and says so.

LEAKAGE. Features are an explicit whitelist (FEATURE_GROUPS): trailing form over weeks < w (nfl_features._roll),
last season's per-game baseline as carryover, the schedule's spread/total/roof/wind for week w, and opponent
trailing allowed-rates. No same-week outcome column (rz_car, xtd, td_rush, ... are the week's own plays) is ever
a feature; assert_no_leak() enforces it. Caveat the data cannot remove: nflverse schedule spread/total are the
(closing) lines for the game, which is the information the production slate also reads at build time, but a
build days before kickoff sees an earlier line.

FOLDS. Rolling origin: train on all earlier seasons, predict the next. 2021 is train-only. Selection uses
2022-2024; 2025 is the untouched holdout. Hyper-parameters are chosen inside the training seasons only (the
latest training season is the inner validation season).

METRICS. Log loss and Brier (probabilities), AUC, top-k per week (k=15, 30), calibration by decile. Uncertainty:
paired bootstrap over (season, week) clusters against the named baseline.
"""
from __future__ import annotations

import json
import math
import os
import sys
import warnings

import numpy as np
import polars as pl
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bots", "nfl"))
sys.path.insert(0, os.path.join(ROOT, "model_lab"))
from build_nfl_tables import build as load_season  # noqa: E402
from nfl_scoring import MODELS, SHADOW_MODELS  # noqa: E402

SEASONS = [2021, 2022, 2023, 2024, 2025]
SELECTION_FOLDS = [2022, 2023, 2024]
HOLDOUT = 2025
POS = ["RB", "WR", "TE"]
SEED = 7
EPS = 1e-4

# ── feature groups (whitelist) ────────────────────────────────────────────────
FEATURE_GROUPS = {
    "opp": ["f_rz_opp", "f_gl_opp", "f_touches", "f_snap_pct", "f_rz_tgt", "f_rz_car", "f_targets", "f_carries"],
    "share": ["f_target_share", "f_wopr", "f_air_yards_share"],
    "xtd": ["f_xtd"],
    "eff": ["f_td_actual", "f_rushing_tds", "f_receiving_tds", "f_receiving_air_yards", "f_receiving_20", "f_rushing_20"],
    "team": ["implied_total", "total_line", "spread", "f_tm_rz_td_rate", "f_tm_drives"],
    "matchup": ["f_opp_d_rec_td", "f_opp_d_rush_td", "f_oppr_dr_td", "f_oppr_dr_car"],
    "env": ["is_home", "wind_mph", "indoors"],
    "ctx": ["is_carryover", "f_gp"],
}
ALL_FEATURES = sorted({c for g in FEATURE_GROUPS.values() for c in g})
FORBIDDEN_SAME_WEEK = {"rz_car", "rz_tgt", "xtd", "td_rush", "td_rec", "td_actual", "car_n", "tgt_n", "i5_car", "i10_tgt",
                       "rushing_tds", "receiving_tds", "carries", "targets", "receptions", "rushing_yards",
                       "receiving_yards", "fantasy_points", "gl_opp", "rz_opp", "snap_pct"}


def assert_no_leak(cols: list[str]) -> None:
    bad = [c for c in cols if c in FORBIDDEN_SAME_WEEK]
    if bad:
        raise AssertionError(f"same-week outcome columns used as features: {bad}")


assert_no_leak(ALL_FEATURES)


# ── data ──────────────────────────────────────────────────────────────────────
def load() -> pl.DataFrame:
    frames = []
    for s in SEASONS:
        t = load_season(s)
        t = t.filter(pl.col("position").is_in(POS)).with_columns(pl.lit(s).alias("sea"))
        frames.append(t)
    d = pl.concat(frames, how="diagonal_relaxed")
    d = d.with_columns(
        ((pl.col("rushing_tds").fill_null(0) + pl.col("receiving_tds").fill_null(0)) >= 1).cast(pl.Int8).alias("y"),
        (pl.col("rushing_tds").fill_null(0) >= 1).cast(pl.Int8).alias("y_rush"),
        (pl.col("receiving_tds").fill_null(0) >= 1).cast(pl.Int8).alias("y_rec"),
        (pl.col("sea") * 100 + pl.col("week")).alias("sw"),
    )
    return d


def add_scores(d: pl.DataFrame) -> pl.DataFrame:
    """The published v2 composite and the shadow v3, as within-week percentile blends (nfl_td_lab's own method)."""
    def comp(w: dict, name: str) -> pl.Expr:
        tot = None
        for k, v in w.items():
            e = (pl.col(k).fill_null(0).rank("average") / pl.len()).over("sw") * v
            tot = e if tot is None else tot + e
        return (tot * 100).alias(name)
    return d.with_columns(comp(MODELS["TD"]["w"], "score_v2"), comp(SHADOW_MODELS["TD"]["w"], "score_v3"))


# ── metrics ───────────────────────────────────────────────────────────────────
def _clip(p):
    return np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)


def topk_hit(sw, p, y, k):
    import collections
    by = collections.defaultdict(list)
    for a, b, c in zip(sw, p, y):
        by[a].append((b, c))
    hits = n = 0
    for rows in by.values():
        rows.sort(key=lambda r: -r[0])
        top = rows[:k]
        hits += int(sum(int(c) for _, c in top))
        n += len(top)
    return hits / n if n else float("nan")


def calibration(p, y, bins=10):
    p = np.asarray(p); y = np.asarray(y)
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    out = []
    for i in range(bins):
        m = (p >= edges[i]) & (p <= edges[i + 1] if i == bins - 1 else p < edges[i + 1])
        if m.sum():
            out.append({"bin": i + 1, "n": int(m.sum()), "mean_pred": round(float(p[m].mean()), 4), "observed": round(float(y[m].mean()), 4)})
    return out


def metrics(sw, p, y) -> dict:
    p = _clip(p); y = np.asarray(y).astype(np.int64)
    return {
        "n": int(len(y)), "base_rate": round(float(y.mean()), 4),
        "logloss": float(log_loss(y, p)), "brier": float(brier_score_loss(y, p)),
        "auc": float(roc_auc_score(y, p)),
        "top15": topk_hit(sw, p, y, 15), "top30": topk_hit(sw, p, y, 30),
        "mean_pred": float(p.mean()),
    }


def paired_bootstrap(sw, p_cand, p_base, y, n_boot=1500):
    """Cluster bootstrap over (season, week): mean per-row log-loss difference, candidate - baseline (negative = better)."""
    y = np.asarray(y); pc = _clip(p_cand); pb = _clip(p_base)
    ll_c = -(y * np.log(pc) + (1 - y) * np.log(1 - pc)); ll_b = -(y * np.log(pb) + (1 - y) * np.log(1 - pb))
    diff = ll_c - ll_b
    keys = np.unique(sw)
    idx = {k: np.where(sw == k)[0] for k in keys}
    rng = np.random.default_rng(SEED)
    stats = []
    for _ in range(n_boot):
        pick = rng.choice(keys, size=len(keys), replace=True)
        ii = np.concatenate([idx[k] for k in pick])
        stats.append(diff[ii].mean())
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(diff.mean()), float(lo), float(hi)


# ── model families ────────────────────────────────────────────────────────────
class Prep:
    """Median imputation + (optional) standardisation fitted on TRAIN only."""
    def __init__(self, cols):
        self.cols = cols

    def fit(self, d: pl.DataFrame):
        X = d.select(self.cols).to_numpy().astype(float)
        self.med = np.nanmedian(X, axis=0)
        self.med = np.where(np.isnan(self.med), 0.0, self.med)
        X = self._imp(X)
        self.sc = StandardScaler().fit(X)
        return self

    def _imp(self, X):
        X = X.copy()
        bad = np.isnan(X) | np.isinf(X)
        X[bad] = np.take(self.med, np.where(bad)[1])
        return X

    def X(self, d: pl.DataFrame, scale=True):
        X = self._imp(d.select(self.cols).to_numpy().astype(float))
        return self.sc.transform(X) if scale else X


def with_pos(d: pl.DataFrame) -> pl.DataFrame:
    return d.with_columns([(pl.col("position") == p).cast(pl.Float64).alias(f"pos_{p}") for p in POS])


def fit_lr(train, cols, target="y", C=None, val_season=None):
    """Regularised logistic regression; C chosen on the latest TRAIN season (never on the test season)."""
    cols = cols + [f"pos_{p}" for p in POS]
    if val_season is not None and train.filter(pl.col("sea") < val_season).height == 0:
        val_season, C = None, C or 0.1      # a single training season: no inner validation season, fixed default C
    Cs = [C] if C else [0.01, 0.03, 0.1, 0.3, 1.0]
    best = None
    inner_tr = train.filter(pl.col("sea") < val_season) if val_season else train
    inner_va = train.filter(pl.col("sea") == val_season) if val_season else None
    for c in Cs:
        pr = Prep(cols).fit(inner_tr)
        m = LogisticRegression(C=c, max_iter=2000).fit(pr.X(inner_tr), inner_tr[target].to_numpy())
        if inner_va is None:
            best = (0, c); break
        ll = log_loss(inner_va[target].to_numpy(), _clip(m.predict_proba(pr.X(inner_va))[:, 1]))
        if best is None or ll < best[0]:
            best = (ll, c)
    pr = Prep(cols).fit(train)
    m = LogisticRegression(C=best[1], max_iter=2000).fit(pr.X(train), train[target].to_numpy())
    return lambda d: m.predict_proba(pr.X(d))[:, 1], {"C": best[1]}


def fit_gbm(train, cols, target="y", val_season=None):
    cols = cols + [f"pos_{p}" for p in POS]
    if val_season is not None and train.filter(pl.col("sea") < val_season).height == 0:
        val_season = None                   # a single training season: default (first) grid entry
    grid = [dict(max_depth=2, learning_rate=0.05, max_iter=200, l2_regularization=1.0, min_samples_leaf=100),
            dict(max_depth=3, learning_rate=0.03, max_iter=300, l2_regularization=2.0, min_samples_leaf=150)]
    pr = Prep(cols).fit(train)
    inner_tr = train.filter(pl.col("sea") < val_season) if val_season else train
    inner_va = train.filter(pl.col("sea") == val_season) if val_season else None
    best = (1e9, grid[0])
    if inner_va is not None:
        for g in grid:
            m = HistGradientBoostingClassifier(random_state=SEED, **g).fit(pr.X(inner_tr, False), inner_tr[target].to_numpy())
            ll = log_loss(inner_va[target].to_numpy(), _clip(m.predict_proba(pr.X(inner_va, False))[:, 1]))
            if ll < best[0]:
                best = (ll, g)
    m = HistGradientBoostingClassifier(random_state=SEED, **best[1]).fit(pr.X(train, False), train[target].to_numpy())
    return lambda d: m.predict_proba(pr.X(d, False))[:, 1], {"params": best[1]}


def fit_platt(train, score_col):
    """Map a heuristic score to a probability: logistic on the score, fitted on TRAIN seasons only."""
    x = train[score_col].to_numpy().reshape(-1, 1)
    m = LogisticRegression(C=1e6, max_iter=1000).fit((x - 50) / 15, train["y"].to_numpy())
    return lambda d: m.predict_proba((d[score_col].to_numpy().reshape(-1, 1) - 50) / 15)[:, 1]


def fit_base_pos(train):
    r = {p: float(train.filter(pl.col("position") == p)["y"].mean()) for p in POS}
    return lambda d: np.array([r[p] for p in d["position"].to_list()])


def fit_poisson_xtd(train, col="f_xtd"):
    """P = 1 - exp(-a * xTD): one fitted scale `a` (xTD is a field-position expected-TD count)."""
    x = np.nan_to_num(train[col].to_numpy().astype(float)); y = train["y"].to_numpy()
    best = min(np.linspace(0.3, 3.0, 55), key=lambda a: log_loss(y, _clip(1 - np.exp(-a * x))))
    return lambda d: 1 - np.exp(-best * np.nan_to_num(d[col].to_numpy().astype(float))), {"a": round(float(best), 3)}


def fit_eb_rate(train, k=30.0):
    """Empirical-Bayes touchdown-per-touch rate: shrink his trailing TDs/touch to his position's rate, times expected touches.
    lambda = touches * (tds + k*r0) / (touches_total + k); P = 1 - exp(-s*lambda), s fitted on train."""
    r0 = {p: float(train.filter(pl.col("position") == p).select(
        (pl.col("f_td_actual").fill_null(0).sum() / pl.col("f_touches").fill_null(0).clip(0.01).sum())).item()) for p in POS}

    def lam(d):
        touches = np.nan_to_num(d["f_touches"].to_numpy().astype(float))
        games = np.nan_to_num(d["f_gp"].to_numpy().astype(float)).clip(1)
        td = np.nan_to_num(d["f_td_actual"].to_numpy().astype(float))
        r = np.array([r0[p] for p in d["position"].to_list()])
        tot_t = touches * games; tot_td = td * games
        rate = (tot_td + k * r) / (tot_t + k)
        return touches * rate
    x = lam(train); y = train["y"].to_numpy()
    s = min(np.linspace(0.5, 3.0, 51), key=lambda a: log_loss(y, _clip(1 - np.exp(-a * x))))
    return lambda d: 1 - np.exp(-s * lam(d)), {"k": k, "s": round(float(s), 3)}


# ── experiments ───────────────────────────────────────────────────────────────
def group_cols(*groups):
    return sorted({c for g in groups for c in FEATURE_GROUPS[g]})


def experiments(train, test, val_season):
    """{id: (probabilities on test, info)}. Everything is fitted on `train` only."""
    out = {}
    out["A_base_by_position"] = (fit_base_pos(train)(test), {})
    out["B_v2_platt"] = (fit_platt(train, "score_v2")(test), {"note": "published Tuddy TD score mapped to a probability on train seasons"})
    out["C_v3_xtd_shadow_platt"] = (fit_platt(train, "score_v3")(test), {})
    f, i = fit_poisson_xtd(train); out["D_poisson_xtd"] = (f(test), i)
    f, i = fit_eb_rate(train); out["E_eb_rate_x_touches"] = (f(test), i)
    ladder = [("F1_lr_opp", ("opp",)), ("F2_lr_opp_xtd", ("opp", "xtd")), ("F3_lr_opp_share", ("opp", "share")),
              ("F4_lr_opp_team", ("opp", "team")), ("F5_lr_opp_eff", ("opp", "eff")), ("F6_lr_opp_matchup", ("opp", "matchup")),
              ("F7_lr_opp_env", ("opp", "env")), ("F8_lr_opp_ctx", ("opp", "ctx")),
              ("F9_lr_opp_xtd_team", ("opp", "xtd", "team")),
              ("G_lr_all", tuple(FEATURE_GROUPS))]
    for name, groups in ladder:
        f, i = fit_lr(train, group_cols(*groups), val_season=val_season); out[name] = (f(test), i)
    f, i = fit_gbm(train, ALL_FEATURES, val_season=val_season); out["H_gbm_all"] = (f(test), i)
    f, i = fit_gbm(train, group_cols("opp", "xtd", "team"), val_season=val_season); out["H2_gbm_opp_xtd_team"] = (f(test), i)
    # position-specific logistic regressions (shared feature set G)
    pp = np.zeros(test.height)
    for p in POS:
        tr = train.filter(pl.col("position") == p); te_mask = (test["position"] == p).to_numpy()
        f, _ = fit_lr(tr, group_cols(*FEATURE_GROUPS), val_season=val_season)
        pp[te_mask] = f(test.filter(pl.col("position") == p))
    out["I_lr_all_by_position"] = (pp, {})
    # rushing and receiving separately, combined as 1 - (1-pr)(1-pc)
    fr, _ = fit_lr(train, group_cols(*FEATURE_GROUPS), target="y_rush", val_season=val_season)
    fc, _ = fit_lr(train, group_cols(*FEATURE_GROUPS), target="y_rec", val_season=val_season)
    out["J_lr_rush_x_rec_combined"] = (1 - (1 - fr(test)) * (1 - fc(test)), {})
    # ensembles of independently fitted models
    out["K_ens_lr_gbm"] = ((out["G_lr_all"][0] + out["H_gbm_all"][0]) / 2, {})
    out["L_ens_lr_gbm_v2platt"] = ((out["G_lr_all"][0] + out["H_gbm_all"][0] + out["B_v2_platt"][0]) / 3, {})
    return out


def run(folds: list[int], tag: str) -> dict:
    d = with_pos(add_scores(load()))
    results = {}
    pooled = {}
    for s in folds:
        train = d.filter(pl.col("sea") < s); test = d.filter(pl.col("sea") == s)
        exps = experiments(train, test, val_season=s - 1)
        sw = test["sw"].to_numpy(); y = test["y"].to_numpy()
        for name, (p, info) in exps.items():
            m = metrics(sw, p, y); m["info"] = info
            results.setdefault(name, {})[str(s)] = m
            pooled.setdefault(name, []).append((sw, p, y))
        print(f"fold {s}: train {train.height} test {test.height} base rate {y.mean():.3f}", flush=True)
    summary = {}
    base_name = "B_v2_platt"
    swa = np.concatenate([x[0] for x in pooled[base_name]]); ya = np.concatenate([x[2] for x in pooled[base_name]])
    pb = np.concatenate([x[1] for x in pooled[base_name]])
    for name, parts in pooled.items():
        pa = np.concatenate([x[1] for x in parts])
        m = metrics(swa, pa, ya)
        diff, lo, hi = paired_bootstrap(swa, pa, pb, ya)
        m.update({"d_logloss_vs_v2": diff, "d_lo": lo, "d_hi": hi,
                  "folds_better_than_v2": int(sum(results[name][str(s)]["logloss"] < results[base_name][str(s)]["logloss"] for s in folds)),
                  "calibration": calibration(pa, ya)})
        summary[name] = m
    out = {"tag": tag, "folds": folds, "per_fold": results, "pooled": summary,
           "n_experiments": len(summary)}
    path = os.path.join(ROOT, "model_lab", "results", f"nfl_td_{tag}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(out, open(path, "w"), indent=1)
    return out


def print_table(res: dict):
    rows = sorted(res["pooled"].items(), key=lambda kv: kv[1]["logloss"])
    print(f"{'model':28}{'n':>7}{'logloss':>9}{'brier':>8}{'auc':>7}{'top15':>7}{'top30':>7}{'dLL vs v2 [95% CI]':>28}{'folds>v2':>9}")
    for name, m in rows:
        print(f"{name:28}{m['n']:>7}{m['logloss']:>9.4f}{m['brier']:>8.4f}{m['auc']:>7.3f}{m['top15']*100:>6.1f}%{m['top30']*100:>6.1f}%"
              f"   {m['d_logloss_vs_v2']:+.4f} [{m['d_lo']:+.4f},{m['d_hi']:+.4f}]{m['folds_better_than_v2']:>6}/{len(res['folds'])}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "selection"
    if mode == "selection":
        r = run(SELECTION_FOLDS, "selection")
    elif mode == "holdout":
        r = run([HOLDOUT], "holdout")
    else:
        raise SystemExit("usage: nfl_td_walkforward.py selection|holdout")
    print_table(r)
