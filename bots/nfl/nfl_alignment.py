#!/usr/bin/env python3
"""nfl_alignment.py -- what the FREE feeds can honestly say about alignment (bot task 2).

THE ASK: `alignment[gsis_id] = {snaps, side{left,middle,right}, slot, wide, tight,
backfield, box}` -- where each man LINED UP. No free feed has that:

  * nflverse play-by-play 2025/2026: pass_location / run_location are where the
    BALL went (the site already turns those into a targeted man's side). No
    formation, personnel or box columns at all (checked 2026-10-11: every one is
    null in both seasons).
  * nflverse participation (2016-2025; the 2026 file does not exist yet, see
    nfl_charting.py): WHO was on the field per play (offense_players, positions
    by roster, not by alignment), plus per-PLAY offense_formation,
    offense_personnel, defenders_in_box. It names the eleven, not their spots.
  * ESPN/nflverse depth charts: WR is WR, no slot/outside split.
  * Per-man alignment is NGS tracking / PFF / SIS (paid). None is wired.

So `per_player` is published EMPTY, on purpose: an invented side or slot share
would be a made-up number. What participation can give, per CLUB and labelled with
the season it is from (charting_season: the newest completed one), is the part of
the ask that is a team fact:

  run_front  defenders in the box on rushing plays: share at 6-or-fewer, 7, 8+
             (the spec's run_front: box count share 6 / 7 / 8+)
  personnel  the offence's skill personnel by RB-TE (11, 12, 21, 13, 10, other),
             share of plays; FB counts as a RB
  formation  shotgun / under centre / pistol share of plays

`switch-on`: when a per-man feed is published, fill `per_player[gsis_id]` with the
spec's shape; nothing else changes. lib/nfl/roles.js isSlot() reads
snaps[id].slot_pct, which this bot publishes only if a feed gives it.
"""
from __future__ import annotations

import re

import polars as pl

SOURCE = "nflverse participation (per play: personnel, formation, defenders in the box)"
MIN_BOX = 4          # participation carries 0 on special-teams rows; a real box has 4+
MIN_PLAYS = 50       # a club with fewer rushing plays gets no front rather than a noisy one


def _skill(personnel: "str | None") -> "str | None":
    """'1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR' -> '11' (RB count then TE count). None
    when the string has no QB (a special-teams or garbled row)."""
    if not personnel or "QB" not in personnel:
        return None
    n = {k: int(v) for v, k in re.findall(r"(\d+)\s+([A-Z]+)", personnel)}
    return f"{n.get('RB', 0) + n.get('FB', 0)}{n.get('TE', 0)}"


def club_tables(plays: pl.DataFrame) -> dict:
    """`plays`: one row per play with posteam, defteam, rush_attempt, pass_attempt,
    defenders_in_box, offense_personnel, offense_formation. Pure."""
    out: dict = {"run_front": {}, "personnel": {}, "formation": {}}
    run = plays.filter(pl.col("rush_attempt") == 1, pl.col("defenders_in_box") >= MIN_BOX,
                       pl.col("defteam").is_not_null())
    for r in run.group_by("defteam").agg(
            pl.len().alias("n"),
            (pl.col("defenders_in_box") <= 6).sum().alias("le6"),
            (pl.col("defenders_in_box") == 7).sum().alias("s7"),
            (pl.col("defenders_in_box") >= 8).sum().alias("ge8")).iter_rows(named=True):
        if r["n"] < MIN_PLAYS:
            continue
        out["run_front"][r["defteam"]] = {
            "plays": int(r["n"]),
            "box_6_or_fewer": round(r["le6"] / r["n"], 3),
            "box_7": round(r["s7"] / r["n"], 3),
            "box_8_plus": round(r["ge8"] / r["n"], 3),
        }
    off = plays.filter((pl.col("pass_attempt") == 1) | (pl.col("rush_attempt") == 1),
                       pl.col("posteam").is_not_null())
    off = off.with_columns(pl.col("offense_personnel").map_elements(_skill, return_dtype=pl.Utf8).alias("_sk"))
    pers = off.filter(pl.col("_sk").is_not_null())
    for team, grp in pers.group_by("posteam"):
        n = grp.height
        if n < MIN_PLAYS:
            continue
        counts = {r["_sk"]: r["c"] for r in grp.group_by("_sk").agg(pl.len().alias("c")).iter_rows(named=True)}
        main = ("11", "12", "21", "13", "10")
        row = {"plays": n, **{k: round(counts.get(k, 0) / n, 3) for k in main}}
        row["other"] = round(1 - sum(row[k] for k in main), 3)
        out["personnel"][team[0]] = row
    form = off.filter(pl.col("offense_formation").is_not_null())
    for team, grp in form.group_by("posteam"):
        n = grp.height
        if n < MIN_PLAYS:
            continue
        c = {r["offense_formation"]: r["c"] for r in grp.group_by("offense_formation").agg(pl.len().alias("c")).iter_rows(named=True)}
        out["formation"][team[0]] = {"plays": n,
                                     "shotgun": round(c.get("SHOTGUN", 0) / n, 3),
                                     "under_center": round(c.get("UNDER CENTER", 0) / n, 3),
                                     "pistol": round(c.get("PISTOL", 0) / n, 3)}
    return out


def build(season: int) -> dict:
    """The `alignment` block for matchup.json, from `season`'s participation file
    (pass nfl_charting.charting_season(stat_season))."""
    import nflreadpy as nfl
    part = nfl.load_participation(seasons=[season]).select(
        "nflverse_game_id", "play_id", "defenders_in_box", "offense_personnel", "offense_formation")
    pbp = nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG").select(
        pl.col("game_id").alias("nflverse_game_id"), "play_id", "posteam", "defteam",
        "rush_attempt", "pass_attempt")
    plays = pbp.join(part, on=["nflverse_game_id", "play_id"], how="inner")
    t = club_tables(plays)
    return {"season": season, "source": SOURCE,
            "per_player": {},
            "per_player_note": ("No per-man alignment feed is wired (it is NGS tracking, PFF or SIS, all paid). "
                                "per_player stays empty rather than carry a made-up side or slot share."),
            **t}
