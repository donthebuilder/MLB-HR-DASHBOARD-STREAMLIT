#!/usr/bin/env python3
"""nfl_qb_td.py -- the QB anytime-TD context block (bot task 1, 2026-10-11).

The QB anytime-TD SCORE is nfl_scoring.QB_TD_MODEL (same weights as the TD
model, scored against the RB/WR/TE league). This module publishes the numbers
the site's spec asked to see BESIDE that score, per QB and per season, with the
games count so the site's low-sample rule has a denominator:

  rush_att_pg        rushing attempts per game (kneels out)
  rush_td_pg         rushing TDs per game
  rz_car_pg          carries inside the 20 per game
  gl_car_pg          carries inside the 5 per game
  designed_share     designed runs / all rushes (scrambles out of the numerator)

and, per upcoming opponent, how many rushing TDs by QBs that defence has
allowed per game (`opp_qb_rush_td_pg`).

CONTEXT ONLY. None of it is a weight: designed-run share and the opponent's QB
rush TDs allowed are candidates to be backtested before they move a score, and
the rule here is that an untested idea does not select. Everything is computed
from play-by-play weeks BEFORE the week being built, so a rebuild after
kickoff never reads the game it is predicting. Nothing is invented: a QB with no
games in a season has no entry for it, and a rate whose denominator is zero is
absent rather than 0.

Shape (per player_id):
  {"2026": {"games": 4, "rush_att_pg": 5.5, ...}, "2025": {...},
   "opp": "KC", "opp_qb_rush_td_pg": {"2026": 0.25, "2025": 0.18, "games": {...}}}
"""
from __future__ import annotations

import polars as pl

DEFENCE_KEY = "opp_qb_rush_td_pg"


def _season_rows(pbp: pl.DataFrame, qb_ids: set[str], before_week: int | None) -> pl.DataFrame:
    p = pbp
    if "season_type" in p.columns:
        p = p.filter(pl.col("season_type") == "REG")
    if before_week is not None:
        p = p.filter(pl.col("week") < before_week)
    return p.filter(pl.col("rush_attempt") == 1, pl.col("qb_kneel") == 0,
                    pl.col("rusher_player_id").is_in(list(qb_ids)))


def qb_rates(pbp: pl.DataFrame, qb_ids: set[str], before_week: int | None = None) -> dict[str, dict]:
    """{player_id: {games, rush_att_pg, rush_td_pg, rz_car_pg, gl_car_pg, designed_share}}
    for ONE season's play-by-play. `games` counts the weeks he rushed or threw
    (a QB who played and never ran still has a game, and rates of 0.0)."""
    if not qb_ids:
        return {}
    p = pbp
    if "season_type" in p.columns:
        p = p.filter(pl.col("season_type") == "REG")
    if before_week is not None:
        p = p.filter(pl.col("week") < before_week)
    played = (p.filter(pl.col("passer_player_id").is_in(list(qb_ids)))
                .select(pl.col("passer_player_id").alias("pid"), "week")
              .vstack(p.filter(pl.col("rusher_player_id").is_in(list(qb_ids)))
                        .select(pl.col("rusher_player_id").alias("pid"), "week"))
              .unique().group_by("pid").agg(pl.len().alias("games")))
    r = _season_rows(pbp, qb_ids, before_week).group_by("rusher_player_id").agg(
        pl.len().alias("att"),
        pl.col("rush_touchdown").sum().alias("td"),
        (pl.col("yardline_100") <= 20).sum().alias("rz"),
        (pl.col("yardline_100") <= 5).sum().alias("gl"),
        (pl.col("qb_scramble") == 0).sum().alias("designed"),
    ).rename({"rusher_player_id": "pid"})
    j = played.join(r, on="pid", how="left").fill_null(0)
    out: dict[str, dict] = {}
    for row in j.iter_rows(named=True):
        g = int(row["games"])
        if g <= 0:
            continue
        att = int(row["att"])
        out[str(row["pid"])] = {
            "games": g,
            "rush_att_pg": round(att / g, 2),
            "rush_td_pg": round(int(row["td"]) / g, 3),
            "rz_car_pg": round(int(row["rz"]) / g, 2),
            "gl_car_pg": round(int(row["gl"]) / g, 2),
            "designed_share": round(int(row["designed"]) / att, 3) if att else None,
        }
    return out


def defence_qb_rush_td(pbp: pl.DataFrame, before_week: int | None = None) -> dict[str, dict]:
    """{defence_team: {"td_pg": float, "games": int}}: rushing TDs scored by a
    QB against that defence, per game it played. A QB rusher is any rusher who
    also threw a pass for the same club in that season's file."""
    p = pbp
    if "season_type" in p.columns:
        p = p.filter(pl.col("season_type") == "REG")
    if before_week is not None:
        p = p.filter(pl.col("week") < before_week)
    qbs = (p.filter(pl.col("passer_player_id").is_not_null())["passer_player_id"].unique().to_list())
    games = (p.filter(pl.col("defteam").is_not_null())
               .select("defteam", "game_id").unique()
               .group_by("defteam").agg(pl.len().alias("g")))
    td = (p.filter(pl.col("rush_touchdown") == 1, pl.col("rusher_player_id").is_in(qbs))
            .group_by("defteam").agg(pl.len().alias("td")))
    j = games.join(td, on="defteam", how="left").fill_null(0)
    return {r["defteam"]: {"td_pg": round(r["td"] / r["g"], 3), "games": int(r["g"])}
            for r in j.iter_rows(named=True) if r["g"] > 0}


def build(pbp_by_season: dict[int, pl.DataFrame], season: int, week: int | None,
          qbs: list[dict]) -> dict[str, dict]:
    """`qbs` = [{"player_id", "opp"}]. `pbp_by_season` = {season: pbp}; the
    current season is cut to weeks before `week`, earlier seasons whole."""
    ids = {str(q["player_id"]) for q in qbs}
    per_season = {s: qb_rates(pbp, ids, week if s == season else None)
                  for s, pbp in pbp_by_season.items()}
    dfn = {s: defence_qb_rush_td(pbp, week if s == season else None)
           for s, pbp in pbp_by_season.items()}
    out: dict[str, dict] = {}
    for q in qbs:
        pid = str(q["player_id"])
        blk = {str(s): per_season[s][pid] for s in per_season if pid in per_season[s]}
        opp = q.get("opp")
        if opp:
            blk["opp"] = opp
            d = {str(s): dfn[s][opp]["td_pg"] for s in dfn if opp in dfn[s]}
            if d:
                blk[DEFENCE_KEY] = {**d, "games": {str(s): dfn[s][opp]["games"]
                                                   for s in dfn if opp in dfn[s]}}
        if blk:
            out[pid] = blk
    return out
