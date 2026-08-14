#!/usr/bin/env python3
"""nfl_explosive.py — explosive-play profiles and team usage shares.

Two things a prop board needs that a per-game average can't give you.

EXPLOSIVE. Ceiling, not median. A receiver who clears 40 yards on one catch is
a different bet from one who grinds twelve. Both sides of it matter:
  · what a DEFENSE allows — 10+/20+/30+/40+ passes, explosive rate, 20+ TDs
  · what a PLAYER produces — his own 10+/20+/30+/40+ receptions and his longest

USAGE SHARES. Opportunity as a fraction of the team, which is what survives a
change in game script. Raw targets fall when a team runs out a lead; target
SHARE doesn't. Red-zone share is the one that matters most for touchdowns and
is the hardest to eyeball from a box score.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

BUCKETS = [10, 20, 30, 40]


@functools.lru_cache(maxsize=4)
def _pbp(season: int) -> pl.DataFrame:
    return nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")


def defense_explosive(season: int) -> dict:
    """Per defense: what it gives up through the air, by chunk size."""
    p = _pbp(season).filter(pl.col("pass_attempt") == 1)
    aggs = [
        pl.len().alias("att"),
        pl.col("complete_pass").fill_null(0).sum().alias("cmp"),
        pl.col("yards_gained").fill_null(0).sum().alias("yds"),
        pl.col("air_yards").fill_null(0).mean().round(1).alias("air"),
    ]
    for b in BUCKETS:
        aggs.append(((pl.col("complete_pass") == 1) &
                     (pl.col("yards_gained") >= b)).sum().alias(f"pass_{b}"))
    # 20+ air yards — the deep-shot lane, distinct from a screen that broke long
    aggs += [
        (pl.col("air_yards") >= 20).sum().alias("deep_att"),
        ((pl.col("air_yards") >= 20) & (pl.col("complete_pass") == 1)).sum().alias("deep_cmp"),
        ((pl.col("air_yards") >= 20) & (pl.col("pass_touchdown") == 1)).sum().alias("deep_td"),
    ]
    g = p.group_by("defteam").agg(aggs)
    out = {}
    for r in g.iter_rows(named=True):
        att = max(1, int(r["att"]))
        exp = int(r["pass_20"])
        out[r["defteam"]] = {
            "yds": int(r["yds"]), "air": float(r["air"]),
            **{f"pass_{b}": int(r[f"pass_{b}"]) for b in BUCKETS},
            "exp": exp, "exp_pct": round(100 * exp / att, 1),
            "deep_att": int(r["deep_att"]), "deep_cmp": int(r["deep_cmp"]),
            "deep_pct": round(100 * int(r["deep_cmp"]) / max(1, int(r["deep_att"])), 1),
            "deep_td": int(r["deep_td"]),
        }
    return out


def player_explosive(season: int, min_targets: int = 8) -> dict:
    """Per receiver: his own chunk plays and his longest."""
    p = _pbp(season).filter(pl.col("pass_attempt") == 1,
                            pl.col("receiver_player_id").is_not_null())
    aggs = [
        pl.len().alias("tgts"),
        pl.col("complete_pass").fill_null(0).sum().alias("rec"),
        pl.col("yards_gained").fill_null(0).sum().alias("yds"),
        pl.col("air_yards").fill_null(0).sum().alias("air"),
        pl.col("yards_gained").fill_null(0).max().alias("lng"),
    ]
    for b in BUCKETS:
        aggs.append(((pl.col("complete_pass") == 1) &
                     (pl.col("yards_gained") >= b)).sum().alias(f"rec_{b}"))
    aggs.append((((pl.col("pass_touchdown") == 1)).cast(pl.Int32) *
                 pl.col("yards_gained").fill_null(0)).max().alias("lng_td"))
    g = p.group_by("receiver_player_id").agg(aggs).filter(pl.col("tgts") >= min_targets)
    return {r["receiver_player_id"]: {
        "tgts": int(r["tgts"]), "rec": int(r["rec"]), "yds": int(r["yds"]),
        "air": int(r["air"]), "lng": int(r["lng"] or 0), "lng_td": int(r["lng_td"] or 0),
        **{f"rec_{b}": int(r[f"rec_{b}"]) for b in BUCKETS},
    } for r in g.iter_rows(named=True)}


def team_usage(season: int) -> dict:
    """Per team: every skill player's share of targets, carries and red-zone work.

    Shares are the point. Raw targets move with game script; a share doesn't,
    which is why it's the number that travels between a blowout and a
    one-score game.
    """
    p = _pbp(season)
    tgt = p.filter(pl.col("pass_attempt") == 1, pl.col("receiver_player_id").is_not_null())
    car = p.filter(pl.col("rush_attempt") == 1, pl.col("rusher_player_id").is_not_null())

    team_tot = {
        "tgt": tgt.group_by("posteam").agg(pl.len().alias("n")),
        "rz_tgt": tgt.filter(pl.col("yardline_100") <= 20).group_by("posteam").agg(pl.len().alias("n")),
        "car": car.group_by("posteam").agg(pl.len().alias("n")),
        "rz_car": car.filter(pl.col("yardline_100") <= 20).group_by("posteam").agg(pl.len().alias("n")),
    }
    tot = {k: {r["posteam"]: int(r["n"]) for r in v.iter_rows(named=True)}
           for k, v in team_tot.items()}

    pr = tgt.group_by(["receiver_player_id", "posteam"]).agg(
        pl.len().alias("tgt"),
        pl.col("complete_pass").fill_null(0).sum().alias("rec"),
        pl.col("yards_gained").fill_null(0).sum().alias("recyd"),
        pl.col("pass_touchdown").fill_null(0).sum().alias("rectd"),
        (pl.col("yardline_100") <= 20).sum().alias("rz_tgt"))
    ru = car.group_by(["rusher_player_id", "posteam"]).agg(
        pl.len().alias("car"),
        pl.col("yards_gained").fill_null(0).sum().alias("ruyd"),
        pl.col("rush_touchdown").fill_null(0).sum().alias("rshtd"),
        (pl.col("yardline_100") <= 20).sum().alias("rz_car"))

    d = pr.join(ru, left_on=["receiver_player_id", "posteam"],
                right_on=["rusher_player_id", "posteam"], how="full", coalesce=True)
    d = d.with_columns([pl.col(c).fill_null(0) for c in d.columns
                        if c not in ("receiver_player_id", "posteam")])

    out: dict = {}
    for r in d.iter_rows(named=True):
        pid = r["receiver_player_id"]
        team = r["posteam"]
        if not pid or not team:
            continue
        pct = lambda v, k: round(100 * float(v) / max(1, tot[k].get(team, 1)), 1)
        out.setdefault(team, {})[pid] = {
            "tgt": int(r["tgt"]), "tgt_share": pct(r["tgt"], "tgt"),
            "rec": int(r["rec"]), "recyd": int(r["recyd"]), "rectd": int(r["rectd"]),
            "rz_tgt": int(r["rz_tgt"]), "rz_tgt_share": pct(r["rz_tgt"], "rz_tgt"),
            "car": int(r["car"]), "car_share": pct(r["car"], "car"),
            "ruyd": int(r["ruyd"]), "rshtd": int(r["rshtd"]),
            "rz_car": int(r["rz_car"]), "rz_car_share": pct(r["rz_car"], "rz_car"),
        }
    return out


if __name__ == "__main__":
    de = defense_explosive(2025)
    print("LA explosive allowed:", de.get("LA"))
    pe = player_explosive(2025)
    print(f"\n{len(pe)} receivers with an explosive profile")
    u = team_usage(2025)
    print(f"{len(u)} teams with usage")
    wk = nfl.load_player_stats(seasons=[2025], summary_level="week")
    ids = wk.filter(pl.col("player_display_name") == "Drake London")["player_id"].to_list()
    if ids:
        print("\nDrake London explosive:", pe.get(ids[0]))
        print("Drake London usage    :", u.get("ATL", {}).get(ids[0]))
