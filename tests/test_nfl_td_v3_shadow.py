"""nfl_td_v3 shadow + book-line grading (2026-10-01, NFL model audit).

ALL DATA IN THIS FILE IS MADE-UP TEST DATA. Player ids are "t1".."t6", teams
are "AAA"/"BBB", every stat, score, line and price is invented to exercise the
math -- none of it describes a real player, game or market.

Covers:
  * the v3 weight vector: one red-zone term, every other term keeps its v2
    ratio, sums to 1
  * the v3 composite through score_all(models=...) is exactly sum(w * pctile)
  * the shadow is additive in the prediction log: every pre-existing field of
    every line is unchanged, no new lines, the published score is v2's
  * the shadow top-5 uses the card's own selection rule and leaves the real
    card alone
  * book-line grading adds fields and never moves hit/actual/totals

Run: python3 -m pytest tests/test_nfl_td_v3_shadow.py -v
"""
import datetime as dt
import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_bot as nb  # noqa: E402
import nfl_picks  # noqa: E402
import nfl_registry  # noqa: E402
import nfl_results as nr  # noqa: E402
from nfl_scoring import MODELS, SHADOW_MODELS  # noqa: E402

V2 = MODELS["TD"]["w"]
V3 = SHADOW_MODELS["TD"]["w"]


# ── the weight vector ────────────────────────────────────────────────────────

def test_v3_weights_sum_to_one():
    assert abs(sum(V3.values()) - 1.0) < 1e-3


def test_v3_has_one_red_zone_term():
    assert "f_xtd" in V3
    assert "f_rz_opp" not in V3 and "f_gl_opp" not in V3


def test_v3_keeps_every_other_v2_ratio():
    # Same inputs otherwise: the non-red-zone terms are v2's, renormalised.
    kept = {k: v for k, v in V2.items() if k not in ("f_rz_opp", "f_xtd", "f_gl_opp")}
    assert set(kept) | {"f_xtd"} == set(V3)
    denom = sum(kept.values()) + V2["f_rz_opp"]   # trio collapses to its biggest weight
    for k, v in kept.items():
        assert V3[k] == pytest.approx(v / denom, abs=1e-4)
    assert V3["f_xtd"] == pytest.approx(V2["f_rz_opp"] / denom, abs=1e-4)


def test_v3_is_not_the_version_of_record():
    assert SHADOW_MODELS["TD"]["model_version"] == "nfl_td_v3"
    assert nfl_registry.MODEL_VERSIONS["TD"] == "nfl_td_v2"
    assert "nfl_td_v3" not in nfl_registry.MODEL_VERSIONS.values()
    assert nfl_registry._VERSION_PATTERN.match("nfl_td_v3")


# ── the term math, through the production pipeline ───────────────────────────

def _fake_slate() -> pl.DataFrame:
    """TEST DATA: six invented players, one week, no ties in any input."""
    return pl.DataFrame({
        "player_id": ["t1", "t2", "t3", "t4", "t5", "t6"],
        "name": ["Test One", "Test Two", "Test Three", "Test Four", "Test Five", "Test Six"],
        "team": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
        "opponent_team": ["BBB", "BBB", "BBB", "AAA", "AAA", "AAA"],
        "position": ["RB", "WR", "TE", "RB", "WR", "WR"],
        "week": [4] * 6,
        "f_xtd":         [0.90, 0.40, 0.10, 0.70, 0.20, 0.55],
        "f_rz_opp":      [3.0, 1.0, 0.5, 2.5, 0.2, 1.5],
        "f_gl_opp":      [1.5, 0.2, 0.1, 1.2, 0.0, 0.4],
        "implied_total": [24.5, 24.5, 24.5, 20.5, 20.5, 20.5],
        "f_tgt_n":       [3.0, 8.0, 4.0, 2.0, 6.0, 7.0],
        "f_car_n":       [15.0, 0.1, 0.0, 12.0, 0.2, 0.3],
        "f_snap_pct":    [0.70, 0.85, 0.55, 0.60, 0.75, 0.90],
    })


def _pct(vals):
    """average-rank / n, same as nfl_scoring._pctile on untied data"""
    order = sorted(vals)
    return [(order.index(v) + 1) / len(vals) for v in vals]


def test_v3_composite_is_weighted_percentiles():
    tbl = _fake_slate()
    out = nb.score_all(tbl, True, None, models=SHADOW_MODELS)["TD"]
    got = dict(zip(out["df"]["player_id"].to_list(), out["df"]["score"].to_list()))
    touches = [a + b for a, b in zip(tbl["f_tgt_n"], tbl["f_car_n"])]
    cols = {"f_xtd": tbl["f_xtd"].to_list(), "f_touches": touches,
            "f_snap_pct": tbl["f_snap_pct"].to_list()}
    # implied_total is tied within each team: average rank
    it = tbl["implied_total"].to_list()
    it_pct = [(sum(1 for x in it if x < v) + (sum(1 for x in it if x == v) + 1) / 2) / len(it) for v in it]
    pcts = {k: _pct(v) for k, v in cols.items()}
    pcts["implied_total"] = it_pct
    # nothing dropped, so the live weights are V3 renormalised (out["weights"]
    # is rounded to 3 dp for display, so use the source vector)
    tot = sum(V3.values())
    w = {k: v / tot for k, v in V3.items()}
    for i, pid in enumerate(tbl["player_id"].to_list()):
        want = 100 * sum(w[k] * pcts[k][i] for k in V3)
        assert got[pid] == pytest.approx(want, abs=1e-6), pid
    assert set(out["dropped"]) == set()


