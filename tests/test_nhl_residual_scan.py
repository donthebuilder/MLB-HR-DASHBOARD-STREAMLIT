"""nhl_residual_scan: TEST fixtures only (seeded, synthetic, labelled TEST). Nothing here is a real NHL result."""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import nhl_residual_scan as S  # noqa: E402


def make_test_rows(seed=1, nights=12, per_night=60, planted=True, shift=0):
    """TEST rows in the lamp_goal_log_export shape. When planted, legs.toi lifts the goal chance INSIDE equal scores;
    pct.shotsPg is pure noise. TEST data, not real."""
    rng = random.Random(seed)
    out = []
    for n in range(nights):
        for i in range(per_night):
            score = rng.uniform(0, 100)
            toi = rng.uniform(900, 1300)
            p = 0.05 + 0.002 * score
            if planted:
                p += 0.25 * (toi - 900) / 400
            out.append({"game_id": 1000 + n * 10 + i % 10, "player_id": 5000 + i + shift, "model_version": "TEST-v1",
                        "game_date": "2099-01-%02d" % (n + 1), "score": score, "dressed": True, "hit": rng.random() < p,
                        "pct": {"shotsPg": rng.uniform(0, 100), "toi": 50.0},
                        "legs": {"shotsPg": rng.uniform(1, 4), "toi": toi, "goalsPg": 0.3, "rookie": None}})
    return out


def test_bh_qvalues_monotone():
    q = S.bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert abs(q[0] - 0.04) < 1e-9 and q[3] == 0.5 and all(0 <= x <= 1 for x in q)


def test_graded_rows_skip_undressed_and_ungraded_and_dedupe():
    rows = make_test_rows(nights=1, per_night=3)
    rows[0]["dressed"] = False
    rows[1]["hit"] = None
    rows.append(dict(rows[2], model_version="TEST-v2"))
    got = S.graded_rows(rows)
    assert len(got) == 1 and got[0]["version"] == "TEST-v2"


def test_flatten_finds_fields_and_ignores_ids():
    f = S.flatten(make_test_rows(nights=1, per_night=1)[0])
    assert "legs.toi" in f and "pct.shotsPg" in f and "legs.rookie" in f and "score" not in f and "game_id" not in f


def test_planted_signal_is_found_and_noise_is_not():
    rows = S.graded_rows(make_test_rows())
    res = S.scan_season(rows, "TEST")
    by = {r["field"]: r for r in res["fields"]}
    assert by["legs.toi"]["testable"] and by["legs.toi"]["lift_pts"] > 5 and by["legs.toi"]["q"] <= 0.1
    assert abs(by["pct.shotsPg"]["lift_pts"]) < 8 and by["pct.shotsPg"]["q"] > 0.1
    assert not by["legs.goalsPg"]["testable"]            # constant
    assert not by["pct.toi"]["testable"]


def test_trend_not_testable_with_few_nights_and_testable_with_many():
    short = S.scan_season(S.graded_rows(make_test_rows(nights=3)), "TEST")
    assert not {r["field"]: r for r in short["fields"]}["trend_toi"]["testable"]
    long = S.scan_season(S.graded_rows(make_test_rows(nights=12)), "TEST")
    assert {r["field"]: r for r in long["fields"]}["trend_toi"]["testable"]


def test_verdicts_follow_the_football_rule():
    good = {"testable": True, "q": 0.02, "lift_pts": 5.0}
    good_neg = {"testable": True, "q": 0.02, "lift_pts": -5.0}
    weak = {"testable": True, "q": 0.5, "lift_pts": 1.0}
    assert S.verdict(good, None, False)[0] == "one short season: cannot confirm"
    assert S.verdict(good, None, False)[1] is False
    assert S.verdict(good, good, True)[:2] == ("holds in both seasons", True)
    assert S.verdict(good, good_neg, True)[0] == "only one season"
    assert S.verdict(good, weak, True)[0] == "only one season"
    assert S.verdict(weak, weak, True)[0] == "no signal"


def test_never_raises_and_explains(tmp_path):
    r = S.build(tmp_path)
    assert r["status"] == "no data" and "not found" in r["notes"][0]
    (tmp_path / "lamp_goal_log_export.json").write_text(json.dumps(make_test_rows(nights=1, per_night=5)))
    assert S.build(tmp_path)["status"] == "too few rows"
    (tmp_path / "lamp_goal_log_export.json").write_text("not json")
    assert S.build(tmp_path)["status"] == "no data"
    assert S.main.__name__ == "main"


def test_two_seasons_and_bad_prior(tmp_path):
    (tmp_path / "lamp_goal_log_export.json").write_text(json.dumps(make_test_rows(seed=1)))
    (tmp_path / "prior.json").write_text(json.dumps(make_test_rows(seed=2, shift=100)))
    r = S.build(tmp_path, tmp_path / "prior.json")
    toi = {t["field"]: t for t in r["fields"]}["legs.toi"]
    assert toi["verdict"] == "holds in both seasons" and toi["add_to_score"]
    (tmp_path / "bad.json").write_text(json.dumps({"built_at": "x", "rows": []}))
    r = S.build(tmp_path, tmp_path / "bad.json")
    assert r["prior"] is None and any("prior season not used" in n for n in r["notes"])
    assert all(t["verdict"] != "holds in both seasons" for t in r["fields"])
