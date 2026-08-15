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
    """games are (ab, hits, runs, rbi, tb, hr) tuples, OLDEST FIRST — the
    order the StatsAPI game log actually arrives in."""
    return {"stats": [{"splits": [
        {"date": f"2026-07-{i + 1:02d}", "stat": {
            "atBats": g[0], "hits": g[1], "runs": g[2], "rbi": g[3],
            "totalBases": g[4], "homeRuns": g[5],
        }} for i, g in enumerate(games)
    ]}]}


FAILED = []


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


# ── the empty and degenerate cases ──────────────────────────────────────────
p = compute_blank_profile({})
check("empty status", p["blank_profile_status"], "empty")
check("empty streak", p["blank_streak"], 0)
check("empty n", p["after_blank_n"], 0)

# A log of nothing but pinch-run appearances is not a log of games.
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

# THE 0-AB GAME IS SKIPPED, NOT COUNTED EITHER WAY. He blanked, then pinch ran.
# His last GAME BATTED is still the 0-fer, and the streak is still 1 — the
# appearance neither breaks it (he didn't get a hit) nor extends it (he never
# had the chance to).
p = compute_blank_profile(log((4, 2, 1, 1, 3, 0), (3, 0, 0, 0, 0, 0), (0, 0, 1, 0, 0, 0)))
check("0-AB skipped: last is the 0-fer", p["last_game_ab"], 3)
check("0-AB skipped: streak", p["blank_streak"], 1)

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
check("0-AB does not break the chain", p["after_blank_n"], 1)
check("0-AB chain hit", p["after_blank_hit"], 1)

# totalBases missing falls back to hits, never to invented extra bases.
p = compute_blank_profile({"stats": [{"splits": [
    {"date": "2026-07-01", "stat": {"atBats": 4, "hits": 0}},
    {"date": "2026-07-02", "stat": {"atBats": 4, "hits": 2}},
]}]})
check("tb fallback", p["last_game_tb"], 2)
check("tb fallback no phantom 2+", p["after_blank_tb2"], 1)   # 2 hits = 2 TB, floor

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print("ok   compute_blank_profile: 22 assertions, definitions hold")
