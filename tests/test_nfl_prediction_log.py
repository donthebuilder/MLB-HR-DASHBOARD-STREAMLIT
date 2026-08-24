"""bots/nfl/nfl_bot.py's prediction log -- build_nfl_run_meta(),
build_nfl_prediction_log_lines(), write_nfl_prediction_log(). The NFL
sibling of the prediction-log checks in tests/test_model_foundation.py,
scoped to what this task actually added (row shape / file shape), not a
port of that file's full MLB-specific coverage.

Run: python3 -m pytest tests/test_nfl_prediction_log.py -v
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_bot as nb  # noqa: E402


def _sample_players():
    return [
        {
            "player_id": "p1", "name": "Player One", "team": "ARI", "opp": "SF",
            "position": "WR", "carryover": False, "questionable": True, "low_sample": False,
            "scores": {"REC_YDS": 72.5, "REC": 60.1},
            "components": {
                "REC_YDS": {"c_f_wopr": 80.0, "c_f_receiving_yards": 55.0},
                "REC": {"c_f_target_share": 55.0},
            },
        },
        {
            "player_id": "p2", "name": "Player Two", "team": "SF", "opp": "ARI",
            "position": "RB", "carryover": True, "questionable": False, "low_sample": True,
            "scores": {"RUSH_YDS": 41.0, "RUSH_ATT": 33.0, "TD": 12.0},
            "components": {"RUSH_YDS": {"c_f_carries": 90.0}},
        },
        {
            # A player with no scored markets at all -- must contribute zero
            # rows, not a row full of Nones.
            "player_id": "p3", "name": "Player Three", "team": "SF", "opp": "ARI",
            "position": "TE", "scores": {}, "components": {},
        },
    ]


def test_run_meta_shape_week_mode():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    for key in ("run_id", "generated_at", "mode", "season", "week", "trigger",
                "git_sha", "model_family", "model_versions", "config_hashes",
                "schema_version", "env"):
        assert key in rm, f"run_meta missing {key!r}"
    assert rm["mode"] == "week"
    assert rm["season"] == 2026
    assert rm["week"] == 3
    assert rm["model_family"] == "moonshot-nfl"
    assert rm["run_id"].startswith("2026-wk03.")
    assert rm["config_hashes"]["nfl"] is None or rm["config_hashes"]["nfl"].startswith("sha256:")


def test_run_meta_week_none_does_not_crash_and_uses_wkNA():
    # The common production case: none of nfl.yml's scheduled firings pass
    # --week, so mode=="week" with week=None happens on essentially every
    # real scheduled run once the season starts.
    rm = nb.build_nfl_run_meta("week", 2026, None, None)
    assert rm["week"] is None
    assert "wkNA" in rm["run_id"]
    assert "None" not in rm["run_id"]


def test_run_meta_preseason_uses_a_date_key():
    rm = nb.build_nfl_run_meta("preseason", 2026, None, None)
    assert rm["mode"] == "preseason"
    # key is the UTC date, e.g. "2026-08-24" -- 10 chars before the first "."
    key = rm["run_id"].split(".", 1)[0]
    assert len(key) == 10 and key.count("-") == 2


def test_run_id_is_traceable_back_to_its_own_generated_at_second():
    rm = nb.build_nfl_run_meta("week", 2026, 5, None)
    hhmmss = rm["run_id"].split(".")[1]
    assert hhmmss.endswith("Z")
    assert len(hhmmss) == 7  # HHMMSSZ


def test_prediction_log_first_line_is_the_run_meta():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    lines = nb.build_nfl_prediction_log_lines(rm, _sample_players())
    assert lines[0] == rm


def test_prediction_log_one_line_per_player_per_scored_market():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    lines = nb.build_nfl_prediction_log_lines(rm, _sample_players())
    # run_meta + 2 markets for p1 + 3 markets for p2 + 0 for p3
    assert len(lines) == 1 + 2 + 3 + 0


def test_prediction_log_row_shape_and_values():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    lines = nb.build_nfl_prediction_log_lines(rm, _sample_players())
    rows = lines[1:]
    expected_keys = {
        "player_id", "player", "team", "opp", "position", "market",
        "run_id", "generated_at", "model_version", "config_hash",
        "score", "components", "carryover", "questionable", "low_sample",
    }
    for row in rows:
        assert set(row.keys()) == expected_keys

    rec_yds_row = next(r for r in rows if r["player_id"] == "p1" and r["market"] == "REC_YDS")
    assert rec_yds_row["score"] == 72.5
    assert rec_yds_row["model_version"] == rm["model_versions"]["REC_YDS"]
    assert rec_yds_row["run_id"] == rm["run_id"]
    assert rec_yds_row["config_hash"] == rm["config_hashes"]["nfl"]
    assert rec_yds_row["components"] == {"c_f_wopr": 80.0, "c_f_receiving_yards": 55.0}
    assert rec_yds_row["questionable"] is True


def test_player_with_no_scores_contributes_no_rows():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    lines = nb.build_nfl_prediction_log_lines(rm, _sample_players())
    assert not any(r.get("player_id") == "p3" for r in lines[1:])


def test_write_nfl_prediction_log_produces_valid_jsonl():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    players = _sample_players()
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        path = nb.write_nfl_prediction_log(rm, players, out, "nfl_")
        assert path is not None
        assert path.name == f"nfl_prediction_log_{rm['run_id']}.jsonl"
        assert path.parent == out

        raw_lines = path.read_text(encoding="utf-8").splitlines()
        assert len(raw_lines) == 1 + 2 + 3  # run_meta + 5 scored rows

        first = json.loads(raw_lines[0])
        assert first["run_id"] == rm["run_id"]
        assert first["model_family"] == "moonshot-nfl"
        assert set(first.keys()) == set(rm.keys())

        for raw in raw_lines[1:]:
            obj = json.loads(raw)
            assert obj["run_id"] == rm["run_id"]
            assert "market" in obj and "score" in obj and "player_id" in obj


def test_write_nfl_prediction_log_filename_never_collides_with_mlb():
    rm = nb.build_nfl_run_meta("week", 2026, 3, None)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        path = nb.write_nfl_prediction_log(rm, _sample_players(), out, "nfl_")
        # MLB's own glob in .github/scripts/publish_data.sh is
        # "prediction_log_*.jsonl" -- this file must not match it, only the
        # nfl_-prefixed glob.
        assert path.name.startswith("nfl_prediction_log_")
