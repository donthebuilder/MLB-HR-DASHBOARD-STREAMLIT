"""A failed/empty Statcast pull must not be cached as a real batter profile.
Run: PYTHONPATH=. python3 tests/test_statcast_profile_cache.py"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bots.mlb_dashboard as D  # noqa: E402


class FakeDB:
    def __init__(self, seed=None):
        self.store = dict(seed or {})
        self.sets = 0

    def get(self, key, max_age_days=1):
        return self.store.get(key)

    def set(self, key, val):
        self.sets += 1
        self.store[key] = val


D.build_recent_bat_tracking_lookup = lambda db, end: {}
end = dt.date(2026, 10, 4)

# 1. pull raises -> returned as "missing", NOT cached
orig = D.statcast_batter
D.statcast_batter = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("savant down"))
db = FakeDB()
out = D.build_batter_statcast_profile(db, 1, end)
assert out["statcast_pull_status"] == "missing" and db.sets == 0, (out["statcast_pull_status"], db.sets)

# 2. empty frame -> not cached
import pandas as pd  # noqa: E402
D.statcast_batter = lambda *a, **k: pd.DataFrame()
db = FakeDB()
out = D.build_batter_statcast_profile(db, 1, end)
assert out["statcast_pull_status"] == "missing" and db.sets == 0

# 3. a poisoned cache entry from an older run is ignored and re-pulled
calls = []
D.statcast_batter = lambda *a, **k: (calls.append(1) or pd.DataFrame())
key = f"batter_statcast_v10_power_metrics:{D.SEASON}:1:{end.isoformat()}"
db = FakeDB({key: {"statcast_pull_status": "missing"}})
D.build_batter_statcast_profile(db, 1, end)
assert calls, "poisoned 'missing' cache row was served instead of re-pulled"

# 4. a genuine cached profile is still served without a pull
calls.clear()
db = FakeDB({key: {"statcast_pull_status": "ok", "recent_350_num": 3}})
out = D.build_batter_statcast_profile(db, 1, end)
assert not calls and out["recent_350_num"] == 3
D.statcast_batter = orig
print("test_statcast_profile_cache: ok")
