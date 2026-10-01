"""make_slim.slate_is_real: a short slate passes only when it is the whole
schedule (2026-10-01, the one-game Wild Card day). TEST DATA: made-up rows
and a stubbed schedule -- no network."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import make_slim as ms  # noqa: E402

def rows(pks, per=17):
    return [{"game_pk": pk, "player_id": i} for pk in pks for i in range(per)]

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got}, want {want}"))

check("big slate passes without asking", ms.slate_is_real(rows([1, 2, 3, 4], 18), scheduled=lambda g: 1 / 0)[0], True)
check("one-game day, the whole schedule -> passes", ms.slate_is_real(rows([776001]), scheduled=lambda g: {776001})[0], True)
check("two games scheduled, one present -> fragment", ms.slate_is_real(rows([776001]), scheduled=lambda g: {776001, 776002})[0], False)
check("whole schedule but 5 rows -> fragment", ms.slate_is_real(rows([776001], per=5), scheduled=lambda g: {776001})[0], False)
check("schedule unreadable -> strict", ms.slate_is_real(rows([776001]), scheduled=lambda g: None)[0], False)
check("empty -> fails", ms.slate_is_real([], scheduled=lambda g: set())[0], False)
print(f"\n{6 - fails}/6 checks passed")
sys.exit(1 if fails else 0)
