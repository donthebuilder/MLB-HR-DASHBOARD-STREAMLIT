"""nfl_espn.py — weather/drive-state parsing and attach_rest_days() (B7,
2026-08-28, dash-network-master-plan-2026-08-28.md).

B7 asked the NFL Games page for drive-level state, a tired-defense signal,
and weather. All three were checked directly against the live codebase
first: no possession/situation data was being read at all, no weather
field was being read at all (though ESPN's real scoreboard response DOES
carry one, confirmed via a live fetch during this work — event.weather.temperature
/ event.weather.displayValue), and no fatigue/rest data exists anywhere in
the NFL data layer. This file covers what actually got built from that:

  1. fetch() now reads event.weather (temperature, displayValue) --
     confirmed against a real live ESPN response, not guessed.
  2. fetch() best-effort reads competitions[0].situation for down/distance
     and red-zone state -- NOT confirmed against a real live game (none was
     in progress when this was built), fails soft to None/False the same
     way an absent weather block does. Tested here for correct MECHANICS
     (extraction when present, safe None/False when absent) using a
     plausible synthetic shape -- not a claim that the field names are
     verified real. The single new field checked against a REAL live
     response is `weather`, not `situation`.
  3. attach_rest_days() -- pure function, real logic, fully verified: given
     a team's full schedule, computes days since that team's prior game,
     and flags a short week (<= 5 days) -- a genuine (if blunt) tired-team
     proxy that needed no new data source at all, just date arithmetic over
     what fetch() already returns.

Run: python tests/test_nfl_espn_rest_weather.py
"""
import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.nfl import nfl_espn  # noqa: E402

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


def _event(game_id, home, away, kickoff, weather=None, situation=None):
    comp = {
        "competitors": [
            {"homeAway": "home", "team": {"abbreviation": home}, "score": "0"},
            {"homeAway": "away", "team": {"abbreviation": away}, "score": "0"},
        ],
        "venue": {"fullName": "Test Stadium", "indoor": False},
    }
    if situation is not None:
        comp["situation"] = situation
    ev = {
        "id": game_id,
        "date": kickoff,
        "competitions": [comp],
        "status": {"type": {"state": "pre", "shortDetail": "TBD", "completed": False}},
    }
    if weather is not None:
        ev["weather"] = weather
    return ev


def _fake_response(events, week_number=1):
    class R:
        ok = True
        def json(self):
            return {"events": events, "week": {"number": week_number}}
    return R()


# ── 1 & 2: fetch() parses weather (real shape) and situation (best-effort) ──

events = [
    _event("1", "BAL", "WSH", "2026-08-30T17:00Z",
           weather={"displayValue": "Intermittent clouds", "temperature": 82, "conditionId": "4"}),
    _event("2", "MIA", "ATL", "2026-08-30T20:00Z"),  # no weather key at all -- e.g. a dome
    _event("3", "KC", "BUF", "2026-08-30T20:00Z",
           situation={"downDistanceText": "3rd & 7 at KC 45", "isRedZone": False}),
    _event("4", "SF", "SEA", "2026-08-30T20:00Z"),  # no situation key -- pregame, the normal case
]

with mock.patch.object(nfl_espn.requests, "get", return_value=_fake_response(events)):
    rows = nfl_espn.fetch(seasontype=1, year=2026)

by_id = {r["game_id"]: r for r in rows}

check("weather present: real temperature carried through", by_id["1"]["weather_temp_f"], 82)
check("weather present: real condition text carried through", by_id["1"]["weather_condition"], "Intermittent clouds")
check("weather absent: temp is None, not 0 or a guessed default", by_id["2"]["weather_temp_f"], None)
check("weather absent: condition is None", by_id["2"]["weather_condition"], None)

check("situation present: down/distance text extracted", by_id["3"]["down_distance"], "3rd & 7 at KC 45")
check("situation present: red_zone False extracted correctly (not just truthy-because-present)", by_id["3"]["red_zone"], False)
check("situation absent: down_distance is None, not a crash or a stale value", by_id["4"]["down_distance"], None)
check("situation absent: red_zone defaults False", by_id["4"]["red_zone"], False)


# ── 2b: THE RED ZONE, AFTER 2026-09-01 ──────────────────────────────────────
#
# This used to be bool(situation.get("isRedZone")) on a field name nobody had
# ever seen on a live game. Two things changed.
#
# ESPN was read directly. No football was live, so the live block still could
# not be observed -- but a completed game's drive data uses the same naming
# family and confirms downDistanceText, shortDownDistanceText and
# yardsToEndzone, spelled exactly so. The red-zone call now rests on three
# independent signals instead of the one that is still inferred.
#
# And bool() was a real bug, not a stylistic one: bool("false") is True in
# Python. A feed sending that flag as a string would have marked every drive
# of every game a red zone -- loud and wrong, which is worse than quiet and
# wrong. The allowlist below is the point of the whole change, and the
# "false"/"0"/"no" cases are the ones that must never regress.

_rz = lambda sit: nfl_espn._red_zone(sit, sit.get("downDistanceText") or sit.get("shortDownDistanceText"))

