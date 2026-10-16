"""bots/nhl/nhl_odds_fetch.py -- market words, game-date keying, freeze. TEST DATA only."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nhl"))
import nhl_odds_fetch as nhl  # noqa: E402
import odds_fetch as mlb  # noqa: E402


def _kind(label):
    saved = mlb.PROP_HINTS
    mlb.PROP_HINTS = nhl.HOCKEY_HINTS
    try:
        return mlb._prop_key(label)
    finally:
        mlb.PROP_HINTS = saved


def test_hockey_hints_file_markets_correctly():
    cases = {"Anytime Goalscorer": "player_anytime_goal", "First Goalscorer": "player_first_goal",
             "Shots on Goal": "player_shots_on_goal", "Player Points": "player_points",
             "Assists": "player_assists", "Goalie Saves": "player_saves",
             "Power Play Points": "_other", "2+ Goals": "_other", "Blocked Shots": "_other"}
    for label, want in cases.items():
        assert _kind(label) == want, label


def test_snapshot_is_keyed_on_the_games_own_date_not_utc():
    # 7:00 pm ET Oct 17 = 23:00 UTC Oct 17; 10:30 pm ET Oct 17 = 02:30 UTC Oct 18
    board = {"test skater": {"player_shots_on_goal": {"line": 2.5, "over": -120, "implied": 54.5,
                                                      "commence": "2026-10-18T02:30:00Z", "game": "A @ B"}}}
    assert list(nhl.slim_by_date(board)) == ["2026-10-17"]


def test_freeze_keeps_pregame_row(tmp_path):
    now = dt.datetime(2026, 10, 17, 23, 30, tzinfo=dt.timezone.utc)
    snap = tmp_path / "nhl_odds_2026-10-17.json"
    snap.write_text(json.dumps({"rows": {"test skater": {"player_shots_on_goal": [2.5, -120, 54.5]}},
                                "commence": {"test skater": "2026-10-17T23:00:00Z"}}))
    rows, _ = nhl.base.freeze_started(snap, {"test skater": {"player_shots_on_goal": [3.5, -300, 75.0]}},
                                      {"test skater": "2026-10-17T23:00:00Z"}, now)
    assert rows["test skater"]["player_shots_on_goal"][0] == 2.5
