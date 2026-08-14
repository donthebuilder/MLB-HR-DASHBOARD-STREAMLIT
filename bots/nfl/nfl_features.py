#!/usr/bin/env python3
"""nfl_features.py — the weekly feature table the seven models score off.

One row per player per week. Three families of input, kept strictly apart
so the backtest can't cheat:

  TRAILING  player form, opponent defense  -> built from weeks < w only
  PREGAME   spread, total, roof, wind      -> known before kickoff, so week w
  OUTCOME   the actual stat line           -> week w, used only for grading

Any feature that isn't provably one of those three doesn't go in.

Four things this closes that the first cut left open:
  1. INJURIES   Out/Doubtful are dropped, Questionable is flagged and damped
  2. NGS        separation, cushion, aDOT, YAC-over-expected, RYOE, box counts
  3. CARRYOVER  weeks 1-3 (and preseason) fall back to last season's baseline
  4. SEASONS    build_multi() stacks years so the report card isn't one sample
"""
from __future__ import annotations
import functools
import nflreadpy as nfl
import polars as pl

FORM_W = 4          # trailing window, in weeks
MIN_GP = 2          # games needed inside the window to score off form alone
QUESTIONABLE_DAMP = 0.85   # Q players keep 85% of their opportunity features


# ── game context ──────────────────────────────────────────────────────────────

def team_context(season: int) -> pl.DataFrame:
    """Per team per week: implied total, spread, venue. Known before kickoff.

    nflverse `spread_line` is from the HOME team's perspective (positive =
    home favored), so the away implied total flips the sign.
    """
    s = nfl.load_schedules().filter(pl.col("season") == season)
    keep = ["week", "spread_line", "total_line", "roof", "surface", "temp", "wind", "div_game"]
    home = s.select([*keep, pl.col("home_team").alias("team"), pl.col("away_team").alias("opp")]) \
            .with_columns(pl.lit(1).alias("is_home"), pl.col("spread_line").alias("spread"))
    away = s.select([*keep, pl.col("away_team").alias("team"), pl.col("home_team").alias("opp")]) \
            .with_columns(pl.lit(0).alias("is_home"), (-pl.col("spread_line")).alias("spread"))
    return pl.concat([home, away]).with_columns(
        (pl.col("total_line") / 2 + pl.col("spread") / 2).alias("implied_total"),
        pl.col("roof").is_in(["dome", "closed"]).cast(pl.Int8).alias("indoors"),
        pl.col("wind").fill_null(0).alias("wind_mph"),
    ).drop(["spread_line", "temp", "wind"])


# ── red-zone usage + expected TDs, from play-by-play ──────────────────────────

@functools.lru_cache(maxsize=6)
def _pbp(season: int) -> pl.DataFrame:
    return nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")


