"""The first-pitch rebuild keeps pregame scores (2026-10-04). TEST DATA, made-up players.
Run: PYTHONPATH=. python3 tests/test_freeze_pregame_rows.py"""
import dataclasses, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import mlb_dashboard as m  # noqa: E402

FAILED, N = [], 0
def check(name, got, want):
    global N
    N += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

fields = m.HitterRecord.__dataclass_fields__
def rec(pid, spot, hr, conf, run):
    base = {k: (f.default if f.default is not dataclasses.MISSING else (f.default_factory() if f.default_factory is not dataclasses.MISSING else None)) for k, f in fields.items()}
    base.update(game_pk=1, player_id=pid, name=f"P{pid}", lineup_spot=spot, lineup_confirmed=conf, hr_score=hr, run_id=run)
    return m.HitterRecord(**base)

pregame = [rec(1, 3, 55.0, False, "pre"), rec(2, 9, 30.0, False, "pre")]          # 2 was a projected guess
rebuilt = [rec(1, 2, 71.0, True, "live"), rec(3, 9, 40.0, True, "live")]           # real order: 3 replaces 2; 1 homered in the 1st
rows, carried = m.freeze_pregame_rows(rebuilt, pregame)
by = {r.player_id: r for r in rows}
check("pregame hitter keeps his pregame hr_score", by[1].hr_score, 55.0)
check("pregame hitter keeps the pregame run's stamp", by[1].run_id, "pre")
check("pregame hitter takes his real lineup spot", by[1].lineup_spot, 2)
check("pregame hitter takes the real confirmation", by[1].lineup_confirmed, True)
check("a hitter the guess missed keeps the rebuilt row", by[3].hr_score, 40.0)
check("the wrong guess is not resurrected", 2 in by, False)
check("carried ids", carried, {1})
print(f"{N - len(FAILED)}/{N} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
