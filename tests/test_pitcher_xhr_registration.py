"""Pitcher xHR registration must survive the profile cache hit -- data
defect #2 (2026-08-22).

pitcher_xhr_allowed / pitcher_hr_luck / pitcher_xhr_bbe were 0 on every
starter of every published slate (59/59 across two slates, then 30/30 on a
third) while the batter half of the identical Docket #20 machinery worked.
Root cause: build_pitcher_profile()'s early cache return sat ABOVE its
_xhr_register_pitcher() call, so any run serving the profile from cache --
every run after the day's first -- registered no pitchers, and
finalize_xhr_fields() found an empty _XHR_PITCHERS. The batter register
call lives in the always-run per-hitter loop, which is why season_xhr /
xhr_bbe populate fine.

What this file proves, with a stub CacheDB and no network:

  1. The CACHED path of build_pitcher_profile() registers the pitcher's
     xhr_hist_allowed in _XHR_PITCHERS (the fix).
  2. finalize_xhr_fields() then stamps pitcher_xhr_allowed /
     pitcher_hr_luck / pitcher_xhr_bbe onto a row facing that pitcher,
     using a league table accumulated from the batter side.
  3. The BBE gate still holds: a pitcher under XHR_MIN_PLAYER_BBE tracked
     balls gets pitcher_xhr_bbe stamped but no expected/luck numbers.

Run: python tests/test_pitcher_xhr_registration.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.mlb_dashboard as md  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def checkTrue(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


PID = 777001
END = dt.date(2026, 8, 22)

# A hist whose buckets we control end to end: 60 balls at 100mph/27deg
# (30% league HR there), 60 at 80mph/9deg (0%). 120 BBE clears
# XHR_MIN_PLAYER_BBE=50. Bucket keys via the real _xhr_key().
HOT = md._xhr_key(100.0, 27.0)
COLD = md._xhr_key(80.0, 9.0)
PITCHER_HIST = {HOT: [60, 25], COLD: [60, 0]}   # 25 actual HR allowed


class StubDB:
    """Answers the two cache keys the cached path reads; records misses."""
    def __init__(self, profile_cached, statcast_cached):
        self.profile_cached = profile_cached
        self.statcast_cached = statcast_cached
        self.misses = []

    def get(self, key, max_age_days=None):
        if key.startswith("pitcher_profile_"):
            return self.profile_cached
        if key.startswith("pitcher_statcast_damage_"):
            return self.statcast_cached
        self.misses.append(key)
        return None

    def set(self, *a, **k):
        pass


CACHED_PROFILE = {
    "player_id": PID, "name": "Cached Arm", "team_abbr": "AAA",
    "throws": "R", "era": 4.5, "whip": 1.4, "hr9": 1.6,
    # present so the cached path never reaches for the network:
    "lineup_spot_damage": {"3": {"hr": 2}}, "lineup_zone_damage": {"up": 1},
    "weak_spots": ("3",),
}
CACHED_STATCAST = {"xhr_hist_allowed": PITCHER_HIST, "statcast_status": "ok"}


def run():
    # clean slate for the module accumulators this test drives
    md._XHR_PITCHERS.clear()
    md._XHR_ACCUM.clear()
    md._XHR_BY_PID.clear()

    # ── 1. the cached path registers the pitcher (the fix) ──────────────
    db = StubDB(CACHED_PROFILE, CACHED_STATCAST)
    summary = md.build_pitcher_profile(None, db, PID, "AAA", data_end_date=END)
    check("cached path still returns the cached profile", summary.name, "Cached Arm")
    checkTrue("THE FIX: cached path registers the pitcher in _XHR_PITCHERS "
              "(was: early return skipped registration on every cache-hit run)",
              PID in md._XHR_PITCHERS)
    check("registered hist is the statcast profile's xhr_hist_allowed",
          md._XHR_PITCHERS[PID]["hist"], PITCHER_HIST)

    # ── 2. finalize stamps the facing row ───────────────────────────────
    # League table from the batter side: same buckets, XHR_MIN_BUCKET+
    # balls each so no neighborhood borrowing, XHR_MIN_LEAGUE_BALLS total.
    per_bucket = max(md.XHR_MIN_BUCKET, md.XHR_MIN_LEAGUE_BALLS // 2)
    md._XHR_ACCUM[HOT] = [per_bucket, int(per_bucket * 0.30)]
    md._XHR_ACCUM[COLD] = [per_bucket, 0]

    # finalize_xhr_fields reads via getattr and stamps via attribute
    # assignment, so a minimal stand-in row is faithful (HitterRecord has
    # 134 required fields; constructing one here would test nothing extra).
    class Row:
        def __init__(self, pid, pitcher_id):
            self.player_id = pid
            self.pitcher_id = pitcher_id
            self.pitcher_xhr_bbe = 0
            self.pitcher_xhr_allowed = 0.0
            self.pitcher_hr_luck = 0.0

    row = Row(1, PID)
    md.finalize_xhr_fields([row], db)
    check("pitcher_xhr_bbe stamped from the registered hist",
          row.pitcher_xhr_bbe, 120)
    check("pitcher_xhr_allowed = balls x league rate per bucket "
          "(60 x .30 + 60 x 0)", row.pitcher_xhr_allowed, 18.0)
    check("pitcher_hr_luck = actual - expected (25 - 18)",
          row.pitcher_hr_luck, 7.0)

    # ── 3. the small-sample gate still refuses a number ─────────────────
    md._XHR_PITCHERS.clear()
    tiny = {HOT: [10, 3]}    # 10 BBE < XHR_MIN_PLAYER_BBE
    db2 = StubDB(CACHED_PROFILE, {"xhr_hist_allowed": tiny, "statcast_status": "ok"})
    md.build_pitcher_profile(None, db2, PID, "AAA", data_end_date=END)
    row2 = Row(2, PID)
    md.finalize_xhr_fields([row2], db2)
    check("under the BBE gate: bbe is stamped honestly", row2.pitcher_xhr_bbe, 10)
    check("under the BBE gate: no expected number is invented",
          row2.pitcher_xhr_allowed, 0.0)
    check("under the BBE gate: no luck number is invented",
          row2.pitcher_hr_luck, 0.0)


run()

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   pitcher xHR registration (data defect #2): {CHECKS} assertions, "
      f"the profile cache hit registers the pitcher's contact-allowed hist, "
      f"finalize stamps expected/luck onto facing rows, and the 50-BBE gate "
      f"still refuses to invent numbers")
