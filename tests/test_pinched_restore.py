"""restore_pinched_slots() -- A1 fix (2026-08-28,
moonshot-a1-results-diagnosis-2026-08-28.md §3): "players pinched don't
show on Results/the Homerun Ledger."

apply_locked_features() already separates out dropped_locked_rows (a
player the locked run knows about who's absent from the currently-
published slate) from the slate-membership shape pick-selection reads --
tests/test_locked_feature_join.py covers that split. This file covers the
next step: folding the DESIGNATED dropped players back into
tracking_slots so they actually reach grading, without corrupting any
existing pick_type-filtered stat.

What this proves:

  1. A dropped player who held a designation (game_pick_role non-empty)
     is restored into tracking_slots under a new pick_type, "PINCHED" --
     never under his original TOP/HR/HIT/HRR/CONTACT designation (that
     would double-count against the live substitute already holding it).
  2. His original designation survives as plain context
     (original_game_pick_role), unmodified/uppercased/order-preserved.
  3. A dropped player with NO designation at lock time is left out
     entirely -- not guessed into any tier.
  4. Every existing tracking_slots entry is left untouched (order and
     content), and restore_pinched_slots() never touches its input list
     in place.
  5. "PINCHED" collides with no existing exact-match pick_type filter in
     this file: not build_hr_category_counts()'s or
     build_hit_results_by_category()'s fixed key sets, not
     DESIGNED_OUTCOME, not build_summary_text()'s hard-coded
     top15/hr_picks/top_picks/hrr_picks/hit_picks/contact_picks filters.
     (category_display() DOES carry a friendly label for it -- that's
     display, not a stat filter, so it's fine for it to know the name.)

Run: python tests/test_pinched_restore.py
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


def checkTrue(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


# ── fixtures ─────────────────────────────────────────────────────────────

EXISTING_SLOT = {
    "player_id": 100, "name": "Live Substitute", "team": "AZ", "game_pk": 555,
    "hr_score": 61.0, "game_pick_role": "HR", "pick_type": "HR",
}

DROPPED_DESIGNATED = {
    "player_id": 200, "name": "Pinched Guy", "team": "AZ", "game_pk": 555,
    "hr_score": 58.0, "game_pick_role": "HR",
    # a field that is NOT in SLOT_FIELDS -- trim_row() must drop it
    "raw_pitch_profile": {"huge": "payload"},
}

DROPPED_DOUBLE_ROLE = {
    "player_id": 201, "name": "Double Badge Guy", "team": "AZ", "game_pk": 556,
    "hr_score": 70.0, "game_pick_role": "top/hr",
}

DROPPED_UNDESIGNATED = {
    "player_id": 202, "name": "Never A Pick", "team": "AZ", "game_pk": 557,
    "hr_score": 12.0, "game_pick_role": "",
}

DROPPED_NO_ROLE_FIELD = {
    "player_id": 203, "name": "No Role Key At All", "team": "AZ", "game_pk": 558,
    "hr_score": 9.0,
}


# ── 1 & 3: designated restored, undesignated skipped ───────────────────────
tracking_slots = [dict(EXISTING_SLOT)]
dropped = [DROPPED_DESIGNATED, DROPPED_UNDESIGNATED, DROPPED_NO_ROLE_FIELD]
result = lrt.restore_pinched_slots(tracking_slots, dropped)

check("existing slot + one restored designated player", len(result), 2)
restored_ids = {r["player_id"] for r in result}
checkTrue("designated dropped player 200 restored", 200 in restored_ids)
checkTrue("undesignated dropped player 202 NOT restored", 202 not in restored_ids)
checkTrue("no-role-field dropped player 203 NOT restored", 203 not in restored_ids)

restored_row = next(r for r in result if r["player_id"] == 200)
check("restored row gets pick_type PINCHED, not HR", restored_row["pick_type"], "PINCHED")
check("restored row keeps original role as context", restored_row["original_game_pick_role"], "HR")
checkTrue("restored row does NOT leak non-SLOT_FIELDS payload", "raw_pitch_profile" not in restored_row)
check("restored row keeps its real hr_score", restored_row["hr_score"], 58.0)

# ── 2: multi-role designation preserved in order, uppercased ───────────────
result2 = lrt.restore_pinched_slots([], [DROPPED_DOUBLE_ROLE])
check("one restored row for double-role player", len(result2), 1)
check("multi-role original_game_pick_role uppercased, order kept", result2[0]["original_game_pick_role"], "TOP/HR")
check("double-role player still just PINCHED, not TOP or HR", result2[0]["pick_type"], "PINCHED")

# ── 4: input list untouched, existing slots unmodified ──────────────────────
original_slots = [dict(EXISTING_SLOT)]
input_slots = [dict(EXISTING_SLOT)]
_ = lrt.restore_pinched_slots(input_slots, [DROPPED_DESIGNATED])
check("restore_pinched_slots does not mutate its tracking_slots argument", input_slots, original_slots)

result3 = lrt.restore_pinched_slots([dict(EXISTING_SLOT)], [DROPPED_DESIGNATED])
check("pre-existing slot unchanged (still pick_type HR)", result3[0]["pick_type"], "HR")
check("pre-existing slot is the first element (order preserved)", result3[0]["player_id"], 100)

# ── 5: PINCHED collides with nothing that filters graded_slots ─────────────
pinched_graded = {**restored_row, "got_hr": 1, "actual_hits": 1, "actual_ab": 4}
hr_counts = lrt.build_hr_category_counts([pinched_graded])
check("PINCHED HR does not get counted into the HR tier", hr_counts.get("HR", 0), 0)
checkTrue("PINCHED is not one of build_hr_category_counts' fixed keys", "PINCHED" not in hr_counts)

hit_groups = lrt.build_hit_results_by_category([pinched_graded])
checkTrue("PINCHED is not one of build_hit_results_by_category's fixed keys", "PINCHED" not in hit_groups)

checkTrue("PINCHED has no DESIGNED_OUTCOME entry (never silently graded as if it were a real tier)",
          "PINCHED" not in lrt.DESIGNED_OUTCOME)

# category_display() DOES know the name -- that's display-only, expected.
checkTrue("category_display has a real label for PINCHED, not a raw passthrough",
          lrt.category_display("PINCHED") != "PINCHED")

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
else:
    print("ok   restore_pinched_slots (A1 fix): pinched-but-designated players are restored into "
          "tracking_slots under a new, non-colliding pick_type, undesignated dropped players are "
          "left alone, and no existing pick_type-filtered stat gains a PINCHED row.")
    sys.exit(0)
