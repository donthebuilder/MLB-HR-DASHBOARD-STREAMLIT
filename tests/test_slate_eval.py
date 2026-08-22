"""slate_eval.py — the unconditional full-slate evaluation (2026-08-22).

Run: python tests/test_slate_eval.py

Why this tool needs tests at all: it is the only measurement in the project
that cannot leak, so it is the one most likely to be trusted without being
re-derived. The two things most likely to go quietly wrong are WHICH RUN'S
SLATE it reads (the roster churns all day — on 2026-08-21 the slate went
266 -> 269 players across 24 runs, and four of that day's five "missed" home
runs were players dropped by a later rebuild) and whether the AUC is right.

Nothing here scores anything.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

import slate_eval as SE  # noqa: E402

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


def close(label, got, want, tol=1e-9):
    global CHECKS
    CHECKS += 1
    if got != got or abs(got - want) > tol:
        FAILED.append(f"{label}: got {got!r}, want ~{want!r}")


# ── AUC ────────────────────────────────────────────────────────────────────
close("auc: perfect separation is 1.0", SE.auc([9, 8, 7], [3, 2, 1]), 1.0)
close("auc: perfect inversion is 0.0", SE.auc([1, 2], [8, 9]), 0.0)
close("auc: all ties is exactly a coin flip", SE.auc([5, 5], [5, 5]), 0.5)
close("auc: one clean win over two, one tie", SE.auc([5], [1, 5]), 0.75)
checkTrue("auc: an empty side is nan, not a fabricated 0.5",
          SE.auc([], [1, 2]) != SE.auc([], [1, 2]))


# ── WHICH RUN'S SLATE ──────────────────────────────────────────────────────
# The roster churns across the day, so "the slate" is ambiguous and the choice
# is the whole ballgame. Preference: the prediction-of-record run; failing
# that the EARLIEST retained run. Never the last — that one has already seen
# the evening.
tmp = Path(tempfile.mkdtemp(prefix="slate_eval_test_"))


def write_log(run_id, date, generated_at, players):
    (tmp / f"prediction_log_{run_id}.jsonl").write_text("\n".join(
        [json.dumps({"run_id": run_id, "generated_at": generated_at})]
        + [json.dumps({"prediction_type": "slate_row", "prediction_date": date,
                       "player_id": pid, "scores": {"hr": s}})
           for pid, s in players.items()]), encoding="utf-8")


def write_graded(date, caught, missed, total=None):
    (tmp / f"graded_results_{date}.json").write_text(json.dumps({
        "date": date,
        "graded_slots": [],
        "hr_capture_report": {
            "total_hrs_on_slate": total if total is not None else len(caught) + len(missed),
            "caught_hrs_on_sheet": len(caught),
            "caught_homer_entries": [{"player_id": p} for p in caught],
            "missed_homer_entries": [{"player_id": p} for p in missed],
        }}), encoding="utf-8")


write_log("EARLY", "2026-08-21", "2026-08-21T07:00:00Z", {1: 80.0, 2: 20.0, 3: 55.0})
write_log("LOCKED", "2026-08-21", "2026-08-21T14:00:00Z", {1: 80.0, 2: 20.0, 4: 61.0})
write_log("LATE", "2026-08-21", "2026-08-22T03:00:00Z", {1: 80.0, 2: 20.0})
(tmp / "por_log_2026-08-21.jsonl").write_text(
    json.dumps({"prediction_date": "2026-08-21", "game_pk": "1", "run_id": "LOCKED"}),
    encoding="utf-8")
write_graded("2026-08-21", caught=[1], missed=[4])

runs, when = SE._slate_runs(tmp)
check("runs: every retained run is read, not just one", sorted(runs["2026-08-21"]),
      ["EARLY", "LATE", "LOCKED"])
check("runs: the roster genuinely differs between runs",
      sorted(runs["2026-08-21"]["EARLY"]), [1, 2, 3])
locked = SE._locked_run_ids(tmp)
check("por_log: the prediction-of-record run is identified", locked["2026-08-21"], ["LOCKED"])

homered, caps = SE._homered(tmp)
check("homered: BOTH caught and missed entries count as home runs",
      homered["2026-08-21"], {1, 4})
check("capture: totals are carried for the ceiling line", caps["2026-08-21"], (2, 1))

# The selection itself, mirroring main()'s logic.
def pick(date):
    rid = next((r for r in locked.get(date, []) if r in runs[date]), None)
    if rid is None:
        rid = min(runs[date], key=lambda r: when.get(r) or "")
        return rid, "fallback"
    return rid, "locked"


rid, how = pick("2026-08-21")
check("selection: the locked run wins when por_log names one", rid, "LOCKED")
check("selection: and it is labelled as locked", how, "locked")

# With no por_log the fallback must be the EARLIEST run, never the latest --
# the last rebuild of the night is the one that has already seen the evening.
os.remove(tmp / "por_log_2026-08-21.jsonl")
locked = SE._locked_run_ids(tmp)
rid, how = pick("2026-08-21")
check("selection: without por_log it falls back to the EARLIEST run", rid, "EARLY")
check("selection: and says so, so nobody reads it as locked", how, "fallback")
checkTrue("selection: it is never the last run of the night", rid != "LATE")

# A por_log naming a run whose log has aged out must not be selected blindly.
(tmp / "por_log_2026-08-21.jsonl").write_text(
    json.dumps({"prediction_date": "2026-08-21", "game_pk": "1", "run_id": "GONE"}),
    encoding="utf-8")
locked = SE._locked_run_ids(tmp)
rid, how = pick("2026-08-21")
check("selection: a por_log run that aged out of retention degrades to the "
      "fallback rather than crashing", rid, "EARLY")


# ── THE JOIN ───────────────────────────────────────────────────────────────
# A player rated but not in either homer list is a genuine negative; a player
# in the missed list was never rated, so he cannot appear on the left side at
# all. Both are correct and neither should be silently dropped.
sel = runs["2026-08-21"]["EARLY"]
rows = [(s, 1 if pid in homered["2026-08-21"] else 0) for pid, s in sel.items()]
check("join: three rated players produce three rows", len(rows), 3)
check("join: the rated home-run hitter is a positive",
      sorted(r for r in rows if r[1]), [(80.0, 1)])
checkTrue("join: an unrated home-run hitter (id 4) is absent from the left side, "
          "not invented as a row", all(x != 61.0 for x, _y in rows))


# ── REFUSALS ───────────────────────────────────────────────────────────────
checkTrue("thresholds: the tool declares a minimum before it will report",
          SE.MIN_DATES >= 2 and SE.MIN_ROWS >= 100)

empty = Path(tempfile.mkdtemp(prefix="slate_eval_empty_"))
r, w = SE._slate_runs(empty)
h, c = SE._homered(empty)
check("empty dir: no runs", dict(r), {})
check("empty dir: no homers", dict(h), {})

# A malformed line must not take the whole file down -- a logging payload can
# never be the reason an evaluation refuses to run.
(tmp / "prediction_log_BROKEN.jsonl").write_text(
    json.dumps({"run_id": "BROKEN", "generated_at": "2026-08-21T09:00:00Z"})
    + "\n{ this is not json\n"
    + json.dumps({"prediction_type": "slate_row", "prediction_date": "2026-08-21",
                  "player_id": 9, "scores": {"hr": 44.0}}),
    encoding="utf-8")
runs2, _ = SE._slate_runs(tmp)
check("robustness: a corrupt line is skipped and the good rows still load",
      runs2["2026-08-21"]["BROKEN"], {9: 44.0})


# ── PERMUTATION IS DATE-STRATIFIED ─────────────────────────────────────────
# Shuffling within a date means a "some nights are homer nights" effect cannot
# manufacture a result. Under a null where the score carries nothing, p should
# be unremarkable.
rng = random.Random(1)
flat = {"d1": [(50.0, 1), (50.0, 0), (50.0, 1), (50.0, 0)],
        "d2": [(10.0, 1), (10.0, 0), (10.0, 1), (10.0, 0)]}
p = SE.permutation_p(flat, 0.5, rng, iters=200)
checkTrue(f"permutation: a score carrying nothing is not significant (p={p:.3f})", p > 0.2)


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   slate_eval (AUC, locked-run selection, the join, refusals): {CHECKS} assertions")
