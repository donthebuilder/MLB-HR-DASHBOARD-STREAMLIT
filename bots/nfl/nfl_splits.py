#!/usr/bin/env python3
"""nfl_splits.py — the situational splits, per player per season.

Same discipline as the MLB side's lib/situational.js: a SHORTLIST that earns
its place, not the whole menu. Football will happily give you forty splits and
thirty-five of them are noise on a seventeen-game sample.

Six dimensions survived, and each is here for a stated reason:

  HOME / AWAY        the direct MLB analog. Travel, crowd noise on snap counts,
                     and it's the one split every reader looks for first.
  INDOORS / OUTDOORS the park-factor analog. Roof is the single environmental
                     variable that moves passing and kicking most.
  GRASS / TURF       real and measurable for skill players; turf is faster.
  SCRIPT             leading / close / trailing, by score at the snap. THE
                     football split. There is no MLB equivalent — baseball's
                     run/pass balance doesn't invert when you go down 14.
                     A back's carries and a receiver's targets live or die on
                     this, and it's the one that explains most "why did he only
                     get six touches" questions.
  1H / 2H            usage patterns and blowout attrition.
  RED ZONE / FIELD   where touchdowns actually happen, separated from the
                     yardage that gets you there.

Deliberately NOT here:
  · man vs zone coverage — the number everyone wants, and nflverse does not
    have it. It's PFF/SIS charting, which is paid. Do not fake it.
  · by-down splits — real, but they slice a 17-game sample four more ways and
    the surviving cells are too thin to read.
  · pass_location / run_gap — genuinely available, genuinely interesting, and
    genuinely not connected to any of the seven markets we grade.

Everything is a PER-GAME RATE plus the game count it came from, because a
split without its sample size is a number you can't argue with.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

# bucket key -> (label, predicate over the pbp frame)
SPLITS: dict[str, tuple[str, pl.Expr]] = {
    "home":     ("Home",        pl.col("posteam") == pl.col("home_team")),
    "away":     ("Away",        pl.col("posteam") != pl.col("home_team")),
    "indoors":  ("Indoors",     pl.col("roof").is_in(["dome", "closed"])),
    "outdoors": ("Outdoors",    pl.col("roof") == "outdoors"),
    "grass":    ("Grass",       pl.col("surface") == "grass"),
    "turf":     ("Turf",        (pl.col("surface") != "grass") & (pl.col("surface") != "")),
    # Score at the SNAP, from the offense's point of view.
    "leading":  ("Leading",     pl.col("score_differential") > 3),
    "close":    ("Close",       pl.col("score_differential").abs() <= 3),
    "trailing": ("Trailing",    pl.col("score_differential") < -3),
    "h1":       ("1st half",    pl.col("game_half") == "Half1"),
    "h2":       ("2nd half",    pl.col("game_half") == "Half2"),
    "rz":       ("Red zone",    pl.col("yardline_100") <= 20),
    "field":    ("Open field",  pl.col("yardline_100") > 20),
}


@functools.lru_cache(maxsize=4)
def _pbp(season: int) -> pl.DataFrame:
    return nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")


def _side(p: pl.DataFrame, id_col: str, prefix: str) -> pl.DataFrame:
    """Aggregate one role (receiver / rusher / passer / kicker) per player."""
    aggs = {
        "receiver_player_id": [
            pl.len().alias("tgt"),
            pl.col("complete_pass").fill_null(0).sum().alias("rec"),
            pl.col("receiving_yards").fill_null(0).sum().alias("rec_yds"),
            pl.col("pass_touchdown").fill_null(0).sum().alias("rec_td"),
        ],
        "rusher_player_id": [
            pl.len().alias("car"),
            pl.col("rushing_yards").fill_null(0).sum().alias("rush_yds"),
            pl.col("rush_touchdown").fill_null(0).sum().alias("rush_td"),
        ],
        "passer_player_id": [
            pl.len().alias("att"),
            pl.col("passing_yards").fill_null(0).sum().alias("pass_yds"),
            pl.col("pass_touchdown").fill_null(0).sum().alias("pass_td"),
        ],
    }[id_col]
    return (p.filter(pl.col(id_col).is_not_null())
             .group_by([pl.col(id_col).alias("player_id"), "game_id"])
             .agg(aggs)
             .group_by("player_id")
             .agg([pl.col(c).sum() for c in
                   [a.meta.output_name() for a in aggs]] +
                  [pl.col("game_id").n_unique().alias(f"{prefix}_g")]))


def splits_for(season: int, min_games: int = 3) -> dict[str, dict]:
    """Per player: {split_key: {stat: per-game rate, 'g': games}}.

    Rates, not totals. A back who played four games leading and thirteen
    trailing has to be comparable across the two, and totals aren't.
    """
    p = _pbp(season)
    out: dict[str, dict] = {}

    for key, (_label, pred) in SPLITS.items():
        sub = p.filter(pred)
        if sub.height == 0:
            continue

        rec = _side(sub, "receiver_player_id", "r")
        rush = _side(sub, "rusher_player_id", "u")
        pas = _side(sub, "passer_player_id", "p")

        d = rec.join(rush, on="player_id", how="full", coalesce=True) \
               .join(pas, on="player_id", how="full", coalesce=True)
        num = [c for c in d.columns if c != "player_id"]
        d = d.with_columns([pl.col(c).fill_null(0) for c in num])
        # Games in this bucket = the most any role saw. A receiver who also
        # takes handoffs shouldn't be double-counted.
        d = d.with_columns(
            pl.max_horizontal("r_g", "u_g", "p_g").alias("g")).filter(pl.col("g") >= min_games)

        for r in d.iter_rows(named=True):
            g = max(1, int(r["g"]))
            stats = {"g": int(r["g"])}
            for src, dst in (("tgt", "tgt"), ("rec", "rec"), ("rec_yds", "recyd"),
                             ("car", "car"), ("rush_yds", "ruyd"),
                             ("att", "att"), ("pass_yds", "payd")):
                v = float(r.get(src) or 0)
                if v:
                    stats[dst] = round(v / g, 2)
            td = float(r.get("rec_td") or 0) + float(r.get("rush_td") or 0)
            if td:
                stats["td"] = round(td / g, 2)
            ptd = float(r.get("pass_td") or 0)
            if ptd:
                stats["ptd"] = round(ptd / g, 2)
            if len(stats) > 1:
                out.setdefault(r["player_id"], {})[key] = stats
    return out


# What the site shows, in display order. Pairs render side by side so the
# comparison is the point rather than a scavenger hunt through a list.
SPLIT_PAIRS = [
    ("home", "away"),
    ("indoors", "outdoors"),
    ("grass", "turf"),
    ("leading", "close"),
    ("close", "trailing"),
    ("h1", "h2"),
    ("rz", "field"),
]

SPLIT_LABELS = {k: v[0] for k, v in SPLITS.items()}


if __name__ == "__main__":
    import json
    s = splits_for(2025)
    print(f"{len(s)} players with splits")
    # a known every-down back, as a smoke test
    wk = nfl.load_player_stats(seasons=[2025], summary_level="week")
    who = wk.filter(pl.col("player_display_name").str.contains("Bijan"))["player_id"].to_list()
    if who:
        print(json.dumps(s.get(who[0], {}), indent=2)[:1400])
