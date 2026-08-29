"""Canonical, pregame-only HR overlay tiers.

These are screening labels, not model probabilities.  They are stamped into
prediction_log at run time so later grading reads the decision that actually
existed before first pitch instead of recreating it from newer data.
"""
from __future__ import annotations

from typing import Any, Mapping


VERSION = "hr_overlay_v2"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _rate(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return number / 100.0 if number > 1 else number


def build_hr_overlay(row: Mapping[str, Any]) -> dict[str, Any]:
    air = _rate(row.get("l25pa_air_rate"))
    if air is None:
        pieces = [_rate(row.get(key)) for key in ("recent_fb_rate", "recent_ld_rate", "recent_popup_rate")]
        air = sum(value for value in pieces if value is not None) if any(value is not None for value in pieces) else None
    exit_velocity = _number(row.get("recent_ev"))
    season_iso = _number(row.get("season_iso"))
    hrw = _number(row.get("hrw_score"))

    checks = {
        "air_50": air is not None and air > 0.50,
        "exit_velocity_87": exit_velocity is not None and exit_velocity > 87.0,
    }
    fit_passed = sum(checks.values())
    core = fit_passed == len(checks)
    power = core and season_iso is not None and season_iso >= 0.230
    premium = power and hrw is not None and hrw >= 60

    qualified = []
    if core:
        qualified.append("hr_overlay")
    if power:
        qualified.append("power_overlay")
    if premium:
        qualified.append("premium_power")

    # Keep the older chronological held-out shape test as a separate evidence
    # badge.  The newest locked slice did not confirm it strongly enough to
    # remain the primary ordering gate, but retaining it lets the live record
    # tell us whether that older result returns with more data.
    barrel = _rate(row.get("recent_barrel_rate"))
    fly_ball = _rate(row.get("recent_fb_rate"))
    pitcher_hr9 = _number(row.get("pitcher_hr9"))
    shape_checks = {
        "barrel_3_1": barrel is not None and barrel >= 0.031,
        "fly_ball_23_2": fly_ball is not None and fly_ball >= 0.232,
        "exit_velocity_89_9": exit_velocity is not None and exit_velocity >= 89.9,
    }

    return {
        "version": VERSION,
        "fit_passed": fit_passed,
        "fit_total": len(checks),
        "checks": checks,
        "qualified_tiers": qualified,
        "primary_tier": qualified[-1] if qualified else None,
        "shape_reference": {
            "passed": sum(shape_checks.values()),
            "total": len(shape_checks),
            "qualified": all(shape_checks.values()),
            "checks": shape_checks,
        },
        "inputs": {
            "l25pa_air_rate": air,
            "recent_barrel_rate": barrel,
            "recent_fb_rate": fly_ball,
            "recent_ev": exit_velocity,
            "recent_hard_hit_rate": _rate(row.get("recent_hard_hit_rate")),
            "recent_pull_rate": _rate(row.get("recent_pull_rate")),
            "recent_pull_air_rate": _rate(row.get("recent_pull_air_rate")),
            "recent_avg_distance": _number(row.get("recent_avg_distance")),
            "recent_squared_up_rate": _rate(row.get("recent_squared_up_rate")),
            "recent_squared_up_sample": _number(row.get("recent_squared_up_sample")),
            "recent_blast_rate": _rate(row.get("recent_blast_rate")),
            "recent_sweet_spot_rate": _rate(row.get("recent_sweet_spot_rate")),
            "season_iso": season_iso,
            "hrw_score": hrw,
            "pitcher_hr9": pitcher_hr9,
            "season_hr_game_probability": _number(row.get("season_hr_game_probability")),
        },
    }
