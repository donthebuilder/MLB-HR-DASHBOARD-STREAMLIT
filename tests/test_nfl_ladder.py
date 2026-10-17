"""bots/nfl/nfl_ladder.py: the high-bar ladder, by each player's own clear rate.

TEST DATA, MADE UP: players a..d and their game logs describe no real person.

Run: python3 -m pytest tests/test_nfl_ladder.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_ladder as L  # noqa: E402


def log(vals, col="g_recyd", s=2099):
    return {"log": [{"s": s, "w": i + 1, col: v} for i, v in enumerate(vals)]}


def player(pid, score, **k):
    return {"player_id": pid, "name": pid, "team": "AAA", "opp": "BBB", "position": "WR",
            "scores": {"REC_YDS": score}, **k}


LOGS = {
    "a": log([110, 105, 20, 30, 40, 101]),          # 3/6 at 100
    "b": log([120, 20, 30, 40, 50, 60]),            # 1/6 at 100
    "c": log([10, 20, 30, 40, 50, 60]),             # never 100
    "d": log([150, 150, 150]),                      # 3 games: under MIN_GAMES
    "e": log([150] * 6),                            # on a bye
    "f": log([150] * 6),                            # low sample
}
PLAYERS = [player("a", 60), player("b", 80), player("c", 90), player("d", 99),
           player("e", 99, on_bye=True), player("f", 99, low_sample=True)]


def test_rung_ranks_by_clear_rate_then_score_and_leaves_out_who_never_cleared():
    m = L.build(PLAYERS, LOGS)["REC_YDS"]
    r100 = next(x for x in m["ladder"] if x["bar"] == 100)["rungs"]
    assert [x["player_id"] for x in r100] == ["a", "b"]
    assert r100[0]["rate"] == 0.5 and r100[0]["n"] == 6
    assert r100[1]["rate"] == round(1 / 6, 3)
    assert m["card_bar"] == 40


def test_card_bar_rung_is_listed_for_reference_and_c_clears_it():
    r40 = next(x for x in L.build(PLAYERS, LOGS)["REC_YDS"]["ladder"] if x["bar"] == 40)["rungs"]
    assert "c" in [x["player_id"] for x in r40]


def test_a_rung_nobody_cleared_is_empty_not_padded():
    rows = [player("c", 90)]
    assert next(x for x in L.build(rows, LOGS)["REC_YDS"]["ladder"] if x["bar"] == 100)["rungs"] == []


def test_window_takes_the_newest_games_across_seasons():
    lg = {"log": [{"s": 2098, "w": 18, "g_recyd": 999}, {"s": 2099, "w": 1, "g_recyd": 1}]}
    assert L.window(lg["log"], "g_recyd", n=1) == [1.0]
    assert L.window([{"s": 2099, "w": 1, "g_recyd": None}], "g_recyd") == []


def test_markets_without_scores_give_empty_ladders():
    assert all(not b["rungs"] for b in L.build([], {})["PASS_YDS"]["ladder"])
