"""compute_blank_profile — the definitions this board stands on.

Run: python tests/test_blank_profile.py

The board Donovan asked for ("all the players who blanked in their last game,
with the price beside the hit rate") is only worth having if "blanked" and
"after a blank" mean exactly one thing. These assertions ARE those
definitions; if one of them has to change, the board's caption changes too.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.mlb_dashboard import compute_blank_profile  # noqa: E402


def log(*games):
    """games are (ab, hits, runs, rbi, tb, hr[, bb]) tuples, OLDEST FIRST — the
    order the StatsAPI game log actually arrives in. plateAppearances is
    derived as ab + bb, because PA is the gate (see below)."""
    out = []
    for i, g in enumerate(games):
        bb = g[6] if len(g) > 6 else 0
        out.append({"date": f"2026-07-{i + 1:02d}", "stat": {
            "atBats": g[0], "hits": g[1], "runs": g[2], "rbi": g[3],
            "totalBases": g[4], "homeRuns": g[5], "baseOnBalls": bb,
            "plateAppearances": g[0] + bb,
        }})
    return {"stats": [{"splits": out}]}


FAILED = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


# ── the empty and degenerate cases ──────────────────────────────────────────
p = compute_blank_profile({})
check("empty status", p["blank_profile_status"], "empty")
check("empty streak", p["blank_streak"], 0)
check("empty n", p["after_blank_n"], 0)

# A log of nothing but pinch-run appearances (never came to the plate) is not
# a log of games. PA = 0 on every one of them.
p = compute_blank_profile(log((0, 0, 1, 0, 0, 0), (0, 0, 0, 0, 0, 0)))
check("no batted games", p["blank_profile_status"], "no_batted_games")
check("no batted streak", p["blank_streak"], 0)

# ── the last game ───────────────────────────────────────────────────────────
p = compute_blank_profile(log((4, 1, 1, 2, 4, 1), (3, 0, 0, 0, 0, 0)))
check("last game ab", p["last_game_ab"], 3)
check("last game hits", p["last_game_hits"], 0)
check("last game date", p["last_game_date"], "2026-07-02")
check("last game hrr", p["last_game_hrr"], 0)
check("status ok", p["blank_profile_status"], "ok")

# THE PINCH-RUN (PA = 0) IS SKIPPED. He blanked, then pinch ran without
# batting. His last game AT THE PLATE is still the 0-fer, and the streak is
# still 1 — the appearance neither breaks it nor extends it, because he never
# came to the plate.
p = compute_blank_profile(log((4, 2, 1, 1, 3, 0), (3, 0, 0, 0, 0, 0), (0, 0, 1, 0, 0, 0)))
check("pinch-run skipped: last is the 0-fer", p["last_game_ab"], 3)
check("pinch-run skipped: streak", p["blank_streak"], 1)

# ── A WALK-ONLY NIGHT IS A BLANK (Donovan, 2026-08-16) ──────────────────────
# "walk only nights count as a blank too still counts." He came to the plate
# (PA 2) and got no hit, so it IS a hitless game. The first version gated on
# atBats and silently dropped this man's 0-fer, which shortened the streaks of
# exactly the patient hitters most likely to be on this board.
p = compute_blank_profile(log((0, 0, 0, 0, 0, 0, 2),))
check("walk-only is a game", p["blank_profile_status"], "ok")
check("walk-only is a blank", p["blank_streak"], 1)
check("walk-only last_game_pa", p["last_game_pa"], 2)
check("walk-only last_game_ab", p["last_game_ab"], 0)

# And it EXTENDS a streak rather than being skipped over.
p = compute_blank_profile(log((4, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 1), (3, 0, 0, 0, 0, 0)))
check("walk-only extends the streak", p["blank_streak"], 3)

# A walk-only game is also a legitimate FOLLOW-UP to a blank, and a failed one.
p = compute_blank_profile(log((4, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 1)))
check("walk-only counts as a follow-up", p["after_blank_n"], 1)
check("walk-only follow-up got no hit", p["after_blank_hit"], 0)

# ── the streak ──────────────────────────────────────────────────────────────
p = compute_blank_profile(log((4, 1, 0, 0, 1, 0), (4, 0, 0, 0, 0, 0), (3, 0, 1, 0, 0, 0), (4, 0, 0, 1, 0, 0)))
check("three straight blanks", p["blank_streak"], 3)

p = compute_blank_profile(log((4, 0, 0, 0, 0, 0), (4, 2, 1, 1, 5, 1)))
check("hit last game ends streak", p["blank_streak"], 0)

# A walk-and-a-run night still counts as a blank if he batted and didn't hit —
# hrr can be nonzero on a hitless game, and that is exactly why the HRR
# columns are a different question from the hit column.
p = compute_blank_profile(log((3, 0, 1, 1, 0, 0),))
check("hitless but productive is still a blank", p["blank_streak"], 1)
check("hitless but productive hrr", p["last_game_hrr"], 2)

# ── after a blank ───────────────────────────────────────────────────────────
# Games:      1: 0-for   2: hit    3: 0-for   4: 0-for   5: hit
# Follow-ups (game whose PREVIOUS batted game was hitless): 2, 4, 5
p = compute_blank_profile(log(
    (4, 0, 0, 0, 0, 0),
    (4, 2, 1, 1, 3, 0),
    (4, 0, 0, 0, 0, 0),
    (4, 0, 1, 0, 0, 0),
    (4, 1, 0, 2, 4, 1),
))
check("after_blank_n", p["after_blank_n"], 3)
check("after_blank_hit", p["after_blank_hit"], 2)          # games 2 and 5
check("after_blank_hrr1", p["after_blank_hrr1"], 3)        # 4 scored a run
check("after_blank_hrr2", p["after_blank_hrr2"], 2)        # 2 (2+1+1) and 5 (1+0+2)
check("after_blank_tb2", p["after_blank_tb2"], 2)          # 3 TB and 4 TB

# THE FIRST GAME OF THE LOG IS NEVER A FOLLOW-UP. Nothing precedes it, and
# counting it would quietly credit or blame him for a game outside the window.
p = compute_blank_profile(log((4, 2, 0, 0, 2, 0),))
check("single game is not a follow-up", p["after_blank_n"], 0)

# THE 0-AB GAME DOES NOT BREAK THE CHAIN EITHER. blank → pinch-run → next
# start is still a follow-up to the blank, because the blank is still the
# previous game he BATTED in.
p = compute_blank_profile(log((4, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (4, 2, 0, 0, 2, 0)))
check("pinch-run does not break the chain", p["after_blank_n"], 1)
check("pinch-run chain hit", p["after_blank_hit"], 1)

# totalBases missing falls back to hits, never to invented extra bases.
# No plateAppearances published at all: the AB+BB fallback keeps both games.
p = compute_blank_profile({"stats": [{"splits": [
    {"date": "2026-07-01", "stat": {"atBats": 4, "hits": 0}},
    {"date": "2026-07-02", "stat": {"atBats": 4, "hits": 2}},
]}]})
check("tb fallback", p["last_game_tb"], 2)
check("tb fallback no phantom 2+", p["after_blank_tb2"], 1)   # 2 hits = 2 TB, floor

# ── THE CONTROL COHORTS (2026-08-16) ────────────────────────────────────────
#
# The board compares a hitter's after-a-blank rate to what the book charges.
# That answers "is he mispriced". It does not answer "does blanking predict
# anything", which needs the rate measured against something of HIS OWN.
#
# Two baselines ship, and the tests below exist mostly to pin down that they
# are DIFFERENT things, because reporting one as the other is the whole risk:
#
#   overall_*   every batted game, first included. Contains the after_blank
#               games, so it is context and not a control.
#   after_hit_* the true complement of after_blank: same follow-up rule, same
#               bars, same PA gate, opposite condition. THIS is what a
#               two-proportion test may use.
#
# Same five-game log as the follow-up block above:
#   1: 0-for   2: hit   3: 0-for   4: 0-for(walked, 1 R)   5: hit
# Previous batted game hitless -> 2, 4, 5 are after_blank.
# Previous batted game had a hit -> 3 only.
# Game 1 is in NEITHER: nothing precedes it.
p = compute_blank_profile(log(
    (4, 0, 0, 0, 0, 0),
    (4, 2, 1, 1, 3, 0),
    (4, 0, 0, 0, 0, 0),
    (4, 0, 1, 0, 0, 0),
    (4, 1, 0, 2, 4, 1),
))
check("overall_n counts every batted game", p["overall_n"], 5)
check("overall_hit", p["overall_hit"], 2)
check("overall_tb2", p["overall_tb2"], 2)
check("after_hit_n", p["after_hit_n"], 1)                  # game 3 only
check("after_hit_hit", p["after_hit_hit"], 0)              # game 3 was a 0-for
check("after_hit_hrr1", p["after_hit_hrr1"], 0)
check("after_hit_tb2", p["after_hit_tb2"], 0)

# THE TWO COHORTS PARTITION THE FOLLOW-UPS, EXACTLY. If this ever fails, one
# of the three walks has drifted from the others and every comparison the
# board draws off them is measuring two different things.
check("cohorts partition the follow-ups",
      p["after_blank_n"] + p["after_hit_n"], p["overall_n"] - 1)

# overall_* IS NOT a control -- it contains the after_blank games. Asserted so
# nobody "simplifies" the two down to one field later.
check("overall_n exceeds after_blank_n", p["overall_n"] > p["after_blank_n"], True)

# A log with no blank in it at all: every follow-up is after_hit, and the
# after_blank counters must be a clean zero rather than absent.
p = compute_blank_profile(log(
    (4, 1, 0, 0, 1, 0),
    (4, 2, 0, 1, 2, 0),
    (4, 1, 1, 2, 4, 1),
))
check("no blanks: after_blank_n", p["after_blank_n"], 0)
check("no blanks: after_hit_n", p["after_hit_n"], 2)
check("no blanks: overall_n", p["overall_n"], 3)
check("no blanks: overall_hit", p["overall_hit"], 3)

# A pinch-run is not a game in EITHER cohort, and does not break the chain for
# either. blank -> pinch-run -> start stays after_blank; the pinch-run itself
# is never tallied anywhere.
p = compute_blank_profile(log((4, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (4, 2, 0, 0, 2, 0)))
check("pinch-run not in overall_n", p["overall_n"], 2)
check("pinch-run not in after_hit_n", p["after_hit_n"], 0)

# A walk-only night IS a game and IS a blank, so the game after it is
# after_blank, not after_hit -- the same correction Donovan made on 08-15,
# now pinned on the control side too.
p = compute_blank_profile({"stats": [{"splits": [
    {"date": "2026-07-01", "stat": {"atBats": 0, "baseOnBalls": 2, "hits": 0}},
    {"date": "2026-07-02", "stat": {"atBats": 4, "hits": 2, "totalBases": 2}},
]}]})
check("walk-only night is a batted game", p["overall_n"], 2)
check("game after a walk-only 0-fer is after_blank", p["after_blank_n"], 1)
check("game after a walk-only 0-fer is NOT after_hit", p["after_hit_n"], 0)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   compute_blank_profile: {CHECKS} assertions, definitions hold")
