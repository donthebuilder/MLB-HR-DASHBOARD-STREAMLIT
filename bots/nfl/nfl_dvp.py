#!/usr/bin/env python3
"""nfl_dvp.py — Defense vs Position, BY DEPTH ROLE.

The distinction that makes this worth building: "what this defense allows to
wide receivers" is close to useless, because it averages a WR1 and a fourth
receiver into one number. What a bettor needs is what it allows to the guy in
the ROLE his player occupies — WR1, WR2, WR3, TE1, TE2, RB1, RB2, QB.

HOW ROLE IS ASSIGNED, and why it isn't circular. The obvious approach — rank a
team's receivers by targets IN that game — is backwards: a true WR1 who gets
blanketed and sees two targets would be scored as a WR3, so a defense would get
credit for shutting down a WR3 it actually shut down a WR1. Role has to be the
role he carried INTO the game, so it's assigned off trailing usage only
(target share for pass catchers, carry share for backs), with season-to-date as
the fallback in the early weeks when no trailing window exists.

Every cell also carries its LEAGUE RANK, because 66 receiving yards allowed
means nothing until you know it's 6th-most in the league. Rank 1 = most
allowed, i.e. the softest matchup, which is the direction a bettor reads.

Windows: full season, plus last 3 / 5 / 10 games, since a defense in week 12 is
often not the defense that played week 2.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

# role -> (position group, depth index). Depth is 1-based.
PASS_ROLES = [("WR", 3), ("TE", 2)]     # WR1-3, TE1-2
RUSH_ROLES = [("RB", 2)]                # RB1-2

WINDOWS = {"season": None, "l3": 3, "l5": 5, "l10": 10}

# The columns the reference view shows, in order.
DVP_STATS = ["td", "rectd", "rshtd", "recyd_g", "rshyd_g", "rz_tgts", "rz_car"]

# Which stats actually mean something for a given role.
_RECEIVING = {"td", "rectd", "recyd_g", "rz_tgts"}
_RUSHING = {"td", "rshtd", "rshyd_g", "rz_car"}


def STATS_FOR_ROLE(role: str) -> set[str]:
    r = str(role or "")
    if r.startswith("QB"):
        return _RUSHING                      # QB rushing line only
    if r.startswith("RB") or "RB" in r:
        return _RECEIVING | _RUSHING         # backs do both
    return _RECEIVING                        # WR / TE


@functools.lru_cache(maxsize=4)
def _pbp(season: int) -> pl.DataFrame:
    return nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")


@functools.lru_cache(maxsize=4)
def _weekly(season: int) -> pl.DataFrame:
    return (nfl.load_player_stats(seasons=[season], summary_level="week")
              .filter(pl.col("season_type") == "REG"))


def depth_roles(season: int) -> pl.DataFrame:
    """Per player-week: the depth role he carried INTO that game.

    Trailing usage only — never the current week — so a shut-down WR1 is still
    scored as a WR1 rather than being demoted by the very outcome we're
    measuring.
    """
    wk = _weekly(season).select(
        ["player_id", "player_display_name", "position", "team", "week",
         "targets", "carries"]).with_columns(
        pl.col("targets").fill_null(0), pl.col("carries").fill_null(0))

    weeks = sorted(wk["week"].unique().to_list())
    out = []
    for w in weeks:
        hist = wk.filter(pl.col("week") < w)
        if hist.height == 0:
            # Week 1 has no history at all. Everyone is unranked rather than
            # guessed at; the row simply carries no role.
            continue
        usage = hist.group_by(["player_id", "team", "position"]).agg(
            pl.col("targets").sum().alias("tgt"),
            pl.col("carries").sum().alias("car"),
            pl.len().alias("gp"))

        parts = []
        for pos, depth in PASS_ROLES:
            r = usage.filter(pl.col("position") == pos).with_columns(
                pl.col("tgt").rank("ordinal", descending=True).over("team").alias("rk"))
            parts.append(r.with_columns(
                pl.when(pl.col("rk") <= depth)
                  .then(pl.format("{}{}", pl.lit(pos), pl.col("rk")))
                  .otherwise(pl.format("Other {}", pl.lit(pos))).alias("role")))
        for pos, depth in RUSH_ROLES:
            r = usage.filter(pl.col("position") == pos).with_columns(
                pl.col("car").rank("ordinal", descending=True).over("team").alias("rk"))
            parts.append(r.with_columns(
                pl.when(pl.col("rk") <= depth)
                  .then(pl.format("{}{}", pl.lit(pos), pl.col("rk")))
                  .otherwise(pl.format("Other {}", pl.lit(pos))).alias("role")))
        qb = usage.filter(pl.col("position") == "QB").with_columns(pl.lit("QB").alias("role"))
        parts.append(qb)

        roles = pl.concat([p.select(["player_id", "team", "role"]) for p in parts])
        out.append(roles.with_columns(pl.lit(w).cast(pl.Int32).alias("week")))

    return pl.concat(out) if out else wk.head(0).select(["player_id", "team", "week"])


def _rz(season: int) -> pl.DataFrame:
    """Red-zone targets and carries per player-week."""
    p = _pbp(season).filter(pl.col("yardline_100") <= 20)
    tgt = (p.filter(pl.col("pass_attempt") == 1, pl.col("receiver_player_id").is_not_null())
            .group_by([pl.col("receiver_player_id").alias("player_id"), "week"])
            .agg(pl.len().alias("rz_tgts")))
    car = (p.filter(pl.col("rush_attempt") == 1, pl.col("rusher_player_id").is_not_null())
            .group_by([pl.col("rusher_player_id").alias("player_id"), "week"])
            .agg(pl.len().alias("rz_car")))
    return tgt.join(car, on=["player_id", "week"], how="full", coalesce=True).with_columns(
        pl.col("rz_tgts").fill_null(0), pl.col("rz_car").fill_null(0))


def build(season: int) -> dict:
    """{window: {defense_team: {role: {stat: value, stat_rank: n}}}}"""
    wk = _weekly(season).select(
        ["player_id", "position", "team", "opponent_team", "week",
         "receiving_tds", "rushing_tds", "receiving_yards", "rushing_yards"])
    for c in ("receiving_tds", "rushing_tds", "receiving_yards", "rushing_yards"):
        wk = wk.with_columns(pl.col(c).fill_null(0))

    wk = (wk.join(depth_roles(season), on=["player_id", "team", "week"], how="inner")
            .join(_rz(season), on=["player_id", "week"], how="left")
            .with_columns(pl.col("rz_tgts").fill_null(0), pl.col("rz_car").fill_null(0)))

    max_week = int(wk["week"].max())
    out: dict = {}

    for wname, wlen in WINDOWS.items():
        sub = wk if wlen is None else wk.filter(pl.col("week") > max_week - wlen)
        if sub.height == 0:
            continue

        # games each defense played inside the window — the per-game denominator
        games = (sub.group_by("opponent_team")
                    .agg(pl.col("week").n_unique().alias("g"))
                    .rename({"opponent_team": "def_team"}))

        agg = (sub.group_by(["opponent_team", "role"]).agg(
                    (pl.col("receiving_tds") + pl.col("rushing_tds")).sum().alias("td"),
                    pl.col("receiving_tds").sum().alias("rectd"),
                    pl.col("rushing_tds").sum().alias("rshtd"),
                    pl.col("receiving_yards").sum().alias("recyd"),
                    pl.col("rushing_yards").sum().alias("rshyd"),
                    pl.col("rz_tgts").sum().alias("rz_tgts"),
                    pl.col("rz_car").sum().alias("rz_car"))
                 .rename({"opponent_team": "def_team"})
                 .join(games, on="def_team", how="left")
                 .with_columns(
                     (pl.col("recyd") / pl.col("g").clip(1)).round(1).alias("recyd_g"),
                     (pl.col("rshyd") / pl.col("g").clip(1)).round(1).alias("rshyd_g")))

        # LEAGUE RANK per (role, stat). 1 = allows the most = softest matchup.
        for s in DVP_STATS:
            agg = agg.with_columns(
                pl.col(s).rank("min", descending=True).over("role").cast(pl.Int32).alias(f"{s}_rank"))

        blob: dict = {}
        for r in agg.iter_rows(named=True):
            cell = {}
            keep = STATS_FOR_ROLE(r["role"])
            for s in DVP_STATS:
                # N/A rather than a number, when the stat doesn't apply to the
                # role. A receiver has no rushing line and a quarterback has no
                # receiving line; printing 0.0 (or -0.6, off a trick play) for
                # those reads as a measurement instead of a category error.
                if s not in keep:
                    continue
                v = r.get(s)
                if v is None:
                    continue
                cell[s] = round(float(v), 1)
                cell[f"{s}_rank"] = int(r[f"{s}_rank"])
            cell["g"] = int(r.get("g") or 0)
            blob.setdefault(r["def_team"], {})[r["role"]] = cell
        out[wname] = blob

    return out


def current_roles(season: int) -> dict:
    """{player_id: role} as of the freshest week we have usage for.

    The site needs this to point a player at his own row in the DvP table.
    Deriving it browser-side from published usage would be a SECOND
    implementation of depth_roles(), and the failure mode is the bad kind:
    the table and the highlight disagree, both look internally consistent,
    and nothing errors. One source, shipped.
    """
    r = depth_roles(season)
    if r.height == 0 or "role" not in r.columns:
        return {}
    last = r["week"].max()
    return {row["player_id"]: row["role"]
            for row in r.filter(pl.col("week") == last).iter_rows(named=True)}


ROLE_ORDER = ["WR1", "WR2", "WR3", "Other WR", "TE1", "TE2", "Other TE",
              "RB1", "RB2", "Other RB", "QB"]

STAT_LABELS = {
    "td": "TD", "rectd": "RECTD", "rshtd": "RSHTD",
    "recyd_g": "RECYD/G", "rshyd_g": "RSHYDS/G",
    "rz_tgts": "RZ TGTS", "rz_car": "RZ CAR",
}


if __name__ == "__main__":
    d = build(2025)
    season = d["season"]
    print("teams:", len(season))
    for team in ("LA", "BAL"):
        if team not in season:
            continue
        print(f"\n=== {team} defense allows (2025 season) ===")
        print(f"{'ROLE':<10}{'TD':>6}{'RECTD':>8}{'RECYD/G':>10}{'RZ TGTS':>10}")
        for role in ROLE_ORDER:
            c = season[team].get(role)
            if not c:
                continue
            print(f"{role:<10}{c.get('td',0):>4.0f} #{c.get('td_rank','-'):<2}"
                  f"{c.get('rectd',0):>5.0f} #{c.get('rectd_rank','-'):<2}"
                  f"{c.get('recyd_g',0):>7.1f} #{c.get('recyd_g_rank','-'):<2}"
                  f"{c.get('rz_tgts',0):>7.0f} #{c.get('rz_tgts_rank','-'):<2}")
