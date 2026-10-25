#!/usr/bin/env python3
"""
nfl_more.py -- the NFL markets that had no backtest, and the two inputs that were never tested.

    python bots/nfl/nfl_more.py [--cache DIR] [--only TD2 QB_TD PASS_COMP PASS_INT LONG_REC DEF_TD WEATHER]

New markets, each fitted on 2023-24 and graded on 2025, then fit 2023-25 and graded on 2026 weeks to date, against the plain-form ranking, same as nfl_lanes.py:
  TD2        2+ touchdowns (RB/WR/TE)
  QB_TD      a quarterback rushing touchdown (the QB side of "anytime TD")
  PASS_COMP  23+ completions            PASS_INT  1+ interception thrown
  LONG_REC   longest reception of 25+ yards (from play-by-play)
  DEF_TD     a team's defence / special teams scores a touchdown (team-game rows from play-by-play; the old DEF_TD score had no backtest)
  WEATHER    wind and roof added to the passing / receiving markets (never used before): does the fit improve?
Trailing features only; interceptions, sacks and longest-reception are rolled over the previous 8 games, across seasons.
"""
import argparse, math, sys
from pathlib import Path
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import nfl_features as F          # noqa: E402
import nfl_lanes as L             # noqa: E402
from tune_models import fit_logistic    # noqa: E402

TD_W = ["f_rz_opp", "implied_total", "f_touches", "f_xtd", "f_gl_opp", "f_snap_pct"]
NEW = {
    "TD1": {"pos": ["RB", "WR", "TE"], "bar": 1, "w": {c: 1 for c in TD_W}, "out": pl.col("rushing_tds") + pl.col("receiving_tds"), "form": "f_td_actual", "extra": ["f_touches"]},
    "TD2": {"pos": ["RB", "WR", "TE"], "bar": 2, "w": {c: 1 for c in TD_W}, "out": pl.col("rushing_tds") + pl.col("receiving_tds"), "form": "f_td_actual", "extra": ["f_touches"]},
    "QB_TD": {"pos": ["QB"], "bar": 1, "w": {"f_rushing_tds": 1, "f_carries": 1, "f_rz_car": 1, "implied_total": 1}, "out": pl.col("rushing_tds"), "form": "f_rushing_tds", "extra": ["f_rushing_yards", "spread"]},
    "PASS_COMP": {"pos": ["QB"], "bar": 23, "w": {"f_completions": 1, "f_attempts": 1, "total_line": 1, "opp_pass_soft": 1}, "out": pl.col("completions"), "form": "f_completions", "extra": ["f_passing_cpoe", "f_sacks"]},
    "PASS_INT": {"pos": ["QB"], "bar": 1, "w": {"f_int": 1, "f_attempts": 1, "total_line": 1, "opp_pass_soft": 1}, "out": pl.col("passing_interceptions"), "form": "f_int", "extra": ["f_sacks", "f_passing_cpoe"]},
    "LONG_REC": {"pos": ["WR", "TE", "RB"], "bar": 25, "w": {"f_longest": 1, "f_targets": 1, "f_receiving_air_yards": 1, "f_wopr": 1}, "out": pl.col("longest_rec"), "form": "f_longest", "extra": ["f_receiving_yards", "f_target_share"]},
}


def augment(cache):
    """all four seasons, with trailing interceptions / sacks / longest reception (previous 8 games, across seasons) and the pbp longest-reception outcome."""
    parts = []
    for s in (2023, 2024, 2025, 2026):
        t = L.table(s, cache)
        lg = Path(cache) / f"longest_{s}.parquet"
        if lg.exists():
            lr = pl.read_parquet(lg)
        else:
            p = F._pbp(s).filter((pl.col("season_type") == "REG") & (pl.col("complete_pass") == 1) & pl.col("receiver_player_id").is_not_null())
            lr = p.group_by(["receiver_player_id", "week"]).agg(pl.col("yards_gained").max().alias("longest_rec")).rename({"receiver_player_id": "player_id"})
            lr.write_parquet(lg)
        parts.append(t.join(lr, on=["player_id", "week"], how="left").with_columns(pl.col("longest_rec").fill_null(0), pl.lit(s).alias("_season")))
    allt = pl.concat(parts, how="diagonal_relaxed").sort(["player_id", "_season", "week"])
    for src, name in (("passing_interceptions", "f_int"), ("sacks_suffered", "f_sacks"), ("longest_rec", "f_longest")):
        allt = allt.with_columns(pl.col(src).fill_null(0).shift(1).rolling_mean(8, min_samples=1).over("player_id").alias(name))
    return {s: allt.filter(pl.col("_season") == s) for s in (2023, 2024, 2025, 2026)}


