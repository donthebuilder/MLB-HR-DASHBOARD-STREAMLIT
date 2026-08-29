"""nfl_pbp.py — same-day drive state from nflverse (2026-08-28).

Donovan asked for a free live-play-by-play source that doesn't depend on
ESPN. There isn't a truly real-time free one (nflreadpy's own docs: pbp
"updates nightly ... and additionally at specific points on game days"),
but nflreadpy was already a dependency here (nfl_field.py already calls
load_pbp() for the field charts), so this wires the SAME source in as a
second, same-day drive-state signal on the Games page -- confirming or
backfilling ESPN's live (but unverified-shape) situation guess, never
replacing it.

This test mocks nflreadpy.load_pbp() (no network) and checks:
  1. last_drive_state() picks the LAST play of each game (min
     game_seconds_remaining among plays with a real down), not the first
     or some arbitrary row.
  2. It's keyed correctly, one row per game_id.
  3. attach_pbp_state() matches ESPN game rows to nflverse's game_id by
     season/week/team codes, does not mutate its input, and every field
     comes back None (not a crash, not a stale/guessed value) for a game
     nflverse has no data for yet.
  4. Any exception from the underlying nflreadpy call is swallowed --
     Donovan's site must never go down because this optional layer broke.

Run: PYTHONPATH=. python3 tests/test_nfl_pbp.py
"""
import os
import sys
from unittest import mock

import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.nfl import nfl_pbp  # noqa: E402

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


# ── fake pbp frame: two games, several plays each, out of chronological order
# on purpose (nflverse doesn't guarantee row order) ──────────────────────────

ROWS = [
    # game 1 (KC @ BAL, week 1): three real plays plus one no-play (down=None)
    {"game_id": "2026_01_KC_BAL", "week": 1, "season_type": "REG", "qtr": 1,
     "down": 1, "ydstogo": 10, "yardline_100": 75, "posteam": "BAL",
     "game_seconds_remaining": 3500, "desc": "1st play"},
    {"game_id": "2026_01_KC_BAL", "week": 1, "season_type": "REG", "qtr": 3,
     "down": 3, "ydstogo": 2, "yardline_100": 40, "posteam": "KC",
     "game_seconds_remaining": 1200, "desc": "3rd quarter play -- the most recent real down"},
    {"game_id": "2026_01_KC_BAL", "week": 1, "season_type": "REG", "qtr": 3,
     "down": None, "ydstogo": None, "yardline_100": None, "posteam": None,
     "game_seconds_remaining": 1150, "desc": "timeout -- no down, must be ignored"},
    # game 2 (SF @ SEA, week 1): only one real play, everything else no-plays
    {"game_id": "2026_01_SF_SEA", "week": 1, "season_type": "REG", "qtr": 2,
     "down": 2, "ydstogo": 8, "yardline_100": 55, "posteam": "SEA",
     "game_seconds_remaining": 2000, "desc": "SEA's only charted down"},
    # a preseason row that must never leak into a REG-filtered result
    {"game_id": "2026_01_DEN_LAR", "week": 1, "season_type": "PRE", "qtr": 1,
     "down": 1, "ydstogo": 10, "yardline_100": 75, "posteam": "LA",
     "game_seconds_remaining": 3500, "desc": "preseason, must be filtered out"},
]


def _fake_df():
    return pl.DataFrame(ROWS)


nfl_pbp._pbp.cache_clear()
with mock.patch.object(nfl_pbp.nfl, "load_pbp", return_value=_fake_df()):
    state = nfl_pbp.last_drive_state(2026, week=1)

check("two REG games with real downs come back", len(state), 2)
checkTrue("game 1 present", "2026_01_KC_BAL" in state)
g1 = state["2026_01_KC_BAL"]
check("picks the LAST real down (min game_seconds_remaining), not the first",
      g1["down"], 3)
check("distance from the correct (last) play", g1["distance"], 2)
check("possession from the correct (last) play", g1["possession"], "KC")
check("quarter from the correct (last) play", g1["quarter"], 3)
check("no-play row (down=None) never wins over a real down", g1["desc"],
      "3rd quarter play -- the most recent real down")

g2 = state["2026_01_SF_SEA"]
check("a game with exactly one real down still resolves correctly", g2["down"], 2)
check("preseason rows are excluded by the REG filter",
      "2026_01_DEN_LAR" in state, False)

# ── attach_pbp_state(): matching, non-mutation, and the no-data case ────────

nfl_pbp._pbp.cache_clear()
games = [
    {"game_id": "espn-1", "home": "BAL", "away": "KC", "week": 1, "season_type": 2},
    {"game_id": "espn-2", "home": "SEA", "away": "SF", "week": 1, "season_type": 2},
    {"game_id": "espn-3", "home": "DAL", "away": "NYG", "week": 1, "season_type": 2},  # no pbp data at all
]
original = [dict(g) for g in games]

with mock.patch.object(nfl_pbp.nfl, "load_pbp", return_value=_fake_df()):
    annotated = nfl_pbp.attach_pbp_state(games, season=2026, week=1)

by_id = {g["game_id"]: g for g in annotated}

check("matched game carries the real down", by_id["espn-1"]["pbp_down"], 3)
check("matched game carries the real possession", by_id["espn-1"]["pbp_possession"], "KC")
check("matched game is tagged with its source", by_id["espn-1"]["pbp_source"], "nflverse")
check("second matched game resolves independently", by_id["espn-2"]["pbp_down"], 2)

check("unmatched game: down is None, not a crash or a stale guess", by_id["espn-3"]["pbp_down"], None)
check("unmatched game: source is None, not a fabricated label", by_id["espn-3"]["pbp_source"], None)

check("attach_pbp_state does not mutate its input list", games, original)
check("attach_pbp_state returns the same number of games given", len(annotated), len(games))

# ── fails soft: any exception from nflreadpy must never propagate ──────────

nfl_pbp._pbp.cache_clear()
with mock.patch.object(nfl_pbp.nfl, "load_pbp", side_effect=RuntimeError("boom")):
    state_on_error = nfl_pbp.last_drive_state(2026, week=1)
    annotated_on_error = nfl_pbp.attach_pbp_state(games, season=2026, week=1)

check("a load_pbp() exception yields an empty state dict, never a raise", state_on_error, {})
check("attach_pbp_state degrades to all-None fields on error, not a crash",
      annotated_on_error[0]["pbp_down"], None)
check("attach_pbp_state still returns one row per game on error",
      len(annotated_on_error), len(games))


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
else:
    print("ok   nfl_pbp: last_drive_state() resolves the correct (most recent, real-down) "
          "play per game from nflreadpy's free pbp source, attach_pbp_state() matches it "
          "onto ESPN game rows without mutating them and degrades to None fields (never a "
          "crash) for unmatched games or a broken/unavailable nflreadpy call.")
    sys.exit(0)