for _label, _sit in [
    ("flag True", {"isRedZone": True}),
    ("flag as the string 'true'", {"isRedZone": "true"}),
    ("flag as 'TRUE ' with padding", {"isRedZone": "TRUE "}),
    ("flag as 1", {"isRedZone": 1}),
    ("the inRedZone spelling", {"inRedZone": True}),
    ("no flag, 12 yards to the end zone", {"yardsToEndzone": 12}),
    ("no flag, exactly 20", {"yardsToEndzone": 20}),
    ("no flag, on the goal line (0)", {"yardsToEndzone": 0}),
    ("no flag, yards as the string '15'", {"yardsToEndzone": "15"}),
    ("no flag, 2nd & Goal", {"downDistanceText": "2nd & Goal"}),
    ("no flag, 1st and Goal (short text)", {"shortDownDistanceText": "1st and Goal"}),
    ("flag says False but the ball is on the 8", {"isRedZone": False, "yardsToEndzone": 8}),
]:
    check("red zone: " + _label, _rz(_sit), True)

for _label, _sit in [
    ("THE OLD BUG -- the string 'false'", {"isRedZone": "false"}),
    ("the string '0'", {"isRedZone": "0"}),
    ("the string 'no'", {"isRedZone": "no"}),
    ("flag False", {"isRedZone": False}),
    ("21 yards out", {"yardsToEndzone": 21}),
    ("midfield down/distance text", {"downDistanceText": "1st & 10 at KC 45"}),
    ("yardsToEndzone as an empty string", {"yardsToEndzone": ""}),
    ("yardsToEndzone None", {"yardsToEndzone": None}),
    ("yardsToEndzone False (float(False) is 0.0)", {"yardsToEndzone": False}),
    ("negative yards", {"yardsToEndzone": -3}),
    ("nonsense yards (400)", {"yardsToEndzone": 400}),
    ("'goal' inside a place name", {"downDistanceText": "1st & 10 at GOALBURG 30"}),
    ("an empty situation block", {}),
]:
    check("NOT red zone: " + _label, _rz(_sit), False)

# yards_to_endzone is published alongside, and None and 0 are different
# answers: 0 is the goal line, None is "the feed did not say".
check("yards_to_endzone: 0 survives as 0, not None", nfl_espn._yards_to_endzone({"yardsToEndzone": 0}), 0)
check("yards_to_endzone: '15' parses", nfl_espn._yards_to_endzone({"yardsToEndzone": "15"}), 15)
check("yards_to_endzone: missing is None", nfl_espn._yards_to_endzone({}), None)
check("yards_to_endzone: False is None, not 0", nfl_espn._yards_to_endzone({"yardsToEndzone": False}), None)

# and it reaches the row fetch() builds, not just the helper
check("row carries yards_to_endzone when absent", by_id["4"].get("yards_to_endzone"), None)


# ── 3: attach_rest_days() ───────────────────────────────────────────────────

season_games = [
    {"game_id": "w1a", "home": "BAL", "away": "WSH", "kickoff": "2026-09-06T17:00Z"},
    {"game_id": "w2a", "home": "MIA", "away": "BAL", "kickoff": "2026-09-13T17:00Z"},   # BAL: 7 days
    {"game_id": "w3a", "home": "BAL", "away": "KC", "kickoff": "2026-09-17T20:15Z"},    # BAL: 4 days (Thu after Sun)
    {"game_id": "w1b", "home": "SF", "away": "SEA", "kickoff": "2026-09-06T20:00Z"},
    # SF has no second game in this pool -- rest_days must come back None, not 0 or a guess.
]

annotated = nfl_espn.attach_rest_days(season_games, season_games)
by_gid = {g["game_id"]: g for g in annotated}

check("week 1: no prior game exists, rest_days is None (not 0)", by_gid["w1a"]["home_rest_days"], None)
check("week 1: short_week False when rest is unknown", by_gid["w1a"]["home_short_week"], False)
check("normal turnaround: BAL's 2nd game is 7 days after the 1st", by_gid["w2a"]["away_rest_days"], 7)
check("normal turnaround is NOT flagged short", by_gid["w2a"]["away_short_week"], False)
check("Thursday-after-Sunday: BAL's 3rd game is 4 days after the 2nd", by_gid["w3a"]["home_rest_days"], 4)
checkTrue("short week correctly flagged at 4 days", by_gid["w3a"]["home_short_week"])
check("a team with only one game in the pool: rest_days stays None", by_gid["w1b"]["home_rest_days"], None)

# target can be a narrower slice than the full pool it computes from
just_week3 = [g for g in season_games if g["game_id"] == "w3a"]
narrow = nfl_espn.attach_rest_days(season_games, just_week3)
check("attach_rest_days works when target is a subset of all_games", len(narrow), 1)
check("subset call still finds the same real rest value", narrow[0]["home_rest_days"], 4)

# does not mutate its inputs
original_w3a = dict(season_games[2])
_ = nfl_espn.attach_rest_days(season_games, season_games)
check("attach_rest_days does not mutate the games it's given", season_games[2], original_w3a)


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
else:
    print("ok   nfl_espn weather/situation parsing + attach_rest_days (B7): real ESPN weather field "
          "surfaces correctly, best-effort situation parsing fails soft when absent, and rest-days/"
          "short-week is computed correctly from schedule dates alone with no new data dependency.")
    sys.exit(0)
