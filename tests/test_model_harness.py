"""model_harness: the maths and the promotion rule. TEST data below, labelled; nothing here is a real result.
Run: PYTHONPATH=. python3 tests/test_model_harness.py"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import model_harness as H

# wilson: 0 of 0 is nothing; 50 of 100 is centred near 50
assert H.wilson(0, 0) == (0.0, 0.0)
lo, hi = H.wilson(50, 100)
assert 39 < lo < 41 and 59 < hi < 61, (lo, hi)

# paired: only games where the two picks differ carry information
p = H.paired([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
assert (p["only_a"], p["only_b"], p["games"]) == (1, 1, 5) and p["z"] == 0.0
p = H.paired([1] * 30 + [0] * 70, [0] * 100)
assert p["diff_pts"] == 30.0 and p["z"] > 5

def rows(n_games, picks):
    """TEST rows: each game has 3 players; picks[score] = function(game_index) -> index of the player that score picks; y set by 'who homers'."""
    out = []
    for g in range(n_games):
        for i in range(3):
            out.append({"night": f"2099-01-{1 + g % 28:02d}", "game": f"g{g}", "id": f"p{g}-{i}", "y": 1 if i == picks["truth"](g) else 0,
                        "s": {k: (10 if picks[k](g) == i else 1) for k in ("prod", "good", "base")}})
    return out

# a candidate that is right far more often than production AND the baseline is promoted; with too few games it is held
picks = {"truth": lambda g: g % 3, "prod": lambda g: (g + 1) % 3, "good": lambda g: g % 3, "base": lambda g: (g + 2) % 3}
big = H.evaluate(rows(300, picks), ["prod", "good", "base"], "prod", "base")
assert {r["score"]: r["verdict"] for r in big["scores"]}["good"].startswith("BEATS production and the baseline"), big["scores"][1]["verdict"]
small = H.evaluate(rows(60, picks), ["prod", "good", "base"], "prod", "base")
assert "not enough games" in {r["score"]: r["verdict"] for r in small["scores"]}["good"]
# determinism: same rows, same answer
assert H.evaluate(rows(300, picks), ["prod", "good", "base"], "prod", "base") == big
# a candidate that beats production but not the baseline is not promoted
picks2 = {"truth": lambda g: g % 3, "prod": lambda g: (g + 1) % 3, "good": lambda g: g % 3, "base": lambda g: g % 3}
held = H.evaluate(rows(300, picks2), ["prod", "good", "base"], "prod", "base")
assert "hold" in {r["score"]: r["verdict"] for r in held["scores"]}["good"] or "no clear" in {r["score"]: r["verdict"] for r in held["scores"]}["good"]
print("ok   model_harness: wilson, paired test, promotion rule (needs production AND baseline), determinism (TEST data)")


# ── NFL and NHL adapters. TEST DATA, labelled: invented players and games, never real results. ──
import json as _json
from pathlib import Path as _Path
import model_harness as _mh


def _nfl_fixture(tmp_path, locked_late=False):
    d = _Path(tmp_path)
    por = [{"season": 2026, "week": 9, "game_id": "G1", "run_id": "RUN1", "home": "AAA", "away": "BBB",
            "locked_late": locked_late, "model_versions": {"TD": "nfl_td_v2"}}]
    (d / "nfl_por_log_2026_w09.jsonl").write_text("\n".join(_json.dumps(x) for x in por))
    plog = [{"run_id": "RUN1"}] + [
        {"player_id": "P1", "team": "AAA", "market": "TD", "score": 80, "components": {"f_touches": 10, "f_xtd": 90}, "shadow": {"nfl_td_v3": {"score": 70}}},
        {"player_id": "P2", "team": "BBB", "market": "TD", "score": 60, "components": {"f_touches": 90, "f_xtd": 10}, "shadow": {"nfl_td_v3": {"score": 95}}},
        {"player_id": "P3", "team": "CCC", "market": "TD", "score": 99, "components": {}, "shadow": {}},      # another game: ignored
        {"player_id": "P4", "team": "AAA", "market": "REC", "score": 50, "components": {}, "shadow": {}},     # another market: ignored
        {"player_id": "P5", "team": "AAA", "market": "TD", "score": 40, "components": {}, "shadow": {}},     # no graded line: left out
    ]
    (d / "nfl_prediction_log_RUN1.jsonl").write_text("\n".join(_json.dumps(x) for x in plog))
    (d / "nfl_results_2026_w09.json").write_text(_json.dumps({"lines": {"P1": {"TD": 0.0}, "P2": {"TD": 2.0}, "P3": {"TD": 1.0}}}))
    return d


def test_nfl_adapter_joins_locked_run_to_graded_lines(tmp_path):
    rows = _mh.nfl_td_rows(_nfl_fixture(tmp_path))
    by = {r["id"]: r for r in rows}
    assert set(by) == {"P1", "P2"}                       # other game, other market and ungraded players are out
    assert by["P1"]["y"] == 0 and by["P2"]["y"] == 1     # 2 TDs counts as a hit
    assert by["P2"]["s"]["td_v3"] == 95 and by["P1"]["s"]["f_touches"] == 10
    assert by["P1"]["night"] == "2026-w09" and by["P1"]["version"] == "nfl_td_v2"


def test_nfl_adapter_drops_a_game_locked_late(tmp_path):
    assert _mh.nfl_td_rows(_nfl_fixture(tmp_path, locked_late=True)) == []


def test_nhl_adapter_reads_graded_dressed_rows_only(tmp_path):
    data = [
        {"game_id": 1, "player_id": 7, "model_version": "v1", "game_date": "2026-10-20", "score": 80, "pct": {"shotsPg": 90, "goalsPg": 70, "toi": 60},
         "legs": {"shotsPg": 3.1, "goalsPg": 0.4}, "dressed": True, "goals": 1, "hit": True},
        {"game_id": 1, "player_id": 8, "model_version": "v1", "game_date": "2026-10-20", "score": 40, "pct": {}, "legs": {}, "dressed": False, "goals": 0, "hit": False},
        {"game_id": 1, "player_id": 9, "model_version": "v1", "game_date": "2026-10-20", "score": 50, "pct": {}, "legs": {}, "dressed": True, "goals": None, "hit": None},
    ]
    (_Path(tmp_path) / "lamp_goal_log_export.json").write_text(_json.dumps(data))
    rows = _mh.nhl_goal_rows(_Path(tmp_path))
    assert [r["id"] for r in rows] == ["7"] and rows[0]["y"] == 1
    assert rows[0]["s"]["shots_pg"] == 3.1 and rows[0]["s"]["pct_goals"] == 70


def test_build_survives_a_market_with_no_rows(tmp_path):
    out = _mh.build(_Path(tmp_path))
    assert out["boards"]["nhl_goal"]["scores"] == [] and "no locked" in out["boards"]["nhl_goal"]["note"]


def test_a_shadow_with_no_rows_yet_does_not_blank_the_other_comparisons():
    """TEST data: the NHL composite is a candidate that has not run yet (its score is None on every row). The comparisons of the
    other scores must still be drawn on all games, and the empty shadow is reported 'no rows yet'."""
    picks = {"truth": lambda g: g % 3, "prod": lambda g: (g + 1) % 3, "good": lambda g: g % 3, "base": lambda g: (g + 2) % 3}
    rs = rows(200, picks)
    for r in rs:
        r["s"]["comp"] = None
    out = H.evaluate(rs, ["prod", "good", "comp", "base"], "prod", "base")
    assert out["games_compared"] == 200, out["games_compared"]
    by = {r["score"]: r for r in out["scores"]}
    assert by["comp"]["verdict"] == "no rows yet" and by["comp"]["best_in_game"]["n"] == 0
    assert by["good"]["best_in_game"]["n"] == 200


def test_nhl_adapter_carries_the_composite_scores(tmp_path):
    """TEST data: comp and comp_v1 ride from the site export into the row's scores."""
    import json
    (tmp_path / "lamp_goal_log_export.json").write_text(json.dumps([
        {"game_id": 1, "player_id": 9, "model_version": "lamp-goal-v3", "game_date": "2099-01-01", "score": 80, "pct": {}, "legs": {},
         "dressed": True, "goals": 1, "hit": True, "comp": 71.5, "comp_v1": 69.0}]))
    rs = H.nhl_goal_rows(tmp_path)
    assert rs[0]["s"]["comp"] == 71.5 and rs[0]["s"]["comp_v1"] == 69.0
    assert "comp" in H.NHL_GOAL["scores"]
