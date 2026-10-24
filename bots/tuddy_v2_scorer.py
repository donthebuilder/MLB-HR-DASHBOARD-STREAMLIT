"""
TUDDY v2 — NFL Anytime TD Scorer Model
Bot job: bots/tuddy_v2_scorer.py
Runs nightly (or weekly before games). Writes to data branch: public/data/current/tuddy_v2_latest.json

WHAT THIS UPGRADES FROM v1:
  v1 (bots/nfl_scoring.py): has team-level context (gl_opp, rz_opp, implied_total, xtd,
    td_soft, regression) with c_<name> components 0–100. Already runs.
  v2 adds the two missing angles:
    1. USAGE TREND — snap count / target share / carry share going UP or DOWN week over week
    2. MATCHUP BY POSITION — RB vs WR vs TE vs specific defensive lane. Not team-level. Lane-level.

SHARED FRAMEWORK (same across all four DASH sports):
  FINAL SCORE = SCORER SCORE × MATCHUP SCORE
  All TDs equal: QB sneak = RB plunge = WR fade. Same grade, same credit.
  Usage going UP = boost. Going DOWN = downgrade even if season stats look good.
  Game script: light nudge (tiebreaker), not a primary signal.
  Model version on every output. Records never rewritten.
  Shadow test 2 weeks alongside v1 before promoting.
"""

import json
import logging
from datetime import datetime, date
from typing import Optional

try:
    from bots.dash_shared import (
        normalize, scale, trend_slope, final_score, write_json_output,
    )
except ImportError:  # run standalone from inside bots/
    from dash_shared import (
        normalize, scale, trend_slope, final_score, write_json_output,
    )

log = logging.getLogger("tuddy_v2")

# nfl_td_v2 is the live TD score (nfl_registry.py) and nfl_td_v3 is its
# shadow, so this usage-trend x lane model takes the next free number.
MODEL_VERSION = "nfl_td_v4"


# ---------------------------------------------------------------------------
# SCORER SCORE — who has the opportunity right now
# ---------------------------------------------------------------------------
# Points breakdown (sums to 100 max):
#   35  red zone touches last 5 games      (carries + targets inside 20)
#   25  usage trend (snap/target/carry trend, week over week)
#   20  goal line touches last 5 games     (inside 5 yard line)
#   15  TD% on red zone opportunities
#    5  role stability (is this the same role they had 3 weeks ago?)

def usage_trend_score(
    snap_pcts: list,          # list of weekly snap%: [0.72, 0.68, 0.74, 0.78, 0.81] most recent last
    target_shares: list,      # list of weekly target share (WR/TE) or carry_shares (RB)
    position: str,            # "QB", "RB", "WR", "TE"
) -> dict:
    """
    Score 0–25 for usage trend.
    Rising = boost. Flat = neutral. Falling = penalty.

    Uses last 5 weeks. Slope of the trend is the signal — not just the average.
    A player at 60% snap but trending from 50%→70% over 5 weeks is ranked higher
    than a player steady at 75%.
    """
    if not snap_pcts or len(snap_pcts) < 2:
        return {"usage_trend_score": 12.5, "usage_trend_dir": "unknown", "usage_slope": 0.0}

    # Use last 5 weeks
    snaps = snap_pcts[-5:]
    shares = (target_shares or [])[-5:] if target_shares else []

    mean_y = sum(snaps) / len(snaps)
    slope = trend_slope(snaps)["slope"]   # change in snap% per week

    # Also check final vs first
    delta = snaps[-1] - snaps[0]

    if slope > 0.02 and delta > 0.05:
        trend = "rising"
        score = min(25.0, 15.0 + slope * 200)   # rising hard = up to 25
    elif slope < -0.02 and delta < -0.05:
        trend = "falling"
        score = max(0.0, 12.5 + slope * 200)    # falling = below neutral
    else:
        trend = "flat"
        # Score based on absolute level (flat at high snap% is still good)
        avg = mean_y
        score = min(20.0, avg * 25)

    return {
        "usage_trend_score": round(score, 2),
        "usage_trend_dir":   trend,
        "usage_slope":       round(slope, 4),
        "snap_pct_last":     snaps[-1],
        "snap_pct_first":    snaps[0],
    }


def red_zone_touches_score(rz_touches_last5: list) -> float:
    """
    Score 0–35 based on red zone touches (carries + targets inside 20) last 5 games.
    0 touches avg = 0, 4+ avg = 35.

    rz_touches_last5: list of ints, one per game, last 5 games.
    """
    if not rz_touches_last5:
        return 0.0
    avg = sum(rz_touches_last5) / len(rz_touches_last5)
    return round(scale(avg, 0.0, 4.0, 0.0, 35.0), 2)


def goal_line_touches_score(gl_touches_last5: list) -> float:
    """
    Score 0–20 based on carries/targets inside the 5 yard line last 5 games.
    0 = 0, 2+ avg = 20.
    """
    if not gl_touches_last5:
        return 0.0
    avg = sum(gl_touches_last5) / len(gl_touches_last5)
    return round(scale(avg, 0.0, 2.0, 0.0, 20.0), 2)


