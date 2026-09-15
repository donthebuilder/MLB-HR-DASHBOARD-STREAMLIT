"""probable_pitcher_id() must prefer the SCHEDULE's hydrated probable pitcher
over the game's own live-feed probablePitchers -- the pregame-card bug found
2026-09-15 (Donovan: "its showing yestreadsy today").

Real incident, reproduced exactly: at ~07:00 UTC on 2026-09-15, LAD @ CIN's
live feed (/game/{pk}/feed/live -> gameData.probablePitchers) was still
carrying Tarik Skubal -- the pitcher who ACTUALLY started that exact
fixture the day before, 2026-09-14 -- while MLB's schedule endpoint
(/api/v1/schedule?...&hydrate=probablePitcher), the same call
client.schedule(slate_date) already makes once for the whole run, had
already been updated to the real 2026-09-15 starter, Yoshinobu Yamamoto.
build_hitter_records used to read ONLY the live feed for this, so every
Called Shots / Tonight's Board post that night named Skubal and scored the
matchup off his (irrelevant, day-old) pitching line instead of Yamamoto's.

What this file proves:

  1. When the schedule and the live feed disagree, the schedule's id wins --
     the live feed is never even worth trusting less-strongly here, it can
     be a calendar day behind.
  2. A game the schedule hasn't posted a probable for yet (spring-training-
     style "TBD" slot) still falls back to the live feed rather than
     resolving to 0 and losing the pitcher entirely.
  3. Both sides missing everywhere resolves to 0, not a crash and not a
     silently-wrong nonzero id -- resolve_probable_pitcher's own TBD chase
     (live-feed direct fetch, then rotation inference) picks up from there.

Run: python tests/test_probable_pitcher_precedence.py
"""
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


SKUBAL_ID = 664350   # LAD @ CIN's actual 09-14 starter (yesterday's game)
YAMAMOTO_ID = 808967  # LAD @ CIN's actual 09-15 starter (today's real probable)


def game_with(sched_id=None, live_id=None):
    """One schedule row (as client.schedule() returns it) plus that same
    game's live-feed gameData, home side only -- away is symmetric and not
    worth doubling every case for."""
    game = {"teams": {"home": {}, "away": {"team": {"id": 1}}}}
    if sched_id is not None:
        game["teams"]["home"]["probablePitcher"] = {"id": sched_id}
    game_data = {"probablePitchers": {}}
    if live_id is not None:
        game_data["probablePitchers"]["home"] = {"id": live_id}
    return game, game_data


# ── 1. the real incident: schedule wins over a stale live feed ───────────
game, game_data = game_with(sched_id=YAMAMOTO_ID, live_id=SKUBAL_ID)
check("schedule's today's-starter wins over the live feed's still-yesterday's-starter",
      md.probable_pitcher_id(game, game_data, "home"), YAMAMOTO_ID)
check("...specifically, NOT the stale name that actually posted",
      md.probable_pitcher_id(game, game_data, "home") != SKUBAL_ID, True)

# ── 2. schedule has it, live feed doesn't (normal case, most of the day) ──
game, game_data = game_with(sched_id=YAMAMOTO_ID, live_id=None)
check("schedule alone is enough", md.probable_pitcher_id(game, game_data, "home"), YAMAMOTO_ID)

# ── 3. schedule is still TBD, live feed has it (early-morning / spring edge) ─
game, game_data = game_with(sched_id=None, live_id=SKUBAL_ID)
check("falls back to the live feed when the schedule has no name yet",
      md.probable_pitcher_id(game, game_data, "home"), SKUBAL_ID)

# ── 4. neither has it -- honest 0, not a crash, not a guess ───────────────
game, game_data = game_with(sched_id=None, live_id=None)
check("both empty -> 0, for resolve_probable_pitcher's own TBD chase to pick up",
      md.probable_pitcher_id(game, game_data, "home"), 0)

# ── 5. malformed/missing keys never crash the resolver ────────────────────
check("missing 'teams' entirely -> 0, no KeyError", md.probable_pitcher_id({}, {}, "home"), 0)
check("missing gameData entirely -> 0, no KeyError", md.probable_pitcher_id({"teams": {}}, {}, "away"), 0)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   probable pitcher precedence (2026-09-15 stale-pitcher incident): "
      f"{CHECKS} assertions, schedule beats a stale live feed, live feed still "
      f"covers a not-yet-posted schedule slot, and neither source crashes or "
      f"guesses when both are empty")
