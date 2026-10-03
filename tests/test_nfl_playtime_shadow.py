"""Playing-time shadows (2026-10-03, BATCH-MODEL-V2 M2): REC and RUSH_ATT + snap share.

ALL DATA IN THIS FILE IS MADE-UP TEST DATA. Player ids "t1".."t6", teams
"AAA"/"BBB"; every stat is invented to exercise the math.

Covers:
  * each shadow is the live weights x (1 - s) plus f_snap_pct at s
    (nfl_playtime_lab.with_snap), sums to 1, same positions and bar as live
  * neither shadow version is a version of record
  * score_shadow() scores TD v3, REC and RUSH_ATT together and leaves the
    published scores untouched
  * a shadow composite through score_all(models=...) is sum(w * pctile)

Run: python3 -m pytest tests/test_nfl_playtime_shadow.py -v
"""
import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_bot as nb  # noqa: E402
import nfl_registry  # noqa: E402
from nfl_scoring import MODELS, SHADOW_MODELS  # noqa: E402
from nfl_playtime_lab import with_snap  # noqa: E402

PICKS = {"REC": 0.30, "RUSH_ATT": 0.10}   # nfl_playtime_lab.py, chosen on 2024


@pytest.mark.parametrize("market,share", PICKS.items())
def test_shadow_is_live_plus_snap(market, share):
    want = with_snap(MODELS[market]["w"], share)
    got = SHADOW_MODELS[market]["w"]
    assert set(got) == set(want)
    for k, v in want.items():
        assert got[k] == pytest.approx(v, abs=1e-9), k
    assert sum(got.values()) == pytest.approx(1.0, abs=1e-9)
    assert SHADOW_MODELS[market]["pos"] == MODELS[market]["pos"]
    assert SHADOW_MODELS[market]["bar"] == MODELS[market]["bar"]


@pytest.mark.parametrize("market", PICKS)
def test_shadow_is_not_the_version_of_record(market):
    v = SHADOW_MODELS[market]["model_version"]
    assert v not in nfl_registry.MODEL_VERSIONS.values()
    assert nfl_registry._VERSION_PATTERN.match(v)


def _fake_slate() -> pl.DataFrame:
    """TEST DATA: six invented players, one week, no ties in the scored inputs."""
    return pl.DataFrame({
        "player_id": ["t1", "t2", "t3", "t4", "t5", "t6"],
        "name": [f"Test {i}" for i in range(1, 7)],
        "team": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
        "opponent_team": ["BBB", "BBB", "BBB", "AAA", "AAA", "AAA"],
        "position": ["RB", "WR", "TE", "RB", "WR", "RB"],
        "week": [4] * 6,
        "f_target_share": [0.08, 0.27, 0.15, 0.05, 0.22, 0.11],
        "f_receptions":   [2.1, 6.4, 3.8, 1.2, 5.1, 2.9],
        "f_targets":      [2.6, 8.9, 5.0, 1.7, 7.3, 3.4],
        "f_carries":      [16.0, 0.4, 0.0, 12.5, 0.2, 9.1],
        "f_rz_car":       [2.4, 0.0, 0.1, 1.8, 0.0, 1.1],
        "f_ngs_rush_yards_over_expected_per_att": [0.31, 0.0, 0.0, -0.12, 0.0, 0.08],
        "f_snap_pct":     [0.72, 0.88, 0.61, 0.55, 0.79, 0.43],
        "f_xtd":          [0.90, 0.40, 0.10, 0.70, 0.20, 0.55],
        "implied_total":  [24.5, 24.5, 24.5, 20.5, 20.5, 20.5],
        "f_tgt_n":        [3.0, 8.0, 4.0, 2.0, 6.0, 7.0],
        "f_car_n":        [15.0, 0.1, 0.0, 12.0, 0.2, 0.3],
    })


def _pct(vals):
    """average-rank / n, same as nfl_scoring._pctile on untied data"""
    order = sorted(vals)
    return [(order.index(v) + 1) / len(vals) for v in vals]


