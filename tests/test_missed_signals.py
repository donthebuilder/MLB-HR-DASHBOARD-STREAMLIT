"""missed_signals.py — the four step-9b task-4 fixes (2026-08-22).

Run: python tests/test_missed_signals.py

This file exists because the research tool had no tests at all, and step 9's
component research found that two of its behaviours could invert a
recommendation:

  · load() deduped by player_id alone, so a doubleheader lost a line — the
    same defect Sol audit #2's finding #1 fixed in live_results_tracker.py.
  · the median split broke ties by input order, and the report described a
    negative residual as "LOWER is better" when it actually means the model
    is OVER-weighting the field.

Nothing here scores anything.
"""
import json
import os
import sys
import tempfile
import random
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

import missed_signals as MS  # noqa: E402

CHECKS = 0
FAILED = []


def check(label, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{label}: got {got!r}, want {want!r}")


def checkTrue(label, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(label)


def write_graded(d: Path, date: str, rows: list) -> None:
    (d / f"graded_results_{date}.json").write_text(
        json.dumps({"date": date, "graded_slots": rows}), encoding="utf-8")


def row(pid, game_pk, hr=0, ab=4, hr_score=50.0, **extra):
    r = {"player_id": pid, "game_pk": game_pk, "actual_ab": ab,
         "actual_hr": hr, "hr_score": hr_score}
    r.update(extra)
    return r


# ── 1. DOUBLEHEADER KEYING ─────────────────────────────────────────────────
# A player who bats in both legs of a split doubleheader has two independent,
# correctly-graded lines. Keying on player_id alone threw one away.
tmp = Path(tempfile.mkdtemp(prefix="missed_signals_test_"))
dh = [row(1, 900, hr=0), row(1, 901, hr=1)] + [row(100 + i, 902) for i in range(12)]
write_graded(tmp, "2026-08-17", dh)
loaded = MS.load([tmp])
pid1 = [r for r in loaded if r["player_id"] == 1]
check("doubleheader: both legs survive load()", len(pid1), 2)
check("doubleheader: the two legs are distinct games",
      {r["game_pk"] for r in pid1}, {900, 901})
checkTrue("doubleheader: the leg where he homered is kept",
          any((r.get("actual_hr") or 0) >= 1 for r in pid1))

# The same player in the SAME game twice (he can be a pick in several
# categories) must still collapse to one row — the fix must not double-count.
tmp2 = Path(tempfile.mkdtemp(prefix="missed_signals_test2_"))
dupe = [row(2, 910, hr=1), row(2, 910, hr=1)] + [row(200 + i, 911) for i in range(12)]
write_graded(tmp2, "2026-08-18", dupe)
loaded2 = MS.load([tmp2])
check("same game twice (multi-category pick) still collapses to one row",
      len([r for r in loaded2 if r["player_id"] == 2]), 1)

# A row with no plate appearance is still excluded — "never batted, not asked".
tmp3 = Path(tempfile.mkdtemp(prefix="missed_signals_test3_"))
write_graded(tmp3, "2026-08-19",
             [row(3, 920, ab=0)] + [row(300 + i, 921) for i in range(12)])
check("a player who never batted is excluded",
      [r for r in MS.load([tmp3]) if r["player_id"] == 3], [])


# ── 2. TIES ARE BROKEN AT RANDOM, NOT BY INPUT ORDER ───────────────────────
# The failure this prevents: on a field that is mostly one repeated value,
# an order-dependent split can manufacture a large lift out of nothing. The
# worst version of this bug (sorting the (value, outcome) tuple, so ties broke
# by the OUTCOME) produced +31.5 points on a field carrying no signal.
#
# Construction: one band, one field that is 0 for everybody except two rows,
# and outcomes ordered so that every y=0 precedes every y=1. Under an
# order-dependent split the low half collects the zeros and the lift is
# maximal; under a random tiebreak it is near zero on average.
# A field that is ENTIRELY constant is correctly skipped by the degenerate
# check, so the tie-break only matters when a field is mostly-but-not-quite
# constant — which is exactly the shape of a count or a flag.
tied = ([(0.0, 0)] * 39 + [(0.0, 1)] * 39 + [(1.0, 0)] * 2)
lifts = [MS.stratified_lift({0: list(tied)}, random.Random(seed))[0]
         for seed in range(80)]
mean_lift = sum(lifts) / len(lifts)
checkTrue("tie-break: the split actually varies with the seed, i.e. ties are "
          "not resolved by input order", len(set(lifts)) > 1)
checkTrue("tie-break: a mostly-constant field does not manufacture a lift on "
          f"average (mean {mean_lift:+.1f} pts over 80 seeds)", abs(mean_lift) < 8.0)

# And the thing it prevents: with the ties resolved by input order instead,
# the same data reports a large lift that is purely an artefact of ordering.
class _NoJitter:
    def random(self):
        return 0.0
order_lift = MS.stratified_lift({0: list(tied)}, _NoJitter())[0]
checkTrue("tie-break: order-resolved ties WOULD have manufactured a large lift "
          f"({order_lift:+.1f} pts) — this is what the fix prevents",
          abs(order_lift) > 40.0)

# And the ordering itself must still be honoured when values genuinely differ.
clean_band = {0: [(float(i), 1 if i >= 20 else 0) for i in range(40)]}
lift, hok, hn, lok, ln = MS.stratified_lift(clean_band, random.Random(0))
check("tie-break: a perfectly separating field still reports its full lift",
      round(lift, 1), 100.0)


# ── 3. FEATURE-SNAPSHOT PROVENANCE ─────────────────────────────────────────
# Step 9b task 1 stamps graded rows with feature_snapshot. Until it lands rows
# carry no stamp at all, so unstamped rows must be COUNTED and reported rather
# than silently analysed or silently dropped.
mixed = [{"feature_snapshot": "locked"}, {"feature_snapshot": "locked"},
         {"feature_snapshot": "post_game"}, {"feature_snapshot": "unavailable"},
         {}, {}, {}]
locked, stamped_bad, unstamped = MS.snapshot_split(mixed)
check("snapshot: locked rows counted", locked, 2)
check("snapshot: explicitly non-locked rows counted", stamped_bad, 2)
check("snapshot: unstamped rows counted separately, not lumped in", unstamped, 3)
check("snapshot: today's archive (no stamps at all) reads as fully unstamped",
      MS.snapshot_split([{}, {}]), (0, 0, 2))
check("snapshot: an all-locked archive reports nothing to warn about",
      MS.snapshot_split([{"feature_snapshot": "locked"}] * 3), (3, 0, 0))


# ── 4. "ADD THIS" vs "RE-WEIGHT THIS" ──────────────────────────────────────
# A negative within-band residual on a field the model already uses means it
# is OVER-weighted. The old wording ("LOWER is better") read as "add it,
# inverted" — the opposite recommendation. Telling the two cases apart needs
# an accurate list of what the model consumes, and a field missing from that
# list is reported as an ADD candidate when it should be a RE-WEIGHT one,
# which is the more dangerous direction to be wrong in.
for f in ("season_iso", "last5_hr", "pitcher_hr9", "park_factor",
          "recent_barrel_rate", "pitch_mix_score", "weak_spot_bonus",
          "batted_shape", "shape_max_ev", "hr_per_pa"):
    checkTrue(f"model-input list: {f} is recognised as already in the model",
              f in MS._MODEL_INPUT_FIELDS)
for f in ("games_since_last_hr", "pitcher_ld_rate", "pitcher_gb_rate",
          "recent_350_num", "longest_hr_score"):
    checkTrue(f"model-input list: {f} is NOT claimed as a model input",
              f not in MS._MODEL_INPUT_FIELDS)

# The blocklists must keep excluding outcomes and the score itself — testing a
# field against itself inside its own band is meaningless, and testing an
# outcome is circular.
checkTrue("blocklist: hr_score is never tested as a signal",
          "hr_score" in MS.BLOCK_EXACT)
checkTrue("blocklist: actual_* outcomes are never tested as signals",
          any("actual_".startswith(p) or p == "actual_" for p in MS.BLOCK_PREFIX))
checkTrue("blocklist: the model's own composite outputs stay excluded by default",
          "damage_conversion_score" in MS.DERIVED_SCORES)


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   missed_signals (doubleheader keying, random tie-break, feature "
      f"provenance, add-vs-reweight): {CHECKS} assertions")
