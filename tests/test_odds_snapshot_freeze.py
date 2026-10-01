"""odds_fetch.freeze_snapshot: the dated odds snapshot freezes each hitter at
his first pitch (2026-10-01). TEST DATA: made-up ids and prices."""
import datetime as dt, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
from odds_fetch import freeze_snapshot

NOW = dt.datetime(2026, 9, 25, 20, 0, tzinfo=dt.timezone.utc)
START = {"1": "2026-09-25T17:05:00Z", "2": "2026-09-25T23:10:00Z", "3": "2026-09-25T17:05:00Z", "4": "2026-09-25T17:05:00Z"}

def test_started_hitter_keeps_his_pregame_price():
    rows, frozen, _ = freeze_snapshot({"1": {"HR": [0.5, 400, 0.2]}}, {"1": {"HR": [0.5, 350, 0.22]}}, START, NOW)
    assert rows["1"] == {"HR": [0.5, 350, 0.22]} and frozen == 1

def test_not_started_takes_the_new_price():
    rows, _, _ = freeze_snapshot({"2": {"HR": [0.5, 500, 0.17]}}, {"2": {"HR": [0.5, 450, 0.18]}}, START, NOW)
    assert rows["2"] == {"HR": [0.5, 500, 0.17]}

def test_first_priced_after_first_pitch_is_left_out():
    rows, _, dropped = freeze_snapshot({"3": {"HR": [0.5, 900, 0.1]}}, {}, START, NOW)
    assert "3" not in rows and dropped == 1

def test_started_hitter_gone_from_board_keeps_frozen_row():
    rows, frozen, _ = freeze_snapshot({}, {"4": {"HR": [0.5, 300, 0.25]}}, START, NOW)
    assert rows["4"] == {"HR": [0.5, 300, 0.25]} and frozen == 1

def test_unknown_start_time_is_treated_as_not_started():
    rows, _, _ = freeze_snapshot({"9": {"HR": [0.5, 600, 0.14]}}, {}, START, NOW)
    assert "9" in rows
