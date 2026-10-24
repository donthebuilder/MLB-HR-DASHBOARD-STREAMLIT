"""
MOONSHOT v4 — MLB HR Scorer Model
Bot job: bots/moonshot_v4_scorer.py
Runs nightly before lock. Writes to data branch: public/data/current/moonshot_v4_latest.json

WHAT THIS REPLACES:
  v3 = pure batter ranking, season averages dominate, no pitcher side.
  v4 = BATTER SCORE × MATCHUP MULTIPLIER. Two scores, multiplied.

SHARED FRAMEWORK (same across all four DASH sports):
  FINAL SCORE = SCORER SCORE × MATCHUP SCORE
  Recency over season. Last 10 is the dominant window.
  Trend tracking: usage/opportunity going UP = boost.
  Slump + good matchup = wait and card. Don't remove from board.
  Model version field on every output. Records never rewritten.
  Shadow test 2 weeks before promoting to live board.

IMPORT INTO mlb_dashboard.py or run standalone.
"""

import json
import logging
from datetime import datetime, date
from typing import Optional

try:
    from bots.dash_shared import (
        normalize, scale, final_score, due_flag_shared, write_json_output,
    )
except ImportError:  # run standalone from inside bots/
    from dash_shared import (
        normalize, scale, final_score, due_flag_shared, write_json_output,
    )

log = logging.getLogger("moonshot_v4")

# "mlb_hr_v4" is already the live hr_score in model_registry.py, so this
# batter x matchup model gets the next free number. Two models, one label
# would mix their rows in the graded archive.
MODEL_VERSION = "mlb_hr_v5"

# ---------------------------------------------------------------------------
# BATTER SCORE — who is locked in RIGHT NOW
# ---------------------------------------------------------------------------
# Points breakdown (sums to 100 max):
#   40  spray chart last 10  (hard hit rate + avg distance + FB/LD mix)
#   20  recent contact       (BA last 5 games)
#   15  season EV            (power ceiling baseline)
#   10  recent ISO           (last 10)
#   10  season fly ball rate
#    5  near-miss flag       (binary: +5 if 2+ near-misses in last 5 games)

def near_miss_flag(batted_ball_log: list) -> bool:
    """
    Near-miss: ball hit 100+ EV at 35+ degrees in last 5 games that did NOT go yard.
    Returns True if 2+ near-misses exist — player is squaring it up, luck hasn't caught up.

    batted_ball_log: list of dicts from Statcast, each with:
      {ev: float, launch_angle: float, is_hr: bool, game_date: str}
    Only looks at last 5 game dates.
    """
    if not batted_ball_log:
        return False

    # Get last 5 distinct game dates
    dates = sorted(set(b["game_date"] for b in batted_ball_log), reverse=True)[:5]
    recent = [b for b in batted_ball_log if b["game_date"] in dates]

    near_misses = [
        b for b in recent
        if b.get("ev", 0) >= 100
        and b.get("launch_angle", 0) >= 35
        and not b.get("is_hr", False)
    ]
    return len(near_misses) >= 2


def spray_chart_score(batted_ball_log: list) -> float:
    """
    Score 0–40 based on last 10 games spray chart:
      - Hard hit rate (EV 95+) / total batted balls
      - Average distance on hard-hit balls
      - FB + LD rate (not ground ball dominant)

    Returns raw 0–40 contribution to batter score.
    """
    if not batted_ball_log:
        return 0.0

    dates = sorted(set(b["game_date"] for b in batted_ball_log), reverse=True)[:10]
    recent = [b for b in batted_ball_log if b["game_date"] in dates]

    total = len(recent)
    if total == 0:
        return 0.0

    hard_hit = [b for b in recent if b.get("ev", 0) >= 95]
    hard_hit_rate = len(hard_hit) / total

    avg_dist = (
        sum(b.get("distance", 0) for b in hard_hit) / len(hard_hit)
        if hard_hit else 0.0
    )
    # Normalize distance: 350ft = neutral, 420ft = max
    dist_norm = normalize(avg_dist, 300, 420)

    fb_ld = [
        b for b in recent
        if b.get("bb_type", "") in ("fly_ball", "line_drive")
    ]
    fb_ld_rate = len(fb_ld) / total

    # Weighted sum of three components
    raw = (
        hard_hit_rate * 0.50     # 50% of this component
        + dist_norm   * 0.30     # 30%
        + fb_ld_rate  * 0.20     # 20%
    )
    return round(raw * 40, 2)   # scale to 0–40 pts


