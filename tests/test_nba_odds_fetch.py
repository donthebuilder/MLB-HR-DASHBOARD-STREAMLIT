"""bots/nba/nba_odds_fetch.py -- market words, game-date keying, freeze. TEST DATA only."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nba"))
import nba_odds_fetch as nba  # noqa: E402
import odds_fetch as mlb  # noqa: E402


def _kind(label):
    saved = mlb.PROP_HINTS
    mlb.PROP_HINTS = nba.BASKETBALL_HINTS
    try:
        return mlb._prop_key(label)
    finally:
        mlb.PROP_HINTS = saved


def test_basketball_hints_file_markets_correctly():
    cases = {"Double Double": "player_double_double", "Triple Double": "player_triple_double",
             "Points + Rebounds + Assists": "player_pra", "Points + Rebounds": "player_pts_reb",
             "Points + Assists": "player_pts_ast", "Rebounds + Assists": "player_reb_ast",
             "Player Points": "player_points", "Player Rebounds": "player_rebounds",
             "Player Assists": "player_assists", "3-Pointers Made": "player_threes",
             "First Basket": "_other", "1st Quarter Points": "_other", "Steals": "_other"}
    for label, want in cases.items():
        assert _kind(label) == want, label
    assert "_other" not in nba.MARKETS


def test_snapshot_is_keyed_on_the_games_own_date():
    board = {"test player": {"player_double_double": {"line": 0.5, "over": 180, "implied": 35.7,
                                                       "commence": "2026-10-23T02:00:00Z", "game": "A @ B"}}}
    assert list(nba.slim_by_date(board)) == ["2026-10-22"]


def test_freeze_keeps_pregame_row(tmp_path):
    now = dt.datetime(2026, 10, 22, 23, 30, tzinfo=dt.timezone.utc)
    snap = tmp_path / "nba_odds_2026-10-22.json"
    snap.write_text(json.dumps({"rows": {"test player": {"player_double_double": [0.5, 180, 35.7]}},
                                "commence": {"test player": "2026-10-22T23:00:00Z"}}))
    rows, _ = nba.base.freeze_started(snap, {"test player": {"player_double_double": [0.5, -150, 60.0]}},
                                      {"test player": "2026-10-22T23:00:00Z"}, now)
    assert rows["test player"]["player_double_double"][1] == 180
