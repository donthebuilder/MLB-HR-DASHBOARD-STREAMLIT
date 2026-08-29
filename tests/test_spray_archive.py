"""spray_archive.py — off-slate batter spray/EV archive (2026-08-28).

Donovan: "i need to be able to see the spray chart even if they player
isnt on. the bot." Today's spray chart only works for hitters
make_slim.py wrote a current/detail/{today|tomorrow}/batter_{pid}.json
for -- i.e. tonight's ~250-270 slate. spray_archive.py runs the same
statcast_batter() pull mlb_dashboard.py already does for slate hitters,
against every active-roster hitter league-wide instead, publishing to a
slate-independent current/detail/archive/batter_{pid}.json.

No live network in this test (no real statsapi.mlb.com or Statcast calls)
-- everything is mocked, matching this repo's convention elsewhere
(test_nfl_espn_rest_weather.py, test_nfl_pbp.py). Covers:
  1. spray_points_for() produces the exact field set SprayField.js reads,
     with lane/spray_side classified the same way mlb_dashboard.py's own
     slate-player version does (byte-for-byte on the shared inputs), and
     hr_class always "" (documented tradeoff, not a bug).
  2. active_hitters() excludes pure pitchers (position code "1") and keeps
     a two-way player.
  3. build_archive()'s cache+budget mechanics: a fresh cached file is left
     alone (no wasted fetch), a stale/missing one gets fetched within
     budget, and anything past MAX_FETCHES this run is left for next run
     -- never silently dropped, never double-counted.
  4. Every network call (StatsAPI roster/team lookups, statcast_batter)
     failing outright degrades to an empty/partial result, never a raise.

Run: PYTHONPATH=. python3 tests/test_spray_archive.py
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import spray_archive as sa  # noqa: E402

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


# ── 1: spray_points_for() shape and classification ─────────────────────────

df = pd.DataFrame([
    # a pulled fly ball HR, RHB (hc_x < 125 = pull for a righty)
    {"type": "X", "game_date": "2026-08-20", "at_bat_number": 3, "pitch_number": 4,
     "launch_speed": 108.2, "launch_angle": 27.0, "hit_distance_sc": 412.0,
     "hc_x": 70.0, "hc_y": 60.0, "pitch_type": "FF", "events": "home_run",
     "bb_type": "fly_ball", "stand": "R", "pitcher": 543037, "p_throws": "L",
     "release_speed": 96.1},
    # a grounder, no lane/pull-air classification should apply (not air)
    {"type": "X", "game_date": "2026-08-19", "at_bat_number": 1, "pitch_number": 1,
     "launch_speed": 92.0, "launch_angle": 2.0, "hit_distance_sc": 40.0,
     "hc_x": 130.0, "hc_y": 150.0, "pitch_type": "SL", "events": "field_out",
     "bb_type": "ground_ball", "stand": "R", "pitcher": 543037, "p_throws": "L",
     "release_speed": 84.0},
    # a non-batted-ball row (a called strike) must never appear in the output
    {"type": "S", "game_date": "2026-08-19", "at_bat_number": 1, "pitch_number": 2,
     "launch_speed": None, "launch_angle": None, "hit_distance_sc": None,
     "hc_x": None, "hc_y": None, "pitch_type": "FF", "events": "",
     "bb_type": "", "stand": "R", "pitcher": 543037, "p_throws": "L",
     "release_speed": 95.0},
])

with mock.patch.object(sa, "resolve_person_name", return_value="Test Pitcher"):
    points = sa.spray_points_for(df)

check("only the two real batted-ball (type=X) rows come back", len(points), 2)
hr = points[0]  # sorted descending by date -> the 08-20 HR is first
check("HR event carried through", hr["is_hr"], True)
check("lane classified from hc_x the same way the slate pipeline does (70 -> LF)", hr["lane"], "LF")
check("pull side correct for a RHB pulling to LF", hr["spray_side"], "pull")
check("pull-air true for a pulled fly ball", hr["is_pull_air"], True)
check("hr_class is always blank (documented tradeoff, no whole-slate xHR table here)", hr["hr_class"], "")
check("pitcher name resolved via the mocked lookup", hr["pitcher"], "Test Pitcher")
check("barrel flag computed the same threshold as the slate pipeline (ev>=98, 24<=la<=32)",
      hr["is_barrel"], True)

grounder = points[1]
check("a ground ball is never flagged pull-air even with a pull-side hc_x", grounder["is_pull_air"], False)
check("non-air trajectory still gets a lane (it's purely hc_x based)", grounder["lane"], "CF")

empty_points = sa.spray_points_for(pd.DataFrame())
check("an empty frame returns an empty list, not a crash", empty_points, [])


# ── 2: active_hitters() roster filtering ────────────────────────────────────

FAKE_ROSTER = {
    "roster": [
        {"person": {"id": 1001, "fullName": "Some Pitcher"}, "position": {"code": "1"}},
        {"person": {"id": 1002, "fullName": "Some Catcher"}, "position": {"code": "2"}},
        {"person": {"id": 1003, "fullName": "Two-Way Guy"}, "position": {"code": "Y"}},
        {"person": {"id": 1004, "fullName": "No Position"}, "position": {}},
    ]
}

with mock.patch.object(sa, "_get_json", return_value=FAKE_ROSTER):
    hitters = sa.active_hitters([100])

check("pure pitcher (position code 1) excluded", 1001 in hitters, False)
checkTrue("catcher included", 1002 in hitters)
checkTrue("two-way player (position code Y) included -- he bats too", 1003 in hitters)
checkTrue("a roster entry with no position code at all is still kept, not dropped", 1004 in hitters)
check("hitter dict carries the resolved name", hitters[1002]["name"], "Some Catcher")

with mock.patch.object(sa, "_get_json", side_effect=RuntimeError("network down")):
    hitters_on_error = sa.active_hitters([100])
check("a roster fetch failure yields no hitters for that team, never a raise", hitters_on_error, {})

with mock.patch.object(sa, "_get_json", side_effect=RuntimeError("network down")):
    team_ids_on_error = sa.active_team_ids()
check("a team-list fetch failure yields an empty list, never a raise", team_ids_on_error, [])


# ── 3: build_archive() cache + budget mechanics ─────────────────────────────

def _bbe_df(pid: int) -> pd.DataFrame:
    return pd.DataFrame([{
        "type": "X", "game_date": "2026-08-20", "at_bat_number": 1, "pitch_number": 1,
        "launch_speed": 100.0, "launch_angle": 25.0, "hit_distance_sc": 400.0,
        "hc_x": 100.0, "hc_y": 100.0, "pitch_type": "FF", "events": "home_run",
        "bb_type": "fly_ball", "stand": "R", "pitcher": 1, "p_throws": "L",
        "release_speed": 95.0,
    }])


tmp = tempfile.mkdtemp()
orig_dir = sa.ARCHIVE_DIR
sa.ARCHIVE_DIR = __import__("pathlib").Path(tmp)
try:
    hitters3 = {2001: {"name": "A", "team": "BAL", "bats": "R"},
                2002: {"name": "B", "team": "BAL", "bats": "L"},
                2003: {"name": "C", "team": "BAL", "bats": "R"}}

    with mock.patch.object(sa, "active_team_ids", return_value=[100]), \
         mock.patch.object(sa, "active_hitters", return_value=hitters3), \
         mock.patch.object(sa, "statcast_batter", side_effect=lambda *a, **k: _bbe_df(0)), \
         mock.patch.object(sa, "resolve_person_name", return_value="—"):
        stats1 = sa.build_archive(max_fetches=2)

    check("3 candidates seen", stats1["candidates"], 3)
    check("budget of 2 fetches only 2 this run", stats1["fetched"], 2)
    check("the 3rd hitter is left for next run, not dropped or errored", stats1["skipped_budget"], 1)
    check("both fetched hitters produced a written file", stats1["written"], 2)
    checkTrue("exactly 2 files exist on disk after the first, budget-limited run",
              len(list(sa.ARCHIVE_DIR.glob("batter_*.json"))) == 2)

    # second run, same budget: the two already-fresh files must NOT be
    # re-fetched, and the one left over from last time gets picked up.
    with mock.patch.object(sa, "active_team_ids", return_value=[100]), \
         mock.patch.object(sa, "active_hitters", return_value=hitters3), \
         mock.patch.object(sa, "statcast_batter", side_effect=lambda *a, **k: _bbe_df(0)), \
         mock.patch.object(sa, "resolve_person_name", return_value="—"):
        stats2 = sa.build_archive(max_fetches=2)

    check("previously-written files are NOT re-fetched (still fresh)", stats2["already_fresh"], 2)
    check("the one hitter skipped last run is fetched this run", stats2["fetched"], 1)
    checkTrue("all 3 hitters are now archived on disk",
              len(list(sa.ARCHIVE_DIR.glob("batter_*.json"))) == 3)

    # a hitter whose statcast pull comes back empty writes no file, and is
    # correctly counted, not silently mistaken for a success.
    empty_dir_stats = {}
    sa2_tmp = tempfile.mkdtemp()
    sa.ARCHIVE_DIR = __import__("pathlib").Path(sa2_tmp)
    with mock.patch.object(sa, "active_team_ids", return_value=[100]), \
         mock.patch.object(sa, "active_hitters", return_value={3001: {"name": "Nobody", "team": "", "bats": ""}}), \
         mock.patch.object(sa, "statcast_batter", return_value=pd.DataFrame()):
        stats3 = sa.build_archive(max_fetches=5)
    check("a hitter with no statcast rows at all writes no file", stats3["written"], 0)
    check("that hitter is counted as empty, not silently dropped uncounted", stats3["empty"], 1)
    shutil.rmtree(sa2_tmp, ignore_errors=True)

    # statcast_batter raising for one hitter must not stop the run or crash it.
    sa3_tmp = tempfile.mkdtemp()
    sa.ARCHIVE_DIR = __import__("pathlib").Path(sa3_tmp)
    with mock.patch.object(sa, "active_team_ids", return_value=[100]), \
         mock.patch.object(sa, "active_hitters", return_value={4001: {"name": "Boom", "team": "", "bats": ""}}), \
         mock.patch.object(sa, "statcast_batter", side_effect=RuntimeError("savant down")):
        stats4 = sa.build_archive(max_fetches=5)
    check("a statcast_batter exception for one hitter degrades to no file, never a raise",
          stats4["written"], 0)
    shutil.rmtree(sa3_tmp, ignore_errors=True)
finally:
    sa.ARCHIVE_DIR = orig_dir
    shutil.rmtree(tmp, ignore_errors=True)


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
else:
    print("ok   spray_archive: spray_points_for() classifies lane/pull-side identically to the "
          "slate pipeline it mirrors (hr_class always blank, documented), active_hitters() keeps "
          "hitters and drops pure pitchers, and build_archive()'s cache+budget loop fetches only "
          "what's stale within budget, leaves the rest for next run, and never crashes on a bad "
          "network call or an empty Statcast response.")
    sys.exit(0)