def td_curve(season: int) -> pl.DataFrame:
    """League TD rate per opportunity by 5-yard bucket — the xTD backbone."""
    p = _pbp(season)
    tgt = p.filter(pl.col("pass_attempt") == 1, pl.col("receiver_player_id").is_not_null()) \
           .select(pl.col("yardline_100"), pl.col("pass_touchdown").alias("td"))
    rush = p.filter(pl.col("rush_attempt") == 1, pl.col("rusher_player_id").is_not_null()) \
            .select(pl.col("yardline_100"), pl.col("rush_touchdown").alias("td"))
    out = {}
    for name, df in (("pass", tgt), ("rush", rush)):
        out[name] = df.with_columns((pl.col("yardline_100") // 5 * 5).alias("bkt")) \
                      .group_by("bkt").agg(pl.col("td").mean().alias(f"{name}_rate")).sort("bkt")
    return out["pass"].join(out["rush"], on="bkt", how="full", coalesce=True).sort("bkt")


def usage(season: int) -> pl.DataFrame:
    """Per player-week: red-zone volume and expected TDs."""
    p = _pbp(season)
    curve = td_curve(season)

    tgt = p.filter(pl.col("pass_attempt") == 1, pl.col("receiver_player_id").is_not_null()) \
           .select(pl.col("receiver_player_id").alias("player_id"), "week", "yardline_100",
                   pl.col("pass_touchdown").alias("td")) \
           .with_columns((pl.col("yardline_100") // 5 * 5).alias("bkt")) \
           .join(curve.select("bkt", "pass_rate"), on="bkt", how="left") \
           .group_by("player_id", "week").agg(
               pl.len().alias("tgt_n"),
               (pl.col("yardline_100") <= 20).sum().alias("rz_tgt"),
               (pl.col("yardline_100") <= 10).sum().alias("i10_tgt"),
               pl.col("pass_rate").fill_null(0).sum().alias("xtd_rec"),
               pl.col("td").sum().alias("td_rec"))

    rush = p.filter(pl.col("rush_attempt") == 1, pl.col("rusher_player_id").is_not_null()) \
            .select(pl.col("rusher_player_id").alias("player_id"), "week", "yardline_100",
                    pl.col("rush_touchdown").alias("td")) \
            .with_columns((pl.col("yardline_100") // 5 * 5).alias("bkt")) \
            .join(curve.select("bkt", "rush_rate"), on="bkt", how="left") \
            .group_by("player_id", "week").agg(
                pl.len().alias("car_n"),
                (pl.col("yardline_100") <= 20).sum().alias("rz_car"),
                (pl.col("yardline_100") <= 5).sum().alias("i5_car"),
                pl.col("rush_rate").fill_null(0).sum().alias("xtd_rush"),
                pl.col("td").sum().alias("td_rush"))

    u = tgt.join(rush, on=["player_id", "week"], how="full", coalesce=True)
    num = [c for c in u.columns if c not in ("player_id", "week")]
    return u.with_columns([pl.col(c).fill_null(0) for c in num]).with_columns(
        (pl.col("xtd_rec") + pl.col("xtd_rush")).alias("xtd"),
        (pl.col("td_rec") + pl.col("td_rush")).alias("td_actual"),
        (pl.col("rz_tgt") + pl.col("rz_car")).alias("rz_opp"),
        (pl.col("i10_tgt") + pl.col("i5_car")).alias("gl_opp"),
    )


def team_stall(season: int) -> pl.DataFrame:
    """Per team-week: drives, RZ trips, and how often a drive ended in a FG.

    The kicker signal. A team that moves the ball and *stalls* is a kicker's
    best week; a team that punches it in is his worst.
    """
    p = _pbp(season)
    dr = p.filter(pl.col("fixed_drive_result").is_not_null(), pl.col("posteam").is_not_null()) \
          .group_by("posteam", "week", "fixed_drive").agg(
              pl.col("fixed_drive_result").first().alias("res"),
              pl.col("drive_inside20").max().alias("in20"))
    return dr.group_by("posteam", "week").agg(
        pl.len().alias("drives"),
        (pl.col("res") == "Field goal").sum().alias("fg_drives"),
        (pl.col("res") == "Touchdown").sum().alias("td_drives"),
        pl.col("in20").fill_null(0).sum().alias("rz_trips"),
    ).rename({"posteam": "team"}).with_columns(
        (pl.col("fg_drives") / pl.col("drives").clip(1)).alias("fg_drive_rate"),
        (pl.col("td_drives") / pl.col("rz_trips").clip(1)).clip(0, 1).alias("rz_td_rate"),
    )


# ── Next Gen Stats — the closest thing football has to exit velocity ──────────

NGS_KEEP = {
    "receiving": ["avg_cushion", "avg_separation", "avg_intended_air_yards",
                  "percent_share_of_intended_air_yards", "avg_yac_above_expectation"],
    "rushing":   ["efficiency", "percent_attempts_gte_eight_defenders", "avg_time_to_los",
                  "rush_yards_over_expected_per_att", "rush_pct_over_expected"],
    "passing":   ["avg_time_to_throw", "aggressiveness", "avg_air_yards_to_sticks",
                  "completion_percentage_above_expectation", "max_air_distance"],
}


def ngs(season: int) -> pl.DataFrame:
    """Per player-week NGS, all three stat types merged on the player id."""
    out = None
    for kind, cols in NGS_KEEP.items():
        try:
            d = nfl.load_nextgen_stats(seasons=[season], stat_type=kind)
        except Exception:
            continue
        have = [c for c in cols if c in d.columns]
        if not have or "player_gsis_id" not in d.columns:
            continue
        d = d.filter(pl.col("week") > 0).select(
            [pl.col("player_gsis_id").alias("player_id"), "week", *have])
        d = d.rename({c: f"ngs_{c}" for c in have})
        out = d if out is None else out.join(d, on=["player_id", "week"], how="full", coalesce=True)
    return out if out is not None else pl.DataFrame(
        {"player_id": [], "week": []}, schema={"player_id": pl.Utf8, "week": pl.Int32})


# ── injuries ──────────────────────────────────────────────────────────────────

def injuries(season: int) -> pl.DataFrame:
    """Per player-week availability. `inj_out` is a hard drop, `inj_q` damps."""
    try:
        d = nfl.load_injuries(seasons=[season])
    except Exception:
        return pl.DataFrame({"player_id": [], "week": []},
                            schema={"player_id": pl.Utf8, "week": pl.Int32})
    if "gsis_id" not in d.columns:
        return pl.DataFrame({"player_id": [], "week": []},
                            schema={"player_id": pl.Utf8, "week": pl.Int32})
    st = pl.col("report_status").fill_null("")
    return d.select([pl.col("gsis_id").alias("player_id"), "week",
                     st.is_in(["Out", "Doubtful"]).cast(pl.Int8).alias("inj_out"),
                     (st == "Questionable").cast(pl.Int8).alias("inj_q")]) \
            .group_by("player_id", "week").agg(pl.col("inj_out").max(), pl.col("inj_q").max())


# ── assembly ──────────────────────────────────────────────────────────────────

def _roll(df: pl.DataFrame, cols: list[str], by: str = "player_id",
          prefix: str = "f_", gp: str = "f_gp") -> pl.DataFrame:
    """Trailing mean over the previous FORM_W weeks. Never includes week w."""
    out = []
    for w in sorted(df["week"].unique().to_list()):
        hist = df.filter(pl.col("week").is_between(w - FORM_W, w - 1))
        if hist.height == 0:
            continue
        out.append(hist.group_by(by).agg(
            [pl.col(c).mean().alias(f"{prefix}{c}") for c in cols] + [pl.len().alias(gp)]
        ).with_columns(pl.lit(w).cast(pl.Int32).alias("week")))
    return pl.concat(out) if out else df.head(0)


PLAYER_FORM = ["target_share", "air_yards_share", "wopr", "receptions", "targets",
               "receiving_yards", "receiving_tds", "receiving_air_yards", "receiving_20",
               "receiving_40", "carries", "rushing_yards", "rushing_tds", "rushing_20",
               "passing_yards", "passing_tds", "attempts", "completions", "passing_cpoe",
               "fg_made", "pat_made", "fg_att"]
USAGE_FORM = ["rz_opp", "gl_opp", "rz_tgt", "rz_car", "xtd", "td_actual", "tgt_n", "car_n"]


def season_baseline(season: int) -> pl.DataFrame:
    """Per-player per-game averages for a whole season — the carryover source.

    Used to fill weeks 1-3 (and preseason), where no trailing window exists.
    Columns are named the same as _roll's output so they drop straight in.
    """
    wk = nfl.load_player_stats(seasons=[season], summary_level="week") \
            .filter(pl.col("season_type") == "REG")
    u = usage(season)
    wk = wk.join(u, on=["player_id", "week"], how="left")
    cols = [c for c in PLAYER_FORM + USAGE_FORM if c in wk.columns]
    wk = wk.with_columns([pl.col(c).fill_null(0) for c in cols])
    return wk.group_by("player_id").agg(
        [pl.col(c).mean().alias(f"b_{c}") for c in cols] + [pl.len().alias("b_gp")]
    ).filter(pl.col("b_gp") >= 4)


def build(season: int, carryover: bool = True) -> pl.DataFrame:
    """The scoreable table for one season."""
    wk = nfl.load_player_stats(seasons=[season], summary_level="week") \
            .filter(pl.col("season_type") == "REG")
    for c in PLAYER_FORM:
        if c in wk.columns:
            wk = wk.with_columns(pl.col(c).fill_null(0))

    u = usage(season)
    wk = wk.join(u, on=["player_id", "week"], how="left").with_columns(
        [pl.col(c).fill_null(0) for c in USAGE_FORM if c in u.columns])

    # NGS is a WEEK-w measurement built from week-w plays, so joining it at w
    # would leak the outcome straight into the features. It goes through the
    # same trailing roll as everything else and is only ever read as history.
    ng = ngs(season)
    ngs_cols = [c for c in ng.columns if c.startswith("ngs_")]
    if ngs_cols:
        wk = wk.join(ng, on=["player_id", "week"], how="left")

    form_cols = [c for c in PLAYER_FORM + USAGE_FORM if c in wk.columns] + ngs_cols
    form = _roll(wk.select(["player_id", "week", *form_cols]), form_cols)

    stall = team_stall(season)
    sform = _roll(stall.select(["team", "week", "fg_drive_rate", "rz_td_rate", "drives"]),
                  ["fg_drive_rate", "rz_td_rate", "drives"], by="team",
                  prefix="f_tm_", gp="f_tm_gp")

    dallow = wk.group_by("opponent_team", "week").agg(
        pl.col("receiving_tds").sum().alias("d_rec_td"),
        pl.col("rushing_tds").sum().alias("d_rush_td"),
        pl.col("receiving_yards").sum().alias("d_pass_yds"),
        pl.col("rushing_yards").sum().alias("d_rush_yds"),
    ).rename({"opponent_team": "team"})
    dform = _roll(dallow, ["d_rec_td", "d_rush_td", "d_pass_yds", "d_rush_yds"], by="team",
                  prefix="f_opp_", gp="f_opp_gp")

    out = (wk
           .join(form, on=["player_id", "week"], how="left")
           .join(team_context(season), on=["team", "week"], how="left")
           .join(sform, on=["team", "week"], how="left")
           .join(dform, left_on=["opponent_team", "week"], right_on=["team", "week"], how="left")
           .join(injuries(season), on=["player_id", "week"], how="left"))

    out = out.with_columns(pl.col("f_gp").fill_null(0),
                           pl.col("inj_out").fill_null(0), pl.col("inj_q").fill_null(0))

    # CARRYOVER — early weeks have no trailing window, so lean on last season.
    if carryover:
        try:
            base = season_baseline(season - 1)
            out = out.join(base, on="player_id", how="left")
            for c in form_cols:
                f, b = f"f_{c}", f"b_{c}"
                if f in out.columns and b in out.columns:
                    out = out.with_columns(
                        pl.when(pl.col("f_gp") >= MIN_GP).then(pl.col(f))
                          .otherwise(pl.col(b)).alias(f))
            out = out.with_columns(
                (pl.col("f_gp") < MIN_GP).cast(pl.Int8).alias("is_carryover"),
                pl.max_horizontal(pl.col("f_gp"), pl.col("b_gp").fill_null(0)).alias("f_gp"))
        except Exception:
            out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("is_carryover"))
    else:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("is_carryover"))

    # INJURY GATE: Out/Doubtful never score. Questionable is damped, not dropped —
    # a Q player who plays is exactly the guy the market misprices.
    out = out.filter(pl.col("inj_out") == 0)
    damp = [f"f_{c}" for c in ("rz_opp", "gl_opp", "carries", "targets", "target_share", "wopr")
            if f"f_{c}" in out.columns]
    if damp:
        out = out.with_columns([
            pl.when(pl.col("inj_q") == 1).then(pl.col(c) * QUESTIONABLE_DAMP)
              .otherwise(pl.col(c)).alias(c) for c in damp])

    return out.filter(pl.col("f_gp") >= MIN_GP).with_columns(pl.lit(season).alias("season_yr"))


def build_multi(seasons: list[int]) -> pl.DataFrame:
    """Stack seasons so the report card isn't a single sample."""
    frames = [build(s) for s in seasons]
    cols = set(frames[0].columns)
    for f in frames[1:]:
        cols &= set(f.columns)
    keep = sorted(cols)
    return pl.concat([f.select(keep) for f in frames], how="vertical_relaxed")


if __name__ == "__main__":
    t = build(2025)
    print("rows:", t.height, "cols:", len(t.columns))
    print("weeks:", t["week"].min(), "-", t["week"].max())
    print("carryover rows:", int(t["is_carryover"].sum()))
    print("questionable:", int(t["inj_q"].sum()))
    print("ngs cols:", [c for c in t.columns if c.startswith("ngs_")])
