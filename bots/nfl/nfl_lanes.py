#!/usr/bin/env python3
"""
nfl_lanes.py -- a FITTED, CALIBRATED probability for every NFL player market (the hand-weighted scores in nfl_scoring.py only rank).

    python bots/nfl/nfl_lanes.py [--cache DIR] [--market REC_YDS ...] [--save]

Markets: receiving yards 100+, receptions 4+, rushing yards 100+, rushing attempts 12+, passing yards 300+, kicking points 6+ (the bars in nfl_scoring.OUTCOME)
plus passing TDs 2+ (new). For each, a ridge logistic over the market's own components and the common context columns (snap %, implied total, spread, home,
position), trailing-only, on the table nfl_features.build() makes. Fit 2023-24 and graded on 2025; fit 2023-25 and graded on 2026 weeks 1-N (a second, newer
holdout). Reported against the existing hand score and plain form: top-15 per week, AUC, log loss vs the base rate, and calibration by tenth.
--save fits on every season (2026 included) with Platt + tail calibration from the 2025 holdout and writes models/nfl_<market>_fit_v1.json.
"""
import argparse, json, math, sys
from pathlib import Path
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import nfl_scoring as S               # noqa: E402
from tune_models import fit_logistic   # noqa: E402

PASS_TD = {"pos": ["QB"], "bar": 2, "w": {"total_line": 0.3, "f_passing_tds": 0.3, "f_attempts": 0.2, "opp_pass_soft": 0.2}}
MARKETS = {k: v for k, v in S.MODELS.items() if k != "TD"}
MARKETS["PASS_TD"] = PASS_TD
OUT = dict(S.OUTCOME); OUT["PASS_TD"] = pl.col("passing_tds")
FORM = {"REC_YDS": "f_receiving_yards", "REC": "f_receptions", "RUSH_YDS": "f_rushing_yards", "RUSH_ATT": "f_carries", "PASS_YDS": "f_passing_yards",
        "KICK_PTS": "f_fg_made", "PASS_TD": "f_passing_tds"}
COMMON = ["f_snap_pct", "f_gp", "implied_total", "spread", "is_home", "inj_q"]
POS = ["QB", "RB", "WR", "TE", "K"]
EXTRA = {"REC_YDS": ["f_targets", "f_target_share", "f_air_yards_share", "f_receptions"], "REC": ["f_targets", "f_receiving_yards"],
         "RUSH_YDS": ["f_rushing_tds", "f_rz_car", "f_receptions"], "RUSH_ATT": ["f_rushing_yards", "f_receptions"],
         "PASS_YDS": ["f_passing_tds", "f_completions", "f_passing_epa" if False else "f_attempts", "total_line"], "KICK_PTS": ["f_pat_made", "f_fg_att"],
         "PASS_TD": ["f_passing_yards", "f_completions", "f_passing_cpoe"]}


def feats(m):
    seen, out = set(), []
    for c in [k.lstrip("-") for k in MARKETS[m]["w"]] + COMMON + EXTRA.get(m, []):
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def table(season, cache):
    p = Path(cache) / f"build_{season}.parquet"
    if p.exists():
        return pl.read_parquet(p)
    from nfl_features import build
    t = build(season); p.parent.mkdir(parents=True, exist_ok=True); t.write_parquet(p); return t


def prep(df, m):
    d = S.derive(df).filter(pl.col("position").is_in(MARKETS[m]["pos"]), pl.col("season_type") == "REG")
    d = d.with_columns(OUT[m].alias("y")).with_columns((pl.col("y") >= MARKETS[m]["bar"]).cast(pl.Int8).alias("hit"))
    if m in S.MODELS:
        d = S.score(df, m).select("player_id", "week", pl.col("score").alias("hand")).join(d, on=["player_id", "week"], how="right")
    else:
        d = d.with_columns(pl.lit(0.0).alias("hand"))
    return d


def matrix(d, cols):
    X = [d[c].cast(pl.Float64).fill_null(0.0).to_numpy() if c in d.columns else np.zeros(d.height) for c in cols]
    X += [(d["position"] == p).cast(pl.Float64).to_numpy() for p in POS]
    return np.column_stack(X)


