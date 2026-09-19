#!/usr/bin/env python3
"""nfl_role_dvp.py — defence-vs-ROLE, as a scoreable feature.

nfl_dvp.py already builds this table for the SITE: what a defence allows to the
WR1 role, the RB2 role, and so on, because "what this defence allows to wide
receivers" averages a WR1 and a fourth receiver into one number and is close to
useless. That table has never touched the score. The score's only matchup input
is team-level (f_opp_d_* in nfl_features.build), and the TD model's version of
it, opp_td_soft, was ZEROED on 2026-09-14 because removing it gained AUC in
both test seasons.

The hypothesis this file was built to test: opp_td_soft failed because it was
the wrong RESOLUTION, not because matchup is worthless.

MEASURED 2026-09-18, three seasons (2023/2024/2025), same harness SCORING.md
reports every other sweep with -- top-15 and top-30 hit rate per week plus AUC
over the whole pool:

    RUSH_ATT  <- carries allowed to his role     EARNS ITS SLOT.
              w=0.20: AUC +1.2 / +0.5 / +0.9, up in all three seasons and
              monotone to ~0.25 in two of them; t30 +1.4 / +0.3 / +0.7, up in
              all three. The one term in this sweep that improves every season
              on the two metrics that survive the ~2pt t15 noise floor.
    RUSH_YDS  <- rushing yards allowed to his role.  MARGINAL, NOT SHIPPED.
              w=0.18: AUC +0.69 / +0.06 / +0.45 -- 2024 is flat, and t15 goes
              backwards there. Left out under #28; revisit with a fourth
              season.
    REC       <- receptions allowed to his role.  MARGINAL, NOT SHIPPED.
              AUC +0.11 / +0.16 / +0.04 at w=0.05. Inside noise.
    REC_YDS   <- receiving yards allowed to his role.  NO.
              Flat, then decays. Receiving yards are dominated by the player's
              own target share; the defence barely moves the ranking.
    TD        <- TDs allowed to his role.  NO, same answer as opp_td_soft.
              AUC +0.2ish, t15 down in 2023. The feature is a count of a rare
              event over three or four games -- median 0 -- so it has almost no
              resolution to rank with. Role-awareness did not rescue it.

So exactly one term ships, in the market whose outcome is a stable volume
count. That is the honest read of the sweep, not a disappointment: rushing
volume against a soft front is a real and repeatable quantity, and touchdowns
are not.

WHY THIS IS NOT LEAKY, which is the whole design:

  ROLE is assigned off TRAILING usage only -- the role he carried INTO the
  game, never his usage in it. Ranking a team's backs by carries IN the game
  would be backwards: a true RB1 who gets stuffed for three carries would be
  scored as an RB3, so the defence would get credit for stopping an RB3 it
  actually stopped an RB1. Same rule nfl_dvp.py's header sets out.

  ALLOWED is summed per (defence, role, week) and then put through the same
  _roll() as every other feature -- trailing mean over the previous FORM_W
  weeks, never including week w.

EARLY SEASON is handled by the gate in nfl_scoring.derive(), not here: the term
is multiplied by min(role_games / 3, 1), so a defence with one role-game on
record cannot select anybody. Week 1 has no window at all, the column is absent,
and nfl_bot drops the constant component -- the score reverts to its other
terms rather than pretending to know a matchup.
"""
from __future__ import annotations
import polars as pl

from nfl_features import _roll

# How deep each position group is charted before everyone else falls into one
# bucket. WR1-3 then "WR4" for the rest, TE1-2 then TE3, RB1-2 then RB3 --
# nfl_dvp.py's ROLE_ORDER in feature form.
DEPTH = {"WR": 3, "TE": 2, "RB": 2}


def assign_roles(wk: pl.DataFrame, form: pl.DataFrame) -> pl.DataFrame:
    """player_id, week, role -- from TRAILING usage, never this week's.

    `form` is _roll()'s output, so f_targets / f_carries are already the
    previous weeks' averages. A player with no window yet ranks on 0 and lands
    in the deep bucket, which is the right default for someone the season has
    not seen play.
    """
    have = [c for c in ("f_targets", "f_carries") if c in form.columns]
    base = wk.select(["player_id", "week", "team", "position"]).join(
        form.select(["player_id", "week", *have]), on=["player_id", "week"], how="left")
    for c in ("f_targets", "f_carries"):
        if c not in base.columns:
            base = base.with_columns(pl.lit(0.0).alias(c))
    base = base.filter(pl.col("position").is_in(list(DEPTH) + ["QB"])).with_columns([
        pl.col("f_targets").fill_null(0.0), pl.col("f_carries").fill_null(0.0),
    ])
    base = base.with_columns(
        pl.when(pl.col("position") == "RB").then(pl.col("f_carries"))
          .otherwise(pl.col("f_targets")).alias("_usage"))
    base = base.with_columns(
        pl.col("_usage").rank("ordinal", descending=True)
          .over(["team", "week", "position"]).alias("_depth"))
    return base.with_columns(
        pl.when(pl.col("position") == "QB").then(pl.lit("QB"))
          .otherwise(pl.col("position") + pl.min_horizontal(
              pl.col("_depth"),
              pl.col("position").replace_strict(DEPTH, default=1) + 1,
          ).cast(pl.Utf8)).alias("role")
    ).select(["player_id", "week", "role"])


def role_allowed(wk: pl.DataFrame, roles: pl.DataFrame,
                 weeks: list[int] | None = None) -> pl.DataFrame:
    """f_oppr_* per (opponent_team, role, week): what that defence has allowed
    to that role, trailing. `weeks` forwards to _roll for the week you are
    about to price, which has no rows of its own yet."""
    cols = ["dr_td", "dr_rec_yds", "dr_rush_yds", "dr_rec", "dr_car"]
    d = wk.join(roles, on=["player_id", "week"], how="inner").group_by(
        ["opponent_team", "role", "week"]).agg(
        (pl.col("receiving_tds").sum() + pl.col("rushing_tds").sum()).alias("dr_td"),
        pl.col("receiving_yards").sum().alias("dr_rec_yds"),
        pl.col("rushing_yards").sum().alias("dr_rush_yds"),
        pl.col("receptions").sum().alias("dr_rec"),
        pl.col("carries").sum().alias("dr_car"),
    ).with_columns((pl.col("opponent_team") + pl.lit("|") + pl.col("role")).alias("tr"))
    if d.height == 0:
        return d.head(0)
    rolled = _roll(d.select(["tr", "week", *cols]), cols, by="tr",
                   prefix="f_oppr_", gp="f_oppr_gp", weeks=weeks)
    if rolled.height == 0:
        return rolled
    return rolled.with_columns(
        pl.col("tr").str.split("|").list.get(0).alias("opponent_team"),
        pl.col("tr").str.split("|").list.get(1).alias("role"),
    ).drop("tr")