def recent_contact_score(last5_ba: float) -> float:
    """
    Score 0–20 from BA in last 5 games.
    .000 = 0, .400+ = 20.
    """
    return round(scale(last5_ba, 0.0, 0.400, 0.0, 20.0), 2)


def season_ev_score(season_ev: float) -> float:
    """
    Score 0–15 from season average exit velocity.
    85 mph = 0, 95 mph = 15.
    """
    return round(scale(season_ev, 85, 95, 0.0, 15.0), 2)


def recent_iso_score(iso_last10: float) -> float:
    """
    Score 0–10 from ISO (isolated power) in last 10 games.
    .000 = 0, .350+ = 10.
    """
    return round(scale(iso_last10, 0.0, 0.350, 0.0, 10.0), 2)


def fly_ball_rate_score(season_fb_rate: float) -> float:
    """
    Score 0–10 from season fly ball rate.
    20% = 0, 50%+ = 10. Ground ball hitters aren't excluded — just weighted less.
    """
    return round(scale(season_fb_rate, 0.20, 0.50, 0.0, 10.0), 2)


def batter_score(
    batted_ball_log: list,
    last5_ba: float,
    season_ev: float,
    iso_last10: float,
    season_fb_rate: float,
) -> dict:
    """
    Compute full batter score (0–100).
    Returns dict with component breakdown for archiving.
    """
    spray      = spray_chart_score(batted_ball_log)
    contact    = recent_contact_score(last5_ba)
    ev         = season_ev_score(season_ev)
    iso        = recent_iso_score(iso_last10)
    fb         = fly_ball_rate_score(season_fb_rate)
    near_miss  = near_miss_flag(batted_ball_log)
    nm_pts     = 5.0 if near_miss else 0.0

    total = spray + contact + ev + iso + fb + nm_pts
    total = min(100.0, total)

    return {
        "batter_score":        round(total, 2),
        "spray_pts":           spray,
        "contact_pts":         contact,
        "ev_pts":              ev,
        "iso_pts":             iso,
        "fb_pts":              fb,
        "near_miss_flag":      near_miss,
        "near_miss_pts":       nm_pts,
    }


# ---------------------------------------------------------------------------
# MATCHUP SCORE — is this a good spot today
# ---------------------------------------------------------------------------
# Multiplier 0.5–1.5 (1.0 = neutral):
#   Pitcher HR rate allowed to this hand   (HIGH)
#   Pitcher FB% allowed                    (HIGH)
#   Pitcher hard hit % allowed             (MEDIUM-HIGH)
#   Pitcher LD% on top 2 pitches           (MEDIUM)
#   Damage control (DC) rating             (MEDIUM)
#   Park nudge                             (LOW: ±5%)