def live_extras(cache):
    """{player_id: {f_int, f_sacks, f_longest}} as of now (mean of each player's last 8 games), for scoring the coming week."""
    parts = []
    T = augment(cache)
    allt = pl.concat([T[s] for s in T], how="diagonal_relaxed").sort(["player_id", "_season", "week"])
    out = {}
    for pid, g in allt.group_by("player_id"):
        pid = pid[0] if isinstance(pid, tuple) else pid
        last = g.tail(8)
        out[pid] = {"f_int": float(last["passing_interceptions"].fill_null(0).mean()), "f_sacks": float(last["sacks_suffered"].fill_null(0).mean()), "f_longest": float(last["longest_rec"].fill_null(0).mean())}
    return out


def save_models(cache):
    """Production fits for the new markets (all seasons incl. 2026), Platt + tail from the 2025 holdout, models/nfl_<market>_fit_v1.json (read by lane_fit.py)."""
    import json
    register(); T = augment(cache)
    for m in NEW:
        if m == "TD1":
            continue
        cols = L.feats(m)
        D = {s: L.prep(T[s], m) for s in T}
        D = {s: d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d for s, d in D.items()}
        tr = pl.concat([D[2023], D[2024]], how="diagonal_relaxed")
        Xtr = L.matrix(tr, cols); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, tr["hit"].to_numpy().astype(float), l2=20.0)
        z25 = ((L.matrix(D[2025], cols) - mu) / sd) @ w[1:] + w[0]; y25 = D[2025]["hit"].to_numpy().astype(float)
        allr = pl.concat([D[s] for s in (2023, 2024, 2025, 2026) if D[s].height], how="diagonal_relaxed")
        X = L.matrix(allr, cols); amu, asd = X.mean(0), X.std(0) + 1e-9
        aw = fit_logistic((X - amu) / asd, allr["hit"].to_numpy().astype(float), l2=20.0)
        ab = fit_logistic(z25.reshape(-1, 1), y25, l2=0.01)
        qs = np.sort(1 / (1 + np.exp(-(ab[0] + ab[1] * z25)))); t = float(qs[int(len(qs) * 0.99)])
        (HERE.parent / "models" / f"nfl_{m.lower()}_fit_v1.json").write_text(json.dumps({
            "model_version": f"nfl_{m.lower()}_fit_v1", "target": f"{m} >= {NEW[m]['bar']}", "positions": NEW[m]["pos"], "features": cols + L.POS, "mu": amu.tolist(), "sd": asd.tolist(),
            "intercept": float(aw[0]), "weights": aw[1:].tolist(), "fit_on": f"{allr.height} player-weeks, 2023-26", "platt": {"a": float(ab[0]), "b": float(ab[1]), "tail": {"t": t, "slope": 0.25}}}, indent=1))
        print(f"saved models/nfl_{m.lower()}_fit_v1.json")


def register():
    for m, c in NEW.items():
        L.MARKETS[m] = {"pos": c["pos"], "bar": c["bar"], "w": c["w"]}
        L.OUT[m] = c["out"]; L.FORM[m] = c["form"]; L.EXTRA[m] = c["extra"]


def run_market(m, T, label=None):
    cols = L.feats(m)
    D = {s: L.prep(T[s], m) for s in T}
    D = {s: d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d for s, d in D.items()}
    print(f"\n{label or m}  (bar {L.MARKETS[m]['bar']}, {', '.join(L.MARKETS[m]['pos'])})")
    tr = pl.concat([D[2023], D[2024]], how="diagonal_relaxed")
    L.fit_eval(m, cols, tr, D[2025], "graded 2025 (fit 2023-24)")
    if D[2026].height:
        L.fit_eval(m, cols, pl.concat([D[2023], D[2024], D[2025]], how="diagonal_relaxed"), D[2026], "graded 2026 W1-7 (fit 2023-25)")


def def_td(cache):
    """team-game rows: did the team's defence / special teams score a touchdown."""
    import nflreadpy as nfl
    rows = []
    for s in (2023, 2024, 2025, 2026):
        p = F._pbp(s).filter(pl.col("season_type") == "REG")
        td = p.filter((pl.col("touchdown") == 1) & pl.col("td_team").is_not_null() & (pl.col("td_team") != pl.col("posteam"))).group_by(["game_id", "td_team"]).len().rename({"td_team": "team", "len": "def_td"})
        give = p.group_by(["game_id", "posteam"]).agg(((pl.col("interception") == 1).sum() + (pl.col("fumble_lost") == 1).sum()).alias("giveaways")).rename({"posteam": "opp_off"})
        sch = nfl.load_schedules([s]).filter(pl.col("game_type") == "REG")
        for r in sch.iter_rows(named=True):
            for team, opp, spread, tot in ((r["home_team"], r["away_team"], r["spread_line"], r["total_line"]), (r["away_team"], r["home_team"], -(r["spread_line"] or 0), r["total_line"])):
                g = r["game_id"]
                d = td.filter((pl.col("game_id") == g) & (pl.col("team") == team))
                gv = give.filter((pl.col("game_id") == g) & (pl.col("opp_off") == opp))
                rows.append({"season": s, "week": r["week"], "game": g, "team": team, "opp": opp, "y": int((d["def_td"].sum() if d.height else 0) >= 1), "opp_give": float(gv["giveaways"][0]) if gv.height else None,
                             "spread": float(spread or 0), "total": float(tot or 0), "played": r["home_score"] is not None})
    df = pl.DataFrame(rows).filter(pl.col("played")).sort(["season", "week"])
    df = df.with_columns(pl.col("y").cast(pl.Float64))
    df = df.with_columns(pl.col("y").shift(1).rolling_mean(16, min_samples=4).over("team").alias("f_def_td"), pl.col("opp_give").shift(1).rolling_mean(8, min_samples=3).over("opp").alias("f_opp_give"))
    # the OPPONENT's giveaways must be its own trailing rate when IT was on offense: use the opponent's rolling mean of their own giveaways
    own = df.select("season", "week", "team", pl.col("opp_give").alias("_g")).rename({"team": "opp_team"})
    return df.filter(pl.col("f_def_td").is_not_null())


