"""
DASH Network — Shared Bot Utilities
bots/dash_shared.py

Import this in all four sport scorer bots:
  from dash_shared import recency_weight, trend_slope, due_flag_shared,
                          normalize, shadow_or_live_path, write_json_output

RULES SHARED ACROSS ALL FOUR SPORTS:
  1. FINAL SCORE = SCORER SCORE × MATCHUP SCORE (0–100, capped)
  2. RECENCY OVER SEASON — last 10 games dominates, season is baseline/ceiling
  3. USAGE TREND — going UP = boost, going DOWN = downgrade
  4. SLUMP + GOOD MATCHUP = flag as DUE, wait for the right night, don't drop
  5. PRICE LAST — score first, check price after (price rules in lib/card/rules.js)
  6. MODEL VERSION on every output row. Records NEVER rewritten.
  7. SHADOW TEST 2 weeks before promoting any new model to the live board.
"""

import json
import math
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Any

log = logging.getLogger("dash_shared")


# ---------------------------------------------------------------------------
# NORMALIZATION HELPERS
# ---------------------------------------------------------------------------

def normalize(value: float, lo: float, hi: float, clip: bool = True) -> float:
    """
    Map value from [lo, hi] to [0, 1].
    clip=True clamps values outside the range.
    """
    if hi == lo:
        return 0.5
    result = (value - lo) / (hi - lo)
    if clip:
        result = max(0.0, min(1.0, result))
    return round(result, 4)


def scale(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    """Normalize then scale to [out_lo, out_hi]."""
    n = normalize(value, lo, hi)
    return round(out_lo + n * (out_hi - out_lo), 4)


# ---------------------------------------------------------------------------
# RECENCY WEIGHTING
# ---------------------------------------------------------------------------

def recency_weight(values: list, decay: float = 0.85) -> float:
    """
    Exponentially weighted average — recent values count more.
    values[0] = oldest, values[-1] = most recent.
    decay: weight of each older game relative to the next. 0.85 means
    last game = 1.0, second-to-last = 0.85, third = 0.72, etc.

    This is the standard DASH recency pattern. Last 10 games is the window.
    Season is only the baseline/ceiling — not the primary signal.
    """
    if not values:
        return 0.0
    weights = [decay ** (len(values) - 1 - i) for i in range(len(values))]
    total_weight = sum(weights)
    weighted_sum = sum(v * w for v, w in zip(values, weights))
    return round(weighted_sum / total_weight, 4)


# ---------------------------------------------------------------------------
# TREND SLOPE
# ---------------------------------------------------------------------------

def trend_slope(values: list) -> dict:
    """
    Compute linear slope of a metric over time.
    values[0] = oldest, values[-1] = most recent.
    Returns slope, direction, and delta (last - first).

    Used identically in MLB (BA trend), NFL (snap count trend),
    NHL (TOI trend), NBA (minutes trend).
    """
    if not values or len(values) < 2:
        return {"slope": 0.0, "direction": "unknown", "delta": 0.0}

    n = len(values)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n)) or 1
    slope = num / den

    delta = values[-1] - values[0]

    if slope > 0.01:
        direction = "rising"
    elif slope < -0.01:
        direction = "falling"
    else:
        direction = "flat"

    return {
        "slope":     round(slope, 5),
        "direction": direction,
        "delta":     round(delta, 4),
    }


# ---------------------------------------------------------------------------
# DUE FLAG (shared across all four sports)
# ---------------------------------------------------------------------------

def due_flag_shared(
    scorer_score: float,
    recent_event_count: int,   # HRs last 10, TDs last 5, goals last 10, points 25+ last 10
    usage_trend: str,
    season_rate: float,        # expected events per game at season pace
    has_proven_track_record: bool,
) -> bool:
    """
    A proven player in a cold streak = DUE. Flag them, don't drop.
    Criteria (all must be true):
      - Recent event count low (below what season rate predicts)
      - Scorer score still high (talent is real, this is a cold snap)
      - Has proven track record (not a hot-start rookie — those can regress)
      - Usage NOT falling (if usage is falling, it's not a cold streak — it's a role change)

    When DUE + good matchup = card. Without good matchup = visible on board, not carded.
    """
    expected = season_rate * (10 if recent_event_count <= 1 else 5)
    cold = recent_event_count < max(1, expected * 0.4)
    talented = scorer_score >= 55
    not_losing_role = usage_trend != "falling"

    return cold and talented and not_losing_role and has_proven_track_record


# ---------------------------------------------------------------------------
# FINAL SCORE CAP (shared)
# ---------------------------------------------------------------------------

def final_score(scorer_score: float, matchup_multiplier: float) -> float:
    """
    FINAL SCORE = SCORER SCORE × MATCHUP MULTIPLIER
    Caps at 100.
    """
    return round(min(100.0, scorer_score * matchup_multiplier), 2)


