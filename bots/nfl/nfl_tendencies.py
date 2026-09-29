#!/usr/bin/env python3
"""nfl_tendencies.py -- how each team lines up THIS season, from FTN charting.

WHY (2026-09-28). The formation mix and pressure numbers on TUDDY's Matchups
come from nflverse participation (nfl_disruption.team_context), which
publishes once a year -- so all season they are last season's. FTN charting
is the in-season source: nflverse publishes it weekly (ftn_charting_2026 went
up 2026-09-28 with weeks 1-3). It has no coverage shell, man/zone or routes,
but it does have what a reader means by "tendencies":

  offense   qb_location (S shotgun / U under center / P pistol), is_motion,
            is_play_action, is_no_huddle, is_rpo, is_screen_pass
  defense   n_defense_box, n_blitzers, n_pass_rushers

WHAT COUNTS AS A PLAY. FTN rows cover every snap-like event, and ~24% carry
"0" in qb_location / n_defense_box -- kicks, kneels, penalties, uncharted.
Joined to play-by-play on (game_id, play_id) and kept to real pass and run
plays only; a "0" box or location that survives is uncharted, never a real
value, and is left out of that one rate's denominator. Every rate publishes
its denominator next to it.

OUTPUT (published as `tendencies` in nfl_matchup.json, ~10 KB):
  {season, weeks: [...], min_plays,
   offense: {team: {plays, shotgun_pct, under_center_pct, pistol_pct,
                    motion_pct, play_action_pct, no_huddle_pct, rpo_pct,
                    screen_pct, dropbacks, heavy_box_faced_pct, runs}},
   defense: {team: {plays, box_avg, box8_pct, runs, blitz_pct,
                    rushers_avg, dropbacks}},
   league:  {offense: {...same keys, league-wide}, defense: {...}},
   rank:    {offense: {team: {key: n}}, defense: {team: {key: n}}}}
Rank 1 = the most of that thing in the league. A team under `min_plays` is
left out rather than ranked on a handful of snaps.
"""
from __future__ import annotations

import functools

import nflreadpy as nfl
import polars as pl

MIN_PLAYS = 50   # per team per side; three weeks is ~180, so this only drops a
                 # team that has barely played (a bye in week 1 of 2)

OFF_KEYS = ("shotgun_pct", "under_center_pct", "pistol_pct", "motion_pct",
            "play_action_pct", "no_huddle_pct", "rpo_pct", "screen_pct",
            "heavy_box_faced_pct")
DEF_KEYS = ("box_avg", "box8_pct", "blitz_pct", "rushers_avg")


@functools.lru_cache(maxsize=4)
def _plays(season: int) -> pl.DataFrame:
    """FTN rows joined to pbp, real pass/run plays only, typed."""
    ftn = nfl.load_ftn_charting(seasons=[season])
    if ftn.is_empty():
        return pl.DataFrame()
    pbp = (nfl.load_pbp(seasons=[season])
              .filter(pl.col("season_type") == "REG")
              .filter(pl.col("play_type").is_in(["pass", "run"]))
              .select(["game_id", "play_id", "posteam", "defteam", "play_type", "week"]))
    f = ftn.select([
        pl.col("nflverse_game_id").cast(pl.Utf8).alias("game_id"),
        pl.col("nflverse_play_id").cast(pl.Float64).alias("play_id"),
        pl.col("qb_location").cast(pl.Utf8),
        pl.col("n_defense_box").cast(pl.Int64, strict=False).alias("box"),
        pl.col("n_blitzers").cast(pl.Int64, strict=False).alias("blitzers"),
        pl.col("n_pass_rushers").cast(pl.Int64, strict=False).alias("rushers"),
        *[pl.col(c).cast(pl.Utf8).str.to_uppercase().eq("TRUE").alias(c)
          for c in ("is_motion", "is_play_action", "is_no_huddle", "is_rpo", "is_screen_pass")],
    ])
    pbp = pbp.with_columns(pl.col("play_id").cast(pl.Float64))
    return f.join(pbp, on=["game_id", "play_id"], how="inner")


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _offense(g: pl.DataFrame) -> dict:
    loc = g.filter(pl.col("qb_location").is_in(["S", "U", "P"]))
    drop = g.filter(pl.col("play_type") == "pass")
    runs = g.filter(pl.col("play_type") == "run")
    runs_box = runs.filter(pl.col("box") > 0)
    return {
        "plays": g.height,
        "games": g["game_id"].n_unique(),
        "shotgun_pct": _pct(loc.filter(pl.col("qb_location") == "S").height, loc.height),
        "under_center_pct": _pct(loc.filter(pl.col("qb_location") == "U").height, loc.height),
        "pistol_pct": _pct(loc.filter(pl.col("qb_location") == "P").height, loc.height),
        "motion_pct": _pct(int(g["is_motion"].sum()), g.height),
        "no_huddle_pct": _pct(int(g["is_no_huddle"].sum()), g.height),
        "rpo_pct": _pct(int(g["is_rpo"].sum()), g.height),
        "dropbacks": drop.height,
        "play_action_pct": _pct(int(drop["is_play_action"].sum()), drop.height),
        "screen_pct": _pct(int(drop["is_screen_pass"].sum()), drop.height),
        "runs": runs_box.height,
        "heavy_box_faced_pct": _pct(runs_box.filter(pl.col("box") >= 8).height, runs_box.height),
    }


def _defense(g: pl.DataFrame) -> dict:
    boxed = g.filter(pl.col("box") > 0)
    runs = boxed.filter(pl.col("play_type") == "run")
    drop = g.filter((pl.col("play_type") == "pass") & (pl.col("rushers") > 0))
    return {
        "plays": g.height,
        "games": g["game_id"].n_unique(),
        "box_avg": round(float(boxed["box"].mean()), 2) if boxed.height else None,
        "runs": runs.height,
        "box8_pct": _pct(runs.filter(pl.col("box") >= 8).height, runs.height),
        "dropbacks": drop.height,
        "blitz_pct": _pct(drop.filter(pl.col("blitzers") >= 1).height, drop.height),
        "rushers_avg": round(float(drop["rushers"].mean()), 2) if drop.height else None,
    }


def _ranks(side: dict, keys: tuple) -> dict:
    out: dict = {t: {} for t in side}
    for k in keys:
        vals = sorted(((v[k], t) for t, v in side.items() if v.get(k) is not None), reverse=True)
        for i, (_, t) in enumerate(vals):
            out[t][k] = i + 1
    return out


def team_tendencies(season: int) -> dict:
    """The block above for `season`, or {} when FTN has nothing for it yet."""
    p = _plays(season)
    if p.is_empty():
        return {}
    offense = {t: _offense(g) for (t,), g in p.group_by(["posteam"]) if t}
    defense = {t: _defense(g) for (t,), g in p.group_by(["defteam"]) if t}
    offense = {t: v for t, v in offense.items() if v["plays"] >= MIN_PLAYS}
    defense = {t: v for t, v in defense.items() if v["plays"] >= MIN_PLAYS}
    if not offense and not defense:
        return {}
    return {
        "season": season,
        "weeks": sorted(int(w) for w in p["week"].unique().to_list() if w is not None),
        "min_plays": MIN_PLAYS,
        "offense": offense,
        "defense": defense,
        "league": {"offense": _offense(p), "defense": _defense(p)},
        "rank": {"offense": _ranks(offense, OFF_KEYS), "defense": _ranks(defense, DEF_KEYS)},
    }
