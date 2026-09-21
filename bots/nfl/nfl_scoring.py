#!/usr/bin/env python3
"""nfl_scoring.py — the seven market scores.

Every score is a 0–100 weighted composite. Each component is percentile-ranked
inside that week's eligible pool BEFORE weighting, so:

  * weights mean what they say (a 0.30 is 30% of the score, always)
  * no component can dominate just because it has a bigger raw scale
  * the score is a ranking instrument, not a probability — it answers
    "who's most likely", never "how likely"

Same shape as the MLB hr_score. Component names are the public vocabulary:
whatever appears here is what the site's signal pills and the Guide tab say.
"""
from __future__ import annotations
import polars as pl

# market -> (label, eligible positions, bar, {component: weight})
# `-name` on a component means invert it: lower raw value scores higher.
MODELS = {
    "TD": {
        "label": "Anytime TD", "pos": ["RB", "WR", "TE"], "bar": 1,
        # REWEIGHTED 2026-09-13 (snap share + touch volume added, from the
        # residual scan in nfl_td_lab.py), then CANDIDATE C 2026-09-14: the
        # one-at-a-time sweep found td_regression and opp_td_soft hurting in
        # BOTH seasons and f_gl_opp over-weighted; C zeroes the first two,
        # halves the third, renormalises. True holdout, tuned on one season
        # and reported on the other, both directions: t15 50.0→51.1 /
        # 51.1→55.2, t30 45.7→46.7 / 44.4→48.1, AUC .7252→.7341 / .7117→.7223.
        # Beats live on all six. Noise floor is ~2 pts t15, so t30 + AUC are
        # the numbers that decided it — see SCORING.md and
        # claude/tuddy-td-weight-sweep-holdout-2026-09-13.md. nfl_td_v2.
        "w": {
            "f_rz_opp":       0.2222,   # all red-zone touches
            "implied_total":  0.1830,   # how many points his team is expected to score
            "f_touches":      0.1569,   # targets + carries — he gets the ball at all
            "f_xtd":          0.1569,   # expected TDs from field position
            "f_gl_opp":       0.1503,   # inside-10 targets + inside-5 carries
            "f_snap_pct":     0.1307,   # share of snaps — the opportunity denominator
            # RETIRED by measurement (not by hunch), 2026-09-14:
            #   opp_td_soft    zeroing gained AUC in both seasons
            #   td_regression  removing it improved all six numbers; raising
            #                  it hurt monotonically in both seasons
            # derive() still computes both; the modal can show them as context.
        },
    },
    # ── volume markets ────────────────────────────────────────────────────────
    # Context (implied total, matchup, script) is capped hard here and absent
    # in places. Ranking a 200-man pool by team context floats scrubs on good
    # offenses into the top 15 — context modulates, it must never select.
    "REC_YDS": {
        "label": "Receiving yards", "pos": ["WR", "TE", "RB"], "bar": 40,
        "w": {
            "f_wopr":                 0.42,   # target share + air yards share
            "f_receiving_yards":      0.36,
            "f_receiving_air_yards":  0.12,   # depth of target
            "implied_total":          0.06,
            "opp_pass_soft":          0.04,
        },
    },
    "REC": {
        "label": "Receptions", "pos": ["WR", "TE", "RB"], "bar": 4,
        "w": {
            "f_target_share":  0.50,
            "f_receptions":    0.35,
            "f_targets":       0.15,
        },
    },
    "RUSH_YDS": {
        "label": "Rushing yards", "pos": ["RB", "QB"], "bar": 50,
        "w": {
            "f_carries":        0.65,
            "f_rushing_yards":  0.20,
            "f_rz_car":         0.15,   # goal-line role = he stays on the field
        },
    },
    # The one place the NGS layer earned a slot. Separation, cushion, YAC-over-
    # expected and box counts were all tested across every market and every one
    # of them made things worse except RYOE here — so that's the only one in.
    "RUSH_ATT": {
        "label": "Rushing attempts", "pos": ["RB"], "bar": 12,
        # MATCHUP EARNED A SLOT HERE, and only here (2026-09-18). The score's
        # only defensive input had been team-level, and the TD model's version
        # of it was zeroed in September for hurting. The role-aware version --
        # carries allowed to HIS depth role, not to running backs in general --
        # was swept 0 to 0.32 across 2023/2024/2025: AUC +1.2 / +0.5 / +0.9 at
        # 0.20 and up in all three seasons, t30 +1.4 / +0.3 / +0.7, also up in
        # all three. RUSH_YDS and REC were positive but not in every season and
        # are NOT shipped; TD and REC_YDS were flat or worse. See
        # nfl_role_dvp.py for the whole table and why this is the one market
        # where it works. nfl_rush_att_v2.
        "w": {
            "f_carries":                              0.52,
            "f_rz_car":                               0.16,
            "f_ngs_rush_yards_over_expected_per_att": 0.12,
            "oppr_rush_soft":                         0.20,
        },
    },
    # QBs are the exception: the pool is 32 starters, all of whom have volume,
    # so context IS selective here rather than diluting.
    "PASS_YDS": {
        "label": "Passing yards", "pos": ["QB"], "bar": 225,
        "w": {
            "total_line":       0.26,   # shootout environment
            "opp_pass_soft":    0.22,
            "f_passing_yards":  0.22,
            "f_attempts":       0.18,
            "f_passing_cpoe":   0.12,
        },
    },
    "KICK_PTS": {
        "label": "Kicking points", "pos": ["K"], "bar": 6,
        "w": {
            "implied_total":       0.35,   # the offense has to move the ball
            "f_tm_fg_drive_rate":  0.25,   # ...and then stall
            "-f_tm_rz_td_rate":    0.15,   # teams that DON'T punch it in kick more
            "f_fg_att":            0.10,
            "kick_env":            0.08,   # indoors / low wind
            "f_tm_drives":         0.07,
        },
    },
}

