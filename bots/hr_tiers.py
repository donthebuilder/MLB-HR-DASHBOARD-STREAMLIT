"""Canonical, pregame-only HR overlay tiers.

These are screening labels, not model probabilities.  They are stamped into
prediction_log at run time so later grading reads the decision that actually
existed before first pitch instead of recreating it from newer data.
"""
from __future__ import annotations

from typing import Any, Mapping


VERSION = "hr_overlay_v1"


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
    barrel = _rate(row.get("recent_barrel_rate"))
    fly_ball = _rate(row.get("recent_fb_rate"))
    exit_velocity = _number(row.get("recent_ev"))
    season_iso = _number(row.get("season_iso"))
    hrw = _number(row.get("hrw_score"))
    pitcher_hr9 = _number(row.get("pitcher_hr9"))

    checks = {
        "barrel_3_1": barrel is not None and barrel >= 0.031,
        "fly_ball_23_2": fly_ball is not None and fly_ball >= 0.232,
        "exit_velocity_89_9": exit_velocity is not None and exit_velocity >= 89.9,
    }
    fit_passed = sum(checks.values())
    verified = fit_passed == len(checks)
    premium = verified and season_iso is not None and season_iso >= 0.230 and hrw is not None and hrw >= 60
    elite = premium and pitcher_hr9 is not None and pitcher_hr9 >= 1.40

    qualified = []
    if verified:
        qualified.append("verified_shape")
    if premium:
        qualified.append("premium_power")
    if elite:
        qualified.append("elite_matchup")

    return {
        "version": VERSION,
        "fit_passed": fit_passed,
        "fit_total": len(checks),
        "checks": checks,
        "qualified_tiers": qualified,
        "primary_tier": qualified[-1] if qualified else None,
        "inputs": {
            "recent_barrel_rate": barrel,
            "recent_fb_rate": fly_ball,
            "recent_ev": exit_velocity,
            "season_iso": season_iso,
            "hrw_score": hrw,
            "pitcher_hr9": pitcher_hr9,
            "season_hr_game_probability": _number(row.get("season_hr_game_probability")),
        },
    }
