#!/usr/bin/env python3
"""nfl_disruption.py — the defense/action stat layer, graded not raw.

Upgrade prompt, Phase 2: "Add: tackles, TFL, pass breakups/deflections,
pressures, forced fumbles, QBR under pressure, formation percentage
(shotgun/pistol/under-center rate)." Confirmed scope note on the same item:
"this isn't defense-only... build the same graded/percentile stat-layer
approach... as the flipside." This module is the defense half plus the
two team-level context stats (formation mix, pressure rate) that came along
for free once the same participation data was loaded for one of them.

Everything below is real nflreadpy data, verified live against the 2025
season before this file was written, not guessed from documentation:
  - `load_player_stats()` already carries per-player-week `def_*` columns
    (tackles_solo, tackles_for_loss, sacks, qb_hits, pass_defended,
    fumbles_forced, ...) computed from play-by-play — the same loader
    nfl_dvp.py/nfl_gamelog.py/nfl_splits.py already trust for everything else
    in this bot, so this is not a new dependency or a new fetch pattern.
  - `load_participation()` carries `offense_formation` (SHOTGUN / PISTOL /
    UNDER CENTER / null) and `was_pressure` (bool) — the exact three
    formation buckets asked for, and a real down-to-down pressure flag, not
    a QB-hit proxy. nfl_coverage.py already established the join needed to
    attach team context (posteam/defteam/pass_attempt aren't on the
    participation row itself, only on pbp) — this module reuses that same
    join, not a second implementation of it.

Deliberately NOT built here, and not faked:
  - "QBR under pressure" — no such rating exists anywhere in nflverse. PFR's
    advanced-passing loader gives raw pressure COUNTS (times_pressured,
    pressure_pct) but nothing conditions a QB's efficiency/rating on being
    pressured specifically; building that would mean a new EPA-under-
    pressure model off play-by-play, a separate and much bigger lift.
  - "yards-per-route-type value" (the offensive flipside's harder half) —
    `load_participation()`'s `route` column is real and ~100% filled per
    nfl_coverage.py's own verified fill rates, so the data exists, but
    building a route-value grade is its own scoped piece, not a two-line
    addition to this one.

Percentile grading, matching Competitive Reference #2's "single 0-100 grade,
mean of graded percentiles": percentile is computed WITHIN position group
(DL/LB/DB), same reasoning nfl_dvp.py's roles use on the allowed side — a
nose tackle's tackle count means something next to other DL, not next to
corners. Only genuine defensive positions qualify; nflverse's aggregation
does not filter out a WR crediting a tackle on a pick-six return, and
grading him against DBs on it would be a category error.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

POS_GROUP = {
    "DE": "DL", "DT": "DL", "DL": "DL", "NT": "DL",
    "OLB": "LB", "ILB": "LB", "MLB": "LB", "LB": "LB",
    "CB": "DB", "DB": "DB", "FS": "DB", "SS": "DB", "S": "DB", "SAF": "DB",
}

STATS = ["tackles", "tackles_for_loss", "sacks", "qb_hits", "pass_defended", "fumbles_forced"]

# Which of those actually mean something for a group's OVERALL grade — same
# idea as nfl_dvp.py's STATS_FOR_ROLE. A corner's sack percentile is real and
# still shown, but it shouldn't drag his grade the way a pass-rusher's does.
GROUP_STATS = {
    "DL": ["tackles", "tackles_for_loss", "sacks", "qb_hits", "fumbles_forced"],
    "LB": ["tackles", "tackles_for_loss", "sacks", "qb_hits", "pass_defended", "fumbles_forced"],
    "DB": ["tackles", "pass_defended", "fumbles_forced"],
}

MIN_GAMES = 4  # a bar clear at Leaders.js's own MIN_QUALIFIED, defense side

GROUP_ORDER = ["DL", "LB", "DB"]

STAT_LABELS = {
    "tackles": "TKL", "tackles_for_loss": "TFL", "sacks": "SACK",
    "qb_hits": "QB HIT", "pass_defended": "PBU", "fumbles_forced": "FF",
}


@functools.lru_cache(maxsize=4)
def _weekly(season: int) -> pl.DataFrame:
    return (nfl.load_player_stats(seasons=[season], summary_level="week")
              .filter(pl.col("season_type") == "REG"))


def player_grades(season: int) -> dict:
    """{player_id: {name, team, position, pos_group, games, stats: {...},
    percentiles: {...}, grade}} for every qualifying defender.
    """
    wk = _weekly(season).filter(pl.col("position").is_in(list(POS_GROUP)))
    if wk.height == 0:
        return {}

    agg = wk.group_by(["player_id", "player_display_name", "team", "position"]).agg(
        (pl.col("def_tackles_solo").fill_null(0) + pl.col("def_tackles_with_assist").fill_null(0)).sum().alias("tackles"),
        pl.col("def_tackles_for_loss").fill_null(0).sum().alias("tackles_for_loss"),
        pl.col("def_sacks").fill_null(0).sum().alias("sacks"),
        pl.col("def_qb_hits").fill_null(0).sum().alias("qb_hits"),
        pl.col("def_pass_defended").fill_null(0).sum().alias("pass_defended"),
        pl.col("def_fumbles_forced").fill_null(0).sum().alias("fumbles_forced"),
        pl.len().alias("games"),
    ).with_columns(pl.col("position").replace(POS_GROUP).alias("pos_group"))

    agg = agg.filter(pl.col("games") >= MIN_GAMES)
    if agg.height == 0:
        return {}

    # Percentile within position group: rank ascending (1 = lowest), then
    # (rank-1)/(n-1)*100 so the group's best in a stat scores ~100. A group
    # of one (shouldn't happen at MIN_GAMES scale; guarded anyway) scores 50
    # rather than dividing by zero.
    n_by_group = agg.group_by("pos_group").agg(pl.len().alias("n_group"))
    agg = agg.join(n_by_group, on="pos_group")
    for stat in STATS:
        agg = agg.with_columns(
            pl.col(stat).rank("average", descending=False).over("pos_group").alias(f"_{stat}_rk"))
        agg = agg.with_columns(
            pl.when(pl.col("n_group") > 1)
              .then((pl.col(f"_{stat}_rk") - 1) / (pl.col("n_group") - 1) * 100)
              .otherwise(50.0)
              .alias(f"{stat}_pctl"))

    out: dict = {}
    for r in agg.iter_rows(named=True):
        group = r["pos_group"]
        keep = GROUP_STATS.get(group, STATS)
        pctls = [r[f"{s}_pctl"] for s in keep if r.get(f"{s}_pctl") is not None]
        out[r["player_id"]] = {
            "name": r["player_display_name"], "team": r["team"],
            "position": r["position"], "pos_group": group, "games": int(r["games"]),
            "stats": {s: round(float(r[s]), 1) for s in STATS},
            "percentiles": {s: round(float(r[f"{s}_pctl"]), 1) for s in STATS},
            "grade": round(sum(pctls) / len(pctls), 1) if pctls else None,
        }
    return out


@functools.lru_cache(maxsize=4)
def _participation_pbp(season: int) -> pl.DataFrame:
    """Participation joined to the pbp columns nfl_coverage.py already joins
    for the same reason — posteam/defteam/pass_attempt aren't on the
    participation row itself. Same join, same season filter; a separate
    lru_cache entry (different column selection) so this module doesn't
    reach into nfl_coverage's private cache.
    """
    part = nfl.load_participation(seasons=[season])
    pbp = nfl.load_pbp(seasons=[season]).select(
        ["game_id", "play_id", "season_type", "posteam", "defteam", "pass_attempt"])
    j = part.join(pbp, left_on=["nflverse_game_id", "play_id"],
                  right_on=["game_id", "play_id"], how="inner")
    return j.filter(pl.col("season_type") == "REG")


_FORMATION_KEY = {"SHOTGUN": "shotgun_pct", "PISTOL": "pistol_pct", "UNDER CENTER": "under_center_pct"}


def team_context(season: int) -> dict:
    """{team: {formation: {shotgun_pct, pistol_pct, under_center_pct, snaps},
    pressure: {created_pct, created_plays, allowed_pct, allowed_plays}}}

    Formation mix is the offense's own tendency across every snap NGS
    charting resolved a formation for — real fill rate ~80%, shown as
    `snaps` rather than assumed 100%, the same "show the denominator" rule
    nfl_coverage.py already follows for its coverage-shell rates. Pressure
    is split by role on the SAME `was_pressure` flag: `created_*` is this
    team's own defense generating it against opposing passers, `allowed_*`
    is this team's offense giving it up — two different questions off one
    column, never conflated into a single number.
    """
    j = _participation_pbp(season)
    out: dict = {}

    form = j.filter(pl.col("offense_formation").is_not_null())
    form_n = form.group_by("posteam").agg(pl.len().alias("snaps")).rename({"posteam": "team"})
    for r in form_n.iter_rows(named=True):
        out.setdefault(r["team"], {})["formation"] = {"snaps": int(r["snaps"])}

    form_mix = (form.group_by(["posteam", "offense_formation"]).agg(pl.len().alias("n"))
                    .rename({"posteam": "team"}))
    for r in form_mix.iter_rows(named=True):
        t = out.setdefault(r["team"], {})
        f = t.setdefault("formation", {"snaps": 0})
        snaps = max(1, f.get("snaps", 0))
        key = _FORMATION_KEY.get(r["offense_formation"])
        if key:
            f[key] = round(100 * r["n"] / snaps, 1)

    pass_p = j.filter(pl.col("pass_attempt") == 1)
    created = (pass_p.group_by("defteam").agg(
        pl.len().alias("plays"), pl.col("was_pressure").fill_null(False).sum().alias("pressures"))
        .rename({"defteam": "team"}))
    allowed = (pass_p.group_by("posteam").agg(
        pl.len().alias("plays"), pl.col("was_pressure").fill_null(False).sum().alias("pressures"))
        .rename({"posteam": "team"}))

    for r in created.iter_rows(named=True):
        t = out.setdefault(r["team"], {})
        plays = max(1, int(r["plays"]))
        t["pressure"] = {**t.get("pressure", {}),
                          "created_pct": round(100 * float(r["pressures"]) / plays, 1),
                          "created_plays": int(r["plays"])}
    for r in allowed.iter_rows(named=True):
        t = out.setdefault(r["team"], {})
        plays = max(1, int(r["plays"]))
        t["pressure"] = {**t.get("pressure", {}),
                          "allowed_pct": round(100 * float(r["pressures"]) / plays, 1),
                          "allowed_plays": int(r["plays"])}
    return out


MIN_PRESSURES = 20  # checked the real distribution before picking this: PFR's
                     # own pressure column across every defender in the league
                     # (most of whom barely rush the passer at all) has a
                     # median of 2 and a 90th percentile of 14 for a full
                     # season -- 10 still lets in players a single cheap sack
                     # would swing 10+ points of rate. 20 sits just past that
                     # 90th percentile: real pass-rush volume, not a token
                     # snap count.


@functools.lru_cache(maxsize=4)
def _pfr_def(season: int) -> pl.DataFrame:
    """Player-level pass-rush counts PFR charts by hand -- not derivable
    from play-by-play alone, since "a pressure" isn't a play-by-play event
    type the way a sack or a completion is. Confirmed live: def_pressures
    and def_sacks are BOTH real, per-player-per-week columns here -- the
    only nflverse loader with pressure counts at the player level.
    load_participation()'s was_pressure (used by team_context() above) is a
    play-level flag with no player attached, which is why that function can
    only report team rates, never "who generated it." PFR's own column is
    game_type, not season_type like every other loader in this file --
    confirmed by inspection, not assumed consistent.
    """
    return (nfl.load_pfr_advstats(seasons=[season], stat_type="def", summary_level="week")
              .filter(pl.col("game_type") == "REG"))


@functools.lru_cache(maxsize=4)
def _pfr_to_gsis(season: int) -> dict:
    """pfr_player_id -> gsis player_id, the id player_grades() already keys
    by. load_players() is nflverse's full roster history, not scoped to one
    season -- fine here, a player's id mapping doesn't change year to year.
    """
    p = nfl.load_players().filter(pl.col("pfr_id").is_not_null())
    return dict(zip(p["pfr_id"].to_list(), p["gsis_id"].to_list()))


def pass_rush_efficiency(season: int) -> dict:
    """{player_id: {name, team, position, pos_group, pressures, sacks,
    sack_rate, percentile}} for DL/LB who cleared MIN_PRESSURES.

    The question player_grades()'s raw sack count can't answer on its own:
    not "how many sacks" (volume) but "of the pressures he actually
    generated, how many did he finish" -- a rusher who beats his blocker 30
    times and finishes 3 sacks is a different problem than one who beats
    his blocker 10 times and finishes 3. Percentile computed within DL vs
    LB separately, same reasoning as player_grades()' own position groups.

    Team-trade handling is a deliberate simplification, not an oversight:
    a mid-season trade sums pressures/sacks across both teams (the rate is
    still meaningful combined) and labels the player with whichever team
    shows up last in PFR's own week ordering -- the other team's snap isn't
    lost from the RATE, only from the team LABEL.
    """
    pfr = _pfr_def(season).sort("week")
    xwalk = _pfr_to_gsis(season)
    agg = pfr.group_by("pfr_player_id").agg(
        pl.col("def_pressures").fill_null(0).sum().alias("pressures"),
        pl.col("def_sacks").fill_null(0).sum().alias("sacks"),
        pl.col("team").last().alias("team"),
    ).filter(pl.col("pressures") >= MIN_PRESSURES)
    if agg.height == 0:
        return {}

    agg = agg.with_columns(
        pl.col("pfr_player_id").replace_strict(xwalk, default=None, return_dtype=pl.Utf8).alias("player_id"))
    agg = agg.filter(pl.col("player_id").is_not_null())
    if agg.height == 0:
        return {}

    plist = nfl.load_players().select(["gsis_id", "display_name", "position"])
    agg = agg.join(plist, left_on="player_id", right_on="gsis_id", how="inner")
    agg = agg.filter(pl.col("position").is_in(list(POS_GROUP)))
    if agg.height == 0:
        return {}
    agg = agg.with_columns(
        pl.col("position").replace(POS_GROUP).alias("pos_group"),
        (pl.col("sacks") / pl.col("pressures") * 100).alias("sack_rate"))

    n_by_group = agg.group_by("pos_group").agg(pl.len().alias("n_group"))
    agg = agg.join(n_by_group, on="pos_group")
    agg = agg.with_columns(
        pl.col("sack_rate").rank("average", descending=False).over("pos_group").alias("_rk"))
    agg = agg.with_columns(
        pl.when(pl.col("n_group") > 1)
          .then((pl.col("_rk") - 1) / (pl.col("n_group") - 1) * 100)
          .otherwise(50.0)
          .alias("percentile"))

    out: dict = {}
    for r in agg.iter_rows(named=True):
        out[r["player_id"]] = {
            "name": r["display_name"], "team": r["team"],
            "position": r["position"], "pos_group": r["pos_group"],
            "pressures": int(r["pressures"]), "sacks": round(float(r["sacks"]), 1),
            "sack_rate": round(float(r["sack_rate"]), 1),
            "percentile": round(float(r["percentile"]), 1),
        }
    return out


if __name__ == "__main__":
    g = player_grades(2025)
    print("defenders graded:", len(g))
    top = sorted(g.items(), key=lambda kv: -(kv[1]["grade"] or 0))[:5]
    for pid, row in top:
        print(f"  {row['name']:<22} {row['position']:<4} {row['pos_group']:<3} "
              f"grade={row['grade']:>5.1f} stats={row['stats']}")

    tc = team_context(2025)
    print("\nteams with context:", len(tc))
    for team in ("PIT", "LA"):
        if team in tc:
            print(f"  {team}: {tc[team]}")

    pr = pass_rush_efficiency(2025)
    print("\npass rushers graded:", len(pr))
    top_pr = sorted(pr.items(), key=lambda kv: -kv[1]["sack_rate"])[:5]
    for pid, row in top_pr:
        print(f"  {row['name']:<22} {row['position']:<4} {row['pos_group']:<3} "
              f"pressures={row['pressures']:>3} sacks={row['sacks']:>4.1f} "
              f"rate={row['sack_rate']:>5.1f}% pctl={row['percentile']:>5.1f}")
