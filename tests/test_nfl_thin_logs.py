"""nfl_thin_logs: keeps the last pre-kickoff run per window, all of the newest two weeks.
Run: PYTHONPATH=. python3 tests/test_nfl_thin_logs.py"""
import datetime as dt, importlib.util, json, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("thin", Path(__file__).resolve().parents[1] / ".github/scripts/nfl_thin_logs.py")
thin = importlib.util.module_from_spec(spec); spec.loader.exec_module(thin)

U = dt.timezone.utc
def mk(d, week, t, n):
    rid = f"2026-wk{week:02d}.{t.strftime('%H%M%S')}Z.gha-{n}"
    h = {"run_id": rid, "generated_at": t.isoformat(), "mode": "week", "week": week}
    (d / f"nfl_prediction_log_{rid}.jsonl").write_text(json.dumps(h) + "\n")
    (d / f"nfl_signal_log_{rid}.jsonl").write_text(json.dumps(h) + "\n")
    return rid

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    # week 1: runs every 15 min 11:00-13:00 and 14:30-16:15 (kickoffs 13:00 / 16:25) on one day
    base = dt.datetime(2026, 9, 13, 11, 0, tzinfo=U)
    ids = {}
    n = 0
    for m in range(0, 121, 15):
        n += 1; t = base + dt.timedelta(minutes=m); ids[t] = mk(d, 1, t, n)
    for m in range(210, 316, 15):
        n += 1; t = base + dt.timedelta(minutes=m); ids[t] = mk(d, 1, t, n)
    # sparse runs in the middle (a lone run 20:00) after everything
    n += 1; late = base + dt.timedelta(hours=9); ids[late] = mk(d, 1, late, n)
    ko1, ko2 = base + dt.timedelta(minutes=121), base + dt.timedelta(minutes=325)
    por = [{"game_id": str(i), "kickoff": (ko1 if i < 3 else ko2).isoformat()} for i in range(6)]
    (d / "nfl_por_log_2026_w01.jsonl").write_text("\n".join(json.dumps(x) for x in por))
    # weeks 2 and 3 (newest two): everything kept
    for wk in (2, 3):
        for i in range(5):
            n += 1; mk(d, wk, dt.datetime(2026, 9, 20 + 7 * (wk - 2), 12 + i, 0, tzinfo=U), n)
    p = thin.plan(d)
    kept = {f.name for f in p["keep"]}; dropped = {f.name for f in p["drop"]}
    # wk1 keep set: last run before ko1 (13:00 run -> base+120), last before ko2, final run, burst tails
    must = [ids[base + dt.timedelta(minutes=120)], ids[base + dt.timedelta(minutes=315)], ids[late]]
    for rid in must:
        assert f"nfl_prediction_log_{rid}.jsonl" in kept and f"nfl_signal_log_{rid}.jsonl" in kept, rid
    mid = ids[base + dt.timedelta(minutes=45)]
    assert f"nfl_prediction_log_{mid}.jsonl" in dropped and f"nfl_signal_log_{mid}.jsonl" in dropped
    # pairs are never split
    for f in list(kept):
        twin = f.replace("prediction", "signal") if "prediction" in f else f.replace("signal", "prediction")
        assert twin in kept, f
    assert not any("wk02" in f or "wk03" in f for f in dropped)
    # no kickoff data -> keep everything for that week
    (d / "nfl_por_log_2026_w01.jsonl").unlink()
    p2 = thin.plan(d)
    assert not p2["drop"]
    # an incomplete record (< 4 locked games) also keeps everything
    (d / "nfl_por_log_2026_w01.jsonl").write_text("\n".join(json.dumps(x) for x in por[:2]))
    assert not thin.plan(d)["drop"]
    # dry run deletes nothing; a real run deletes
    (d / "nfl_por_log_2026_w01.jsonl").write_text("\n".join(json.dumps(x) for x in por))
    before = len(list(d.iterdir()))
    assert thin.main(["--dir", str(d), "--dry-run"]) == 0 and len(list(d.iterdir())) == before
    assert thin.main(["--dir", str(d)]) == 0 and len(list(d.iterdir())) < before
print("test_nfl_thin_logs: ok")
