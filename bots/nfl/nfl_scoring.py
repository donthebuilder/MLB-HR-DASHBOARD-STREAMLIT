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
        "w": {
            "f_gl_opp":       0.30,   # inside-10 targets + inside-5 carries
            "f_rz_opp":       0.22,   # all red-zone touches
            "implied_total":  0.18,   # how many points his team is expected to score
            "f_xtd":          0.15,   # expected TDs from field position
            "opp_td_soft":    0.08,   # defense that gives up TDs
            "td_regression":  0.07,   # xTD minus actual — buy the cold guy
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
        "w": {
            "f_carries":                              0.65,
            "f_rz_car":                               0.20,
            "f_ngs_rush_yards_over_expected_per_att": 0.15,
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
    """Composite inputs that aren't raw columns."""
    return df.with_columns(
        # softness of the defense he faces (more allowed = better matchup)
        (pl.col("f_opp_d_rec_td").fill_null(0) + pl.col("f_opp_d_rush_td").fill_null(0)).alias("opp_td_soft"),
        pl.col("f_opp_d_pass_yds").fill_null(0).alias("opp_pass_soft"),
        pl.col("f_opp_d_rush_yds").fill_null(0).alias("opp_rush_soft"),
        # regression: expected TDs above what he's actually scored = due
        (pl.col("f_xtd") - pl.col("f_td_actual")).alias("td_regression"),
        # game script. negative spread = underdog = pass volume; positive = run volume
        (-pl.col("spread")).fill_null(0).alias("pass_script"),
        pl.col("spread").fill_null(0).alias("run_script"),
        (pl.col("f_receptions") / pl.col("f_targets").clip(0.5)).alias("catch_rate"),
        # kicking environment: indoors is clean, wind is the enemy
        (pl.col("indoors").fill_null(0) * 10 - pl.col("wind_mph").fill_null(0)).alias("kick_env"),
    )


def _pctile(df: pl.DataFrame, col: str, invert: bool) -> pl.Expr:
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


if __name__ == "__main__":
    for k in MODELS:
        print(weight_table(k), "\n")
