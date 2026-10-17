"""QB anytime TD (bot task 1, 2026-10-11): nfl_scoring.QB_TD_MODEL, the context
block in nfl_qb_td.py, grading eligibility, and the game-call rule staying put.

TEST DATA, MADE UP. Players q1.., r1.. and every number are invented for these
checks and describe no real player.

Run: python3 -m pytest tests/test_nfl_qb_td.py -v
"""
import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_bot as nb  # noqa: E402
import nfl_game_calls as gc  # noqa: E402
import nfl_qb_td  # noqa: E402
import nfl_registry  # noqa: E402
import nfl_results as nr  # noqa: E402
from nfl_scoring import MODELS, QB_TD_MODEL, TD_GRADE_POS  # noqa: E402


def _row(pid, pos, car, tgt, snap, it, gl, rz, xtd):
    return {"player_id": pid, "name": pid, "team": "AAA", "opponent_team": "BBB", "week": 1,
            "position": pos, "implied_total": it, "f_tgt_n": tgt, "f_car_n": car,
            "f_snap_pct": snap, "f_gl_opp": gl, "f_rz_opp": rz, "f_xtd": xtd}


def _league():
    """TEST DATA: 30 RB/WR/TE spread evenly, enough for the 20-row floor."""
    return [_row(f"r{i}", ["RB", "WR", "TE"][i % 3], car=i * 0.5, tgt=(30 - i) * 0.3,
                 snap=0.4 + i * 0.02, it=17 + i * 0.3, gl=i * 0.05, rz=i * 0.1, xtd=i * 0.03)
            for i in range(30)]


def _qbs():
    return [
        _row("q_run", "QB", car=9.0, tgt=0.0, snap=0.99, it=26.0, gl=0.8, rz=2.0, xtd=0.6),
        _row("q_pocket", "QB", car=1.0, tgt=0.0, snap=0.99, it=26.0, gl=0.0, rz=0.1, xtd=0.02),
    ]


def test_weights_are_the_td_models_today():
    assert QB_TD_MODEL["w"] == MODELS["TD"]["w"]
    assert QB_TD_MODEL["pos"] == ["QB"]
    assert QB_TD_MODEL["ref_pos"] == MODELS["TD"]["pos"]


def test_version_is_new_and_registry_shaped():
    assert QB_TD_MODEL["model_version"] == "nfl_td_qb_v1"
    assert nfl_registry.MODEL_VERSIONS["TD"] == "nfl_td_v2"
    assert nfl_registry._VERSION_PATTERN.match(QB_TD_MODEL["model_version"])
    assert QB_TD_MODEL["model_version"] not in nfl_registry.MODEL_VERSIONS.values()


def test_adding_qbs_moves_no_rb_wr_te_score():
    league = pl.DataFrame(_league())
    both = pl.DataFrame(_league() + _qbs())
    a = nb.score_all(league, True, league)["TD"]["df"]
    b = nb.score_all(both, True, league)["TD"]["df"]
    assert dict(zip(a["player_id"], a["score"])) == dict(zip(b["player_id"], b["score"]))


def test_qb_scored_on_the_skill_league_and_comparable():
    league = pl.DataFrame(_league())
    tbl = pl.DataFrame(_league() + _qbs())
    out = nb.score_all(tbl, True, league, models={"TD": QB_TD_MODEL})["TD"]["df"]
    got = dict(zip(out["player_id"], out["score"]))
    assert set(got) == {"q_run", "q_pocket"}
    assert got["q_run"] > got["q_pocket"]
    # A QB whose inputs equal a league man's gets that man's score: one scale.
    twin = _row("q_twin", "QB", **{k: _league()[20][k2] for k, k2 in
               (("car", "f_car_n"), ("tgt", "f_tgt_n"), ("snap", "f_snap_pct"),
                ("it", "implied_total"), ("gl", "f_gl_opp"), ("rz", "f_rz_opp"),
                ("xtd", "f_xtd"))})
    tbl2 = pl.DataFrame(_league() + [twin])
    o2 = nb.score_all(tbl2, True, league, models={"TD": QB_TD_MODEL})["TD"]["df"]
    base = nb.score_all(tbl2, True, league)["TD"]["df"]
    assert o2["score"][0] == pytest.approx(
        dict(zip(base["player_id"], base["score"]))["r20"], abs=1e-6)


def test_no_league_reference_publishes_no_qb_score():
    tbl = pl.DataFrame(_league() + _qbs())
    assert nb.score_all(tbl, True, None, models={"TD": QB_TD_MODEL}) == {}


# ── context block ────────────────────────────────────────────────────────────