def td_conversion_rate_score(rz_td_pct: float) -> float:
    """
    Score 0–15 from TD conversion rate on red zone opportunities.
    0% = 0, 40%+ = 15.
    """
    return round(scale(rz_td_pct, 0.0, 0.40, 0.0, 15.0), 2)


def role_stability_score(role_weeks_consistent: int) -> float:
    """
    Score 0–5: has this player held the same primary role for 3+ weeks?
    Protects against guys who just got promoted due to injury.
    """
    if role_weeks_consistent >= 3:
        return 5.0
    return round(role_weeks_consistent / 3.0 * 5, 2)


def scorer_score_tuddy(
    rz_touches_last5: list,
    gl_touches_last5: list,
    rz_td_pct: float,
    snap_pcts: list,
    target_shares: list,
    position: str,
    role_weeks_consistent: int,
) -> dict:
    """Compute full scorer score (0–100) for a TUDDY player."""
    rz   = red_zone_touches_score(rz_touches_last5)
    gl   = goal_line_touches_score(gl_touches_last5)
    tdp  = td_conversion_rate_score(rz_td_pct)
    role = role_stability_score(role_weeks_consistent)
    ut   = usage_trend_score(snap_pcts, target_shares, position)

    total = rz + ut["usage_trend_score"] + gl + tdp + role
    total = min(100.0, total)

    return {
        "scorer_score":        round(total, 2),
        "rz_touches_pts":      rz,
        "usage_trend_pts":     ut["usage_trend_score"],
        "usage_trend_dir":     ut["usage_trend_dir"],
        "usage_slope":         ut["usage_slope"],
        "gl_touches_pts":      gl,
        "td_pct_pts":          tdp,
        "role_stability_pts":  role,
    }


# ---------------------------------------------------------------------------
# MATCHUP SCORE — position-specific defense, not team-level
# ---------------------------------------------------------------------------
# Multiplier 0.5–1.5:
#   TD allowed by this position (RB, WR, TE — separate)   (HIGH)
#   Red zone TD% allowed by receiver depth / lane          (HIGH)
#   Rush TD at goal line allowed                           (MEDIUM, for RB/QB)
#   Defensive ranking at this position's specific lane     (MEDIUM)
#   Game script nudge (game total ±5%)                     (LOW)

def position_matchup_multiplier(
    position: str,              # "QB", "RB", "WR", "TE"
    def_td_rate_vs_pos: float,  # TDs allowed per game vs this position (last 10 games)
    def_rz_td_pct_vs_pos: float,# Red zone TD% allowed to this position
    lane: Optional[str],        # "slot", "outside", "in_line", "backfield" — None if unknown
    def_lane_td_rate: Optional[float],  # TDs allowed in this lane, if known
    game_total: float,          # Over/under for tonight's game
    is_goal_line_specialist: bool = False,  # For RBs/QBs who get the sneaks
) -> dict:
    """
    Returns position-specific matchup multiplier (0.5–1.5).

    KEY UPGRADE FROM v1: This is lane-level, not team-level.
    "DEF vs slot WR" is different from "DEF vs outside WR."
    PropFinder's exact named feature. Now in DASH.
    """
    # Normalize TD rate vs position: league average ~0.5 TDs/game
    td_norm = normalize(def_td_rate_vs_pos, 0, 1.2)

    # Normalize red zone conversion rate: league avg ~50%
    rz_norm = normalize(def_rz_td_pct_vs_pos, 0, 0.80)

    # Lane-specific signal (bonus if we have it)
    if lane and def_lane_td_rate is not None:
        lane_norm = normalize(def_lane_td_rate, 0, 1.0)
        use_lane = True
    else:
        lane_norm = 0.5  # neutral
        use_lane = False

    # Game script: light nudge. High total = more TDs for skill guys.
    # 44 = neutral, 50+ = small boost
    game_script_nudge = 1.0 + max(-0.05, min(0.05, (game_total - 44) / 120))

    # Goal line specialist boost for RBs/QBs in obvious goal-line packages
    gl_boost = 1.05 if is_goal_line_specialist else 1.0

    # Weighted composite
    if use_lane:
        raw = td_norm * 0.30 + rz_norm * 0.30 + lane_norm * 0.25 + 0.15
    else:
        raw = td_norm * 0.45 + rz_norm * 0.40 + 0.15

    multiplier = 0.5 + raw            # 0.5–1.5 range
    multiplier *= game_script_nudge   # ±5%
    multiplier *= gl_boost
    multiplier = max(0.5, min(1.5, multiplier))

    return {
        "matchup_multiplier":        round(multiplier, 3),
        "def_td_rate_vs_pos_norm":   round(td_norm, 3),
        "def_rz_td_pct_norm":        round(rz_norm, 3),
        "lane_norm":                 round(lane_norm, 3) if use_lane else None,
        "lane_used":                 use_lane,
        "game_script_nudge":         round(game_script_nudge, 4),
        "gl_specialist_boost":       gl_boost,
        "position":                  position,
        "lane":                      lane,
    }