def matchup_multiplier(
    pitcher_hr_rate_to_hand: float,   # HR/PA or HR/9 — provide normalized 0–1
    pitcher_fb_pct: float,            # 0.0–1.0
    pitcher_hardhit_pct: float,       # 0.0–1.0
    pitcher_ld_pct_top2: float,       # LD% on their top 2 pitch types, 0.0–1.0
    dc_rating: float,                 # 0–100, already in the model
    park_factor: float = 1.0,         # 0.95–1.05 nudge only
) -> dict:
    """
    Returns matchup multiplier (0.5–1.5) with component breakdown.

    Each input is normalized to a 0–1 contribution.
    League average inputs produce multiplier ≈ 1.0.
    Soft targets for league average:
      pitcher_hr_rate_to_hand: ~0.30 (normalized)
      pitcher_fb_pct:          ~0.35
      pitcher_hardhit_pct:     ~0.35
      pitcher_ld_pct_top2:     ~0.20
      dc_rating:               ~50
    """
    # Normalize each component to 0–1 where 1.0 = best for batter
    hr_norm = normalize(pitcher_hr_rate_to_hand, 0, 0.60)
    fb_norm = normalize(pitcher_fb_pct, 0, 0.55)
    hh_norm = normalize(pitcher_hardhit_pct, 0, 0.55)
    ld_norm = normalize(pitcher_ld_pct_top2, 0, 0.35)
    dc_norm = normalize(dc_rating, 0, 100)

    # Weighted composite: 0–1 where 0.5 = league neutral
    raw = (
        hr_norm  * 0.35
        + fb_norm  * 0.25
        + hh_norm  * 0.20
        + ld_norm  * 0.12
        + dc_norm  * 0.08
    )

    # Map composite to 0.5–1.5 range
    # raw=0 → 0.5, raw=0.5 → 1.0 (neutral), raw=1.0 → 1.5
    multiplier = 0.5 + raw

    # Park nudge: ±5% tiebreaker only
    multiplier = multiplier * park_factor
    multiplier = max(0.5, min(1.5, multiplier))

    return {
        "matchup_multiplier":       round(multiplier, 3),
        "pitcher_hr_norm":          round(hr_norm, 3),
        "pitcher_fb_norm":          round(fb_norm, 3),
        "pitcher_hardhit_norm":     round(hh_norm, 3),
        "pitcher_ld_norm":          round(ld_norm, 3),
        "pitcher_dc_norm":          round(dc_norm, 3),
        "park_factor":              round(park_factor, 3),
    }


# ---------------------------------------------------------------------------
# FINAL SCORE — multiply and cap
# ---------------------------------------------------------------------------

def final_score_v4(
    batter: dict,
    matchup: dict,
    model_version: str = MODEL_VERSION,
) -> dict:
    """
    FINAL SCORE = BATTER SCORE × MATCHUP MULTIPLIER. Caps at 100.
    Returns full row ready for archive / JSON output.
    """
    final = final_score(batter["batter_score"], matchup["matchup_multiplier"])

    return {
        "final_score":      final,
        "batter_score":     batter["batter_score"],
        "matchup_multiplier": matchup["matchup_multiplier"],
        "model_version":    model_version,
        **{k: v for k, v in batter.items() if k != "batter_score"},
        **{k: v for k, v in matchup.items() if k != "matchup_multiplier"},
    }


# ---------------------------------------------------------------------------
# DUE FLAG — slump handling
# ---------------------------------------------------------------------------

def due_flag(
    recent_hr_last10: int,
    season_hr_per_game: float,
    batter_score_val: float,
) -> bool:
    """
    A proven power hitter in a cold streak is flagged DUE, not dropped.
    Criteria: season rate says they should have X HR by now, haven't.
    Also: batter_score > 60 (real talent) but cold last 10 (< 1 HR in 10 games).

    Don't card DUE guys blindly — wait until matchup multiplier > 1.1.
    The DUE flag makes them visible on the board so Donovan can manually review.
    """
    return due_flag_shared(
        scorer_score=batter_score_val,
        recent_event_count=recent_hr_last10,
        usage_trend="flat",   # MLB has no usage-trend input here
        season_rate=season_hr_per_game,
        has_proven_track_record=season_hr_per_game >= 0.08,   # ~13+ HR pace over 162
    )


# ---------------------------------------------------------------------------
# OUTPUT FORMAT — what gets written to data branch nightly
# ---------------------------------------------------------------------------

