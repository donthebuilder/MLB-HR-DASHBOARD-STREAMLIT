#!/usr/bin/env python3
"""nfl_offense_value.py — the offensive flipside of nfl_disruption.py:
red-zone touch conversion and per-target route value.

Upgrade prompt, Phase 2's stat-layer item, offensive half: "explosive-play
rate, opportunity share, yards-per-route-type value, ceiling potential."
Explosive-play rate and opportunity share are already shipped —
nfl_explosive.py's player_explosive()/team_usage() cover WOPR and big-play
rate today, confirmed by reading that file before starting this one, not
assumed. This module is the two pieces that weren't.

Both verified live against 2025 nflreadpy data before being written:

RED-ZONE TOUCH CONVERSION — reuses nfl_dvp.py's own `yardline_100 <= 20`
play-by-play filter (already established there for the DEFENSE side, same
threshold, not a second one invented here), grouped by the offensive
ball-carrier/target instead of the defense. "TD per RZ touch" isolates
finishing from volume: a back who gets 10 goal-line carries and scores 5
is a different problem than one who gets 30 and scores 5.

ROUTE-TYPE VALUE — an honest limitation found while verifying this, not
assumed from nfl_disruption.py's own docstring (which undersold the gap
when it called this "the offensive flipside's harder half... the data
exists"). load_participation()'s `route` column is real and ~100% filled,
confirmed by direct inspection — but participation is ONE ROW PER PLAY
(confirmed: zero plays have more than one participation row), and
nfl_coverage.py's own join ties that single `route` value to
`receiver_player_id`, i.e. the play's TARGET. There is no per-player,
per-route field for a receiver who ran a route but wasn't thrown to. True
"yards per route RUN" — the PFF-style stat crediting every route
regardless of target — cannot be built from this table. What CAN be built,
and is what this module builds, is value PER TARGET, broken out by which
route type produced that target: a real, useful, but narrower question
("when he's thrown the ball on a corner route, how much does he do with
it") than the one the spec's wording implied ("how much does he do every
time he runs a corner route, targeted or not"). Filed here rather than
silently shipping the narrower stat under the bigger stat's name.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

MIN_RZ_TOUCHES = 5
MIN_ROUTE_TARGETS = 5  # per specific route type, before that route cell is
                        # shown for a player -- same "don't offer a number
                        # the sample can't support" rule as everywhere else
                        # in this bot (player_grades()' MIN_GAMES, etc.)

SKILL_POS = ["RB", "FB", "WR", "TE"]


@functools.lru_cache(maxsize=4)
def _pbp(season: int) -> pl.DataFrame:
    return (nfl.load_pbp(seasons=[season])
              .filter(pl.col("season_type") == "REG"))


@functools.lru_cache(maxsize=2)
def _players() -> pl.DataFrame:
    return nfl.load_players().select(["gsis_id", "display_name", "position"])


def red_zone_conversion(season: int) -> dict:
    """{player_id: {name, position, touches, tds, rate, percentile}} for
    RB/FB/WR/TE with >= MIN_RZ_TOUCHES red-zone touches (carries + targets
    inside the 20, combined). Percentile computed within position — the mix
    of touches that get inside the 20 differs structurally by role, so a
    back isn't ranked against a slot receiver on this.
    """
    p = _pbp(season).filter(pl.col("yardline_100") <= 20)

    car = (p.filter(pl.col("rush_attempt") == 1, pl.col("rusher_player_id").is_not_null())
             .group_by("rusher_player_id")
             .agg(pl.len().alias("touches"),
                  pl.col("rush_touchdown").fill_null(0).sum().alias("tds"))
             .rename({"rusher_player_id": "player_id"}))
    tgt = (p.filter(pl.col("pass_attempt") == 1, pl.col("receiver_player_id").is_not_null())
             .group_by("receiver_player_id")
             .agg(pl.len().alias("touches"),
                  pl.col("pass_touchdown").fill_null(0).sum().alias("tds"))
             .rename({"receiver_player_id": "player_id"}))

    both = (pl.concat([car, tgt]).group_by("player_id")
              .agg(pl.col("touches").sum(), pl.col("tds").sum())
              .filter(pl.col("touches") >= MIN_RZ_TOUCHES))
    if both.height == 0:
        return {}

    both = both.join(_players(), left_on="player_id", right_on="gsis_id", how="inner")
    both = both.filter(pl.col("position").is_in(SKILL_POS))
    if both.height == 0:
        return {}

    both = both.with_columns((pl.col("tds") / pl.col("touches") * 100).alias("rate"))
    n_by_pos = both.group_by("position").agg(pl.len().alias("n_pos"))
    both = both.join(n_by_pos, on="position")
    both = both.with_columns(
        pl.col("rate").rank("average", descending=False).over("position").alias("_rk"))
    both = both.with_columns(
        pl.when(pl.col("n_pos") > 1)
          .then((pl.col("_rk") - 1) / (pl.col("n_pos") - 1) * 100)
          .otherwise(50.0)
          .alias("percentile"))

    out: dict = {}
    for r in both.iter_rows(named=True):
        out[r["player_id"]] = {
            "name": r["display_name"], "position": r["position"],
            "touches": int(r["touches"]), "tds": int(r["tds"]),
            "rate": round(float(r["rate"]), 1),
            "percentile": round(float(r["percentile"]), 1),
        }
    return out


@functools.lru_cache(maxsize=4)
def _targets_by_route(season: int) -> pl.DataFrame:
    """The same participation-joined-to-pbp pattern nfl_coverage.py
    established, re-run here rather than imported from it — that module's
    own cached join selects a different pbp column set (defteam/coverage-
    shell columns this module doesn't need) and decorates it with its own
    lru_cache; reaching into nfl_coverage's private cache would either miss
    columns this module needs or double the cache entries for the same
    join. Same season/pass_attempt filters, independent cache.
    """
    part = nfl.load_participation(seasons=[season])
    pbp = nfl.load_pbp(seasons=[season]).select(
        ["game_id", "play_id", "season_type", "receiver_player_id",
         "pass_attempt", "complete_pass", "yards_gained", "pass_touchdown"])
    j = part.join(pbp, left_on=["nflverse_game_id", "play_id"],
                  right_on=["game_id", "play_id"], how="inner")
    return j.filter(pl.col("season_type") == "REG", pl.col("pass_attempt") == 1,
                    pl.col("receiver_player_id").is_not_null(), pl.col("route") != "")


def route_value(season: int) -> dict:
    """{player_id: {name, position, routes: {route_type: {targets, catches,
    yards, tds, yds_per_tgt}}, best_route, best_yds_per_tgt}}

    Per-target value by route type — see the module docstring for exactly
    why this is "per target" and not "per route run." Only route types with
    >= MIN_ROUTE_TARGETS targets are included for a given player.
    """
    j = _targets_by_route(season)
    agg = (j.group_by(["receiver_player_id", "route"]).agg(
        pl.len().alias("targets"),
        pl.col("complete_pass").fill_null(0).sum().alias("catches"),
        pl.col("yards_gained").fill_null(0).sum().alias("yards"),
        pl.col("pass_touchdown").fill_null(0).sum().alias("tds"),
    ).filter(pl.col("targets") >= MIN_ROUTE_TARGETS)
     .with_columns((pl.col("yards") / pl.col("targets")).alias("yds_per_tgt")))
    if agg.height == 0:
        return {}

    agg = agg.join(_players(), left_on="receiver_player_id", right_on="gsis_id", how="inner")
    agg = agg.filter(pl.col("position").is_in(SKILL_POS))
    if agg.height == 0:
        return {}

    out: dict = {}
    for r in agg.iter_rows(named=True):
        pid = r["receiver_player_id"]
        entry = out.setdefault(pid, {"name": r["display_name"], "position": r["position"], "routes": {}})
        entry["routes"][r["route"]] = {
            "targets": int(r["targets"]), "catches": int(r["catches"]),
            "yards": int(r["yards"]), "tds": int(r["tds"]),
            "yds_per_tgt": round(float(r["yds_per_tgt"]), 1),
        }
    for entry in out.values():
        best = max(entry["routes"].items(), key=lambda kv: kv[1]["yds_per_tgt"])
        entry["best_route"] = best[0]
        entry["best_yds_per_tgt"] = best[1]["yds_per_tgt"]
    return out


if __name__ == "__main__":
    rz = red_zone_conversion(2025)
    print("RZ-qualified skill players:", len(rz))
    for pid, row in sorted(rz.items(), key=lambda kv: -kv[1]["rate"])[:5]:
        print(f"  {row['name']:<22} {row['position']:<3} touches={row['touches']:>3} "
              f"tds={row['tds']:>2} rate={row['rate']:>5.1f}% pctl={row['percentile']:>5.1f}")

    rv = route_value(2025)
    print("\nroute-value-qualified players:", len(rv))
    for pid, row in sorted(rv.items(), key=lambda kv: -kv[1]["best_yds_per_tgt"])[:5]:
        print(f"  {row['name']:<22} {row['position']:<3} best={row['best_route']:<20} "
              f"yds/tgt={row['best_yds_per_tgt']:>5.1f}")
