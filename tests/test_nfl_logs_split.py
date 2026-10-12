"""bots/nfl/nfl_logs_split.py: the slim team-games index and per-player files.

TEST DATA, MADE UP: clubs AAA/BBB, players p1..p3.

Run: python3 -m pytest tests/test_nfl_logs_split.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_logs_split as ls  # noqa: E402


def e(s, w, tm, opp, td, h=1, d="2099-01-01"):
    return {"s": s, "w": w, "tm": tm, "opp": opp, "g_td": td, "h": h, "d": d}


LOGS = {
    "p1": {"log": [e(2098, 18, "AAA", "BBB", 1.0), e(2099, 1, "AAA", "BBB", 2.0)], "rates": {}},
    "p2": {"log": [e(2099, 1, "AAA", "BBB", 1.0)], "rates": {}},
    "p3": {"log": [e(2099, 1, "BBB", "AAA", 0.0, h=0), {"s": 2099, "tm": "BBB"}], "rates": {}},
}


def test_team_games_sum_a_clubs_tracked_players_oldest_first():
    rows = ls.team_games(LOGS)
    assert [(r["s"], r["w"], r["tm"], r["td"]) for r in rows] == [
        (2098, 18, "AAA", 1.0), (2099, 1, "AAA", 3.0), (2099, 1, "BBB", 0.0)]
    assert rows[2]["h"] == 0 and rows[1]["h"] == 1 and rows[1]["opp"] == "BBB"


def test_write_makes_index_and_player_files_and_replaces_the_directory(tmp_path):
    (tmp_path / "nfl_logs").mkdir()
    (tmp_path / "nfl_logs" / "stale.json").write_text("{}")
    info = ls.write(tmp_path, "nfl_", {"TD": ["g_td", 1]}, LOGS, 2099)
    assert info == {"team_games": 3, "players": 3}
    idx = json.loads((tmp_path / "nfl_logs_index.json").read_text())
    assert idx["bars"] == {"TD": ["g_td", 1]} and idx["hot"]["market"] == "TD" and idx["players"] == 3 and len(idx["team_games"]) == 3
    assert json.loads((tmp_path / "nfl_logs" / "p1.json").read_text()) == LOGS["p1"]
    assert not (tmp_path / "nfl_logs" / "stale.json").exists()


def test_unsafe_ids_are_skipped(tmp_path):
    ls.write(tmp_path, "nfl_", {}, {"../x": {"log": []}, "ok": {"log": []}})
    assert sorted(p.name for p in (tmp_path / "nfl_logs").iterdir()) == ["ok.json"]


def test_hot_streaks_keeps_only_players_whose_newest_game_reached_the_bar():
    def g(w, td, s=2099, rec=2, car=1):
        return {"s": s, "w": w, "tm": "AAA", "opp": "BBB", "g_td": td, "g_rec": rec, "g_car": car}
    logs = {
        "hot": {"log": [g(1, 0), g(2, 1), g(3, 2)]},
        "cold": {"log": [g(1, 1), g(2, 1), g(3, 0)]},     # scored, then did not: no streak
        "lastyr": {"log": [g(18, 1, s=2098)]},            # no game this season
    }
    h = ls.hot_streaks(logs, {"TD": ["g_td", 1], "REC": ["g_rec", 4]})
    assert h["market"] == "TD" and h["bar"] == 1 and h["season"] == 2099
    assert list(h["players"]) == ["hot"]
    assert h["players"]["hot"] == [[2099, 1, 0.0, 2.0, 1.0], [2099, 2, 1.0, 2.0, 1.0], [2099, 3, 2.0, 2.0, 1.0]]


def test_hot_streaks_empty_when_the_first_market_is_not_td_shaped():
    assert ls.hot_streaks({"p": {"log": []}}, {"REC": ["g_rec", 4]}) == {}
    assert ls.hot_streaks({}, {"TD": ["g_td", 1]}) == {}
