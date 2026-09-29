#!/usr/bin/env python3
"""nfl_qb_pressure.py -- how often each QB is pressured, and what he does when blitzed.

Two in-season sources, each saying only what it measures:

  PFR advanced passing (weekly)   times_pressured / times_pressured_pct,
      times_sacked, times_hit, times_blitzed, passing_bad_throws. Dropbacks
      are backed out of pressured / pct per game (PFR's own denominator).
  FTN charting (weekly) + pbp     n_blitzers on each of his dropbacks, joined
      to the play's yards (sacks count, as lost yards). Yards per dropback
      blitzed (1+ non-lineman rushing) vs not. FTN charts most games, not all:
      the counts are published so the page can say "over N dropbacks".

Nothing here says how a QB does "under pressure" -- no free source flags
pressure per play in-season (participation's was_pressure is once a year).

OUTPUT (`qb_pressure` in nfl_matchup.json, ~8 KB):
  {season, qbs: {gsis_id: {name, team, games, dropbacks, pressured,
                 pressure_pct, sacks, hits, blitzed, bad_throw_pct,
                 blitz: {n, ypd}, no_blitz: {n, ypd}}},
   starter: {team: gsis_id},   # most dropbacks in the team's latest game
   rank: {gsis_id: n}}         # pressure_pct, 1 = pressured most (2+ games)
"""
from __future__ import annotations

import nflreadpy as nfl
import polars as pl

MIN_GAMES_RANK = 2


def _pfr_to_gsis() -> dict:
    p = nfl.load_players().filter(pl.col("pfr_id").is_not_null())
    return dict(zip(p["pfr_id"].to_list(), p["gsis_id"].to_list()))


def _blitz_splits(season: int) -> dict:
    try:
        ftn = nfl.load_ftn_charting(seasons=[season])
    except Exception:  # noqa: BLE001 -- no FTN yet = no split, said as "—"
        return {}
    if ftn.is_empty():
        return {}
    pbp = (nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")
              .filter((pl.col("qb_dropback") == 1) & pl.col("passer_player_id").is_not_null())
              .select(["game_id", pl.col("play_id").cast(pl.Float64), "passer_player_id", "yards_gained"]))
    f = ftn.select([pl.col("nflverse_game_id").cast(pl.Utf8).alias("game_id"),
                    pl.col("nflverse_play_id").cast(pl.Float64).alias("play_id"),
                    pl.col("n_blitzers").cast(pl.Int64, strict=False).alias("blitzers"),
                    pl.col("n_pass_rushers").cast(pl.Int64, strict=False).alias("rushers")])
    j = f.join(pbp, on=["game_id", "play_id"], how="inner").filter(pl.col("rushers") > 0)
    out: dict = {}
    for (qb,), g in j.group_by(["passer_player_id"]):
        b = g.filter(pl.col("blitzers") >= 1)
        n = g.filter(pl.col("blitzers") == 0)
        ypd = lambda d: round(float(d["yards_gained"].fill_null(0).mean()), 1) if d.height else None  # noqa: E731
        out[qb] = {"blitz": {"n": b.height, "ypd": ypd(b)}, "no_blitz": {"n": n.height, "ypd": ypd(n)}}
    return out


def qb_pressure(season: int) -> dict:
    d = nfl.load_pfr_advstats(seasons=[season], stat_type="pass", summary_level="week")
    d = d.filter(pl.col("game_type") == "REG")
    if d.is_empty():
        return {}
    ids = _pfr_to_gsis()
    d = d.with_columns(
        pl.col("pfr_player_id").replace_strict(ids, default=None).alias("gsis"),
        pl.when(pl.col("times_pressured_pct") > 0)
          .then((pl.col("times_pressured") / pl.col("times_pressured_pct")).round(0))
          .otherwise(None).alias("dropbacks"),
    ).filter(pl.col("gsis").is_not_null())
    splits = _blitz_splits(season)
    qbs: dict = {}
    for (gid,), g in d.group_by(["gsis"]):
        drop = int(g["dropbacks"].fill_null(0).sum())
        pres = int(g["times_pressured"].fill_null(0).sum())
        bad = g["passing_bad_throws"].fill_null(0).sum()
        qbs[gid] = {
            "name": g["pfr_player_name"][-1], "team": g.sort("week")["team"][-1], "games": g.height,
            "dropbacks": drop, "pressured": pres,
            "pressure_pct": round(100.0 * pres / drop, 1) if drop else None,
            "sacks": int(g["times_sacked"].fill_null(0).sum()), "hits": int(g["times_hit"].fill_null(0).sum()),
            "blitzed": int(g["times_blitzed"].fill_null(0).sum()),
            "bad_throw_pct": round(100.0 * float(bad) / drop, 1) if drop else None,
            **splits.get(gid, {"blitz": None, "no_blitz": None}),
        }
    # This week's QB per team: most dropbacks in the team's latest game.
    latest = d.group_by("team").agg(pl.col("week").max().alias("week"))
    last = d.join(latest, on=["team", "week"]).sort("dropbacks", descending=True, nulls_last=True)
    starter = {r["team"]: r["gsis"] for r in last.unique("team", keep="first").iter_rows(named=True)}
    ranked = sorted(((v["pressure_pct"], k) for k, v in qbs.items()
                     if v["pressure_pct"] is not None and v["games"] >= MIN_GAMES_RANK), reverse=True)
    return {"season": season, "qbs": qbs, "starter": starter,
            "rank": {k: i + 1 for i, (_, k) in enumerate(ranked)}, "ranked": len(ranked)}
