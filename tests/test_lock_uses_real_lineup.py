"""should_reuse_locked_rows() must not freeze a pregame LINEUP GUESS onto a
game before the real lineup was ever checked, even once.

Run: python tests/test_lock_uses_real_lineup.py

WHY THIS EXISTS (2026-09-12, OPEN-ITEMS #18/#19, parked 2026-09-11). Donovan
sent a screenshot of two real starters (Michael Busch, Emmanuel Rodriguez)
who homered with zero row on the board at all -- no rank, no score, no
pitcher line, nothing. Root cause: once game_has_started() went true, main()
used to freeze whatever row set the LAST pregame run had saved. When that
last pregame run happened to land before the real lineup posted for that
specific game, the frozen row set was build_projected_lineup()'s season
OBP/OPS/SLG guess -- which gets most of a lineup right and 1-2 bench/platoon
spots wrong. The real player the guess picked wrong for a spot never got a
row, ever, for that game: nothing after lock ever rebuilt the row list, only
refresh_locked_lineup_status()'s confirmation flag (its own docstring: "does
not rebuild or replace any player picks").

The fix: built_after_lock marks a row as having survived one real build
attempted AT OR AFTER first pitch -- a live game is guaranteed to have its
true battingOrder posted by then, so this is the one moment a fresh build
can only improve the roster. should_reuse_locked_rows() is what decides
whether that one-time build has already happened.

These assertions pin: (1) a game still pregame never reuses saved rows
(unchanged, always rebuilds); (2) a game that JUST started, with only
pregame-built rows saved, does NOT reuse them -- it falls through to one
more real build; (3) once that real build has run once and stamped
built_after_lock=True, a later run for the same still-live game DOES reuse
the saved rows, so this isn't rebuilding every tick; (4) a game found
already live/final on a first-ever look (a late/failed earlier run) behaves
like case 2 -- no built_after_lock rows exist yet, so it still gets one real
build rather than reusing nothing or crashing on a missing game_pk.
"""
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bots.mlb_dashboard import HitterRecord, should_reuse_locked_rows  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def make_hitter(**over) -> HitterRecord:
    """Every no-default field filled with the empty value for its type."""
    kw = {}
    for f in dataclasses.fields(HitterRecord):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return HitterRecord(**kw)


PREGAME = {"gamePk": 823499, "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"}}
LIVE = {"gamePk": 823499, "status": {"abstractGameState": "Live", "detailedState": "In Progress"}}
FINAL = {"gamePk": 823499, "status": {"abstractGameState": "Final", "detailedState": "Final"}}

# -- 1. Pregame: never reuses, whatever is saved (unchanged behavior) -------
pregame_guess = make_hitter(player_id=101, built_after_lock=True)  # even if flagged
check("pregame game never reuses saved rows",
      should_reuse_locked_rows(PREGAME, 823499, {823499: [pregame_guess]}), False)

# -- 2. Just went live, only a pregame-built (built_after_lock=False) row set
#    saved -- must NOT reuse it. This is the bug, pinned: without the fix
#    this returned True the instant the game started, freezing the guess. --
just_locked_guess = make_hitter(player_id=101, built_after_lock=False)
check("game just live, only pregame rows saved -> do NOT reuse (needs one real build)",
      should_reuse_locked_rows(LIVE, 823499, {823499: [just_locked_guess]}), False)

# -- 3. After the one real build has run and stamped built_after_lock=True,
#    a later run for the same still-live game DOES reuse -- confirms this
#    fires once, not on every subsequent tick. ------------------------------
refreshed = make_hitter(player_id=101, built_after_lock=True)
check("game still live, rows already survived the real-lineup build -> reuse",
      should_reuse_locked_rows(LIVE, 823499, {823499: [refreshed]}), True)

# -- 4. Mixed saved rows for one game (shouldn't happen in practice, but the
#    ANY check means even one built_after_lock=True row is enough to trust
#    the whole saved set, matching how the fix stamps every row in one game
#    together in the same loop iteration). ---------------------------------
mixed = [make_hitter(player_id=101, built_after_lock=True), make_hitter(player_id=102, built_after_lock=True)]
check("all rows stamped together -> reuse", should_reuse_locked_rows(LIVE, 823499, {823499: mixed}), True)

# -- 5. Final game found on a first-ever look (a late/failed earlier run --
#    game_has_started()'s own docstring names this case) -- no saved rows
#    for this game_pk at all yet. Must fall through to a real build, not
#    crash on the missing key and not silently return True. ----------------
check("final game, nothing saved yet for this game_pk -> falls through to a real build",
      should_reuse_locked_rows(FINAL, 823499, {}), False)

# -- 6. Same late-first-look case, but a stale entry exists for a DIFFERENT
#    game_pk -- must not be confused by it. --------------------------------
other_game_row = make_hitter(player_id=999, built_after_lock=True)
check("unrelated game_pk in the dict doesn't leak into this one",
      should_reuse_locked_rows(FINAL, 823499, {555555: [other_game_row]}), False)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  * {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   lock waits for one real lineup build before reusing: {CHECKS} assertions")
