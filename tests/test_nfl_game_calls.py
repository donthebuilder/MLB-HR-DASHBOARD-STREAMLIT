"""bots/nfl/nfl_game_calls.py -- per-game TD calls, the lock log, grading.

TEST DATA, MADE UP. Teams AAA..HHH, players P1.. and their scores are
invented for these checks and describe no real player or game.

Run: python3 -m pytest tests/test_nfl_game_calls.py -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_game_calls as gc  # noqa: E402

NOW = dt.datetime(2099, 10, 4, 15, 0, tzinfo=dt.timezone.utc)
WEIGHTS = {"f_rz_opp": 0.22, "implied_total": 0.18, "f_touches": 0.16}


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def player(pid, team, opp, score, xtd=0.5, pos="RB", rz=50.0, it=50.0, bye=False, q=False):
    return {"player_id": pid, "name": f"Test {pid}", "team": team, "opp": opp, "position": pos,
            "on_bye": bye, "questionable": q, "scores": {"TD": score},
            "stats": {"xTD": xtd},
            "components": {"TD": {"f_rz_opp": rz, "implied_total": it, "f_touches": 50.0}}}


def payload(players, games, built=NOW):
    return {"season": 2099, "week": 4, "mode": "week", "built_at": built.isoformat(),
            "markets": [{"key": "TD", "positions": ["RB", "WR", "TE"], "weights": WEIGHTS}],
            "players": players, "games": games}


def game(gid, away, home, ko, state="pre"):
    return {"game_id": gid, "away": away, "home": home, "kickoff": iso(ko), "state": state}


def filler(n, team="ZZZ"):
    """Low scorers on a team with no game -- they fill out the board so the
    top-third cut is meaningful."""
    return [player(f"F{i}", team, "YYY", 10 + i * 0.01) for i in range(n)]


def by_gid(doc):
    return {g["game_id"]: g for g in doc["games"]}


# ── selection ────────────────────────────────────────────────────────────────

def test_top_and_second_and_floor():
    ko = NOW + dt.timedelta(hours=3)
    ps = [player("A1", "AAA", "BBB", 80), player("A2", "AAA", "BBB", 70),
          player("B1", "BBB", "AAA", 60), *filler(9)]
    doc = gc.build_calls(payload(ps, [game("g1", "AAA", "BBB", ko)]))
    assert doc["board_n"] == 12 and doc["top_third"] == 4
    g = by_gid(doc)["g1"]
    assert [(c["role"], c["player_id"]) for c in g["calls"]] == [("TOP", "A1"), ("TD", "B1")]
    assert g["calls"][1]["slate_rank"] == 3 and g["calls"][0]["of"] == 12
    assert g["no_call"] == []
    assert g["calls"][0]["probability_source"] == gc.PROBABILITY_SOURCE


def test_no_call_side_below_floor_and_top_never_floored():
    ko = NOW + dt.timedelta(hours=3)
    ps = [player("A1", "AAA", "BBB", 50), player("B1", "BBB", "AAA", 40),
          player("C1", "CCC", "DDD", 90), player("C2", "CCC", "DDD", 89),
          player("D1", "DDD", "CCC", 88), *[player(f"X{i}", "XXX", "YYY", 60 + i) for i in range(7)]]
    # board 12, cut 4: C1 C2 D1 X6 ... A1 is #11, B1 #12
    doc = gc.build_calls(payload(ps, [game("g1", "AAA", "BBB", ko)]))
    g = by_gid(doc)["g1"]
    assert [(c["role"], c["player_id"], c["slate_rank"]) for c in g["calls"]] == [("TOP", "A1", 11)]
    assert len(g["no_call"]) == 1
    nc = g["no_call"][0]
    assert nc["team"] == "BBB" and nc["best_player_id"] == "B1" and nc["best_rank"] == 12
    assert nc["reason"] == "below_floor" and "#12 of 12" in nc["text"]


def test_tie_broken_by_xtd_then_weight_order_and_logged():
    ko = NOW + dt.timedelta(hours=3)
    ps = [player("A1", "AAA", "BBB", 70, xtd=0.4), player("A2", "AAA", "BBB", 70, xtd=0.6),
          player("B1", "BBB", "AAA", 65, xtd=0.5, rz=40), player("B2", "BBB", "AAA", 65, xtd=0.5, rz=90), *filler(8)]
    g = by_gid(gc.build_calls(payload(ps, [game("g1", "AAA", "BBB", ko)])))["g1"]
    top, second = g["calls"]
    assert top["player_id"] == "A2" and "broken by xTD/gm" in top["tie_break"]
    assert second["player_id"] == "B2" and "broken by f_rz_opp" in second["tie_break"]
    # Deterministic: same input, same output.
    g2 = by_gid(gc.build_calls(payload(list(reversed(ps)), [game("g1", "AAA", "BBB", ko)])))["g1"]
    assert [c["player_id"] for c in g2["calls"]] == ["A2", "B2"]


def test_bye_team_and_ineligible_excluded_from_board():
    ko = NOW + dt.timedelta(hours=3)
    ps = [player("A1", "AAA", "BBB", 70), player("B1", "BBB", "AAA", 60),
          player("Q1", "AAA", "BBB", 99, pos="QB"),
          player("E1", "EEE", None, 95, bye=True),
          {**player("N1", "AAA", "BBB", 0), "scores": {"TD": None}}, *filler(4)]
    doc = gc.build_calls(payload(ps, [game("g1", "AAA", "BBB", ko)]))
    assert doc["board_n"] == 6
    assert [c["player_id"] for c in by_gid(doc)["g1"]["calls"]] == ["A1", "B1"]
    assert all(g["away"] != "EEE" and g["home"] != "EEE" for g in doc["games"])


def test_only_one_team_scored():
    ko = NOW + dt.timedelta(hours=3)
    ps = [player("A1", "AAA", "BBB", 70)]
    g = by_gid(gc.build_calls(payload(ps, [game("g1", "AAA", "BBB", ko)])))["g1"]
    assert [c["role"] for c in g["calls"]] == ["TOP"]
    assert g["no_call"][0]["team"] == "BBB" and g["no_call"][0]["reason"] == "no_scored_player"


def test_game_missing_from_week_file_and_game_with_nobody_scored():
    ko = NOW + dt.timedelta(hours=3)
    # Players on CCC/DDD, but their game is not in the week file: no entry is
    # invented for it. Game g2 has no scored player on either side: it keeps
    # its entry, with no calls and both reasons.
    ps = [player("A1", "AAA", "BBB", 70), player("B1", "BBB", "AAA", 60),
          player("C1", "CCC", "DDD", 99)]
    doc = gc.build_calls(payload(ps, [game("g1", "AAA", "BBB", ko), game("g2", "GGG", "HHH", ko)]))
    assert set(by_gid(doc)) == {"g1", "g2"}
    g2 = by_gid(doc)["g2"]
    assert g2["calls"] == [] and {n["team"] for n in g2["no_call"]} == {"GGG", "HHH"}


# ── the lock log ─────────────────────────────────────────────────────────────

def _write(d, ps, games, built, now):
    return gc.write(payload(ps, games, built), d, "nfl_", run_meta={"run_id": "test"}, now=now)


def test_repick_before_lock_and_locked_after_kickoff():
    d = Path(tempfile.mkdtemp())
    ko = NOW + dt.timedelta(minutes=60)
    g = [game("g1", "AAA", "BBB", ko)]
    # Build 1 (T-60): A1 tops. Build 2 (T-20): A1 ruled out, A2 re-picked.
    _write(d, [player("A1", "AAA", "BBB", 80), player("A2", "AAA", "BBB", 70), player("B1", "BBB", "AAA", 60)],
           g, NOW, NOW)
    t2 = ko - dt.timedelta(minutes=20)
    _write(d, [player("A2", "AAA", "BBB", 70), player("B1", "BBB", "AAA", 60)], g, t2, t2)
    # Build 3 AFTER kickoff: the live board now ranks someone else first.
    t3 = ko + dt.timedelta(minutes=30)
    doc = _write(d, [player("A9", "AAA", "BBB", 99), player("B1", "BBB", "AAA", 60)],
                 [game("g1", "AAA", "BBB", ko, state="in")], t3, t3)
    rows = gc.read_log(gc.log_path(d, 2099, 4))
    assert len(rows) == 2                       # nothing written after kickoff
    assert all(gc.parse_ts(r["built_at"]) < ko for r in rows)
    lk = gc.locked_rows(rows)["g1"]
    assert lk["calls"][0]["player_id"] == "A2"  # the last pre-kick row wins
    out = by_gid(doc)["g1"]
    assert out["locked"] is True and out["calls"][0]["player_id"] == "A2"
    assert json.loads((d / "nfl_game_calls.json").read_text())["games"][0]["calls"][0]["player_id"] == "A2"


def test_never_writes_after_kickoff_even_if_built_before():
    d = Path(tempfile.mkdtemp())
    ko = NOW + dt.timedelta(minutes=5)
    # Built at T-10 but the write lands at T+1: no row.
    _write(d, [player("A1", "AAA", "BBB", 80)], [game("g1", "AAA", "BBB", ko)],
           ko - dt.timedelta(minutes=10), ko + dt.timedelta(minutes=1))
    assert gc.read_log(gc.log_path(d, 2099, 4)) == []
    doc = json.loads((d / "nfl_game_calls.json").read_text())
    assert doc["games"][0]["locked"] is True and doc["games"][0]["calls"] == []
    assert "no pregame build" in doc["games"][0]["no_lock_reason"]


def test_locked_rows_ignores_a_post_kick_row_in_the_file():
    ko = NOW
    rows = [{"game_id": "g1", "kickoff": iso(ko), "built_at": (ko - dt.timedelta(minutes=30)).isoformat(),
             "calls": [{"role": "TOP", "player_id": "PRE"}]},
            {"game_id": "g1", "kickoff": iso(ko), "built_at": (ko + dt.timedelta(minutes=1)).isoformat(),
             "calls": [{"role": "TOP", "player_id": "POST"}]}]
    assert gc.locked_rows(rows)["g1"]["calls"][0]["player_id"] == "PRE"


def test_log_is_append_only_one_line_per_game_per_build():
    d = Path(tempfile.mkdtemp())
    ko = NOW + dt.timedelta(hours=2)
    gs = [game("g1", "AAA", "BBB", ko), game("g2", "CCC", "DDD", ko)]
    ps = [player("A1", "AAA", "BBB", 80), player("C1", "CCC", "DDD", 70)]
    _write(d, ps, gs, NOW, NOW)
    first = gc.log_path(d, 2099, 4).read_text()
    _write(d, ps, gs, NOW + dt.timedelta(minutes=15), NOW + dt.timedelta(minutes=15))
    after = gc.log_path(d, 2099, 4).read_text()
    assert after.startswith(first) and len(gc.read_log(gc.log_path(d, 2099, 4))) == 4


def test_restore_failure_stands_down():
    d = Path(tempfile.mkdtemp())
    os.environ[gc.RESTORE_ENV] = "0"
    try:
        _write(d, [player("A1", "AAA", "BBB", 80)], [game("g1", "AAA", "BBB", NOW + dt.timedelta(hours=1))], NOW, NOW)
        assert not gc.log_path(d, 2099, 4).exists()
        assert (d / "nfl_game_calls.json").exists()
    finally:
        del os.environ[gc.RESTORE_ENV]


def test_next_week_preview_never_logged():
    d = Path(tempfile.mkdtemp())
    doc = gc.write(payload([player("A1", "AAA", "BBB", 80)], [game("g1", "AAA", "BBB", NOW + dt.timedelta(days=7))]),
                   d, "nfl_next_", now=NOW)
    assert doc["mode"] == "next" and (d / "nfl_next_game_calls.json").exists()
    assert list(d.glob("*log*")) == []


# ── grading ──────────────────────────────────────────────────────────────────

def _log(d, ko):
    _write(d, [player("A1", "AAA", "BBB", 80), player("B1", "BBB", "AAA", 70, q=True), *filler(3)],
           [game("g1", "AAA", "BBB", ko), game("g2", "CCC", "DDD", ko + dt.timedelta(days=1))],
           ko - dt.timedelta(hours=1), ko - dt.timedelta(hours=1))


def test_grade_hit_miss_void_pending_and_never_regrade():
    d = Path(tempfile.mkdtemp())
    ko = NOW - dt.timedelta(hours=5)
    _log(d, ko)
    # A1 scored; B1 has no line (void). CCC/DDD have not played: pending.
    lines = {"A1": {"TD": 1.0}, "X": {"TD": 0.0}}
    gp = gc.run_grading(d, 2099, 4, lines, {"AAA", "BBB"}, set(lines), now=NOW)
    doc = json.loads(gp.read_text())
    g = {x["game_id"]: x for x in doc["games"]}
    res = {c["player_id"]: c["result"] for c in g["g1"]["calls"]}
    assert res == {"A1": "hit", "B1": "void"}
    vr = next(c for c in g["g1"]["calls"] if c["player_id"] == "B1")["void_reason"]
    assert "did not play" in vr and "questionable" in vr
    assert g["g2"]["status"] == "pending"
    s = doc["summary"]
    assert s["top"] == {"n": 1, "hit": 1, "void": 0} and s["second"]["void"] == 1 and s["voids"] == 1
    # Stats change later (a correction): the graded game is NOT regraded.
    gc.run_grading(d, 2099, 4, {"A1": {"TD": 0.0}, "B1": {"TD": 2.0}}, {"AAA", "BBB"}, {"A1", "B1"}, now=NOW)
    g = {x["game_id"]: x for x in json.loads(gp.read_text())["games"]}
    assert {c["player_id"]: c["result"] for c in g["g1"]["calls"]} == {"A1": "hit", "B1": "void"}
    tot = json.loads(gc.totals_path(d, 2099).read_text())
    assert tot["top"]["n"] == 1 and tot["calls"] == 1 and tot["bands"]["1-10"]["hit"] == 1


def test_grade_miss_and_position_void():
    d = Path(tempfile.mkdtemp())
    ko = NOW - dt.timedelta(hours=5)
    _log(d, ko)
    # A1 played, no TD (miss). B1 recorded a line, but not TD-eligible (raw id present).
    gp = gc.run_grading(d, 2099, 4, {"A1": {"TD": 0.0}}, {"AAA"}, {"A1", "B1"}, now=NOW)
    g = {x["game_id"]: x for x in json.loads(gp.read_text())["games"]}["g1"]
    res = {c["player_id"]: (c["result"], c.get("void_reason", "")) for c in g["calls"]}
    assert res["A1"][0] == "miss" and res["B1"][0] == "void" and "TD-eligible" in res["B1"][1]


def test_no_log_writes_nothing():
    d = Path(tempfile.mkdtemp())
    assert gc.run_grading(d, 2099, 4, {"A1": {"TD": 1.0}}, {"AAA"}, {"A1"}, now=NOW) is None
    assert list(d.iterdir()) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