def test_rec_shadow_composite_is_weighted_percentiles():
    tbl = _fake_slate()
    out = nb.score_all(tbl, True, None, models={"REC": SHADOW_MODELS["REC"]})["REC"]
    got = dict(zip(out["df"]["player_id"].to_list(), out["df"]["score"].to_list()))
    w = SHADOW_MODELS["REC"]["w"]
    pcts = {k: _pct(tbl[k].to_list()) for k in w}
    for i, pid in enumerate(tbl["player_id"].to_list()):
        assert got[pid] == pytest.approx(100 * sum(w[k] * pcts[k][i] for k in w), abs=1e-6), pid
    assert set(out["dropped"]) == set()


def test_score_shadow_scores_all_three_and_published_untouched():
    tbl = _fake_slate()
    before = {m: nb.score_all(tbl, True, None)[m]["df"]["score"].to_list() for m in ("REC", "RUSH_ATT")}
    sh = nb.score_shadow(tbl, True, None)
    after = {m: nb.score_all(tbl, True, None)[m]["df"]["score"].to_list() for m in ("REC", "RUSH_ATT")}
    assert before == after
    assert {"TD", "REC", "RUSH_ATT"} <= set(sh)
    assert sh["REC"]["model_version"] == "nfl_rec_snap_v1"
    assert sh["RUSH_ATT"]["model_version"] == "nfl_rushatt_snap_v1"
    # RUSH_ATT is RB only
    assert set(sh["RUSH_ATT"]["scores"]) == {"t1", "t4", "t6"}
    assert "f_snap_pct" in sh["REC"]["scores"]["t2"]["components"]


# ── the live record (TEST DATA logs) ─────────────────────────────────────────
import datetime as dt  # noqa: E402
import json  # noqa: E402
from nfl_playtime_lab import pregame_rows, grade_top5  # noqa: E402

_KO = {"AAA": dt.datetime(2099, 1, 1, 18, tzinfo=dt.timezone.utc), "BBB": dt.datetime(2099, 1, 1, 21, tzinfo=dt.timezone.utc)}


def _log(gen, rows):
    head = json.dumps({"mode": "week", "week": 4, "generated_at": gen})
    return [head] + [json.dumps(r) for r in rows]


def _row(pid, team, score, shadow, market="REC", low=False):
    return {"player_id": pid, "player": f"Test {pid}", "team": team, "market": market, "score": score,
            "low_sample": low, "shadow": {"nfl_rec_snap_v1": {"score": shadow}}}


def test_pregame_rows_latest_before_kickoff_only():
    early = _log("2099-01-01T12:00:00+00:00", [_row("t1", "AAA", 50, 40), _row("t2", "BBB", 60, 70)])
    late = _log("2099-01-01T19:00:00+00:00", [_row("t1", "AAA", 99, 99), _row("t2", "BBB", 65, 75)])  # after AAA kicks off
    rows = {r["player_id"]: r for r in pregame_rows([early, late], 4, _KO, "REC")}
    assert rows["t1"]["score"] == 50      # the 19:00 line came after AAA's 18:00 kickoff: not pregame
    assert rows["t2"]["score"] == 65      # BBB kicks off at 21:00: the later line is the record


def test_pregame_rows_drop_low_sample_and_other_markets():
    body = _log("2099-01-01T12:00:00+00:00", [_row("t1", "AAA", 50, 40, low=True), _row("t2", "AAA", 60, 70, market="TD")])
    assert pregame_rows([body], 4, _KO, "REC") == []


def test_grade_top5_voids_and_bar():
    rows = [_row(f"t{i}", "AAA", 100 - i, i) for i in range(1, 8)]
    actual = {"t1": 5, "t2": 3, "t3": 4, "t5": 6, "t6": 1, "t7": 4}   # t4 has no line: void
    # live top-5 = t1..t5 -> t4 void -> graded t1,t2,t3,t5: hits t1 (5), t3 (4), t5 (6)
    assert grade_top5(rows, lambda r: r["score"], actual, 4) == (3, 4)
    # shadow top-5 = t7..t3 -> t4 void -> graded t7,t6,t5,t3: hits t7, t5, t3
    assert grade_top5(rows, lambda r: r["shadow"]["nfl_rec_snap_v1"]["score"], actual, 4) == (3, 4)
