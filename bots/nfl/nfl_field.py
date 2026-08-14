#!/usr/bin/env python3
"""nfl_field.py — the field chart. Where the ball goes, and where a defence leaks.

The football answer to the MLB spray chart, and the same job: a shape you read
in one glance instead of a column of numbers you read one at a time.

TWO GRIDS, because football has two ways to move the ball.

  PASSING   direction (left / middle / right) x depth, where depth comes from
            air_yards: behind the line, short (0-9), intermediate (10-19),
            deep (20+). Twelve zones.
  RUSHING   the gaps, as the play-by-play actually charts them: left/middle/
            right x end/tackle/guard, plus middle. Seven lanes.

Each zone carries volume, yards, TDs and success rate — and every grid can be
built for a PLAYER (where he works) or for a DEFENCE (where it leaks). The
defence version is the one worth having: "they're soft deep left" is a
sentence you can bet, and it's invisible in a season yardage total.

Coverage note on the inputs: pass_location is charted on ~90% of attempts and
air_yards on ~92%; run_gap is missing on ~26% of carries (mostly scrambles and
designed QB runs, which have no gap). Unlabelled plays are dropped from the
grid rather than dumped into a bucket they don't belong in, and the zone total
is published so the denominator is visible.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

DEPTHS = [("behind", -99, -0.001), ("short", 0, 9.999),
          ("mid", 10, 19.999), ("deep", 20, 99)]
DEPTH_LABEL = {"behind": "Behind LOS", "short": "Short 0–9",
               "mid": "Intermediate 10–19", "deep": "Deep 20+"}
SIDES = ["left", "middle", "right"]
GAPS = ["end", "tackle", "guard"]


@functools.lru_cache(maxsize=4)
def _pbp(season: int) -> pl.DataFrame:
    return nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")


def _depth_expr() -> pl.Expr:
    e = pl.when(pl.col("air_yards") < 0).then(pl.lit("behind"))
    e = e.when(pl.col("air_yards") < 10).then(pl.lit("short"))
    e = e.when(pl.col("air_yards") < 20).then(pl.lit("mid"))
    return e.otherwise(pl.lit("deep")).alias("depth")


def _pass_grid(df: pl.DataFrame, key: str) -> dict:
    d = (df.filter(pl.col("pass_location").is_in(SIDES), pl.col("air_yards").is_not_null())
           .with_columns(_depth_expr())
           .group_by([key, "pass_location", "depth"]).agg(
               pl.len().alias("att"),
               pl.col("complete_pass").fill_null(0).sum().alias("cmp"),
               pl.col("yards_gained").fill_null(0).sum().alias("yds"),
               pl.col("pass_touchdown").fill_null(0).sum().alias("td")))
    out: dict = {}
    for r in d.iter_rows(named=True):
        att = max(1, int(r["att"]))
        out.setdefault(r[key], {})[f"{r['pass_location']}|{r['depth']}"] = {
            "att": int(r["att"]), "cmp": int(r["cmp"]), "yds": int(r["yds"]),
            "td": int(r["td"]),
            "ypa": round(float(r["yds"]) / att, 1),
            "cmp_pct": round(100 * float(r["cmp"]) / att),
        }
    return out


def _rush_grid(df: pl.DataFrame, key: str) -> dict:
    d = df.filter(pl.col("run_location").is_in(SIDES))
    # 'middle' has no gap charted — it IS the lane.
    d = d.with_columns(
        pl.when(pl.col("run_location") == "middle").then(pl.lit("middle|middle"))
          .when(pl.col("run_gap").is_in(GAPS))
          .then(pl.concat_str([pl.col("run_location"), pl.lit("|"), pl.col("run_gap")]))
          .otherwise(pl.lit(None)).alias("lane")).filter(pl.col("lane").is_not_null())
    g = d.group_by([key, "lane"]).agg(
        pl.len().alias("att"),
        pl.col("yards_gained").fill_null(0).sum().alias("yds"),
        pl.col("rush_touchdown").fill_null(0).sum().alias("td"))
    out: dict = {}
    for r in g.iter_rows(named=True):
        att = max(1, int(r["att"]))
        out.setdefault(r[key], {})[r["lane"]] = {
            "att": int(r["att"]), "yds": int(r["yds"]), "td": int(r["td"]),
            "ypc": round(float(r["yds"]) / att, 1),
        }
    return out


def build(season: int, player_ids: set[str] | None = None) -> dict:
    """{'def_pass', 'def_rush', 'player_pass', 'player_rush'} keyed by team / id."""
    p = _pbp(season)
    passes = p.filter(pl.col("pass_attempt") == 1)
    rushes = p.filter(pl.col("rush_attempt") == 1)

    out = {
        # What each defence gives up, by zone. The headline use.
        "def_pass": _pass_grid(passes.filter(pl.col("defteam").is_not_null()), "defteam"),
        "def_rush": _rush_grid(rushes.filter(pl.col("defteam").is_not_null()), "defteam"),
    }

    pp = passes.filter(pl.col("receiver_player_id").is_not_null()) \
               .rename({"receiver_player_id": "pid"})
    rr = rushes.filter(pl.col("rusher_player_id").is_not_null()) \
               .rename({"rusher_player_id": "pid"})
    if player_ids:
        pp = pp.filter(pl.col("pid").is_in(list(player_ids)))
        rr = rr.filter(pl.col("pid").is_in(list(player_ids)))
    out["player_pass"] = _pass_grid(pp, "pid")
    out["player_rush"] = _rush_grid(rr, "pid")

    # League baselines, so a zone can be read as hot or cold rather than just
    # busy. Without these every grid's darkest cell is simply its own maximum.
    lp = _pass_grid(passes.with_columns(pl.lit("ALL").alias("_")), "_").get("ALL", {})
    lr = _rush_grid(rushes.with_columns(pl.lit("ALL").alias("_")), "_").get("ALL", {})
    out["league_pass"] = lp
    out["league_rush"] = lr
    return out


ZONES_PASS = [f"{s}|{d}" for d in ("deep", "mid", "short", "behind") for s in SIDES]
ZONES_RUSH = ["left|end", "left|tackle", "left|guard", "middle|middle",
              "right|guard", "right|tackle", "right|end"]
RUSH_LABEL = {"left|end": "L End", "left|tackle": "L Tackle", "left|guard": "L Guard",
              "middle|middle": "Middle", "right|guard": "R Guard",
              "right|tackle": "R Tackle", "right|end": "R End"}


if __name__ == "__main__":
    f = build(2025)
    print("defences:", len(f["def_pass"]), "| receivers:", len(f["player_pass"]))
    print("\nDEN pass defence allowed, by zone (ypa):")
    den = f["def_pass"].get("DEN", {})
    for d in ("deep", "mid", "short", "behind"):
        row = "  ".join(
            f"{s[:1].upper()}:{den.get(f'{s}|{d}',{}).get('ypa','—'):>5}" for s in SIDES)
        print(f"  {DEPTH_LABEL[d]:<20} {row}")
    print("\nleague, same view:")
    for d in ("deep", "mid", "short", "behind"):
        row = "  ".join(
            f"{s[:1].upper()}:{f['league_pass'].get(f'{s}|{d}',{}).get('ypa','—'):>5}" for s in SIDES)
        print(f"  {DEPTH_LABEL[d]:<20} {row}")
