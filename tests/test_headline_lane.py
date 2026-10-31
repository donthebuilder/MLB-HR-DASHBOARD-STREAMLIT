import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
from headline_lane import headline_lane


def test_three_point_shooter_leads_with_threes():
    r = headline_lane({"reb": 0.10, "3pm": 0.30, "pra": 0.12}, {"reb": 0.09, "3pm": 0.15, "pra": 0.11}, "nba")
    assert r["lane"] == "3pm" and r["label"] == "3PM 4+" and r["lift"] == 2.0


def test_long_shot_and_tiny_base_never_lead():
    assert headline_lane({"dd": 0.03, "ast": 0.5}, {"dd": 0.01, "ast": 0.01}, "nba") is None


def test_missing_values_are_skipped():
    assert headline_lane({"hit": None, "hr": 0.2}, {"hit": 0.7, "hr": 0.1}, "mlb")["lane"] == "hr"
    assert headline_lane({}, {}, "nhl") is None
