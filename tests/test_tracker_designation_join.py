"""build_tracking_slots() must grade the SITE'S published picks -- tracker
join fix, defect C (2026-08-22).

The dashboard's TOP/HR double-up (2026-08-12, build_game_pick_role_map)
deliberately allows one hitter to hold both badges, and CONTACT excludes no
one. build_tracking_slots()'s `used` set predated that change: it consumed
TOP's player and _designated("HR") skipped anyone already used, so in every
dual-badge game the Results board graded a DIFFERENT player than the site's
published HR pick. Measured on the archive before the fix: 94 of 94
TOP/HR-dual games since 08-12 had a wrong player in the board's HR row; only
16% of board "HR" rows (14% of "CONTACT") carried that badge on the site,
the board understated the real HR designation (19.9% shown vs 20.8% actual)
and overstated CONTACT by 16 points (52.9% vs 37.0%).
Evidence: claude/moonshot-hr-pick-correction-and-scoring.md.

The rule this file pins down: the tracker's exclusion behaviour mirrors the
dashboard's own, per role --
  TOP      first pick, no double-up question arises
  HR       may repeat TOP's player   (dashboard's _hr_slot excludes no one)
  HIT/HRR  still exclude prior picks (dashboard still passes `used`)
  CONTACT  may repeat anyone         (dashboard's CONTACT anchor excludes no one)

Run: python tests/test_tracker_designation_join.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.live_results_tracker as lrt  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def hitter(pid, name, role, **scores):
    r = {
        "player_id": pid, "name": name, "game_pk": 900100, "team": "AAA",
        "game_pick_role": role,
        "hr_score": 10.0, "overall_score": 10.0, "hit_score": 10.0,
        "hrr_score": 10.0, "contact_score": 10.0,
    }
    r.update(scores)
    return r


def slot_map(tracking):
    """{pick_type: player_id} for the per-game slots (ignores TOP15)."""
    return {t["pick_type"]: t["player_id"] for t in tracking
            if t["pick_type"] != "TOP15"}


def run():
    # ── 1. THE PRODUCTION SHAPE: one man badged TOP/HR ──────────────────
    rows = [
        hitter(1, "Dual Badge", "TOP/HR", overall_score=90, hr_score=90),
        hitter(2, "Second Bat", "", overall_score=60, hr_score=80),
        hitter(3, "Hit Guy", "HIT", hit_score=95),
        hitter(4, "Hrr Guy", "HRR", hrr_score=95),
        hitter(5, "Contact Guy", "CONTACT", contact_score=95),
    ]
    slots = slot_map(lrt.build_tracking_slots(rows))
    check("TOP slot is the site's TOP pick", slots["TOP"], 1)
    check("HR slot is the site's HR pick EVEN WHEN he is also TOP "
          "(the 94-of-94 defect)", slots["HR"], 1)
    check("HIT slot untouched", slots["HIT"], 3)
    check("HRR slot untouched", slots["HRR"], 4)
    check("CONTACT slot untouched", slots["CONTACT"], 5)

    # ── 2. CONTACT double-up: the anchor may already hold another badge ─
    rows = [
        hitter(1, "Top Bat", "TOP/CONTACT", overall_score=90, contact_score=95),
        hitter(2, "Hr Bat", "HR", hr_score=90),
        hitter(3, "Hit Guy", "HIT", hit_score=95),
        hitter(4, "Hrr Guy", "HRR", hrr_score=95),
    ]
    slots = slot_map(lrt.build_tracking_slots(rows))
    check("CONTACT slot is the site's CONTACT pick even when he is also TOP",
          slots["CONTACT"], 1)
    check("HR slot follows its own badge", slots["HR"], 2)

    # ── 3. HIT/HRR keep their exclusions (dashboard still excludes) ─────
    # No HIT badge published; the fallback must NOT hand HIT to a player
    # already holding TOP, matching pick_top(..., used)'s existing rule.
    rows = [
        hitter(1, "Top Bat", "TOP", overall_score=90, hit_score=99),
        hitter(2, "Hr Bat", "HR", hr_score=90),
        hitter(3, "Fallback Hit", "", hit_score=80),
        hitter(4, "Hrr Guy", "HRR", hrr_score=95),
    ]
    slots = slot_map(lrt.build_tracking_slots(rows))
    check("HIT fallback still excludes used players", slots["HIT"], 3)

    # ── 4. no badges at all: fallbacks behave exactly as before ─────────
    rows = [
        hitter(1, "A", "", overall_score=90, hr_score=50),
        hitter(2, "B", "", overall_score=50, hr_score=90),
        hitter(3, "C", "", hit_score=90),
        hitter(4, "D", "", hrr_score=90),
        hitter(5, "E", "", contact_score=90),
    ]
    slots = slot_map(lrt.build_tracking_slots(rows))
    check("fallback TOP by overall_score", slots["TOP"], 1)
    check("fallback HR by hr_score (excludes TOP, pre-08-12 archive shape)",
          slots["HR"], 2)
    check("fallback CONTACT by contact_score", slots["CONTACT"], 5)


run()

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   tracker designation join (defect C): {CHECKS} assertions, the "
      f"Results board grades the site's published HR/CONTACT picks even when "
      f"they double up with another badge, and HIT/HRR keep their exclusions")
