"""Passing TDs on the published NFL lines and game logs (2026-10-20, site milestones).

The site grades a "career passing TDs" milestone on the week's results line (PASS_TD) and rates it from the game
log (g_patd). Both are additive, non-market fields: not in MODELS, no bar, no card, no `rates` block. QBs only on the
line -- a receiver's 0 passing TDs is "not his", never a published 0 (eligible_lines()'s rule).
TEST DATA ONLY: the players and numbers below are made up.

Run: python3 -m pytest tests/test_nfl_pass_td_lines.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import polars as pl  # noqa: E402

from nfl_results import STATS, LINE_ONLY, eligible_lines, line_only, outcomes, _merge_lines  # noqa: E402
from nfl_scoring import MODELS  # noqa: E402
import nfl_gamelog  # noqa: E402


def _df(rows):
    schema = {"player_id": pl.Utf8, "name": pl.Utf8, "team": pl.Utf8, "position": pl.Utf8, **{c: pl.Float64 for c in STATS}}
    return pl.DataFrame([{**{c: 0.0 for c in STATS}, **r} for r in rows], schema=schema)


def test_pass_td_rides_the_qb_line_only():
    d = _df([
        {"player_id": "T-QB", "name": "Test QB", "team": "AAA", "position": "QB", "passing_yards": 250.0, "passing_tds": 2.0},
        {"player_id": "T-QB0", "name": "Test QB Zero", "team": "AAA", "position": "QB", "passing_yards": 90.0, "passing_tds": 0.0},
        {"player_id": "T-WR", "name": "Test WR", "team": "BBB", "position": "WR", "receiving_yards": 80.0, "receptions": 5.0},
    ])
    pos = {r["player_id"]: r["position"] for r in d.iter_rows(named=True)}
    lines = _merge_lines(eligible_lines(outcomes(d), pos), line_only(d, pos))
    assert lines["T-QB"]["PASS_TD"] == 2.0
    assert lines["T-QB0"]["PASS_TD"] == 0.0, "a QB's zero is a real line"
    assert "PASS_TD" not in lines["T-WR"], "a receiver's zero passing TDs is not his market"
    assert lines["T-QB"]["PASS_YDS"] == 250.0, "the market lines are untouched"


def test_pass_td_is_not_a_market():
    assert "PASS_TD" not in MODELS
    assert set(LINE_ONLY) == {"PASS_TD"} and LINE_ONLY["PASS_TD"][0] in STATS


def test_gamelog_carries_g_patd_but_no_rates_block():
    assert nfl_gamelog.LOG_ONLY == {"g_patd": "passing_tds"}
    assert "g_patd" not in {v[0] for v in nfl_gamelog.MARKET_VALUE.values()}


if __name__ == "__main__":   # CI runs each file as a script (.github/workflows/tests.yml)
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
