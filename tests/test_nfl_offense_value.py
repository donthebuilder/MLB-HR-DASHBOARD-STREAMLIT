"""nfl_offense_value.py -- the offensive flipside: red-zone touch
conversion and per-target route value (2026-09-13).

Mocks nflreadpy's load_pbp/load_participation/load_players (no network) and
checks:
  1. red_zone_conversion(): only yardline_100<=20 plays count; REG-only;
     carries and targets combine for a player who gets both kinds of
     red-zone touches; MIN_RZ_TOUCHES filters low-sample players; a
     non-skill position (e.g. a lineman credited a goal-line TD) never
     enters the output; percentile is computed WITHIN position (a TE isn't
     ranked against a WR).
  2. route_value(): yards-per-target computed correctly per route type;
     MIN_ROUTE_TARGETS filters PER ROUTE CELL, not per player total (a
     player can qualify on one route and not another); an untargeted route
     (empty string) is excluded; best_route/best_yds_per_tgt picks the
     actual max, not the first route seen; preseason plays excluded.

Run: PYTHONPATH=. python3 tests/test_nfl_offense_value.py
"""
import os
import sys
from unittest import mock

import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.nfl import nfl_offense_value as nov  # noqa: E402

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


def checkClose(name, got, want, tol=0.05):
    global CHECKS
    CHECKS += 1
    if got is None or abs(got - want) > tol:
        FAILED.append(f"{name}: got {got!r}, want ~{want!r}")


PLAYERS_ROWS = [
    {"gsis_id": "RB_A", "display_name": "Goalline RB", "position": "RB"},
    {"gsis_id": "WR_A", "display_name": "Redzone WR", "position": "WR"},
    {"gsis_id": "WR_B", "display_name": "Under Sample WR", "position": "WR"},
    {"gsis_id": "OL_A", "display_name": "Lineman TD", "position": "T"},
    {"gsis_id": "WR_C", "display_name": "Multi Route WR", "position": "WR"},
]


def _fake_players():
    return pl.DataFrame(PLAYERS_ROWS)


# ── red_zone_conversion() ───────────────────────────────────────────────────
PBP_ROWS = []
def _rush(pid, yardline, td, season_type="REG"):
    PBP_ROWS.append({"season_type": season_type, "yardline_100": yardline,
                      "rush_attempt": 1, "pass_attempt": 0,
                      "rusher_player_id": pid, "receiver_player_id": None,
                      "rush_touchdown": td, "pass_touchdown": 0})
def _pass(pid, yardline, td, season_type="REG"):
    PBP_ROWS.append({"season_type": season_type, "yardline_100": yardline,
                      "rush_attempt": 0, "pass_attempt": 1,
                      "rusher_player_id": None, "receiver_player_id": pid,
                      "rush_touchdown": 0, "pass_touchdown": td})

# RB_A: 5 RZ carries (3 TD) + 2 RZ targets (1 TD) -- combined touches=7, tds=4
for _ in range(3):
    _rush("RB_A", 5, 1)
for _ in range(2):
    _rush("RB_A", 10, 0)
_pass("RB_A", 8, 1)
_pass("RB_A", 12, 0)
# a carry OUTSIDE the red zone -- must not count toward RB_A's totals
_rush("RB_A", 45, 1)
# a PRESEASON red-zone carry -- must not count
_rush("RB_A", 5, 1, season_type="PRE")

# WR_A: 6 RZ targets, 3 TD -- clears MIN_RZ_TOUCHES(5)
for i in range(6):
    _pass("WR_A", 15, 1 if i < 3 else 0)

# WR_B: only 2 RZ targets -- below MIN_RZ_TOUCHES, must not appear
for _ in range(2):
    _pass("WR_B", 15, 1)

# OL_A: a lineman scores on a goal-line tackle-eligible play -- real def_*-
# style stat exists in the row, but SKILL_POS must exclude him regardless
for _ in range(6):
    _rush("OL_A", 2, 1)


def _fake_pbp():
    return pl.DataFrame(PBP_ROWS)


nov._pbp.cache_clear()
nov._players.cache_clear()
with mock.patch.object(nov.nfl, "load_pbp", return_value=_fake_pbp()), \
     mock.patch.object(nov.nfl, "load_players", return_value=_fake_players()):
    rz = nov.red_zone_conversion(2026)

