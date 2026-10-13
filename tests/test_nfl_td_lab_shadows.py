"""The two model-lab TD shadows (nfl_td_reweight_v1, nfl_td_rzonly_v1) and nfl_shadow_report.py.

TEST DATA, MADE UP. Run: python3 -m pytest tests/test_nfl_td_lab_shadows.py -v
"""
import datetime as dt
import json
import os
import sys

import numpy as np
import polars as pl
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "bots", "nfl"))

import nfl_bot as nb  # noqa: E402
import nfl_shadow_report as sr  # noqa: E402
from nfl_scoring import MODELS, SHADOW_MODELS  # noqa: E402

RW, RZ = SHADOW_MODELS["TD#nfl_td_reweight_v1"], SHADOW_MODELS["TD#nfl_td_rzonly_v1"]


def test_weights_sum_to_one_and_use_only_v2_inputs():
    for m in (RW, RZ):
        assert abs(sum(m["w"].values()) - 1.0) < 1e-3
        assert set(m["w"]) <= set(MODELS["TD"]["w"])
        assert m["pos"] == MODELS["TD"]["pos"] and m["bar"] == 1


def test_rzonly_keeps_v2_relative_weights_so_no_weight_is_invented():
    v2 = MODELS["TD"]["w"]; fam = ["f_rz_opp", "f_xtd", "f_gl_opp"]
    tot = sum(v2[k] for k in fam)
    for k in fam:
        assert RZ["w"][k] == pytest.approx(v2[k] / tot, abs=1e-3)


def test_reweight_matches_the_lab_frozen_candidate_file():
    p = os.path.join(ROOT, "model_lab", "results", "nfl_weight_candidates.json")
    if not os.path.exists(p):
        pytest.skip("lab results not present")
    frozen = json.load(open(p))["TD"]["frozen_candidate"]
    for k, v in frozen.items():
        assert RW["w"][k] == pytest.approx(v, abs=1e-3)
    assert "f_gl_opp" not in RW["w"]


def _slate():
    rng = np.random.default_rng(3)
    n = 40
    return pl.DataFrame({
        "player_id": [f"t{i}" for i in range(n)], "name": [f"Test {i}" for i in range(n)],
        "team": ["AAA" if i % 2 else "BBB" for i in range(n)], "opponent_team": ["BBB" if i % 2 else "AAA" for i in range(n)],
        "position": [["RB", "WR", "TE"][i % 3] for i in range(n)], "week": [1] * n,
        "implied_total": rng.uniform(17, 30, n), "f_tgt_n": rng.uniform(0, 9, n), "f_car_n": rng.uniform(0, 15, n),
        "f_snap_pct": rng.uniform(0.3, 1, n), "f_rz_opp": rng.uniform(0, 3, n), "f_gl_opp": rng.uniform(0, 1, n), "f_xtd": rng.uniform(0, .8, n),
    })


def test_shadows_run_through_score_shadow_and_published_scores_do_not_move():
    tbl = _slate()
    before = nb.score_all(tbl, True, None)["TD"]["df"]["score"].to_list()
    sh = nb.score_shadow(tbl, True, None)
    after = nb.score_all(tbl, True, None)["TD"]["df"]["score"].to_list()
    assert before == after
    assert {"TD", "TD#nfl_td_reweight_v1", "TD#nfl_td_rzonly_v1"} <= set(sh)
    assert sh["TD#nfl_td_reweight_v1"]["model_version"] == "nfl_td_reweight_v1"
    assert set(sh["TD#nfl_td_rzonly_v1"]["scores"]["t0"]["components"]) == set(RZ["w"])


def test_log_lines_carry_every_td_shadow_version_and_the_header_lists_them():
    tbl = _slate()
    sh = nb.score_shadow(tbl, True, None)
    players = [{"player_id": "t0", "name": "T", "team": "AAA", "opp": "BBB", "position": "RB", "scores": {"TD": 70.0}, "components": {"TD": {}}}]
    lines = nb.build_nfl_prediction_log_lines(nb.build_nfl_run_meta("week", 2026, 5, None), players, sh)
    assert {"nfl_td_v3", "nfl_td_reweight_v1", "nfl_td_rzonly_v1"} <= set(lines[1]["shadow"])
    assert {"TD#nfl_td_reweight_v1", "TD#nfl_td_rzonly_v1"} <= set(lines[0]["shadow"])
    assert lines[1]["score"] == 70.0 and lines[1]["model_version"] == "nfl_td_v2"


# ── the weekly report ─────────────────────────────────────────────────────────
KO = {"AAA": dt.datetime(2099, 10, 4, 17, 0, tzinfo=dt.timezone.utc)}


def _log(week, gen, rows):
    head = {"mode": "week", "week": week, "generated_at": gen.isoformat()}
    return [head] + [{"market": "TD", "player_id": p, "team": "AAA", "score": s, "shadow": {"v_good": {"score": g}}, "low_sample": False} for p, s, g in rows]


def test_pregame_rows_takes_the_last_line_before_kickoff_only():
    early = dt.datetime(2099, 10, 4, 10, 0, tzinfo=dt.timezone.utc); mid = dt.datetime(2099, 10, 4, 15, 0, tzinfo=dt.timezone.utc)
    late = dt.datetime(2099, 10, 4, 18, 0, tzinfo=dt.timezone.utc)      # after kickoff: never read
    got = sr.pregame_rows([_log(1, early, [("a", 10, 1)]), _log(1, mid, [("a", 20, 2)]), _log(1, late, [("a", 99, 9)])], KO, 1)
    assert got["a"]["score"] == 20


def _week(w, rng):
    """40 players; the 'good' shadow is the real signal, the published score is that signal plus noise."""
    sig = rng.uniform(0, 1, 40); y = (rng.uniform(0, 1, 40) < sig * 0.6).astype(int)
    rows = {f"p{i}": {"score": float(sig[i] * 100 + rng.normal(0, 40)), "shadow": {"v_good": {"score": float(sig[i] * 100)}}, "low_sample": False} for i in range(40)}
    return rows, {f"p{i}": int(y[i]) for i in range(40)}


def test_report_proposes_only_after_enough_weeks_and_a_clear_ranking_gain():
    rng = np.random.default_rng(11)
    rows, outs = {}, {}
    for w in range(1, 4):
        rows[w], outs[w] = _week(w, rng)
    early = sr.report(rows, outs)["versions"]["v_good"]
    assert early["verdict"].startswith("too early")
    for w in range(4, 21):
        rows[w], outs[w] = _week(w, rng)
    late = sr.report(rows, outs)["versions"]["v_good"]
    assert late["pooled"]["weeks"] == 20 and late["auc_diff"] > 0 and late["lo"] > 0
    assert late["verdict"].startswith("MEETS THE RULE")


def test_report_does_not_propose_an_identical_shadow():
    rng = np.random.default_rng(12); rows, outs = {}, {}
    for w in range(1, 11):
        r, o = _week(w, rng)
        for v in r.values():
            v["shadow"]["v_same"] = {"score": v["score"]}
        rows[w], outs[w] = r, o
    e = sr.report(rows, outs)["versions"]["v_same"]
    assert e["auc_diff"] == 0 and e["verdict"] == "does not meet the rule"


def test_void_players_are_excluded():
    rows = {f"p{i}": {"score": float(i), "shadow": {}, "low_sample": False} for i in range(30)}
    out = {f"p{i}": 1 for i in range(10, 30)}                     # p0..p9 have no stat line: void
    m = sr.week_metrics(rows, out, "published")
    assert m["n"] == 20


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
