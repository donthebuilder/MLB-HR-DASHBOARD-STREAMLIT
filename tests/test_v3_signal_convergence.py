"""Signal convergence in the shadow lane (2026-09-01).

Pins: the seven signals are read off the fields the slate publishes, the count
is what rides on each v3 row, the tier buckets grade correctly, and the
tie-break never touches hr_score_v3 itself (it orders inside equal scores).

Run: python tests/test_v3_signal_convergence.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import hr_v3_shadow as V  # noqa: E402

FAILED = []
N = 0


def check(name, cond, detail=""):
    global N
    N += 1
    if not cond:
        FAILED.append(f"{name}: {detail}")


row = {"pitch_type_match_flag": True, "weak_spot_flag": False, "pitcher_mistake_match": True,
       "hr_pace_flag": False, "weather_wind_boost": 0.03, "park_hr_factor": 0.9,
       "last5_hr": 2, "power_watch_flag": False, "high_confidence_hr_flag": True}
sigs = V.signals_of(row)
check("five of seven fire on the fixture", sorted(sigs) == ["air", "form", "mistake", "pitch", "power"], sigs)
check("air fires on the park alone", "air" in V.signals_of({"park_hr_factor": 1.07}), None)
check("air does not fire on a dead park with no wind", "air" not in V.signals_of({"park_hr_factor": 1.0, "weather_wind_boost": 0.0}), None)
check("a bad row never raises", V.signals_of({"last5_hr": "x", "park_hr_factor": None}) == [], None)
check("tiers", [V.tier_of(c) for c in (0, 1, 3, 4, 9)] == ["0", "1", "3", "4+", "4+"], None)

# grade: two nights in a temp DATA dir
tmp = Path(tempfile.mkdtemp())
V.DATA = tmp
V.BBE = tmp / "bbe_history"
V.BBE.mkdir()
board = {"date": "2026-09-01", "rows": [
    {"player_id": 1, "hr_score_v3": 80, "hr_score_live": 70, "signal_count": 4, "game_pick_role": "TOP"},
    {"player_id": 2, "hr_score_v3": 80, "hr_score_live": 60, "signal_count": 1, "game_pick_role": ""},
    {"player_id": 3, "hr_score_v3": 60, "hr_score_live": 50, "signal_count": 0, "game_pick_role": ""},
    {"player_id": 4, "hr_score_v3": 55, "hr_score_live": 40, "signal_count": 2, "game_pick_role": ""},
]}
(tmp / "hr_v3_2026-09-01.json").write_text(json.dumps(board))
(V.BBE / "bbe_2026-09-01.jsonl").write_text("\n".join(json.dumps(x) for x in [
    {"batter_id": 1, "is_hr": True}, {"batter_id": 2, "is_hr": False},
    {"batter_id": 3, "is_hr": False}, {"batter_id": 4, "is_hr": True},
]) + "\n")


class A: date = "2026-09-01"


rc = V.cmd_grade(A())
check("grade runs", rc == 0, rc)
rec = [json.loads(l) for l in (tmp / "hr_v3_record.jsonl").read_text().splitlines()][-1]
check("tier 4+ graded 1/1", rec["sig_tiers"].get("4+") == [1, 1], rec["sig_tiers"])
check("tier 0 graded 0/1", rec["sig_tiers"].get("0") == [0, 1], rec["sig_tiers"])
check("tier 2 graded 1/1", rec["sig_tiers"].get("2") == [1, 1], rec["sig_tiers"])
check("tie-break top-15 recorded", rec.get("v3_sig_top15") == [2, 4], rec.get("v3_sig_top15"))
check("v3 top-N itself untouched by the count", rec["v3_top5"] == [2, 4], rec["v3_top5"])

print(f"{N - len(FAILED)}/{N} checks passed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
