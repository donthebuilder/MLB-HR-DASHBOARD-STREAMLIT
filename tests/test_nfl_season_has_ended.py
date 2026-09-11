"""bots/nfl/nfl_features.py -- season_has_ended() (item 4, 2026-09-11).

THE BUG. schedule_weeks()'s `grade` (the latest week that has STARTED) is a
max() with nothing past the season's last week to raise it further, so once
the season ends it freezes on the final week number forever instead of
following `price` back to None the way nfl_bot.py's pricing side already
does. nfl_results.py's grading side had no guard for this at all -- unlike
nfl_bot.py's build_payload(), which explicitly stops once `price` goes None
("NOTHING LEFT TO PRICE... the last card and the final grades stay where
they are"). Without an equivalent on the grading side, nfl.yml's ~12
firings/week would re-grade and re-append an identical final-week payload
to results.json AND the outcome log for the ENTIRE off-season, every week,
until next August -- the same "rebuilt and re-graded a finished week
roughly twelve times a week for six weeks" failure `_kickoffs()`'s own
comment already documents happening once before, at the regular-season/
playoff seam, just recurring at the next seam (the one after the season's
actual last game) since nothing was fixed there too.

THE FIX. season_has_ended(season, now) -- new function, same file as
schedule_weeks(). True once `now` is past the season's very last kickoff
(across every week the schedule has, playoffs included -- same source
schedule_weeks() already trusts) plus GAME_HOURS plus a
SEASON_GRADE_GRACE_DAYS=14 grace window for stat corrections to settle.
nfl_results.py's main() checks it right where it resolves the week to
grade and, when true (and no explicit --week was passed), returns early
without touching results.json, the week archive, or the outcome log --
mirroring nfl_bot.py's own "nothing left to price" early return.

WHY THIS IS MOCKED. nfl_features.py imports nflreadpy/polars at MODULE
level (unlike nfl_espn.py's lazy per-function imports), so this test file
faking sys.modules before `import nfl_features` is not enough on its own --
`import nflreadpy as nfl` inside nfl_features.py binds nfl_features's own
module-global `nfl` name to whatever object was in sys.modules at THAT
import, and later swapping sys.modules does not retroactively change it.
So each scenario below directly reassigns `nfl_features.nfl.load_schedules`
and clears `_kickoffs`'s lru_cache (real code, real cache, same season
number reused across scenarios on purpose) rather than relying on
sys.modules alone past the first import.

Neither nflreadpy nor polars is installed anywhere this session has tools
for (confirmed: no PyPI network path reaches them, from either the linked
Mac's sandboxed VM or the cloud container) -- same constraint as item 22's
test file, same reason this one fakes both rather than running for real.

Run: python tests/test_nfl_season_has_ended.py
"""
import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, _pred):
        return self  # the fake loader below is already scoped to one season

    def select(self, *_cols):
        return self

    def iter_rows(self, named=True):
        assert named is True
        return iter(self._rows)


class _FakePolarsCol:
    def __eq__(self, _other):
        return None  # never actually evaluated by _FakeFrame.filter


class _FakePolars:
    @staticmethod
    def col(_name):
        return _FakePolarsCol()


class _FakeNflreadpy:
    def __init__(self, rows):
        self._rows = rows

    def load_schedules(self):
        return _FakeFrame(self._rows)


class _RaisingNflreadpy:
    def load_schedules(self):
        raise RuntimeError("simulated: no network path to nflverse's release host")


def _rows(*kickoffs):
    """Build fake schedule rows shaped like _kickoffs() expects: week,
    gameday (ET calendar date), gametime (ET clock). One row per (week,
    iso_et_datetime) pair."""
    out = []
    for w, iso in kickoffs:
        d, t = iso.split("T")
        out.append({"week": w, "gameday": d, "gametime": t})
    return out


with mock.patch.dict(sys.modules, {"polars": _FakePolars(), "nflreadpy": _FakeNflreadpy([])}):
    from bots.nfl import nfl_features  # noqa: E402

SEASON = 2026


def _season_has_ended(rows, now):
    nfl_features.nfl = _FakeNflreadpy(rows)
    nfl_features._kickoffs.cache_clear()
    return nfl_features.season_has_ended(SEASON, now=now)


# 1. Mid-season: the latest game is hours old, nowhere near the grace window.
# Same shape as a normal Sunday-night check -- must never look "ended".
mid_season_rows = _rows(
    (1, "2026-09-10T17:00:00"),
    (2, "2026-09-17T17:00:00"),
)
now_mid = dt.datetime(2026, 9, 17, 23, 30, tzinfo=dt.timezone.utc)  # ~2.5h after week 2's 17:00 ET kickoff (21:00 UTC in September/EDT)
check("mid-season, hours after the latest kickoff: not ended",
      _season_has_ended(mid_season_rows, now_mid), False)