def test_score_shadow_shape_and_published_scores_untouched():
    tbl = _fake_slate()
    before = nb.score_all(tbl, True, None)["TD"]["df"]["score"].to_list()
    sh = nb.score_shadow(tbl, True, None)
    after = nb.score_all(tbl, True, None)["TD"]["df"]["score"].to_list()
    assert before == after
    assert sh["TD"]["model_version"] == "nfl_td_v3"
    assert set(sh["TD"]["scores"]) == {"t1", "t2", "t3", "t4", "t5", "t6"}
    assert set(sh["TD"]["scores"]["t1"]["components"]) == set(V3)


def test_score_shadow_failure_is_empty_not_fatal():
    assert nb.score_shadow(pl.DataFrame({"player_id": ["t1"]}), True, None) == {}


# ── the prediction log ───────────────────────────────────────────────────────

def _players():
    """TEST DATA: payload-shaped rows with invented v2 scores."""
    rows = []
    for i, (pid, sc, low) in enumerate([("t1", 70.0, False), ("t2", 65.0, False), ("t3", 60.0, False),
                                        ("t4", 55.0, False), ("t5", 50.0, False), ("t6", 45.0, True)]):
        rows.append({"player_id": pid, "name": f"Test {pid}", "team": "AAA", "opp": "BBB",
                     "position": "WR", "carryover": False, "questionable": False, "low_sample": low,
                     "scores": {"TD": sc, "REC": sc - 10},
                     "components": {"TD": {"f_xtd": 50.0}, "REC": {"f_targets": 40.0}}})
    return rows


def _shadow():
    # TEST DATA: v3 reverses the v2 order
    return {"TD": {"model_version": "nfl_td_v3", "weights": dict(V3), "dropped": [],
                   "scores": {p: {"score": s, "components": {"f_xtd": s}}
                              for p, s in [("t1", 40.0), ("t2", 45.0), ("t3", 50.0),
                                           ("t4", 55.0), ("t5", 60.0), ("t6", 99.0)]}}}


def _meta():
    return {"run_id": "2026-wk04.000000Z.test", "generated_at": "2026-10-01T00:00:00+00:00",
            "mode": "week", "season": 2026, "week": 4,
            "model_versions": {"TD": "nfl_td_v2", "REC": "nfl_rec_v1"}, "config_hashes": {"nfl": None}}


def test_shadow_log_is_additive():
    plain = nb.build_nfl_prediction_log_lines(_meta(), _players())
    shad = nb.build_nfl_prediction_log_lines(_meta(), _players(), _shadow())
    assert len(plain) == len(shad)                       # no new lines
    assert {k: v for k, v in shad[0].items() if k != "shadow"} == plain[0]
    for a, b in zip(plain[1:], shad[1:]):
        assert {k: v for k, v in b.items() if k != "shadow"} == a
        if a["market"] == "TD":
            assert b["model_version"] == "nfl_td_v2"     # the published score is v2's
            assert b["shadow"]["nfl_td_v3"]["score"] == _shadow()["TD"]["scores"][a["player_id"]]["score"]
        else:
            assert "shadow" not in b
    # shadow=None is byte-identical to before
    assert nb.build_nfl_prediction_log_lines(_meta(), _players(), None) == plain


def test_shadow_card_in_header_uses_card_rule():
    hdr = nb.build_nfl_prediction_log_lines(_meta(), _players(), _shadow())[0]["shadow"]["TD"]
    assert hdr["model_version"] == "nfl_td_v3" and hdr["of_record"] is False
    # t6 has the top shadow score but is low_sample, and five solid rows exist,
    # so -- exactly like the real card -- it is left off.
    assert [r["player_id"] for r in hdr["card"]] == ["t5", "t4", "t3", "t2", "t1"]
    assert [r["rank"] for r in hdr["card"]] == [1, 2, 3, 4, 5]


def test_real_card_unchanged_by_shadow():
    rows = _players()
    card = nfl_picks.build(rows)
    assert [r["player_id"] for r in card["TD"]["rungs"]] == ["t1", "t2", "t3", "t4", "t5"]
    nfl_picks.shadow_ladder(rows, _shadow()["TD"]["scores"])
    assert nfl_picks.build(rows) == card
    assert rows == _players()                            # rows not mutated


# ── book-line grading ────────────────────────────────────────────────────────

KO = {"AAA": dt.datetime(2026, 10, 4, 17, 0, tzinfo=dt.timezone.utc),
      "BBB": dt.datetime(2026, 10, 4, 17, 0, tzinfo=dt.timezone.utc)}


