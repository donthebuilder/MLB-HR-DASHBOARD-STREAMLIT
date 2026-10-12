"""nfl_explosive.team_usage: share-of-club fields (bot task 5).

TEST DATA, MADE UP: club AAA, players a1 (RB), a2 (RB), w1 (WR).

Run: python3 -m pytest tests/test_nfl_usage_shares.py -v
"""
import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_explosive as nx  # noqa: E402


def _pbp():
    def play(**k):
        base = dict(posteam="AAA", week=1, pass_attempt=0, rush_attempt=0, complete_pass=0,
                    yards_gained=0, pass_touchdown=0, rush_touchdown=0, yardline_100=50,
                    air_yards=None, receiver_player_id=None, rusher_player_id=None)
        base.update(k)
        return base
    rows = [
        play(rush_attempt=1, rusher_player_id="a1", yardline_100=3),
        play(rush_attempt=1, rusher_player_id="a1", yardline_100=15),
        play(rush_attempt=1, rusher_player_id="a2", yardline_100=4, week=2),
        play(rush_attempt=1, rusher_player_id="a2", yardline_100=40, week=2),
        play(pass_attempt=1, receiver_player_id="w1", air_yards=10, yardline_100=18),
        play(pass_attempt=1, receiver_player_id="a1", air_yards=-2, week=2),
    ]
    return pl.DataFrame(rows, infer_schema_length=None)


def test_goal_line_air_and_games_shares(monkeypatch):
    monkeypatch.setattr(nx, "_pbp", lambda season: _pbp())
    u = nx.team_usage(2099)["AAA"]
    assert u["a1"]["car_share"] == pytest.approx(50.0)
    assert u["a1"]["gl_car_share"] == pytest.approx(50.0)      # 1 of the club's 2 inside-5 carries
    assert u["a2"]["gl_car_share"] == pytest.approx(50.0)
    assert u["w1"]["gl_car_share"] == 0.0
    assert u["w1"]["air_share"] == pytest.approx(125.0)         # 10 of a club total of 8
    assert u["a1"]["air_share"] == pytest.approx(-25.0)
    assert u["a1"]["games"] == 2 and u["w1"]["games"] == 1
    assert u["a1"]["rz_car_share"] == pytest.approx(66.7, abs=0.05)   # 2 of the club's 3 carries inside the 20
