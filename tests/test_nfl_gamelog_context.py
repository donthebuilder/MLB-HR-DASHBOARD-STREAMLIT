"""nfl_gamelog / nfl_splits: the per-game context (date, home/away, weekday, roof,
surface, result, rest) and the new split buckets. TEST fixtures only, no network.
Run: PYTHONPATH=. python3 tests/test_nfl_gamelog_context.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots" / "nfl"))
import polars as pl
import nfl_gamelog as gl
import nfl_splits as sp

# TEST schedule: wk1 Thu home win, wk2 Neutral-site tie, wk3 unplayed (no score)
TEST_SCHED = pl.DataFrame({
    "game_id": ["T_01_AAA_BBB", "T_02_AAA_CCC", "T_03_DDD_AAA"],
    "game_type": ["REG"] * 3, "week": [1, 2, 3],
    "gameday": ["2099-09-03", "2099-09-14", "2099-09-21"],
    "weekday": ["Thursday", "Monday", "Sunday"],
    "location": ["Home", "Neutral", "Home"],
    "home_team": ["BBB", "CCC", "DDD"], "away_team": ["AAA", "AAA", "AAA"],
    "home_score": [10, 17, None], "away_score": [20, 17, None],
    "home_rest": [7, 7, 7], "away_rest": [7, 4, 14],
    "roof": ["dome", "outdoors", "closed"], "surface": ["fieldturf", "grass", "a_turf"],
})
gl._sched.cache_clear()
gl._sched = lambda season: TEST_SCHED          # no network
ctx = gl.game_context(2099)

a1 = ctx[(1, "AAA")]
assert a1 == {"d": "2099-09-03", "h": 0, "wd": "Thu", "rf": "dome", "sf": "fieldturf", "r": "W", "rs": 7}, a1
assert ctx[(1, "BBB")]["h"] == 1 and ctx[(1, "BBB")]["r"] == "L"
a2 = ctx[(2, "AAA")]
assert "h" not in a2, "neutral site must carry no home/away flag"
assert a2["r"] == "T" and a2["rs"] == 4 and a2["wd"] == "Mon"
a3 = ctx[(3, "AAA")]
assert "r" not in a3 and a3["rs"] == 14, "unplayed game has no result"

# existing keys + order are untouched by the additive fields
assert list(gl.MARKET_VALUE) == ["TD", "REC_YDS", "REC", "RUSH_YDS", "RUSH_ATT", "PASS_YDS", "KICK_PTS"]

# splits: every pair key is a real bucket; the old pairs come first, unchanged
for a, b in sp.SPLIT_PAIRS:
    assert a in sp.SPLITS and b in sp.SPLITS and a in sp.SPLIT_LABELS
assert sp.SPLIT_PAIRS[:7] == [("home", "away"), ("indoors", "outdoors"), ("grass", "turf"),
                              ("leading", "close"), ("close", "trailing"), ("h1", "h2"), ("rz", "field")]

# TEST pbp rows -> wd / won / rest
pbp = pl.DataFrame({
    "game_id": ["T_01_AAA_BBB"] * 2 + ["T_02_AAA_CCC"],
    "game_date": ["2099-09-03"] * 2 + ["2099-09-14"],
    "posteam": ["AAA", "BBB", "AAA"], "home_team": ["BBB", "BBB", "CCC"],
    "result": [-10, -10, 0],
})
sp.nfl = type("N", (), {"load_schedules": staticmethod(lambda seasons: TEST_SCHED)})
out = sp._with_context(pbp, 2099).to_dicts()
assert out[0]["wd"] == "Thursday" and out[0]["won"] == 1 and out[0]["rest"] == 7   # AAA away won
assert out[1]["won"] == 0                                                          # BBB home lost
assert out[2]["won"] is None and out[2]["rest"] == 4                               # tie: neither
print("test_nfl_gamelog_context OK")