# what each market actually grades against
OUTCOME = {
    "TD":       pl.col("rushing_tds") + pl.col("receiving_tds"),
    "REC_YDS":  pl.col("receiving_yards"),
    "REC":      pl.col("receptions"),
    "RUSH_YDS": pl.col("rushing_yards"),
    "RUSH_ATT": pl.col("carries"),
    "PASS_YDS": pl.col("passing_yards"),
    "KICK_PTS": pl.col("fg_made") * 3 + pl.col("pat_made"),
}


def derive(df: pl.DataFrame) -> pl.DataFrame:
    """Composite inputs that aren't raw columns.

    MISSING-COLUMN SAFE, and that is load-bearing rather than defensive
    (2026-09-13). The week-1 table has no opponent-allowed roll — there are no
    prior weeks to average — so `f_opp_d_rec_td` simply does not exist, and a
    bare pl.col() raised. nfl_bot caught that and fell back to checking raw
    columns, which silently dropped EVERY derived component: td_regression and
    f_touches, whose inputs were all present and fine, went down with the one
    that was actually missing.

    An absent input reads as zero here, which makes its component a constant
    column — and nfl_bot drops constant columns, so the thing that is genuinely
    unavailable still falls out. The difference is that it falls out alone.
    """
    def c(name: str) -> pl.Expr:
        return pl.col(name) if name in df.columns else pl.lit(0.0)

    return df.with_columns(
        # softness of the defense he faces (more allowed = better matchup)
        (c("f_opp_d_rec_td").fill_null(0) + c("f_opp_d_rush_td").fill_null(0)).alias("opp_td_soft"),
        c("f_opp_d_pass_yds").fill_null(0).alias("opp_pass_soft"),
        c("f_opp_d_rush_yds").fill_null(0).alias("opp_rush_soft"),
        # ROLE-AWARE SOFTNESS, GATED. Carries this defence has allowed to the
        # role he actually occupies, damped while the trailing window is thin:
        # full weight at three role-games, proportionally less below, zero at
        # none. A defence that has faced one RB1 cannot select anybody, and in
        # week 1 the column does not exist at all -- c() reads that as 0, the
        # component goes constant, and nfl_bot drops it, so the score falls
        # back to its other terms instead of pricing a matchup it cannot see.
        (c("f_oppr_dr_car").fill_null(0)
         * pl.min_horizontal(c("f_oppr_gp").fill_null(0) / 3.0, pl.lit(1.0))
         ).alias("oppr_rush_soft"),
        # regression: expected TDs above what he's actually scored = due
        (c("f_xtd").fill_null(0) - c("f_td_actual").fill_null(0)).alias("td_regression"),
        # game script. negative spread = underdog = pass volume; positive = run volume
        (-c("spread")).fill_null(0).alias("pass_script"),
        c("spread").fill_null(0).alias("run_script"),
        (c("f_receptions").fill_null(0) / c("f_targets").fill_null(0).clip(0.5)).alias("catch_rate"),
        # TOUCH VOLUME. The TD pool is running backs and receivers together, so
        # a targets-only term ranks every back last for a reason that has
        # nothing to do with touchdowns — measured, it cost 3-5 points of
        # top-15 hit rate in 2024. Carries + targets is the position-fair
        # version and is the one that shipped.
        (c("f_tgt_n").fill_null(0) + c("f_car_n").fill_null(0)).alias("f_touches"),
        # kicking environment: indoors is clean, wind is the enemy
        (c("indoors").fill_null(0) * 10 - c("wind_mph").fill_null(0)).alias("kick_env"),
    )


def _pctile(df: pl.DataFrame, col: str, invert: bool) -> pl.Expr:
    # NULL IS ZERO, INCLUDING FOR SNAP SHARE — and that was measured, not
    # assumed. The reasonable-sounding argument is that a missing snap row
    # means nflverse has not published one rather than that he did not play,
    # so the week's median would say "unknown" instead of "worst". Tried it:
    # it is WORSE in both seasons (top-15 2024 47.0% vs 51.1%, 2025 49.6% vs
    # 50.4%). A player with no snap row is a fringe player, so ranking him
    # last is not a slander, it is the correct prior — about 8% of the TD
    # pool, and they are the right 8% to bury.
    e = pl.col(col).fill_null(0)
    if invert:
        e = -e
    return (e.rank("average") / pl.len()).over("week")