# ---------------------------------------------------------------------------
# OUTPUT FILE ROUTING
# ---------------------------------------------------------------------------

SHADOW_SUFFIX = "_shadow_"

def shadow_or_live_path(live_path: str, shadow: bool) -> str:
    """
    Returns the file path to write to.
    shadow=True: writes to shadow file (YYYYMMDD suffix), not the live file.
    shadow=False: writes to live file.
    """
    if not shadow:
        return live_path

    p = Path(live_path)
    date_str = date.today().strftime("%Y%m%d")
    shadow_name = p.stem + SHADOW_SUFFIX + date_str + p.suffix
    return str(p.parent / shadow_name)


def write_json_output(
    rows: list,
    live_path: str,
    model_version: str,
    sport: str,
    shadow: bool = False,
) -> str:
    """
    Write scorer output rows to JSON.
    Returns the path written.

    shadow=False (default) writes the live file; shadow=True writes a dated
    shadow file instead.

    Records NEVER overwrite graded results. This is a new-night file.
    """
    path = shadow_or_live_path(live_path, shadow)

    payload = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "slate_date":    date.today().isoformat(),
        "sport":         sport,
        "model_version": model_version,
        "shadow":        shadow,
        "players":       sorted(rows, key=lambda r: r.get("final_score", 0), reverse=True),
    }

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

    log.info(f"[{sport}] Wrote {len(rows)} players to {path} (shadow={shadow})")
    return path


# ---------------------------------------------------------------------------
# SHADOW TEST GRADING HELPER
# ---------------------------------------------------------------------------

def grade_shadow_vs_live(
    shadow_path: str,
    live_path: str,
    graded_results: list,   # [{player_id, game_date, scored: bool}]
) -> dict:
    """
    After a night's results, compare shadow model hit rate vs live model.
    Used during the 2-week shadow test to decide when to promote.

    Returns:
      {
        "shadow_hit_rate": float,
        "live_hit_rate": float,
        "shadow_top_n": int,
        "live_top_n": int,
        "promote": bool,   # True if shadow >= live after 14+ nights
      }
    """
    def load(path):
        try:
            with open(path) as f:
                return json.load(f)["players"]
        except Exception:
            return []

    shadow_rows = load(shadow_path)
    live_rows   = load(live_path)

    graded_map = {(r["player_id"], r["game_date"]): r["scored"] for r in graded_results}

    def hit_rate(rows, top_n=10):
        slate_date = rows[0].get("game_date") if rows else None
        top = rows[:top_n]
        hits = sum(
            1 for r in top
            if graded_map.get((r.get("player_id"), slate_date), False)
        )
        return hits / len(top) if top else 0.0

    shadow_hr = hit_rate(shadow_rows)
    live_hr   = hit_rate(live_rows)

    return {
        "shadow_hit_rate": round(shadow_hr, 4),
        "live_hit_rate":   round(live_hr, 4),
        "shadow_top_n":    min(10, len(shadow_rows)),
        "live_top_n":      min(10, len(live_rows)),
        "promote":         shadow_hr >= live_hr,
    }


# ---------------------------------------------------------------------------
# ATG DEVIG SHARED UTILITIES
# (used by lamp-atg-devig.py and the ports to MLB + NFL)
# ---------------------------------------------------------------------------

def devig_two_way(odds_a: int, odds_b: int) -> tuple:
    """
    Remove the book's vig from a two-way market.
    Returns (fair_prob_a, fair_prob_b).

    American odds → implied prob → normalize to sum to 1.0.
    """
    def to_prob(odds: int) -> float:
        if odds > 0:
            return 100 / (odds + 100)
        else:
            return (-odds) / (-odds + 100)

    raw_a = to_prob(odds_a)
    raw_b = to_prob(odds_b)
    total = raw_a + raw_b
    return (raw_a / total, raw_b / total)


def expected_value(fair_prob: float, market_odds: int) -> float:
    """
    EV% = fair_prob * payout - (1 - fair_prob)
    Where payout is the decimal multiplier from American odds.
    """
    if market_odds >= 0:
        payout = market_odds / 100
    else:
        payout = 100 / (-market_odds)
    return round(fair_prob * payout - (1 - fair_prob), 4)


def kalshi_is_trusted(orderbook_fp: dict) -> bool:
    """
    A Kalshi market is TRUSTED (deep enough to be meaningful) when
    both yes and no sides have $500+ in the orderbook.

    orderbook_fp is the full orderbook dict from Kalshi API (key: orderbook_fp).
    """
    try:
        yes_depth = sum(lvl.get("cost", 0) for lvl in orderbook_fp.get("yes", []))
        no_depth  = sum(lvl.get("cost", 0) for lvl in orderbook_fp.get("no", []))
        return yes_depth >= 500 and no_depth >= 500
    except Exception:
        return False
