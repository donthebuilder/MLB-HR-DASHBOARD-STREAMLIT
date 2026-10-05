"""pick_lock: headerless-payload slate date is the game's ET date, not UTC; --dry-run never posts.
Run: PYTHONPATH=. python3 tests/test_pick_lock_slate_date.py"""
import datetime as dt
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import pick_lock as pl  # noqa: E402

U = dt.timezone.utc
# an evening-only slate whose first pitch is 02:10Z on the 6th = 10:10pm ET on the 5th
assert pl.slate_date_fallback({"1": dt.datetime(2026, 10, 6, 2, 10, tzinfo=U)}) == "2026-10-05"
assert pl.slate_date_fallback({"1": dt.datetime(2026, 10, 5, 17, 5, tzinfo=U), "2": dt.datetime(2026, 10, 5, 23, 5, tzinfo=U)}) == "2026-10-05"
assert pl.slate_date_fallback({}, dt.datetime(2026, 10, 6, 3, 0, tzinfo=U)) == "2026-10-05"
src = inspect.getsource(pl.main)
assert "if not a.dry_run:\n          post_discord(" in src, "dry-run must not post to Discord"
print("test_pick_lock_slate_date: ok")