def _pbp():
    """TEST DATA: two games, QB q1 (AAA) at BBB and CCC. q1 has 3 designed runs
    and 1 scramble, one TD from the 3, one kneel that must not count."""
    def play(**k):
        base = dict(season_type="REG", week=1, game_id="g1", defteam="BBB", posteam="AAA",
                    rush_attempt=0, qb_kneel=0, qb_scramble=0, rush_touchdown=0,
                    yardline_100=60, rusher_player_id=None, passer_player_id=None)
        base.update(k)
        return base
    rows = [
        play(passer_player_id="q1"),
        play(rush_attempt=1, rusher_player_id="q1", yardline_100=3, rush_touchdown=1),
        play(rush_attempt=1, rusher_player_id="q1", yardline_100=15),
        play(rush_attempt=1, rusher_player_id="q1", yardline_100=40, qb_scramble=1),
        play(rush_attempt=1, rusher_player_id="q1", yardline_100=1, qb_kneel=1),
        play(week=2, game_id="g2", defteam="CCC", passer_player_id="q1"),
        play(week=2, game_id="g2", defteam="CCC", rush_attempt=1, rusher_player_id="q1"),
        # the week being built: must be invisible
        play(week=3, game_id="g3", defteam="BBB", rush_attempt=1, rusher_player_id="q1",
             rush_touchdown=1, yardline_100=2),
    ]
    return pl.DataFrame(rows, infer_schema_length=None)


def test_qb_rates_exclude_kneels_and_the_unplayed_week():
    r = nfl_qb_td.qb_rates(_pbp(), {"q1"}, before_week=3)["q1"]
    assert r["games"] == 2
    assert r["rush_att_pg"] == pytest.approx(4 / 2)          # 3 + 1, kneel out
    assert r["rush_td_pg"] == pytest.approx(1 / 2)
    assert r["rz_car_pg"] == pytest.approx(2 / 2)            # the 3 and the 15
    assert r["gl_car_pg"] == pytest.approx(1 / 2)
    assert r["designed_share"] == pytest.approx(3 / 4)       # one scramble


def test_defence_qb_rush_td_per_game():
    d = nfl_qb_td.defence_qb_rush_td(_pbp(), before_week=3)
    assert d["BBB"] == {"td_pg": 1.0, "games": 1}
    assert d["CCC"] == {"td_pg": 0.0, "games": 1}


def test_build_has_no_entry_for_a_qb_without_games():
    out = nfl_qb_td.build({2026: _pbp()}, 2026, 3, [{"player_id": "q1", "opp": "BBB"},
                                                    {"player_id": "ghost", "opp": "BBB"}])
    assert "ghost" not in out or "2026" not in out["ghost"]
    assert out["q1"]["2026"]["games"] == 2
    assert out["q1"][nfl_qb_td.DEFENCE_KEY]["2026"] == 1.0


# ── grading + game calls + log ───────────────────────────────────────────────

def test_a_qb_td_is_graded_not_voided():
    actual = {"q1": {"TD": 1.0, "PASS_YDS": 250.0}, "k1": {"TD": 0.0}}
    lines = nr.eligible_lines(actual, {"q1": "QB", "k1": "K"})
    assert lines["q1"]["TD"] == 1.0
    assert "TD" not in lines["k1"]
    card = {"TD": {"bar": 1, "rungs": [{"player_id": "q1"}]}}
    graded, totals = nr.grade(card, actual, {"q1": "QB"})
    assert graded["TD"]["rungs"][0]["hit"] is True and totals["TD"]["n"] == 1
    assert "QB" in TD_GRADE_POS


def test_game_calls_rule_stays_rb_wr_te_when_qb_is_published():
    mk = {"key": "TD", "positions": ["RB", "WR", "TE", "QB"], "weights": {"f_rz_opp": 1.0}}

    def p(pid, pos, score):
        return {"player_id": pid, "name": pid, "team": "AAA", "opp": "BBB", "position": pos,
                "on_bye": False, "scores": {"TD": score}, "stats": {}, "components": {"TD": {}}}
    board = gc.td_board({"markets": [mk], "players": [p("q", "QB", 99), p("r", "RB", 50)]})
    assert [x["player_id"] for x in board] == ["r"]


def test_log_line_carries_the_qb_model_version():
    rm = nb.build_nfl_run_meta("week", 2026, 5, None)
    assert "nfl_td_qb" in rm["config_hashes"]
    rows = [
        {"player_id": "q", "name": "Q", "team": "AAA", "opp": "BBB", "position": "QB",
         "scores": {"TD": 70.0}, "components": {"TD": {}}, "model_versions": {"TD": "nfl_td_qb_v1"}},
        {"player_id": "r", "name": "R", "team": "AAA", "opp": "BBB", "position": "RB",
         "scores": {"TD": 60.0}, "components": {"TD": {}}},
    ]
    lines = {l["player_id"]: l for l in nb.build_nfl_prediction_log_lines(rm, rows)[1:]}
    assert lines["q"]["model_version"] == "nfl_td_qb_v1"
    assert lines["r"]["model_version"] == "nfl_td_v2"
