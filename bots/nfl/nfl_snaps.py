#!/usr/bin/env python3
"""nfl_snaps.py — snap share, and the direction it is moving.

WHY THIS EXISTS. `load_snap_counts()` had zero call sites anywhere in
bots/nfl before this file (checked 2026-09-13). It is the only source in
nflverse for how much a player is actually on the field, which is the
denominator under every opportunity stat the bot already computes: a target
share, a carry count and a route total all mean something different at 38%
of snaps than at 92%.

Unlike participation (see nfl_charting.py), snap counts ARE an in-season
dataset -- nflverse refreshes them four times a day, and a Sunday game is
usually there Monday morning ET. So this runs on stat_season like every
other current table, with no clamp.

TWO THINGS, NOT ONE:

  LEVEL    what share of his team's snaps he plays. The denominator.
  TREND    last TREND_W games against his season rate. This is the half
           that is actually actionable -- a receiver who was at 45% and is
           now at 80% has moved up the depth chart, and that shows up here
           a week before it shows up in his counting stats.

The trend half is also the honest answer to the Streaks cold-board problem:
a low-volume name needs a stated reason to be shown, and "his snap share
jumped 30 points in three weeks" is a reason, where "he is due" is not.

KEYS. Snap counts are keyed by `pfr_player_id`; everything else in this bot
is keyed by gsis id. load_players() carries both, and the join covers 99.8%
of snap rows for 2025 (26,557 of 26,612). The misses are fringe linemen and
practice-squad call-ups with no gsis record at all, and they are dropped
rather than carried on a second key -- a row nothing else in the payload can
join to is not usable by any tab.

PERCENTILE is computed within the exact position (WR against WR), not a
broad group. Snap share is positional by construction: a starting TE and a
starting WR are both "starters" at very different rates, and grading them
against each other would say something about roster shape, not about either
player. A position with fewer than MIN_GROUP qualifying players gets no
percentile rather than a made-up one off a sample of three.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

MIN_GAMES = 3    # below this a season rate is one bad script away from noise
TREND_W = 3      # the recent window
MIN_GROUP = 5    # smallest position cohort that gets a percentile

# What counts as "his" snaps. A defender's offense_pct is 0 and meaningless;
# grading him on it would put every defender at the same percentile.
OFFENSE = {"QB", "RB", "FB", "WR", "TE", "T", "G", "C", "OL", "OT", "OG"}
DEFENSE = {"DE", "DT", "DL", "NT", "OLB", "ILB", "MLB", "LB", "CB", "DB",
           "FS", "SS", "S", "SAF"}


@functools.lru_cache(maxsize=4)
def _snaps(season: int) -> pl.DataFrame:
    """Regular-season snap rows, keyed to gsis id."""
    sn = (nfl.load_snap_counts(seasons=[season])
            .filter(pl.col("game_type") == "REG"))
    ids = (nfl.load_players()
             .select(["gsis_id", "pfr_id"])
             .drop_nulls()
             .unique(subset=["pfr_id"]))
    return (sn.join(ids, left_on="pfr_player_id", right_on="pfr_id", how="inner")
              .with_columns(pl.col("week").cast(pl.Int64)))


def _side(position: str) -> str | None:
    if position in OFFENSE:
        return "offense"
    if position in DEFENSE:
        return "defense"
    return None


def weekly_pct(season: int) -> pl.DataFrame:
    """Per player-week offensive snap share, keyed to gsis — for the MODEL.

    `player_snaps()` above answers "where does he stand now", which is a
    display question, so it aggregates to one row per player. Scoring needs
    the opposite shape: one row per player-WEEK, so nfl_features can put it
    through the same trailing roll as everything else and never see week w
    while scoring week w.

    Offence only. A defender's offense_pct is 0 by construction and a column
    that is 0 for half the table would percentile-rank every defender
    identically.
    """
    df = _snaps(season)
    if df.height == 0:
        return pl.DataFrame({"player_id": [], "week": [], "snap_pct": []})
    return (df.filter(pl.col("position").is_in(OFFENSE))
              .filter(pl.col("offense_pct").is_not_null())
              .select(pl.col("gsis_id").alias("player_id"), "week",
                      pl.col("offense_pct").alias("snap_pct")))


def player_snaps(season: int) -> dict:
    """{gsis_id: {name, team, position, side, games, snap_pct, recent_pct,
    trend, snaps, st_pct, percentile}}

    `snap_pct` is his season rate on his own side of the ball, `recent_pct`
    the same over the last TREND_W games he appeared in, and `trend` the
    difference in percentage points -- positive means he is on the field
    more now than he has been.
    """
    df = _snaps(season)
    if df.height == 0:
        return {}

    df = df.with_columns(
        pl.col("position").map_elements(_side, return_dtype=pl.Utf8).alias("side"))
    df = df.filter(pl.col("side").is_not_null())
    df = df.with_columns(
        pl.when(pl.col("side") == "offense")
          .then(pl.col("offense_pct"))
          .otherwise(pl.col("defense_pct")).alias("pct"),
        pl.when(pl.col("side") == "offense")
          .then(pl.col("offense_snaps"))
          .otherwise(pl.col("defense_snaps")).alias("snaps"),
    ).filter(pl.col("pct").is_not_null())

    # nflverse gives these as fractions (0.0-1.0). Confirmed against the real
    # 2025 file before relying on it -- the max is 1.0, not 100.
    season_agg = df.group_by(["gsis_id", "player", "position", "side"]).agg(
        pl.len().alias("games"),
        pl.col("pct").mean().alias("snap_pct"),
        pl.col("snaps").sum().alias("snaps"),
        pl.col("st_pct").mean().alias("st_pct"),
        pl.col("team").last().alias("team"),
    ).filter(pl.col("games") >= MIN_GAMES)
    if season_agg.height == 0:
        return {}

    # Last TREND_W games he actually appeared in -- not the last TREND_W
    # weeks. A player who missed week 5 should be compared on the three games
    # he played, not credited with a zero he was never on the field for.
    recent = (df.sort(["gsis_id", "week"])
                .group_by("gsis_id")
                .agg(pl.col("pct").tail(TREND_W).mean().alias("recent_pct")))
    agg = season_agg.join(recent, on="gsis_id", how="left")

    n_by_pos = agg.group_by("position").agg(pl.len().alias("n_pos"))
    agg = agg.join(n_by_pos, on="position")
    agg = agg.with_columns(
        pl.col("snap_pct").rank("average").over("position").alias("_rk"))
    agg = agg.with_columns(
        pl.when(pl.col("n_pos") >= MIN_GROUP)
          .then((pl.col("_rk") - 1) / (pl.col("n_pos") - 1) * 100)
          .otherwise(None).alias("pctile"))

    out: dict = {}
    for r in agg.iter_rows(named=True):
        sp = float(r["snap_pct"] or 0) * 100
        rp = float(r["recent_pct"] if r["recent_pct"] is not None else r["snap_pct"] or 0) * 100
        out[r["gsis_id"]] = {
            "name": r["player"],
            "team": r["team"],
            "position": r["position"],
            "side": r["side"],
            "games": int(r["games"]),
            "snap_pct": round(sp, 1),
            "recent_pct": round(rp, 1),
            "trend": round(rp - sp, 1),
            "snaps": int(r["snaps"] or 0),
            "st_pct": round(float(r["st_pct"] or 0) * 100, 1),
            "percentile": None if r["pctile"] is None else round(float(r["pctile"]), 1),
        }
    return out


# The positions a prop is ever written on. movers() defaults to these: the
# unfiltered list is dominated by backup guards and third-string QBs, whose
# snap share swings hardest and matters least. player_snaps() keeps everyone
# -- a defender's snap share is real context on a matchup card.
SKILL = {"QB", "RB", "FB", "WR", "TE"}

# A move worth showing. MEASURED on the real 2025 file, not guessed -- the
# first draft of this said one sigma was about 8 points and that was wrong.
# Actual spread of `trend` across the 571 qualifying skill players: sd 12.1,
# 10th percentile -12.7, 90th +14.9. So a 12-point cut would call 26% of the
# league a role change, which is not a board, it is a roster. 20 points is
# past 1.5 sigma and lands 53 players (9%) across a full season -- and the
# names it lands are the right ones: the backup who took the job.
RISING = 20.0
FALLING = -20.0


def movers(season: int, limit: int = 40, skill_only: bool = True) -> dict:
    """{'rising': [...], 'falling': [...]} — the players whose role has
    actually changed, biggest move first. This is the field the cold board
    should be reading before it shows a low-volume name: "his snap share
    went from 39% to 73%" is a stated reason, "he is due" is not."""
    p = player_snaps(season)
    rows = [{"player_id": k, **v} for k, v in p.items()
            if not skill_only or v["position"] in SKILL]
    rising = sorted([r for r in rows if r["trend"] >= RISING],
                    key=lambda r: -r["trend"])[:limit]
    falling = sorted([r for r in rows if r["trend"] <= FALLING],
                     key=lambda r: r["trend"])[:limit]
    return {"rising": rising, "falling": falling}


if __name__ == "__main__":
    import sys
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    p = player_snaps(yr)
    print(f"{yr}: {len(p)} qualifying players")
    wr = sorted([v for v in p.values() if v["position"] == "WR"],
                key=lambda v: -v["snap_pct"])[:5]
    for v in wr:
        print(f"  {v['name']:22} {v['team']:4} {v['snap_pct']:5.1f}%  "
              f"recent {v['recent_pct']:5.1f}%  trend {v['trend']:+5.1f}  "
              f"pctile {v['percentile']}")
    m = movers(yr)
    print(f"\nrising {len(m['rising'])}, falling {len(m['falling'])}")
    for v in m["rising"][:5]:
        print(f"  UP   {v['name']:22} {v['position']:3} {v['team']:4} "
              f"{v['snap_pct']:5.1f}% -> {v['recent_pct']:5.1f}%  ({v['trend']:+.1f})")
    for v in m["falling"][:3]:
        print(f"  DOWN {v['name']:22} {v['position']:3} {v['team']:4} "
              f"{v['snap_pct']:5.1f}% -> {v['recent_pct']:5.1f}%  ({v['trend']:+.1f})")
