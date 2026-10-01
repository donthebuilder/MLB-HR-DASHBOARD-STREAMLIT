"""nfl_pick_lock: one player, one rung (2026-10-01). Test data, made up.

Run 1, before kickoff: TD card #1 C, #2 A, #3 B. Run 2, after kickoff, the
board re-ranks: #2 B, #3 A. The lock freezes #2 as A (the last pregame
occupant); the restore must not also keep the live #3 A -- A appears once.
Also: a slot held by the same player pre-game takes the newer score.
Run: PYTHONPATH=. python3 tests/test_nfl_pick_lock_dupes.py"""
import datetime as dt, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
import nfl_pick_lock as pl  # noqa: E402

FAILED, CHECKS = [], 0
def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

d = Path(tempfile.mkdtemp())
def write(kickoff, rungs):
    (d / "week.json").write_text(json.dumps({"season": 2099, "week": 4, "mode": "week",
        "games": [{"game_id": "g1", "home": "AAA", "away": "BBB", "kickoff": kickoff}]}))
    (d / "picks.json").write_text(json.dumps({"card": {"TD": {"rungs": rungs}}}))
def rung(rank, pid, score):
    return {"rank": rank, "player_id": pid, "name": f"T.{pid}", "team": "AAA", "opp": "BBB", "position": "RB", "score": score}
def run():
    sys.argv = ["nfl_pick_lock.py", "--dir", str(d), "--apply"]
    pl.main()
    return [(r["rank"], r["player_id"]) for r in json.loads((d / "picks.json").read_text())["card"]["TD"]["rungs"]]

future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
write(future, [rung(1, "C", 80), rung(2, "A", 75), rung(3, "B", 70)])
run()
write(future, [rung(1, "C", 80), rung(2, "A", 77), rung(3, "B", 70)])   # same occupant, newer score
run()
ledger = json.loads((d / "pick_lock.json").read_text())
slot2 = next(v for k, v in (ledger.get("card") or ledger.get("card_locks") or {}).items() if k.endswith("|2")) if (ledger.get("card") or ledger.get("card_locks")) else None
check("pre-game score refreshed for the same occupant", (slot2 or {}).get("meta", {}).get("score"), 77)
write(past, [rung(1, "C", 80), rung(2, "B", 74), rung(3, "A", 72)])     # re-ranked after kickoff
out = run()
# the late-lock case: a slot first seen after kickoff (#4) holding a player
# already locked at #2
write(past, [rung(1, "C", 80), rung(2, "B", 74), rung(3, "B2", 71), rung(4, "A", 70)])
out2 = run()
check("late-locked duplicate dropped", [p for _, p in out2].count("A"), 1)
pids = [p for _, p in out]
check("A appears once", pids.count("A"), 1)
check("#2 stays the locked A", dict(out).get(2), "A")
print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
