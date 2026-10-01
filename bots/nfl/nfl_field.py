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

THE FIELD, PLAY BY PLAY (2026-09-30, BATCH-NFL-FIELD). The grids above are
totals; TUDDY's "The Field" draws every target as a dot at its real depth, so
it needs the plays themselves. `team_plays()` publishes one file per offence,
nfl_field_{TEAM}.json, and the site's player card and the Matchups defence
detail both read it. The chain, end to end:

  source     nflverse load_pbp(stat_season), REG only, pass_attempt == 1 with
             a receiver_player_id (a target -- sacks have no receiver; two-
             point tries are not targets), left-joined on (game_id, play_id)
             to load_ftn_charting for hash / box / play action / screen.
             Red-zone carries: rush_attempt == 1, yardline_100 <= 20.
  transform  one row per target: week, opponent, quarter, down, distance,
             yardline_100, lane (pass_location L/M/R), air_yards, yac, gain
             (yards_gained on a catch, 0 otherwise), result catch / inc / int
             / td, EPA, hash, box, play action, screen. The red-zone list is
             every target and every carry inside the 20, result catch / inc /
             int / carry / td.
  state      nfl_field_{TEAM}.json on the data branch, written by nfl_bot.py
             beside nfl_matchup.json. Columns once, rows as arrays (~45 B a
             target; a full season's offence is ~30 KB).
  output     components/nfl/FieldChart.js on the site: the window ("last N
             games he was targeted in"), NORMAL (yardline_100 > 20) / RED
             ZONE / ALL, the dots, the share rings, THE SPOT. The defence's
             leak per cell is NOT in this file -- it is the existing
             matchup.field.def_pass against league_pass (MatchupMap's
             definition), so the Field and the Matchup map can never disagree
             about a zone.
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


# RUN LANES, WHAT HAPPENS IN THEM (2026-09-28). A stuff is a carry for zero
# or fewer yards; an explosive run is 10+ (the usual line). Counted for the
# defences and the league only (`outcomes=True`) -- that's the question the
# Matchups page asks ("where do they stuff it, where do they spring leaks"),
# and a count per lane per rusher would add ~1,600 cells nobody reads.
EXPLOSIVE_RUN = 10


def _rush_grid(df: pl.DataFrame, key: str, outcomes: bool = False) -> dict:
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
        pl.col("rush_touchdown").fill_null(0).sum().alias("td"),
        (pl.col("yards_gained").fill_null(0) <= 0).sum().alias("stf"),
        (pl.col("yards_gained").fill_null(0) >= EXPLOSIVE_RUN).sum().alias("x10"))
    out: dict = {}
    for r in g.iter_rows(named=True):
        att = max(1, int(r["att"]))
        out.setdefault(r[key], {})[r["lane"]] = {
            "att": int(r["att"]), "yds": int(r["yds"]), "td": int(r["td"]),
            "ypc": round(float(r["yds"]) / att, 1),
        }
        if outcomes:
            out[r[key]][r["lane"]]["stf"] = int(r["stf"])
            out[r[key]][r["lane"]]["x10"] = int(r["x10"])
    return out


def build(season: int, player_ids: set[str] | None = None) -> dict:
    """{'def_pass', 'def_rush', 'player_pass', 'player_rush', 'qb_pass'} keyed by team / id."""
    p = _pbp(season)
    passes = p.filter(pl.col("pass_attempt") == 1)
    rushes = p.filter(pl.col("rush_attempt") == 1)

    # TARGETS, NOT ATTEMPTS (2026-09-30, BATCH-NFL-FIELD). A defence's zone is
    # what it allows on balls thrown TO someone there. A throwaway or a ball
    # with no charted receiver still carries a pass_location, and counted here
    # it was a 0-yard "target allowed": 147 of 3,104 located attempts through
    # 2026 wk 3 (4.7%), enough to move ATL deep-right-intermediate from +10%
    # to +9% and to push thin zones over the 8-target floor on plays nobody
    # was covered on. The league baseline below follows the same rule, so
    # every leak is targets against targets. qb_pass (where HE throws) keeps
    # every attempt.
    targets = passes.filter(pl.col("receiver_player_id").is_not_null())
    out = {
        # What each defence gives up, by zone. The headline use.
        "def_pass": _pass_grid(targets.filter(pl.col("defteam").is_not_null()), "defteam"),
        "def_rush": _rush_grid(rushes.filter(pl.col("defteam").is_not_null()), "defteam", outcomes=True),
    }

    pp = passes.filter(pl.col("receiver_player_id").is_not_null()) \
               .rename({"receiver_player_id": "pid"})
    rr = rushes.filter(pl.col("rusher_player_id").is_not_null()) \
               .rename({"rusher_player_id": "pid"})
    # QB PASSING, 2026-09-21. Same _pass_grid, same zones — just grouped by
    # who THREW it (passer_player_id) instead of who it was thrown TO.
    # player_pass has always been the receiver's side, and a QB never gets
    # targeted, so picking him on the map did nothing no matter what. This
    # is his own real zone distribution: where HE puts it, not where it was
    # caught relative to him.
    qp = passes.filter(pl.col("passer_player_id").is_not_null()) \
               .rename({"passer_player_id": "pid"})
    if player_ids:
        pp = pp.filter(pl.col("pid").is_in(list(player_ids)))
        rr = rr.filter(pl.col("pid").is_in(list(player_ids)))
        qp = qp.filter(pl.col("pid").is_in(list(player_ids)))
    out["player_pass"] = _pass_grid(pp, "pid")
    out["player_rush"] = _rush_grid(rr, "pid")
    out["qb_pass"] = _pass_grid(qp, "pid")

    # League baselines, so a zone can be read as hot or cold rather than just
    # busy. Without these every grid's darkest cell is simply its own maximum.
    lp = _pass_grid(targets.with_columns(pl.lit("ALL").alias("_")), "_").get("ALL", {})
    lr = _rush_grid(rushes.with_columns(pl.lit("ALL").alias("_")), "_", outcomes=True).get("ALL", {})
    out["league_pass"] = lp
    out["league_rush"] = lr
    return out


ZONES_PASS = [f"{s}|{d}" for d in ("deep", "mid", "short", "behind") for s in SIDES]
ZONES_RUSH = ["left|end", "left|tackle", "left|guard", "middle|middle",
              "right|guard", "right|tackle", "right|end"]
RUSH_LABEL = {"left|end": "L End", "left|tackle": "L Tackle", "left|guard": "L Guard",
              "middle|middle": "Middle", "right|guard": "R Guard",
              "right|tackle": "R Tackle", "right|end": "R End"}


# ── THE FIELD: every target, every red-zone touch ────────────────────────
PLAY_COLS = ["pid", "wk", "opp", "q", "dn", "tg", "yl", "lane", "air", "yac",
             "gain", "res", "epa", "hash", "box", "pa", "sc"]
RZ_COLS = ["pid", "wk", "d", "kind", "res"]
_LANE = {"left": "L", "middle": "M", "right": "R"}


def _num(v, nd: int | None = None):
    """int (or a rounded float with `nd`), None for null / NaN / junk."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return round(f, nd) if nd is not None else int(round(f))


def _ftn(season: int) -> pl.DataFrame:
    """FTN per-play charting, only the four columns the Field reads. Empty --
    never a raise -- when FTN has nothing for the season: every field it feeds
    is null-safe on the site."""
    try:
        f = nfl.load_ftn_charting(seasons=[season])
    except Exception as exc:
        print(f"  field plays: FTN charting unavailable ({type(exc).__name__}: {exc})")
        return pl.DataFrame()
    if f.is_empty():
        return pl.DataFrame()
    box = pl.col("n_defense_box").cast(pl.Int64, strict=False)
    hsh = pl.col("starting_hash").cast(pl.Utf8)
    return f.select([
        pl.col("nflverse_game_id").cast(pl.Utf8).alias("game_id"),
        pl.col("nflverse_play_id").cast(pl.Float64).alias("play_id"),
        # "0" is FTN's uncharted marker, never a real hash or a real box.
        pl.when(hsh.is_in(["L", "M", "R"])).then(hsh).otherwise(None).alias("hash"),
        pl.when(box > 0).then(box).otherwise(None).alias("box"),
        pl.col("is_play_action").cast(pl.Utf8).str.to_uppercase().eq("TRUE").alias("pa"),
        pl.col("is_screen_pass").cast(pl.Utf8).str.to_uppercase().eq("TRUE").alias("sc"),
    ]).unique(subset=["game_id", "play_id"], keep="first")


def _result(r: dict) -> str:
    if r.get("interception") == 1:
        return "int"
    if r.get("complete_pass") == 1:
        return "td" if r.get("pass_touchdown") == 1 else "catch"
    return "inc"


def team_plays(season: int, pbp: pl.DataFrame | None = None,
               ftn: pl.DataFrame | None = None) -> dict:
    """{TEAM: {season, team, weeks, cols, plays, rz_cols, redzone, names}}:
    every target the offence threw, and every touch inside the 20.
    `pbp` / `ftn` are injectable for the tests; the bot passes neither."""
    p = _pbp(season) if pbp is None else pbp
    f = _ftn(season) if ftn is None else ftn
    p = p.with_columns(pl.col("play_id").cast(pl.Float64))
    if "two_point_attempt" in p.columns:
        p = p.filter(pl.col("two_point_attempt").fill_null(0) != 1)
    tg = p.filter(pl.col("pass_attempt") == 1, pl.col("receiver_player_id").is_not_null(),
                  pl.col("posteam").is_not_null())
    if f.is_empty():
        tg = tg.with_columns(*[pl.lit(None).alias(c) for c in ("hash", "box", "pa", "sc")])
    else:
        tg = tg.join(f, on=["game_id", "play_id"], how="left")
    tg = tg.sort(["week", "game_id", "play_id"])
    ca = p.filter(pl.col("rush_attempt") == 1, pl.col("rusher_player_id").is_not_null(),
                  pl.col("posteam").is_not_null(), pl.col("yardline_100") <= 20) \
          .sort(["week", "game_id", "play_id"])

    out: dict = {}

    def team(t: str) -> dict:
        if t not in out:
            out[t] = {"season": season, "team": t, "weeks": set(), "cols": PLAY_COLS,
                      "plays": [], "rz_cols": RZ_COLS, "redzone": [], "names": {}}
        return out[t]

    for r in tg.iter_rows(named=True):
        o = team(r["posteam"])
        pid, wk = r["receiver_player_id"], int(r["week"])
        o["names"].setdefault(pid, r.get("receiver_player_name") or pid)
        o["weeks"].add(wk)
        res = _result(r)
        caught = res in ("catch", "td")
        yl = _num(r.get("yardline_100"))
        o["plays"].append([
            pid, wk, r.get("defteam"), _num(r.get("qtr")), _num(r.get("down")),
            _num(r.get("ydstogo")), yl, _LANE.get(r.get("pass_location")),
            _num(r.get("air_yards")),
            (_num(r.get("yards_after_catch")) or 0) if caught else 0,
            (_num(r.get("yards_gained")) or 0) if caught else 0,
            res, _num(r.get("epa"), 2), r.get("hash"), r.get("box"),
            1 if r.get("pa") else 0, 1 if r.get("sc") else 0,
        ])
        if yl is not None and yl <= 20:
            o["redzone"].append([pid, wk, yl, "pass", res])
    for r in ca.iter_rows(named=True):
        o = team(r["posteam"])
        pid, wk = r["rusher_player_id"], int(r["week"])
        o["names"].setdefault(pid, r.get("rusher_player_name") or pid)
        o["weeks"].add(wk)
        o["redzone"].append([pid, wk, _num(r.get("yardline_100")), "rush",
                             "td" if r.get("rush_touchdown") == 1 else "carry"])
    for o in out.values():
        o["weeks"] = sorted(o["weeks"], reverse=True)    # most recent first
        o["redzone"].sort(key=lambda x: (-x[1], x[2]))
    return out


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