def score(df: pl.DataFrame, market: str) -> pl.DataFrame:
    """Attach a 0–100 score plus every component's percentile, for the modal."""
    m = MODELS[market]
    d = derive(df).filter(pl.col("position").is_in(m["pos"]))
    parts, comps = [], []
    for raw, wgt in m["w"].items():
        invert = raw.startswith("-")
        col = raw.lstrip("-")
        if col not in d.columns:
            raise KeyError(f"{market}: component '{col}' not in feature table")
        name = f"c_{raw.lstrip('-')}" + ("_inv" if invert else "")
        d = d.with_columns(_pctile(d, col, invert).alias(name))
        parts.append(pl.col(name) * wgt)
        comps.append(name)
    total = parts[0]
    for p in parts[1:]:
        total = total + p
    return d.with_columns((total * 100).alias("score")).with_columns(
        [(pl.col(c) * 100).round(0).alias(c) for c in comps])


def weight_table(market: str) -> str:
    m = MODELS[market]
    rows = [f"{'':2}{r.lstrip('-'):<24}{w:>6.0%}{'  (inverted)' if r.startswith('-') else ''}"
            for r, w in m["w"].items()]
    return f"{m['label']}  —  bar {m['bar']}, positions {'/'.join(m['pos'])}\n" + "\n".join(rows)


# ── v1 markets: one real component, not yet weighted or backtested ─────────
#
# Donovan, 2026-09-21: "everyone ranked, just like MLB... and special teams
# and defense." Traced first (project rule #15): the seven MODELS above are
# weighted composites of engineered features, each swept against a holdout
# season before shipping (see SCORING.md). There is no equivalent feature
# table for a defense/special-teams unit -- nflverse's def_tds and
# special_teams_tds are already exactly the outcome being graded, not a
# leading indicator of it, so there is nothing to build a multi-component
# model FROM yet. Inventing weights over nothing would violate rule #16.
#
# So this ranks each team's own trailing rate of the exact thing it's
# grading -- one real, already-computed number (team_defense()'s
# def_touchdowns, built for FRANCHISE's D/ST scoring in nfl_bot.py, reused
# here rather than recomputed), not a projection. Explicitly v1: the site
# and SCORING.md both say so, and it does not get folded into MODELS until
# it has been swept the way TD and RUSH_ATT were.
V1_MODELS = {
    "DEF_TD": {"label": "Defense/ST TD", "pos": ["DEF"], "bar": 1},
}


def score_def_td(team_defense: dict, week: int | None = None) -> dict[str, dict]:
    """Percentile-rank every team by its real trailing def_touchdowns rate.

    `team_defense` is nfl_bot.team_defense()'s own output: {"per_game":
    {team: {...}}, "weeks": {"1": {team: {...}}, ...}}. Prefers this
    season's weeks-to-date average; a team with no games logged yet this
    season (bye, or too early in the year) falls back to last season's
    per_game rate rather than being scored on zero real games. `week` is
    accepted for a future per-week cutoff and unused today -- `weeks`
    already only contains games that have actually been played.

    Returns {team: {"score": 0-100, "rate": <real per-game rate>,
    "sample": "season"|"prior"}}, or {} if team_defense carried nothing
    (its own fetch failed) -- never a fabricated number.
    """
    per_game = team_defense.get("per_game") or {}
    weeks = team_defense.get("weeks") or {}
    teams = set(per_game) | {t for wk in weeks.values() for t in wk}
    if not teams:
        return {}

    rate, sample = {}, {}
    for team in teams:
        games = [wk[team]["def_touchdowns"] for wk in weeks.values() if team in wk]
        if games:
            rate[team] = sum(games) / len(games)
            sample[team] = "season"
        else:
            rate[team] = (per_game.get(team) or {}).get("def_touchdowns", 0.0)
            sample[team] = "prior"

    # Average-rank percentile, ties share the same percentile -- same idea as
    # _pctile()'s rank("average") / n above, done in plain Python because this
    # is 32 team-rows, not a polars feature table.
    ordered = sorted(rate.values())
    n = len(ordered)

    def pct_of(v: float) -> float:
        lo = ordered.index(v)
        hi = lo
        while hi + 1 < n and ordered[hi + 1] == v:
            hi += 1
        return ((lo + hi) / 2 + 1) / n

    return {
        team: {"score": round(pct_of(r) * 100), "rate": round(r, 3), "sample": sample[team]}
        for team, r in rate.items()
    }


if __name__ == "__main__":
    for k in MODELS:
        print(weight_table(k), "\n")
    print(f"{V1_MODELS['DEF_TD']['label']}  —  v1, single real component "
          f"(def_touchdowns rate), not yet weighted or backtested")