def build_output_row(
    player_id: int,
    name: str,
    team: str,
    bats: str,            # "L" or "R"
    opponent: str,
    pitcher_id: int,
    pitcher_name: str,
    pitcher_hand: str,    # "L" or "R"
    park_name: str,
    game_pk: int,
    batter_inputs: dict,  # raw inputs for batter_score()
    matchup_inputs: dict, # raw inputs for matchup_multiplier()
    recent_hr_last10: int,
    season_hr_per_game: float,
    game_date: str,
) -> dict:
    """
    Full row builder. One call per player per night.
    This is what gets written to moonshot_v4_latest.json.
    """
    bat = batter_score(
        batted_ball_log=batter_inputs["batted_ball_log"],
        last5_ba=batter_inputs["last5_ba"],
        season_ev=batter_inputs["season_ev"],
        iso_last10=batter_inputs["iso_last10"],
        season_fb_rate=batter_inputs["season_fb_rate"],
    )

    mat = matchup_multiplier(
        pitcher_hr_rate_to_hand=matchup_inputs["pitcher_hr_rate_to_hand"],
        pitcher_fb_pct=matchup_inputs["pitcher_fb_pct"],
        pitcher_hardhit_pct=matchup_inputs["pitcher_hardhit_pct"],
        pitcher_ld_pct_top2=matchup_inputs["pitcher_ld_pct_top2"],
        dc_rating=matchup_inputs["dc_rating"],
        park_factor=matchup_inputs.get("park_factor", 1.0),
    )

    result = final_score_v4(bat, mat)

    result["due_flag"] = due_flag(
        recent_hr_last10=recent_hr_last10,
        season_hr_per_game=season_hr_per_game,
        batter_score_val=bat["batter_score"],
    )

    result.update({
        "player_id":      player_id,
        "name":           name,
        "team":           team,
        "bats":           bats,
        "opponent":       opponent,
        "pitcher_id":     pitcher_id,
        "pitcher_name":   pitcher_name,
        "pitcher_hand":   pitcher_hand,
        "park_name":      park_name,
        "game_pk":        game_pk,
        "game_date":      game_date,
        "generated_at":   datetime.utcnow().isoformat() + "Z",
    })

    return result


# ---------------------------------------------------------------------------
# SHADOW TEST — run v4 alongside v3 before promoting
# ---------------------------------------------------------------------------
# Run this for 2 weeks:
#   1. Compute v4 scores
#   2. Write them to moonshot_v4_shadow_YYYYMMDD.json (NOT the live file)
#   3. After each night's games, grade both v3 and v4 against outcomes
#   4. After 2 weeks: if v4 hit rate >= v3, promote (rename to moonshot_v4_latest.json)
#   5. Archive v3 output with model_version="mlb_hr_v3"
#   Records are NEVER rewritten. v3 stays in the archive.

LIVE_OUTPUT_FILE = "moonshot_v4_latest.json"


def write_output(rows: list, output_path: str, shadow: bool = False) -> str:
    """
    Write the nightly v4 output.
    shadow=True → writes to shadow file .
    shadow=False → writes to live file (after promotion).
    """
    return write_json_output(
        rows, output_path, model_version=MODEL_VERSION, sport="mlb", shadow=shadow,
    )


# ---------------------------------------------------------------------------
# MISSING DATA STILL NEEDED FROM THE BOT
# (these are the open bot-side items — NOT done yet)
# ---------------------------------------------------------------------------
#
# 1. pitcher_xhr_allowed / pitcher_hr_luck — returns 0 on all starters.
#    Fix location: mlb_dashboard.py pipeline for pitcher Statcast pulls.
#    This is the highest-value open bot task (per Aug 22 handoff).
#    Once fixed, map it into matchup_inputs["pitcher_hardhit_pct"].
#
# 2. pitcher_ld_pct_top2 — per-pitch LD% on top 2 pitch types.
#    Needs Statcast pitch-level data. Currently not pulled at all.
#    pybaseball.statcast_pitcher() has pitch_type + launch_speed_angle.
#    Pull last 30 days, filter to pitcher's top 2 pitch types by usage,
#    compute LD% on those. Add to today_bot.py pitcher enrichment.
#
# 3. pitcher_bb9 — constant 3.20 on every starter. Fix or delete.
#    (Low priority — not in v4 formula)
#
# 4. Five split axes (park, day/night, RISP, count-state, calendar) —
#    ordinary StatsAPI queries. Site modal is waiting. Add to today_bot.py.
#    (These are display/research, not the v4 model itself)
#
# 5. Per-pitch outcome data — pitch_type_summary has usage only, count=null.
#    Same Statcast pull as #2 above. Fix together.
#
# 6. batted_ball_log last 10 — already exists in the bot pipeline.
#    Confirm it carries: {game_date, ev, launch_angle, distance, bb_type, is_hr}
#    These are the exact fields near_miss_flag() and spray_chart_score() need.
#
# 7. ATG devig for MLB HR props — same SGO + Kalshi pattern already working
#    for NHL. Port it: scripts/mlb/moonshot-atg-devig.py
#    SGO league key for MLB, correct market type for HR props.
