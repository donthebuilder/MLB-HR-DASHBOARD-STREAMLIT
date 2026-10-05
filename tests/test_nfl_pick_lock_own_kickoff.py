"""nfl_pick_lock: a slot freezes at ITS OWN player's kickoff (2026-10-03 audit).
Test data, made up. Two games: EARLY (team EEE) and LATE (team LLL).

Run 1, both pregame: TD #1 L1 (LATE), #2 E1 (EARLY).
Run 2, EARLY has kicked off, LATE hasn't: the board re-ranks #2 to L2 (LATE).
The old code read the kickoff of the REPLACEMENT (LATE, not started) and swapped
E1 out after his game began. Now #2 must stay E1, locked.
Run 3, same instant: #1 (held by L1, LATE not started) is re-ranked to E2,
whose EARLY game already started -- a post-kickoff pick, refused: #1 stays L1
in the ledger AND on the published card (which the week archive copies).
Run: PYTHONPATH=. python3 tests/test_nfl_pick_lock_own_kickoff.py"""
import datetime as dt, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
import nfl_pick_lock as pl  # noqa: E402

# These exercise main(), not the network: a fresh week has no previous ledger.
# (A FAILED read now raises instead of reading as empty -- see test_nfl_failed_reads.)
import io, urllib.error, urllib.request  # noqa: E402
def _no_branch_copy(*a, **k):   # local ledger (if any) is read first; the branch has none yet
    raise urllib.error.HTTPError("u", 404, "not found", {}, io.BytesIO(b""))
urllib.request.urlopen = _no_branch_copy

FAILED, CHECKS = [], 0
def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

d = Path(tempfile.mkdtemp())
now = dt.datetime.now(dt.timezone.utc)
def iso(m): return (now + dt.timedelta(minutes=m)).isoformat()
def write(early_kick, late_kick, rungs):
    (d / "week.json").write_text(json.dumps({"season": 2099, "week": 5, "mode": "week", "games": [
        {"game_id": "gE", "home": "EEE", "away": "EAA", "kickoff": early_kick},
        {"game_id": "gL", "home": "LLL", "away": "LAA", "kickoff": late_kick}]}))
    (d / "picks.json").write_text(json.dumps({"card": {"TD": {"rungs": rungs}}}))
def rung(rank, pid, team):
    return {"rank": rank, "player_id": pid, "name": f"T.{pid}", "team": team, "opp": "X", "position": "RB", "score": 70 - rank}
def run():
    sys.argv = ["nfl_pick_lock.py", "--dir", str(d), "--apply"]
    pl.main()
    return dict((r["rank"], r["player_id"]) for r in json.loads((d / "picks.json").read_text())["card"]["TD"]["rungs"])
def ledger():
    j = json.loads((d / "pick_lock.json").read_text())
    return j.get("card_locks") or j.get("card") or {}

write(iso(60), iso(240), [rung(1, "L1", "LLL"), rung(2, "E1", "EEE")])
run()
check("run 1 pregame: nothing locked", sum(1 for s in ledger().values() if s.get("locked")), 0)

write(iso(-5), iso(240), [rung(1, "L1", "LLL"), rung(2, "L2", "LLL")])   # EARLY started; L2 re-ranked into #2
out = run()
check("#2 keeps the EARLY player after his kickoff", out.get(2), "E1")
check("#2 is locked", ledger().get("TD|2", {}).get("locked"), True)
check("#2 history has no L2", [h["player_id"] for h in ledger().get("TD|2", {}).get("history", [])].count("L2"), 0)
check("#1 (LATE, not started) still open", ledger().get("TD|1", {}).get("locked"), False)

write(iso(-5), iso(240), [rung(1, "E2", "EEE"), rung(2, "E1", "EEE")])   # an EARLY (started) player re-ranked into LATE's #1
out = run()
check("#1 ledger refuses a player whose game already started", ledger().get("TD|1", {}).get("stub", {}).get("player_id"), "L1")
check("#1 published card refuses him too", out.get(1), "L1")
check("#1 stays open for its own (LATE) kickoff", ledger().get("TD|1", {}).get("locked"), False)

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
