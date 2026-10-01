"""pick_lock.append_por_rows / backfill_por_rows (2026-10-01).

Test data, made up: one run log with two games, one por_log. Checks the
locked game's rows are copied from the locked run (only that game), a second
call writes nothing, a lock whose run log is gone writes nothing, and the
backfill marks a date done so it never re-reads it.

Run: PYTHONPATH=. python3 tests/test_pick_lock_por_rows.py
"""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import pick_lock  # noqa: E402

FAILED, CHECKS = [], 0
def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

d = Path(tempfile.mkdtemp())
hdr = {"run_id": "2026-09-30.170000Z.test", "generated_at": "2026-09-30T17:00:00+00:00", "model_versions": {"hr": "mlb_hr_v4"}}
rows = [dict(prediction_type="slate_row", prediction_date="2026-09-30", player_id=i, player=f"T.Est{i}", game_pk=g,
             scores={"hr": 50 + i}, components={"season_power": 60}, run_id=hdr["run_id"]) for i, g in ((1, "111"), (2, "111"), (3, "222"))]
(d / "prediction_log_2026-09-30.170000Z.test.jsonl").write_text("\n".join(json.dumps(x) for x in [hdr] + rows) + "\n")
lock = {"run_id": hdr["run_id"], "generated_at": hdr["generated_at"]}

n = pick_lock.append_por_rows("2026-09-30", [("111", lock)], d)
check("rows copied for the locked game only", n, 2)
out = [json.loads(l) for l in (d / "por_rows_2026-09-30.jsonl").read_text().splitlines()]
check("game 111 rows", sorted(r["player_id"] for r in out), [1, 2])
check("scores kept", out[0]["scores"], {"hr": 51})
check("second call writes nothing", pick_lock.append_por_rows("2026-09-30", [("111", lock)], d), 0)
check("missing run log writes nothing", pick_lock.append_por_rows("2026-09-30", [("222", {"run_id": "gone"})], d), 0)

# backfill: a past date with a por_log and no por_rows file yet
(d / "prediction_log_2026-09-29.170000Z.test.jsonl").write_text("\n".join(json.dumps(x) for x in [dict(hdr, run_id="r29")] + [dict(r, prediction_date="2026-09-29", run_id="r29") for r in rows]) + "\n")
(d / "por_log_2026-09-29.jsonl").write_text(json.dumps({"prediction_date": "2026-09-29", "game_pk": "222", "run_id": "r29"}) + "\n")
check("backfill fills the past date", pick_lock.backfill_por_rows(d), 1)
check("backfill runs once per date", pick_lock.backfill_por_rows(d), 0)

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
