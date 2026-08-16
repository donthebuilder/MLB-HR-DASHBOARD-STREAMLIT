"""One run, one piece of advice — the two label contradictions this bot shipped.

Run: python tests/test_hr_gate_label.py

Both halves of this file test the same class of bug: a field that describes
another field, written at a different moment from the field it describes, and
therefore free to say the opposite of it. Neither is a scoring bug — no pick and
no number moves in either fix — which is exactly why they survived so long. A
label that contradicts itself renders perfectly.

1. `best_bet_type` said "Avoid for HR" about hitters the same run designated TOP
   or HR, whose bar IS a home run (3 hitters on mock/fix_slate.json, 266 rows).
   `best_bet_type` is per-hitter and finalised in score_hitter(); the designation
   is a slate-level one-pick-per-game ranking that cannot run until every game's
   rows exist. The label literally could not know.

2. `weak_spot_flag` and `weak_spot_reason` are set together and consistently at
   HitterRecord construction, and then score_hitter() overwrites ONLY the flag
   with a different, better test. On the same slate: 42 flags, 57 reasons, 27
   both — 45 rows where two fields describing one ⭐ disagreed.

The assertions below ARE the two invariants. If one has to change, the promise
the site makes to a reader changes with it.
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.mlb_dashboard import (  # noqa: E402
    HitterRecord,
    reconcile_best_bet_with_designation,
    score_hitter,
)

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def row(pid, role, bbt, **extra):
    """A published payload row, in the shape reconcile_* actually receives —
    a plain dict off dataclasses.asdict() with game_pick_role already stamped."""
    r = {"player_id": pid, "name": f"P{pid}", "game_pk": 700 + (pid % 3),
         "game_pick_role": role, "best_bet_type": bbt}
    r.update(extra)
    return r


# ═══ 1. THE HR GATE LABEL ════════════════════════════════════════════════════
#
# The whole slate at once, so the invariant is checked against the mixed input
# the function really gets — designated and undesignated, avoiding and not.
payload = [
    row(1, "TOP", "Avoid for HR"),            # the contradiction, TOP only
    row(2, "TOP/HR", "Avoid for HR"),         # the contradiction, doubled up
    row(3, "TOP/HR/CONTACT", "Avoid HR"),     # the OTHER avoid string
    row(4, "HR", "avoid for hr"),             # case must not matter
    row(5, "TOP", "  Avoid for HR  "),        # nor whitespace
    row(6, "HIT", "Avoid for HR"),            # NOT designated on a HR bar
    row(7, "HRR/CONTACT", "Avoid for HR"),    # nor is this one
    row(8, "", "Avoid for HR"),               # undesignated: the 177-hitter case
    row(9, "TOP", "HR"),                      # designated, already coherent
    row(10, "TOP/HR", "HRR + HR Sprinkle"),   # designated, a non-avoid caution
    row(11, None, None),                      # a row with nothing on it at all
]
out = reconcile_best_bet_with_designation(payload)
check("returns the same list it was handed", out is payload, True)

by_id = {r["player_id"]: r for r in out}

# THE INVARIANT. Stated as the sweep it is, not as eleven separate equalities:
# no row wearing a home-run badge may publish an avoid.
offenders = [
    r["player_id"] for r in out
    if ({x.strip().upper() for x in str(r.get("game_pick_role") or "").split("/") if x.strip()}
        & {"TOP", "HR"})
    and str(r.get("best_bet_type") or "").strip().lower().startswith("avoid")
]
check("no TOP/HR row publishes an avoid", offenders, [])

# NOTHING IS DELETED. The avoid verdict is real information — it is what the
# site's hrGateVerdict() prints next to the flag's own 18/55 record — so every
# row that changed must still carry the exact original string.
check("raw kept, TOP only", by_id[1].get("best_bet_type_raw"), "Avoid for HR")
check("raw kept, TOP/HR", by_id[2].get("best_bet_type_raw"), "Avoid for HR")
check("raw kept verbatim, second avoid string", by_id[3].get("best_bet_type_raw"), "Avoid HR")
check("raw kept verbatim, odd case", by_id[4].get("best_bet_type_raw"), "avoid for hr")
check("raw kept verbatim, whitespace not trimmed", by_id[5].get("best_bet_type_raw"), "  Avoid for HR  ")

# hr_gate_flagged is what the site keys on, so it must be set on exactly the
# rows that changed and absent everywhere else — absent has to keep meaning
# "there is nothing here to explain".
flagged = sorted(r["player_id"] for r in out if r.get("hr_gate_flagged"))
check("flagged set is exactly the changed rows", flagged, [1, 2, 3, 4, 5])
check("raw appears on exactly the flagged rows",
      sorted(r["player_id"] for r in out if "best_bet_type_raw" in r), [1, 2, 3, 4, 5])

# THE SUBSTITUTED VALUE IS A BET TYPE, NOT A ROLE NAME. The first cut of this
# repair wrote 'TOP' for TOP-only rows. "TOP" is a game_pick_role value; the
# best_bet_type value space is what _hr2_best_bet_and_label() and
# apply_decision_engine_v31() can return, and live_results_tracker archives this
# field and slices the graded table by it — a role name in there invents a
# category. TOP and HR are both graded on a home run, so both say HR.
VOCAB = {"HR", "HR or HRR", "HRR + HR Sprinkle", "HRR / XBH", "HRR / Hits",
         "Avoid for HR", "Avoid HR"}
check("TOP-only gets a bet type, not the role name", by_id[1]["best_bet_type"], "HR")
check("TOP/HR gets HR", by_id[2]["best_bet_type"], "HR")
check("every rewritten value is in the best_bet_type vocabulary",
      [r["best_bet_type"] for r in out if r.get("hr_gate_flagged") and r["best_bet_type"] not in VOCAB],
      [])

# NOBODY ELSE IS TOUCHED. "Skip the homer, take him for hits" is coherent advice
# for a hitter with no home-run badge, and it is the majority of the slate (177
# of 266 rows on the reference slate). Those rows must come out byte-identical.
check("HIT pick keeps its avoid", by_id[6]["best_bet_type"], "Avoid for HR")
check("HRR/CONTACT pick keeps its avoid", by_id[7]["best_bet_type"], "Avoid for HR")
check("undesignated keeps its avoid", by_id[8]["best_bet_type"], "Avoid for HR")
check("untouched rows carry no raw", [k for k in (6, 7, 8) if "best_bet_type_raw" in by_id[k]], [])
check("untouched rows carry no gate flag", [k for k in (6, 7, 8) if "hr_gate_flagged" in by_id[k]], [])
check("coherent TOP row untouched", by_id[9]["best_bet_type"], "HR")
check("non-avoid caution on a TOP/HR row survives", by_id[10]["best_bet_type"], "HRR + HR Sprinkle")
check("no gate flag on the coherent designated rows",
      [k for k in (9, 10) if "hr_gate_flagged" in by_id[k]], [])
check("the empty row is left empty", by_id[11]["best_bet_type"], None)

# Idempotent: the hourly slate rebuild reruns this over rows that may already
# have been through it, and a second pass must not overwrite raw with the
# substitute (which would erase the original verdict on run two).
reconcile_best_bet_with_designation(payload)
check("second pass does not eat the raw verdict", by_id[1].get("best_bet_type_raw"), "Avoid for HR")
check("second pass leaves the label alone", by_id[1]["best_bet_type"], "HR")

check("empty payload is fine", reconcile_best_bet_with_designation([]), [])
check("None payload is fine", reconcile_best_bet_with_designation(None), None)


# ═══ 2. THE ⭐ FLAG AND ITS REASON ════════════════════════════════════════════
#
# HitterRecord has 130 fields with no default. Rather than hand-write 130
# zeroes (which would rot the first time a field is added), fill every
# defaultless field with the empty value for its type and override only what
# the star_flag branch under test actually reads.
_MISSING = dataclasses.MISSING


def hitter(**over) -> HitterRecord:
    kw = {}
    for f in dataclasses.fields(HitterRecord):
        if f.default is not _MISSING or f.default_factory is not _MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return HitterRecord(**kw)


PITCHER_NOTE = "Pitcher has allowed 2 HR to the #3 spot in 38 PA this season (.500 SLG)."


def scored(**over) -> HitterRecord:
    """A hitter run through the real score_hitter(), which is where the flag is
    overwritten and where the reason now gets rewritten with it."""
    base = dict(player_id=1, name="X", team="NYY", game_pk=700,
                bats="R", pitcher_throws="L", season_pa=400, lineup_spot=3)
    base.update(over)
    return score_hitter(hitter(**base))


# (a) THE ⭐ FIRES ON REAL PITCHER DAMAGE. Spot 7 keeps primary_weak/secondary_weak
# out of it, so only the (true_spot_hot and power_gate) clause can be firing.
r = scored(lineup_spot=7, iso_vs_lhp=0.240, weak_spot_reason=PITCHER_NOTE,
           pitcher_lineup_spot_damage={"7": {"damage_score": 71.0, "label": "HITTER ADV"}})
check("a: star fires on hot spot damage", r.weak_spot_flag, True)
check("a: reason is not empty", bool(r.weak_spot_reason.strip()), True)
check("a: the pitcher-side sentence is kept, not thrown away",
      r.weak_spot_reason.startswith(PITCHER_NOTE), True)
check("a: and the star's own clause is appended after it",
      "power to use it" in r.weak_spot_reason, True)
check("a: the spot damage number is quoted", "71" in r.weak_spot_reason, True)

# (b) THE ⭐ FIRES WITH NO PITCHER WEAKNESS AT ALL. This is the 15-row case: spot
# 3 + platoon edge + power, against a pitcher who is DOMINANT at that spot. It
# used to publish a star with a blank reason. It must now say what it really
# means, and must NOT claim the pitcher is weak there.
r = scored(lineup_spot=3, iso_vs_lhp=0.240, pitcher_weak_side="RHB",
           pitcher_lineup_spot_damage={"3": {"damage_score": 4.9, "label": "PITCHER ADV"}})
check("b: star fires on the matchup clause", r.weak_spot_flag, True)
check("b: no longer a star with no reason", bool(r.weak_spot_reason.strip()), True)
check("b: says the star is the matchup, not a hole in the pitcher",
      "not a hole in the pitcher" in r.weak_spot_reason, True)
check("b: and quotes how strong the pitcher actually is there",
      "5 of 100" in r.weak_spot_reason, True)

# (c) NO ⭐, BUT THE PITCHER IS WEAK AT THAT SPOT. The 30-row case, and the one
# that made the Games spot read look broken: a reason with no star. The reason
# is now blank, and no fact is lost — pitcher_spot_damage_reason on this very
# row carries the same numbers in more detail.
r = scored(lineup_spot=3, iso_vs_lhp=0.020, weak_spot_reason=PITCHER_NOTE,
           pitcher_lineup_spot_damage={"3": {"damage_score": 46.6, "label": "NEUTRAL",
                                             "reason": "spot #3: 38 PA, 0.500 SLG"}})
check("c: no star", r.weak_spot_flag, False)
check("c: and therefore no ⭐ reason", r.weak_spot_reason, "")
check("c: the pitcher-side facts still ship on the same row",
      r.pitcher_spot_damage_reason, "spot #3: 38 PA, 0.500 SLG")
check("c: and the number behind them", r.pitcher_spot_damage_score, 46.6)

# (d) THE PLAIN NEGATIVE: nothing going on, no star, no reason, and the
# construction-time reason (which is "" here anyway) stays "".
r = scored(lineup_spot=9, iso_vs_rhp=0.020, pitcher_throws="R")
check("d: no star", r.weak_spot_flag, False)
check("d: no reason", r.weak_spot_reason, "")

# THE INVARIANT, swept across every case above plus a small grid, so it is
# checked as a rule rather than case by case.
grid = []
for spot in (1, 3, 5, 7, 9):
    for iso in (0.020, 0.190, 0.260):
        for dmg in (4.9, 46.6, 71.0):
            for weak_side in ("", "RHB"):
                for note in ("", PITCHER_NOTE):
                    grid.append(scored(
                        lineup_spot=spot, iso_vs_lhp=iso, pitcher_weak_side=weak_side,
                        weak_spot_reason=note,
                        pitcher_lineup_spot_damage={str(spot): {"damage_score": dmg}},
                    ))
broken = [(h.lineup_spot, h.iso_vs_lhp, h.pitcher_spot_damage_score, h.pitcher_weak_side)
          for h in grid if bool(h.weak_spot_flag) != bool(str(h.weak_spot_reason or "").strip())]
# Reported as (count, first three) rather than the whole list: a full revert
# breaks 90 of these 180 rows and a 90-tuple dump buries the other failures.
check(f"flag and reason agree on all {len(grid)} combinations",
      (len(broken), broken[:3]), (0, []))
# ...and the grid has to actually contain both answers, or the sweep above is
# vacuous — a sweep of 180 all-False rows would pass with the fix reverted.
check("the grid exercises stars", any(h.weak_spot_flag for h in grid), True)
check("the grid exercises non-stars", any(not h.weak_spot_flag for h in grid), True)


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   hr_gate_label + weak_spot pair: {CHECKS} assertions, "
      f"a TOP/HR row never publishes an avoid and weak_spot_flag <=> weak_spot_reason")