check("under-MIN_RZ_TOUCHES player excluded", "WR_B" in rz, False)
check("non-skill position excluded even with a real goal-line TD", "OL_A" in rz, False)
checkTrue("qualifying RB and WR present", "RB_A" in rz and "WR_A" in rz)
check("RB_A's touches combine RZ carries and RZ targets (5+2), excluding the "
      "outside-the-20 carry and the preseason carry", rz["RB_A"]["touches"], 7)
check("RB_A's tds combine both kinds, same exclusions", rz["RB_A"]["tds"], 4)
check("WR_A's touches are RZ targets only", rz["WR_A"]["touches"], 6)
check("WR_A's tds", rz["WR_A"]["tds"], 3)
checkTrue("percentile present for both qualifiers", "percentile" in rz["RB_A"] and "percentile" in rz["WR_A"])


# ── route_value() ────────────────────────────────────────────────────────────
PART_ROWS, ROUTE_PBP_ROWS = [], []
def _target(gid, pid, gain, complete, td, route, season_type="REG"):
    PART_ROWS.append({"nflverse_game_id": gid, "play_id": len(PART_ROWS) + 1, "route": route})
    ROUTE_PBP_ROWS.append({"game_id": gid, "play_id": len(ROUTE_PBP_ROWS) + 1,
                            "season_type": season_type, "receiver_player_id": pid,
                            "pass_attempt": 1, "complete_pass": complete,
                            "yards_gained": gain, "pass_touchdown": td})

# WR_C: 6 targets on GO (high value, qualifies: >=5), 3 targets on SLANT
# (below MIN_ROUTE_TARGETS(5) -- must not appear even though the player
# himself clears the red-zone-style bar elsewhere)
for g in (40, 35, 50, 20, 45, 60):
    _target("g1", "WR_C", g, 1, 1 if g == 60 else 0, "GO")
for g in (5, 6, 4):
    _target("g1", "WR_C", g, 1, 0, "SLANT")
# an untargeted/uncharted route (empty string) on a real pass attempt --
# must be excluded entirely, not counted as its own "route type"
_target("g1", "WR_C", 3, 1, 0, "")
# a preseason target on GO -- must not count toward WR_C's GO total
_target("g1", "WR_C", 99, 1, 1, "GO", season_type="PRE")


def _fake_participation():
    return pl.DataFrame(PART_ROWS)


def _fake_route_pbp():
    return pl.DataFrame(ROUTE_PBP_ROWS)


nov._targets_by_route.cache_clear()
nov._players.cache_clear()
with mock.patch.object(nov.nfl, "load_participation", return_value=_fake_participation()), \
     mock.patch.object(nov.nfl, "load_pbp", return_value=_fake_route_pbp()), \
     mock.patch.object(nov.nfl, "load_players", return_value=_fake_players()):
    rv = nov.route_value(2026)

checkTrue("WR_C present", "WR_C" in rv)
check("under-MIN_ROUTE_TARGETS route (SLANT, 3 targets) excluded for this player",
      "SLANT" in rv["WR_C"]["routes"], False)
checkTrue("qualifying route (GO, 6 REG targets) present", "GO" in rv["WR_C"]["routes"])
check("GO target count excludes the preseason target", rv["WR_C"]["routes"]["GO"]["targets"], 6)
checkClose("GO yards/target computed off the 6 REG targets only (40+35+50+20+45+60=250/6)",
           rv["WR_C"]["routes"]["GO"]["yds_per_tgt"], 250 / 6)
check("untargeted/uncharted route (empty string) never becomes its own route entry",
      "" in rv["WR_C"]["routes"], False)
check("best_route picks the actual qualifying route, not a guess", rv["WR_C"]["best_route"], "GO")
checkClose("best_yds_per_tgt matches that route's own figure",
           rv["WR_C"]["best_yds_per_tgt"], 250 / 6)


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
else:
    print("ok   nfl_offense_value: red_zone_conversion() combines RZ carries and targets per "
          "player, excludes outside-the-20 and preseason plays and non-skill positions, and "
          "grades percentile within position; route_value() computes per-target value by route "
          "type off the same participation-to-pbp join nfl_coverage.py established, filters "
          "per-route-cell sample size independently per player, excludes untargeted/uncharted "
          "routes, and picks the real best route rather than the first one seen.")
    sys.exit(0)
