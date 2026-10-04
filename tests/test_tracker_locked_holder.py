"""build_tracking_slots grades the LOCKED holder of a role, not a stand-in
(2026-10-04, record audit A2). TEST DATA, made-up players.
A rebuilt slate dropped the man who held TOP/HR at lock; the tracker used to
fall back to pick_top() and grade whoever was left under TOP and HR.
Run: PYTHONPATH=. python3 tests/test_tracker_locked_holder.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import live_results_tracker as t  # noqa: E402

FAILED, N = [], 0
def check(name, got, want):
    global N
    N += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

def row(pid, role, hr=10.0):
    return {"player_id": pid, "player_name": f"P{pid}", "game_pk": 1, "team": "AAA", "game_pick_role": role,
            "hr_score": hr, "overall_score": hr, "hit_score": hr, "hrr_score": hr, "contact_score": hr}

rows = [row(101, "WATCH", 60.0), row(102, "HIT/HRR/CONTACT", 40.0)] + [row(200 + i, "", 20.0 - i) for i in range(6)]
holder = row(999, "TOP/HR", 0.0)   # dropped from the rebuilt slate; synthetic locked row

def roles(slots):
    return {s["pick_type"]: int(s["player_id"]) for s in slots if s["pick_type"] in ("TOP", "HR", "HIT", "HRR", "CONTACT")}

old = roles(t.build_tracking_slots(rows))
check("without the holder: TOP is a stand-in", old["TOP"] != 999, True)
new = roles(t.build_tracking_slots(rows, [holder]))
check("TOP graded on the locked holder", new["TOP"], 999)
check("HR graded on the locked holder", new["HR"], 999)
check("HIT still the slate's designated man", new["HIT"], 102)
top15 = [int(s["player_id"]) for s in t.build_tracking_slots(rows, [holder]) if s["pick_type"] == "TOP15"]
check("the holder never enters TOP15", 999 in top15, False)

print(f"{N - len(FAILED)}/{N} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