def _card():
    """TEST DATA: one invented REC_YDS ladder, bar 40."""
    mk = lambda pid, team: {"player_id": pid, "name": f"Test {pid}", "team": team, "rank": 0}
    return {"REC_YDS": {"key": "REC_YDS", "bar": 40, "rungs": [
        mk("t1", "AAA"), mk("t2", "AAA"), mk("t3", "BBB"), mk("t4", "BBB"),
        mk("t5", "AAA"), mk("t6", "AAA"), mk("t1", "AAA")]}}


ACTUAL = {"t1": {"REC_YDS": 80.0}, "t2": {"REC_YDS": 45.0}, "t3": {"REC_YDS": 60.0},
          "t4": {"REC_YDS": 50.0}, "t5": {"REC_YDS": 41.0}}          # t6: did not play

BOOK = {  # TEST DATA lines / prices / snapshot times
    "t1": {"REC_YDS": {"line": 70.5, "over": -115, "taken_at": "2026-10-04T16:00:00+00:00"}},  # over
    "t2": {"REC_YDS": {"line": 57.5, "over": -110, "taken_at": "2026-10-04T16:00:00+00:00"}},  # bar hit, line miss
    "t3": {"REC_YDS": {"line": 60.0, "over": -110, "taken_at": "2026-10-04T15:00:00+00:00"}},  # push
    "t4": {"REC_YDS": {"line": 40.5, "over": -110, "taken_at": "2026-10-04T17:30:00+00:00"}},  # after kickoff
    "t5": {"REC_YDS": {"line": 35.5, "over": -110, "taken_at": "2026-09-28T23:00:00+00:00"}},  # last week's
    "t6": {"REC_YDS": {"line": 30.5, "over": -110, "taken_at": "2026-10-04T16:00:00+00:00"}},  # DNP
}


def test_book_lines_never_move_bar_grade():
    graded, totals = nr.grade(_card(), ACTUAL)
    snap_hits = [(r.get("hit"), r.get("actual")) for r in graded["REC_YDS"]["rungs"]]
    snap_totals = {k: dict(v) for k, v in totals.items()}
    out, lt = nr.attach_book_lines(graded, BOOK, KO)
    assert [(r.get("hit"), r.get("actual")) for r in out["REC_YDS"]["rungs"]] == snap_hits
    assert totals == snap_totals
    assert [(r.get("hit"), r.get("actual")) for r in graded["REC_YDS"]["rungs"]] == snap_hits  # input not mutated
    assert "line" not in graded["REC_YDS"]["rungs"][0]


def test_book_line_fields():
    graded, _ = nr.grade(_card(), ACTUAL)
    out, lt = nr.attach_book_lines(graded, BOOK, KO)
    r = {(x["player_id"], bool(x.get("dup"))): x for x in out["REC_YDS"]["rungs"]}
    assert r[("t1", False)]["line"] == 70.5 and r[("t1", False)]["line_hit"] is True
    assert r[("t1", False)]["line_over"] == -115
    assert r[("t2", False)]["hit"] is True and r[("t2", False)]["line_hit"] is False
    assert r[("t3", False)]["line_hit"] is None and r[("t3", False)]["line_push"] is True
    assert r[("t4", False)]["line"] is None                   # taken after kickoff: refused
    assert r[("t5", False)]["line"] is None                   # outside the 48h window: refused
    assert r[("t6", False)]["line"] == 30.5 and r[("t6", False)]["line_hit"] is None  # DNP: priced, ungraded
    assert "line" not in r[("t1", True)]                      # dup rung untouched
    assert lt["REC_YDS"] == {"priced": 4, "n": 2, "hit": 1, "push": 1, "pct": 50.0}


def test_book_lines_unknown_kickoff_is_unpriced():
    graded, _ = nr.grade(_card(), ACTUAL)
    out, lt = nr.attach_book_lines(graded, BOOK, {})
    assert all(x.get("line") is None for x in out["REC_YDS"]["rungs"] if not x.get("dup"))
    assert lt["REC_YDS"]["priced"] == 0


def test_fetch_book_lines_maps_markets(monkeypatch):
    import io
    import json
    import urllib.request
    body = {"by_player_id": {"t1": {  # TEST DATA route response
        "player_reception_yds": {"line": 70.5, "over": -115, "taken_at": "2026-10-04T16:00:00+00:00"},
        "player_anytime_td": {"line": 0.5, "over": 150, "taken_at": "2026-10-04T16:00:00+00:00"},
        "player_1st_td": {"line": 0.5, "over": 900, "taken_at": "2026-10-04T16:00:00+00:00"}}}}
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(url, timeout=0):
        seen["url"] = url
        return R(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    got = nr.fetch_book_lines(2026, 4, KO)
    assert seen["url"].endswith("sport=nfl&date=2026-10-03")      # day before the first game
    assert set(got["t1"]) == {"REC_YDS", "TD"}                     # FTD is not a card market
    assert got["t1"]["REC_YDS"]["line"] == 70.5


def test_fetch_book_lines_failure_is_empty(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise OSError("test: no network")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert nr.fetch_book_lines(2026, 4, KO) == {}
