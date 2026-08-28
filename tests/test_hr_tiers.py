import datetime as dt

from bots.eval_report import hr_overlay_performance
from bots.hr_tiers import build_hr_overlay


def test_hr_overlay_tiers_are_nested():
    elite = build_hr_overlay({
        "recent_barrel_rate": 0.05,
        "recent_fb_rate": 0.30,
        "recent_ev": 92,
        "season_iso": 0.250,
        "hrw_score": 70,
        "pitcher_hr9": 1.55,
        "season_hr_game_probability": 18.2,
    })
    assert elite["fit_passed"] == 3
    assert elite["qualified_tiers"] == ["verified_shape", "premium_power", "elite_matchup"]
    assert elite["primary_tier"] == "elite_matchup"

    shape_only = build_hr_overlay({
        "recent_barrel_rate": 3.1,  # percentage-form inputs normalize too
        "recent_fb_rate": 23.2,
        "recent_ev": 89.9,
        "season_iso": 0.200,
        "hrw_score": 80,
        "pitcher_hr9": 2.0,
    })
    assert shape_only["qualified_tiers"] == ["verified_shape"]


def test_eval_uses_stored_membership_and_never_backfills():
    included = [
        {
            "went_yard": True,
            "game_date_actual": "2026-08-27",
            "row": {"hr_overlay": {"qualified_tiers": ["verified_shape", "premium_power"]}},
        },
        {
            "went_yard": False,
            "game_date_actual": "2026-08-27",
            # Even perfect-looking raw values cannot enter without the
            # pregame overlay stamped on the locked prediction.
            "row": {"components": {"barrel_rate": 0.10, "fb_rate": 0.50, "recent_ev": 100}},
        },
    ]
    report = hr_overlay_performance(included, dt.date(2026, 8, 27))
    assert report["eligible_schema_n"] == 1
    assert report["legacy_without_overlay_n"] == 1
    assert report["tiers"]["verified_shape"]["all"]["n"] == 1
    assert report["tiers"]["premium_power"]["all"]["hrs"] == 1
    assert report["tiers"]["elite_matchup"]["all"]["n"] == 0
