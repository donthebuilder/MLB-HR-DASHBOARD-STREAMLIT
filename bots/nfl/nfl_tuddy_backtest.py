#!/usr/bin/env python3
"""nfl_tuddy_backtest.py -- TUDDY v4 (usage trend x position matchup) against the
published TD model (nfl_td_v2), on the same player-weeks.

    python nfl/nfl_tuddy_backtest.py --history 2023 2024 2025

Same table and grading as nfl_td_v3_backtest.py: nfl_features.build(season),
within-week percentiles for v2, top-k per week, per-week AUC. 2023 is the
season no weight was tuned on -- read it as the holdout.

v4 is scored through dash_models.tuddy_inputs, the adapter the live run uses.
Everything it reads comes from weeks BEFORE the week being scored:
  usage (red-zone / goal-line touches, TDs, snap%)  this player, last 5 prior weeks
  defence vs position                               this opponent, last 10 prior weeks
  game total                                        the pregame line

Compared on the intersection of rows: a player needs 3+ prior games this season
for v4's trend to mean anything, and v2 is scored on exactly those same rows.
Not covered: lane (no route data), explosive plays (overlay stays 1.0).
"""
from __future__ import annotations
import argparse
import os
import sys
from collections import defaultdict

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from nfl_scoring import MODELS, OUTCOME, derive          # noqa: E402
from nfl_td_v3_backtest import _score, _topk, _auc       # noqa: E402
import dash_models as dm                                  # noqa: E402

POS = MODELS["TD"]["pos"]
MIN_PRIOR = 3


def season_table(season: int, cache: str | None) -> pl.DataFrame:
    from nfl_features import build, snap_share
    path = os.path.join(cache, f"tuddy_{season}.parquet") if cache else None
    if path and os.path.exists(path):
        return pl.read_parquet(path)
    t = build(season)
    s = snap_share(season).select("player_id", "week", "snap_pct")
    t = t.join(s, on=["player_id", "week"], how="left")
    if path:
        os.makedirs(cache, exist_ok=True)
        t.write_parquet(path)
    return t


def score_v4(tbl: pl.DataFrame) -> pl.DataFrame:
    """Add v4_score and prior-game count; rows without MIN_PRIOR prior games get None."""
    tbl = tbl.filter(pl.col("season_type") == "REG")
    cols = ["player_id", "week", "team", "opponent_team", "position", "rz_opp", "gl_opp",
            "td_actual", "snap_pct", "rushing_tds", "receiving_tds", "total_line"]
    base = tbl.select([c for c in cols if c in tbl.columns]).to_dicts()

    by_player = defaultdict(list)
    vs_pos = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # (opp,pos) -> week -> [td, rz]
    for r in base:
        by_player[r["player_id"]].append(r)
        if r["position"] in POS:
            cell = vs_pos[(r["opponent_team"], r["position"])][r["week"]]
            cell[0] += (r["rushing_tds"] or 0) + (r["receiving_tds"] or 0)
            cell[1] += r["rz_opp"] or 0
    for rows in by_player.values():
        rows.sort(key=lambda r: r["week"])

    out = {}
    gaps = dm.Gaps()
    for pid, rows in by_player.items():
        for i, r in enumerate(rows):
            if r["position"] not in POS or i < MIN_PRIOR:
                continue
            prior = [w for w in rows[:i] if w["week"] < r["week"]]
            if len(prior) < MIN_PRIOR:
                continue
            cells = vs_pos[(r["opponent_team"], r["position"])]
            opp_prior = [{"td": cells[w][0], "rz_opp": cells[w][1]} for w in sorted(cells) if w < r["week"]]
            s_in, m_in, spec = dm.tuddy_inputs(prior, opp_prior, r["position"], gaps)
            out[(pid, r["week"])] = dm.tuddy_score(s_in, m_in, r["position"], float(r["total_line"] or 44.0), spec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", nargs="+", type=int, default=[2023, 2024, 2025])
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()
    pooled = {"v2": [0, 0, 0, 0], "v4": [0, 0, 0, 0]}
    games_n = [0, 0, 0, 0, 0]       # games, v2 #1 hit, v4 #1 hit, v2 hit when they differ, v4 hit when they differ
    for s in a.history:
        tbl = season_table(s, a.cache)
        v4 = score_v4(tbl)
        d = derive(tbl).filter(pl.col("position").is_in(POS), pl.col("season_type") == "REG")
        d = d.with_columns((OUTCOME["TD"] >= 1).cast(pl.Int8).alias("hit"))
        d = d.with_columns(pl.struct(["player_id", "week"]).map_elements(
            lambda x: v4.get((x["player_id"], x["week"])), return_dtype=pl.Float64).alias("v4_score"))
        d = d.filter(pl.col("v4_score").is_not_null())
        print(f"\n{s}{' (HOLDOUT)' if s == 2023 else ''}: {d.height} player-weeks, base {100 * float(d['hit'].mean()):.1f}%")
        scored = {"v2": _score(d, MODELS["TD"]["w"]),
                  "v4": d.with_columns(pl.col("v4_score").alias("score"))}
        # per game: each model's single best player (both teams together)
        agree = dis = 0
        for _, g in scored["v2"].with_columns(pl.col("v4_score")).group_by("game_id"):
            if g.height < 2:
                continue
            top2 = g.sort(["score", "player_id"], descending=[True, False]).row(0, named=True)
            top4 = g.sort(["v4_score", "player_id"], descending=[True, False]).row(0, named=True)
            games_n[0] += 1
            games_n[1] += top2["hit"]; games_n[2] += top4["hit"]
            if top2["player_id"] == top4["player_id"]:
                agree += 1
            else:
                dis += 1; games_n[3] += top2["hit"]; games_n[4] += top4["hit"]
        for name, x in (("v2 nfl_td_v2 (live)", scored["v2"]),
                        ("v4 TUDDY usage x matchup", scored["v4"])):
            (h5, n5), (h15, n15) = _topk(x, 5), _topk(x, 15)
            print(f"  {name:<26} top-5 {h5}/{n5} {100*h5/n5:5.1f}%   top-15 {h15}/{n15} {100*h15/n15:5.1f}%   AUC {_auc(x):.4f}")
            k = "v2" if name.startswith("v2") else "v4"
            for i, v in enumerate((h5, n5, h15, n15)):
                pooled[k][i] += v
    print("\nPOOLED  " + "   ".join(
        f"{k}: top-5 {100*v[0]/v[1]:.1f}%  top-15 {100*v[2]/v[3]:.1f}%" for k, v in pooled.items()))
    g, h2, h4, d2, d4 = games_n
    print(f"\nBEST PLAYER IN EACH GAME ({g} games): v2's #1 scored {100*h2/g:.1f}%, v4's #1 scored {100*h4/g:.1f}%")
    print(f"  they picked different players in some games; in those, v2's pick scored {d2} times, v4's {d4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