def topk_week(d, key, k=15):
    h = n = 0
    for _, g in d.group_by("week"):
        s = g.sort([key, "player_id"], descending=[True, False]).head(k)
        h += int(s["hit"].sum()); n += s.height
    return 100 * h / n if n else float("nan")


def auc(y, s):
    o = np.argsort(s); r = np.empty(len(s)); r[o] = np.arange(1, len(s) + 1)
    p = y.sum(); q = len(y) - p
    return float((r[y == 1].sum() - p * (p + 1) / 2) / (p * q)) if p and q else float("nan")


def fit_eval(m, cols, tr, te, name):
    Xtr, Xte = matrix(tr, cols), matrix(te, cols)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = fit_logistic((Xtr - mu) / sd, tr["hit"].to_numpy().astype(float), l2=20.0)
    z = ((Xte - mu) / sd) @ w[1:] + w[0]
    y = te["hit"].to_numpy().astype(float)
    p = np.clip(1 / (1 + np.exp(-z)), 1e-6, 1 - 1e-6)
    t = te.with_columns(pl.Series("fit", z))
    base = y.mean()
    ll = -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)); l0 = -np.mean(y * math.log(base) + (1 - y) * math.log(1 - base)) if 0 < base < 1 else float("nan")
    o = np.argsort(p); k = max(1, len(o) // 8)
    cal = " ".join(f"{100 * p[o[i * k:(i + 1) * k if i < 7 else None]].mean():.0f}>{100 * y[o[i * k:(i + 1) * k if i < 7 else None]].mean():.0f}" for i in range(8))
    print(f"   {name}: {te.height} player-weeks, base {100 * base:.1f}%  top-15/wk  fitted {topk_week(t, 'fit'):.1f}%  hand {topk_week(t, 'hand'):.1f}%  form {topk_week(t, FORM[m]):.1f}%   "
          f"AUC fitted {auc(y, z):.3f} hand {auc(y, t['hand'].to_numpy()):.3f}   log loss {ll:.4f} vs {l0:.4f}\n      calibration by eighth (pred>actual %): {cal}")
    return mu, sd, w, z, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl")); ap.add_argument("--market", nargs="*", default=None); ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    T = {s: table(s, a.cache) for s in (2023, 2024, 2025, 2026)}
    for m in (a.market or list(MARKETS)):
        cols = feats(m)
        D = {s: prep(T[s], m) for s in T}
        D = {s: d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d for s, d in D.items()}
        print(f"\n{m}  (bar {MARKETS[m]['bar']}, {', '.join(MARKETS[m]['pos'])})")
        tr = pl.concat([D[2023], D[2024]], how="diagonal_relaxed")
        mu, sd, w, z25, y25 = fit_eval(m, cols, tr, D[2025], "graded 2025 (fit 2023-24)")
        if D[2026].height:
            fit_eval(m, cols, pl.concat([D[2023], D[2024], D[2025]], how="diagonal_relaxed"), D[2026], "graded 2026 W1-7 (fit 2023-25)")
        if a.save:
            allr = pl.concat([D[s] for s in (2023, 2024, 2025, 2026) if D[s].height], how="diagonal_relaxed")
            X = matrix(allr, cols); amu, asd = X.mean(0), X.std(0) + 1e-9
            aw = fit_logistic((X - amu) / asd, allr["hit"].to_numpy().astype(float), l2=20.0)
            ab = fit_logistic(z25.reshape(-1, 1), y25, l2=0.01)
            qs = np.sort(1 / (1 + np.exp(-(ab[0] + ab[1] * z25)))); t = float(qs[int(len(qs) * 0.99)])
            (HERE.parent / "models" / f"nfl_{m.lower()}_fit_v1.json").write_text(json.dumps({
                "model_version": f"nfl_{m.lower()}_fit_v1", "target": f"{m} >= {MARKETS[m]['bar']}", "positions": MARKETS[m]["pos"], "features": cols + POS, "mu": amu.tolist(), "sd": asd.tolist(),
                "intercept": float(aw[0]), "weights": aw[1:].tolist(), "fit_on": f"{allr.height} player-weeks, 2023-26",
                "platt": {"a": float(ab[0]), "b": float(ab[1]), "tail": {"t": t, "slope": 0.25}}}, indent=1))
            print(f"      saved models/nfl_{m.lower()}_fit_v1.json")


if __name__ == "__main__":
    main()
