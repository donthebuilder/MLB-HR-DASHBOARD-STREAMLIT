#!/usr/bin/env python3
"""nfl_pass_game.py -- a team's top targets, and a defence's corners.

WHY (Donovan 09-28): "we need to see the top receivers with the top corners
when we're looking at a matchup." .claude-notes/BATCH-NFL-STATS-DEPTH-PLAN.md
section C.

HONEST LIMIT, kept on purpose: no free source says which corner covered which
receiver (that's paid tracking; play-by-play names a defender only on a
breakup or a pick, ~13% of targets). So this publishes two lists side by side
and never pairs them. The site connects them only with TEAM numbers (what the
defence allows to WR1s), labelled as such.

  targets  per offense, its top 3 by targets: player_id, name, position,
           games, tgt, share of the team's targets, aDOT (mean air yards on
           his targets), rec, yds, td. From play-by-play for `season`.
  corners  per defence, the depth chart's rank-1 LCB, RCB and NB (nickel):
           gsis/espn ids, name, and this season's games, passes defended and
           interceptions from weekly player stats (0 games = none yet).
"""
from __future__ import annotations

import nflreadpy as nfl
import polars as pl

TOP_N = 3
CORNER_SLOTS = ("LCB", "RCB", "NB")


def top_targets(season: int) -> dict:
    p = nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")
    t = p.filter((pl.col("pass_attempt") == 1) & pl.col("receiver_player_id").is_not_null()
                 & pl.col("posteam").is_not_null())
    if t.is_empty():
        return {}
    team_tgt = {r["posteam"]: int(r["n"]) for r in t.group_by("posteam").agg(pl.len().alias("n")).iter_rows(named=True)}
    g = t.group_by(["posteam", "receiver_player_id"]).agg(
        pl.col("receiver_player_name").drop_nulls().last().alias("name"),
        pl.len().alias("tgt"),
        pl.col("game_id").n_unique().alias("games"),
        pl.col("air_yards").drop_nulls().mean().alias("adot"),
        pl.col("complete_pass").fill_null(0).sum().alias("rec"),
        pl.col("yards_gained").fill_null(0).sum().alias("yds"),
        pl.col("pass_touchdown").fill_null(0).sum().alias("td"),
    )
    pos = {}
    try:
        ps = nfl.load_player_stats(seasons=[season], summary_level="week")
        pos = {r["player_id"]: r["position"] for r in ps.select(["player_id", "position"]).unique("player_id").iter_rows(named=True)}
    except Exception:  # noqa: BLE001 -- a missing position is a blank, not a failure
        pass
    out: dict = {}
    for team, rows in g.sort("tgt", descending=True).group_by("posteam", maintain_order=True):
        team = team[0] if isinstance(team, tuple) else team
        out[team] = [{
            "player_id": r["receiver_player_id"], "name": r["name"], "position": pos.get(r["receiver_player_id"]),
            "games": int(r["games"]), "tgt": int(r["tgt"]),
            "share": round(100.0 * r["tgt"] / team_tgt[team], 1) if team_tgt.get(team) else None,
            "adot": round(float(r["adot"]), 1) if r["adot"] is not None else None,
            "rec": int(r["rec"]), "yds": int(r["yds"]), "td": int(r["td"]),
        } for r in rows.head(TOP_N).iter_rows(named=True)]
    return out


def corners(season: int) -> dict:
    dc = nfl.load_depth_charts(seasons=[season])
    if dc.is_empty():
        return {}
    latest = dc.group_by("team").agg(pl.col("dt").max().alias("dt"))
    dc = dc.join(latest, on=["team", "dt"]).filter(
        pl.col("pos_abb").is_in(list(CORNER_SLOTS)) & (pl.col("pos_rank") == 1))
    stats: dict = {}
    try:
        ps = nfl.load_player_stats(seasons=[season], summary_level="week").filter(pl.col("season_type") == "REG")
        agg = ps.group_by("player_id").agg(
            pl.len().alias("games"),
            pl.col("def_pass_defended").fill_null(0).sum().alias("pd"),
            pl.col("def_interceptions").fill_null(0).sum().alias("int"))
        stats = {r["player_id"]: r for r in agg.iter_rows(named=True)}
    except Exception:  # noqa: BLE001 -- no stats yet is "0 games", said on the page
        pass
    out: dict = {}
    for r in dc.sort("pos_slot").iter_rows(named=True):
        s = stats.get(r["gsis_id"], {})
        out.setdefault(r["team"], []).append({
            "slot": r["pos_abb"], "gsis_id": r["gsis_id"], "espn_id": r["espn_id"], "name": r["player_name"],
            "games": int(s.get("games") or 0), "pd": int(s.get("pd") or 0), "int": int(s.get("int") or 0),
        })
    return out


def pass_game(stat_season: int, season: int) -> dict:
    """Targets on the stats clock (like DvP), corners on this season's roster."""
    tg = top_targets(stat_season)
    cb = corners(season)
    if not tg and not cb:
        return {}
    return {"season": stat_season, "corner_season": season, "targets": tg, "corners": cb}
