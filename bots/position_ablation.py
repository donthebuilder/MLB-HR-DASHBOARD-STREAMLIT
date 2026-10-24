#!/usr/bin/env python3
"""
position_ablation.py -- does defence BY POSITION add anything the team-level number didn't?

    python bots/position_ablation.py nba [--cache DIR]

NBA: for each opponent, what guards (G), forwards (F) and centres (C) have scored / rebounded / assisted against it over its last 10 games,
minus the league's average for that group. Each player gets the number for HIS group (ESPN's listed position). Compared on the holdout:
  team-level only   (opp points / rebounds / assists allowed, as the tuned models use now)
  + position-level  (the same, plus what his position gets on that defence)
  position only     (position-level in place of the team-level number)
Same train seasons, same holdout, same rows. Lower log loss = better.
"""
import argparse
import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb          # noqa: E402
from tune_models import fit_logistic, topk   # noqa: E402


def positions(cache):
    cache = Path(cache)
    pids = sorted({p["pid"] for s in (2023, 2024, 2025) for g in bb.load(s, str(cache)) for t in g["teams"] for p in t["players"]})

    def one(pid):
        f = cache / f"pos_{pid}.json"
        if f.exists():
            return pid, json.load(open(f))
        try:
            a = json.loads(subprocess.run(["curl", "-s", "-m", "30", f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}"], capture_output=True, text=True).stdout)["athlete"]
            ab = (a.get("position") or {}).get("abbreviation") or ""
        except Exception:
            ab = ""
        g = "G" if "G" in ab else "C" if ab == "C" else "F" if "F" in ab else None
        f.write_text(json.dumps(g))
        return pid, g
    with ThreadPoolExecutor(12) as ex:
        return {pid: g for pid, g in ex.map(one, pids) if g}


def logloss(rs):
    return -sum(math.log(min(1 - 1e-6, max(1e-6, 1 / (1 + math.exp(-r["_z"]))))) if r["y"] else math.log(min(1 - 1e-6, max(1e-6, 1 - 1 / (1 + math.exp(-r["_z"]))))) for r in rs) / len(rs)


def compare(label, rows, getf, gety, train, test, team_cols, pos_cols):
    tr = [r for r in rows if r["season"] in train]; te = [r for r in rows if r["season"] == test]
    names = sorted(getf(tr[0]))
    out = []
    variants = [("team-level only", [n for n in names if n not in pos_cols]),
                ("+ position-level", names),
                ("position only (no team-level)", [n for n in names if n not in team_cols])]
    for vname, use in variants:
        X = lambda rs: np.array([[getf(r)[n] for n in use] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([gety(r) for r in tr], float))
        for r, z in zip(te, ((X(te) - mu) / sd) @ w[1:] + w[0]):
            r["_z"], r["y"], r["_g"] = float(z), gety(r), (r["night"], r["game"])
        coef = {n: c for n, c in zip(use, w[1:]) if n in pos_cols}
        out.append((vname, topk(te, "_z", "y", 1, "_g"), topk(te, "_z", "y", 10, "night"), logloss(te), coef))
    print(f"{label}  (graded on {len(te)} player-games)")
    base = out[0]
    for v, t1, t10, ll, coef in out:
        extra = "" if v == "team-level only" else f"   (vs team-level: best/game {t1 - base[1]:+.1f}, top10 {t10 - base[2]:+.1f}, loss {ll - base[3]:+.4f})"
        print(f"   {v:<30} best/game {t1:5.1f}%  top10/night {t10:5.1f}%  log loss {ll:.4f}{extra}")
    if out[1][4]:
        print("   position-level weights: " + ", ".join(f"{k} {c:+.3f}" for k, c in out[1][4].items()))
    print()


def nfl(cache):
    """NFL anytime TD: nfl_td_v2 as it is (no opponent term) vs adding the ROLE-AWARE defence (how many TDs this defence gives to HIS role -- WR1, slot,
    RB1, TE... -- over its recent games) vs adding the team-level softness. One fixed weight (.10), no tuning, three seasons, both ends reported."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "nfl"))
    import polars as pl
    import nfl_td_v3_backtest as T
    import nfl_tuddy_backtest as NT
    from nfl_scoring import MODELS, derive, OUTCOME
    V2 = MODELS["TD"]["w"]
    variants = {"nfl_td_v2 (live)": V2, "+ role-aware defence .10": {**V2, "f_oppr_dr_td": 0.10}, "+ team-level softness .10": {**V2, "opp_td_soft": 0.10},
                "+ both .10 each": {**V2, "f_oppr_dr_td": 0.10, "opp_td_soft": 0.10}}
    pool = {k: [0, 0, 0, 0] for k in variants}
    per = {k: [] for k in variants}
    for yr in (2023, 2024, 2025):
        tbl = NT.season_table(yr, cache)
        d = derive(tbl).filter(pl.col("position").is_in(MODELS["TD"]["pos"]), pl.col("season_type") == "REG")
        d = d.with_columns((OUTCOME["TD"] >= 1).cast(pl.Int8).alias("hit"), pl.col("f_oppr_dr_td").fill_null(pl.col("f_oppr_dr_td").mean()))
        for name, w in variants.items():
            x = T._score(d, w)
            (h5, n5), (h15, n15) = T._topk(x, 5), T._topk(x, 15)
            for i, v in enumerate((h5, n5, h15, n15)):
                pool[name][i] += v
            per[name].append(f"{100 * h15 / n15:.1f}")
    print("NFL anytime TD, 2023-25 (role-aware defence = nfl_role_dvp's trailing TD rate to the player's role)")
    for k, v in pool.items():
        print(f"   {k:<28} top-5 {100 * v[0] / v[1]:5.1f}%   top-15 {100 * v[2] / v[3]:5.1f}%   top-15 by season {' / '.join(per[k])}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=["nba", "nfl"])
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    a = ap.parse_args()
    if a.sport == "nfl":
        nfl(a.cache if a.cache != str(Path.home() / ".cache/dash_nba") else str(Path.home() / ".cache/dash_nfl"))
        return 0
    bb.POS_GROUP.update(positions(a.cache))
    print(f"positions known for {len(bb.POS_GROUP)} players ({sum(v == 'G' for v in bb.POS_GROUP.values())} G, {sum(v == 'F' for v in bb.POS_GROUP.values())} F, {sum(v == 'C' for v in bb.POS_GROUP.values())} C)\n")
    rows = [r for s in (2023, 2024, 2025) for r in bb.run(s, a.cache) if r.get("lanes")]
    compare("NBA points 25+", rows, lambda r: r["f"], lambda r: r["y25"], [2023, 2024], 2025, {"opp_allowed"}, {"opp_pos_pts_rel", "opp_pos_reb_rel", "opp_pos_ast_rel"})
    for lane, label in (("reb", "NBA rebounds 10+"), ("ast", "NBA assists 8+"), ("pra", "NBA PRA 35+")):
        compare(label, rows, lambda r, l=lane: r["lanes"][l]["f"], lambda r, l=lane: r["lanes"][l]["y"], [2023, 2024], 2025, {"opp_allowed"}, {"opp_pos_rel"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