# ---------------------------------------------------------------------------
# EXPLOSIVE PLAY ANGLE — discussed and spec'd, now in the model
# ---------------------------------------------------------------------------

def explosive_play_angle(
    player_explosive_play_rate: float,   # player's rate of 15+ yard gains, last 5 games
    def_explosive_play_rate_allowed: float,  # defense's rate of giving up 15+ yard gains
) -> float:
    """
    Returns a bonus multiplier (1.0–1.15).
    Defense gives up explosive plays + player IS the explosive play guy = the angle.
    Used as an overlay on the matchup multiplier, not a standalone component.
    """
    if player_explosive_play_rate >= 0.15 and def_explosive_play_rate_allowed >= 0.15:
        # Both high: real angle. Scale the overlap.
        overlap = (player_explosive_play_rate + def_explosive_play_rate_allowed) / 2
        return round(1.0 + min(0.15, overlap * 0.5), 4)
    return 1.0


# ---------------------------------------------------------------------------
# FINAL SCORE
# ---------------------------------------------------------------------------

def final_score_tuddy(
    scorer: dict,
    matchup: dict,
    explosive_multiplier: float = 1.0,
    model_version: str = MODEL_VERSION,
) -> dict:
    """FINAL SCORE = SCORER SCORE × MATCHUP MULTIPLIER × EXPLOSIVE OVERLAY."""
    final = final_score(
        scorer["scorer_score"],
        matchup["matchup_multiplier"] * explosive_multiplier,
    )

    return {
        "final_score":             final,
        "scorer_score":            scorer["scorer_score"],
        "matchup_multiplier":      matchup["matchup_multiplier"],
        "explosive_multiplier":    explosive_multiplier,
        "model_version":           model_version,
        **{k: v for k, v in scorer.items() if k != "scorer_score"},
        **{k: v for k, v in matchup.items() if k != "matchup_multiplier"},
    }


# ---------------------------------------------------------------------------
# OUTPUT ROW
# ---------------------------------------------------------------------------

def build_output_row_tuddy(
    player_id: int,
    name: str,
    team: str,
    position: str,
    lane: Optional[str],
    opponent: str,
    game_pk: int,
    game_total: float,
    scorer_inputs: dict,
    matchup_inputs: dict,
    is_goal_line_specialist: bool,
    game_date: str,
) -> dict:
    sc = scorer_score_tuddy(**scorer_inputs, position=position)
    mt = position_matchup_multiplier(
        position=position,
        lane=lane,
        is_goal_line_specialist=is_goal_line_specialist,
        game_total=game_total,
        **matchup_inputs,
    )
    exp = explosive_play_angle(
        player_explosive_play_rate=scorer_inputs.get("player_explosive_rate", 0.0),
        def_explosive_play_rate_allowed=matchup_inputs.get("def_explosive_rate", 0.0),
    )
    result = final_score_tuddy(sc, mt, exp)

    result.update({
        "player_id":   player_id,
        "name":        name,
        "team":        team,
        "position":    position,
        "lane":        lane,
        "opponent":    opponent,
        "game_pk":     game_pk,
        "game_total":  game_total,
        "game_date":   game_date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })
    return result


LIVE_OUTPUT_FILE = "tuddy_v2_latest.json"


def write_output(rows: list, output_path: str, shadow: bool = False) -> str:
    return write_json_output(
        rows, output_path, model_version=MODEL_VERSION, sport="nfl", shadow=shadow,
    )


# ---------------------------------------------------------------------------
# MISSING DATA STILL NEEDED FROM THE BOT (NFL)
# ---------------------------------------------------------------------------
#
# 1. Lane data (slot vs outside vs in-line TE vs backfield) —
#    NFL Next Gen Stats or nflfastR has route type + target location.
#    Need to add to the weekly features pull. Without it, lane_norm defaults
#    to neutral (0.5). With it, this is PropFinder's exact edge.
#
# 2. Snap count trend week-over-week — data is in nfl_features.upcoming_rows()
#    but the TREND (slope) is not computed. This module computes it from
#    the weekly snap_pcts list. The bot needs to pass the last 5 weeks' snap%.
#
# 3. Red zone targets/carries by game — need game-level RZ touches per player.
#    nflfastR pbp data has this. Add to nfl_features.py.
#
# 4. Explosive play rate per player + per defense —
#    nflfastR: plays with yards_gained >= 15. Both sides (player and defense).
#    Two queries, cheap. Add to nfl_features.py.
#
# 5. DEF vs position TD rate (last 10 games) — split by RB/WR/TE.
#    nflfastR pbp has receiver_position. Group by defense + posteam_position.
#    This is the #1 missing signal in v1.
#
# 6. ATG devig for NFL TDs — same SGO + Kalshi pattern. Same auth rules.
#    scripts/nfl/tuddy-atg-devig.py (port from lamp-atg-devig.py).