# 2. Right after the season's last game (the Super Bowl), still well inside
# the 14-day grace window -- must still grade normally, not cut off early.
final_week_rows = _rows((22, "2027-02-07T23:30:00"))  # Super Bowl kickoff, ET
now_just_after = dt.datetime(2027, 2, 10, 12, 0, tzinfo=dt.timezone.utc)  # 3 days later
check("3 days after the Super Bowl, inside the grace window: not ended",
      _season_has_ended(final_week_rows, now_just_after), False)

# 3. Just before the cutoff (kickoff + GAME_HOURS + 14 days, minus a minute).
# _kickoffs() reads the naive gameday/gametime as ET and converts to UTC
# (America/New_York, EST in February = UTC-5) -- the cutoff here has to go
# through that same conversion or this boundary check is testing the wrong
# instant.
from zoneinfo import ZoneInfo  # noqa: E402
_kickoff_utc = (dt.datetime(2027, 2, 7, 23, 30)
                .replace(tzinfo=ZoneInfo("America/New_York"))
                .astimezone(dt.timezone.utc))
cutoff = (_kickoff_utc
          + dt.timedelta(hours=nfl_features.GAME_HOURS)
          + dt.timedelta(days=nfl_features.SEASON_GRADE_GRACE_DAYS))
check("one minute before the grace cutoff: not ended",
      _season_has_ended(final_week_rows, cutoff - dt.timedelta(minutes=1)), False)

# 4. Just past the cutoff -- this is the actual bug: without season_has_ended(),
# nothing ever flips this back to "nothing to grade" and every later firing
# keeps re-grading the same finished week forever.
check("one minute after the grace cutoff: ended",
      _season_has_ended(final_week_rows, cutoff + dt.timedelta(minutes=1)), True)

# 5. Deep off-season, months later -- the actual failure mode item 4
# describes (nfl.yml still fires ~12x/week all year).
check("two months into the off-season: ended",
      _season_has_ended(final_week_rows, dt.datetime(2027, 4, 15, tzinfo=dt.timezone.utc)), True)

# 6. Unknown/empty schedule (e.g. a season nflreadpy has no data for) --
# fails soft to "not ended" rather than silently blocking grading forever.
check("no schedule rows at all: not ended (fails soft)",
      _season_has_ended([], dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)), False)

# 7. load_schedules() itself raising (real network failure) -- same
# fails-soft contract, and it must not raise out of season_has_ended().
nfl_features.nfl = _RaisingNflreadpy()
nfl_features._kickoffs.cache_clear()
check("load_schedules() raises: not ended (fails soft, no crash)",
      nfl_features.season_has_ended(SEASON, now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)), False)

# NOT tested for real, stated plainly: item 22's test could exercise
# season_schedule_for_rest()'s fails-soft path against the REAL (missing)
# nflreadpy, because nfl_espn.py imports it lazily, inside the function.
# nfl_features.py imports nflreadpy/polars at its own MODULE top, so when
# neither is installed, `import nfl_features` itself fails before
# season_has_ended() is ever reachable -- confirmed directly: `import
# nfl_features` alone raises ModuleNotFoundError in this environment. So
# season_has_ended()'s `except Exception` branch (check 7 above) only
# protects against load_schedules() failing at RUNTIME once nflreadpy is
# genuinely importable (a network hiccup, a schema change) -- not against
# nflreadpy being entirely absent, which is a harder failure this function
# cannot fail soft from because it can't even be imported. That's an
# accurate description of production (nflreadpy IS installed there, only
# this dev environment lacks it), not a gap in what's covered here.

# 8. Regression guard: the early-return guard is actually wired into
# nfl_results.py's main(), not just present as an unused function. A light
# source check rather than an execution test -- main() itself needs polars
# at module level too (same constraint as every other nfl_results.py test
# in this suite), so this is the same tradeoff test_nfl_results_lines_filter.py
# and the rest of this file's siblings already make.
nfl_results_src = (
    open(os.path.join(os.path.dirname(__file__), "..", "bots", "nfl", "nfl_results.py"))
    .read()
)
check("nfl_results.py imports season_has_ended",
      "season_has_ended" in nfl_results_src, True)
check("nfl_results.py's guard checks season_over before resolving a.week",
      "season_over" in nfl_results_src and "nothing left to grade" in nfl_results_src, True)


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ok   season_has_ended() correctly expires the frozen final-week grade "
          "past a 14-day post-season grace window, fails soft on missing/broken "
          "schedule data, and nfl_results.py's grading loop actually calls it "
          "before resolving which week to grade.")
    sys.exit(0)