def run_def_td(df):
    print("\nDEF_TD  (a team's defence / special teams scores a touchdown, team-game rows)")
    cols = ["f_def_td", "spread", "total"]
    def mat(d): return np.column_stack([d[c].fill_null(0).to_numpy() for c in cols])
    for test, trs in ((2025, (2023, 2024)), (2026, (2023, 2024, 2025))):
        tr = df.filter(pl.col("season").is_in(list(trs))); te = df.filter(pl.col("season") == test)
        if not te.height:
            continue
        mu, sd = mat(tr).mean(0), mat(tr).std(0) + 1e-9
        w = fit_logistic((mat(tr) - mu) / sd, tr["y"].to_numpy(), l2=20.0)
        z = ((mat(te) - mu) / sd) @ w[1:] + w[0]; y = te["y"].to_numpy(); p = np.clip(1 / (1 + np.exp(-z)), 1e-6, 1 - 1e-6)
        t = te.with_columns(pl.Series("fit", z), pl.col("y").cast(pl.Int8).alias("hit"), pl.col("team").alias("player_id"))
        base = y.mean()
        o = np.argsort(p); k = len(o) // 6
        print(f"   graded {test}: {te.height} team-games, base {100 * base:.1f}%  top-5/wk fitted {L.topk_week(t, 'fit', 5):.1f}%  form {L.topk_week(t, 'f_def_td', 5):.1f}%  AUC fitted {L.auc(y, z):.3f} form {L.auc(y, te['f_def_td'].fill_null(0).to_numpy()):.3f}  "
              f"log loss {-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)):.4f} vs {-(base * math.log(base) + (1 - base) * math.log(1 - base)):.4f}")
        print("      calibration by sixth (pred>actual %): " + " ".join(f"{100 * p[o[i * k:(i + 1) * k if i < 5 else None]].mean():.1f}>{100 * y[o[i * k:(i + 1) * k if i < 5 else None]].mean():.1f}" for i in range(6)))


def weather(T):
    print("\nWEATHER / ROOF: wind and indoors added to the same fit (graded 2025, fit 2023-24); change in AUC and log loss, positive AUC change = better")
    for m in ("REC_YDS", "REC", "RUSH_YDS", "PASS_YDS", "PASS_TD", "KICK_PTS"):
        res = []
        for extra in ([], ["wind_mph", "indoors"]):
            keep = list(L.COMMON); L.COMMON[:] = keep + extra
            cols = L.feats(m)
            D = {s: L.prep(T[s], m) for s in T}
            D = {s: d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d for s, d in D.items()}
            tr = pl.concat([D[2023], D[2024]], how="diagonal_relaxed"); te = D[2025]
            Xtr, Xte = L.matrix(tr, cols), L.matrix(te, cols)
            mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            w = fit_logistic((Xtr - mu) / sd, tr["hit"].to_numpy().astype(float), l2=20.0)
            z = ((Xte - mu) / sd) @ w[1:] + w[0]; y = te["hit"].to_numpy().astype(float); p = np.clip(1 / (1 + np.exp(-z)), 1e-6, 1 - 1e-6)
            res.append((L.auc(y, z), -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))))
            L.COMMON[:] = keep
        print(f"   {m:<9} AUC {res[0][0]:.4f} -> {res[1][0]:.4f} ({res[1][0] - res[0][0]:+.4f})   log loss {res[0][1]:.4f} -> {res[1][1]:.4f} ({res[1][1] - res[0][1]:+.4f})")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl")); ap.add_argument("--only", nargs="*", default=None); ap.add_argument("--save", action="store_true"); a = ap.parse_args()
    if a.save:
        return save_models(a.cache)
    want = a.only or list(NEW) + ["DEF_TD", "WEATHER"]
    register(); T = augment(a.cache)
    for m in NEW:
        if m in want and m != "TD1":
            run_market(m, T)
    if "DEF_TD" in want:
        run_def_td(def_td(a.cache))
    if "WEATHER" in want:
        weather(T)


if __name__ == "__main__":
    main()
