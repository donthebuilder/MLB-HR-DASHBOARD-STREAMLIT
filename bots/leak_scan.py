#!/usr/bin/env python3
"""
LEAK SCAN — does the graded archive know tonight's result before it happened?
============================================================================

WHY THIS EXISTS

Studying the "due" score turned up a number that cannot be true: across the
graded archive, hitters with `last5_hr == 0` homered 8 times in 584 rows, a
1.4% rate against an 18.1% base. Real baseball has no cold streak that
absolute. A hitter who has not gone deep in five games still homers at most of
his usual rate -- he does not stop being able to.

The explanation is mechanical, not baseball. `last5_hr` counts his last five
GAMES, and if the value written into the graded file is refreshed after the
game finishes, tonight is one of those five. A man who homered tonight then has
last5_hr >= 1 by construction, so last5_hr == 0 excludes him automatically.
The column is not predicting the outcome. It is remembering it.

Any model tuned against a leaked field learns the leak. That is why this
project's standing rule says no hr_blend weight changes before 9c: the archive
manufactures tuning signals out of its own contamination. This script is how
that rule stops being folklore and starts being a check you can run.

HOW THE DETECTOR WORKS

For every numeric field, take the rows where it is exactly zero -- the "he has
none of this" bucket -- and ask how likely that many home runs are under the
night's own base rate, as a binomial lower tail. A pre-game field's zero bucket
should be unremarkable, or at worst mildly predictive. A leaked field's zero
bucket is impossible: it excludes the outcome rather than predicting it.

Computed in log space on purpose. The first version used math.comb and
overflowed on n=600 -- a detector that dies on the biggest sample is a detector
that only ever sees small ones.

WHAT IS DELIBERATELY NOT FLAGGED

The graded file carries the OUTCOME columns beside the feature columns:
got_hr, tb_2_plus, hrr_total, designed_hit and friends. Those correlate with
actual_hr because they ARE the result. They are excluded by name, and the
exclusion list is explicit rather than a prefix guess, so a new outcome column
shows up as a loud false positive and gets added on purpose instead of being
silently swallowed by a pattern.

WHAT A CLEAN SCAN DOES AND DOES NOT PROVE

It proves no field's zero bucket is impossible at the sample you gave it. On a
handful of nights that is a weak statement, and the report says so with the
smallest leak this scan could actually have caught. Absence of evidence is
reported as absence of evidence.
"""
from __future__ import annotations

import glob
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), "public", "data", "current")

# Columns that ARE the result. Correlation with actual_hr is their job.
OUTCOME_FIELDS = {
    "got_hr", "got_xbh", "got_base_hit", "designed_hit",
    "hrr_total", "hrr_2_plus", "hrr_3_plus",
    "tb_2_plus", "tb_3_plus", "tb_total",
    "hit_2_plus", "multi_hit", "graded", "slot_hit", "slot_result",
}

# Identity and bookkeeping — never features.
SKIP_FIELDS = {"player_id", "game_pk", "mlb_id", "lineup_spot", "batting_order"}

MIN_ROWS = 200      # a field needs this many rows before it is worth testing
MIN_BUCKET = 40     # ...and this many in the zero bucket
LOG_P_ALARM = -3.0  # log10 p; -3 is one in a thousand

# THE LINE BETWEEN A LEAK AND A GOOD SIGNAL, and the reason this scan needs two
# tiers rather than one threshold.
#
# The first version tested each zero bucket against the board's own base rate.
# That flags a leak — and it also flags any genuinely strong predictor, because
# "far below base" is exactly what a strong predictor looks like. A field that
# HALVES the home-run rate over 600 rows came out at log10 p = -9.9, which is
# a discovery being reported as a defect.
#
# What separates them is not distance from base, it is possibility. A real
# pre-game signal depresses the rate. A leaked column excludes the outcome. So
# the leak test runs against a DELIBERATELY GENEROUS floor: the rate a very
# strong honest signal could reach, taken here as a third of the board's base.
# Below that, no arrangement of pitcher, park and form explains it and the
# column is remembering rather than predicting.
#
# 0.35 is a judgement, not a measurement, and it is the one number in this file
# worth arguing with. Raise it and honest signals start being called leaks;
# lower it and a weak leak hides behind it. It is named so the argument can be
# had in one place.
STRONG_SIGNAL_FLOOR = 0.35


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def log10_binom_tail_le(k: int, n: int, p: float) -> float:
    """log10 P(X <= k) for X ~ Binomial(n, p), summed in log space.

    Log space is not decoration: n runs to several hundred here and the direct
    factorial form overflows a float long before that.
    """
    if n <= 0 or p <= 0:
        return 0.0
    if p >= 1:
        return 0.0 if k >= n else float("-inf")
    lg = math.lgamma
    terms = [
        lg(n + 1) - lg(i + 1) - lg(n - i + 1) + i * math.log(p) + (n - i) * math.log1p(-p)
        for i in range(0, k + 1)
    ]
    top = max(terms)
    return (top + math.log(sum(math.exp(t - top) for t in terms))) / math.log(10)


def smallest_detectable(n: int, p: float) -> Optional[float]:
    """The highest zero-bucket rate this scan would still have flagged, given a
    bucket of n rows at base rate p. The honest companion to a clean result:
    'nothing found' means nothing at or below THIS."""
    if n <= 0:
        return None
    for k in range(0, n + 1):
        if log10_binom_tail_le(k, n, p) >= LOG_P_ALARM:
            return (k / n) if k else 0.0
    return None


def load_nights(dirpath: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(dirpath, "graded_results_*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            continue
        night = str(blob.get("date") or os.path.basename(path))
        for r in blob.get("results") or []:
            r = dict(r)
            r["_night"] = night
            rows.append(r)
    return rows


def homered(r: Dict[str, Any]) -> bool:
    v = _f(r.get("actual_hr"))
    return bool(v and v > 0)


def scan(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n_all = len(rows)
    if not n_all:
        return {"rows": 0, "base": 0.0, "suspects": [], "watch": [], "tested": 0}
    base = sum(1 for r in rows if homered(r)) / n_all

    keys = set()
    for r in rows[:500]:
        for k, v in r.items():
            if k.startswith("actual_") or k.startswith("_"):
                continue
            if k in OUTCOME_FIELDS or k in SKIP_FIELDS:
                continue
            if _f(v) is not None:
                keys.add(k)

    suspects: List[Dict[str, Any]] = []
    watch: List[Dict[str, Any]] = []
    tested = 0
    for k in sorted(keys):
        have = [r for r in rows if _f(r.get(k)) is not None]
        if len(have) < MIN_ROWS:
            continue
        zero = [r for r in have if _f(r.get(k)) == 0]
        if len(zero) < MIN_BUCKET:
            continue
        tested += 1
        hits = sum(1 for r in zero if homered(r))
        lp_base = log10_binom_tail_le(hits, len(zero), base)
        lp_leak = log10_binom_tail_le(hits, len(zero), base * STRONG_SIGNAL_FLOOR)
        if lp_base >= LOG_P_ALARM:
            continue
        row = {
            "field": k, "hits": hits, "n": len(zero),
            "rate": hits / len(zero), "log10p": lp_base,
            "log10p_leak": lp_leak, "expected": base * len(zero),
            # LEAK: impossible even against a floor a very strong honest signal
            # could reach. WATCH: far below base, but a real signal could do it.
            "tier": "LEAK" if lp_leak < LOG_P_ALARM else "WATCH",
        }
        (suspects if row["tier"] == "LEAK" else watch).append(row)
    suspects.sort(key=lambda s: s["log10p"])
    watch.sort(key=lambda s: s["log10p"])
    return {"rows": n_all, "base": base, "suspects": suspects,
            "watch": watch, "tested": tested}


def report(dirpath: str = DEFAULT_DIR) -> str:
    rows = load_nights(dirpath)
    L: List[str] = []
    add = L.append
    add("=" * 78)
    add("LEAK SCAN — is a feature column remembering tonight instead of predicting it?")
    add("=" * 78)
    if not rows:
        add(f"No graded files under {dirpath}.")
        return "\n".join(L)

    # Split by whether the pre-game snapshot overlay actually reached the row.
    locked = [r for r in rows if str(r.get("feature_snapshot") or "") == "locked"]
    unlocked = [r for r in rows if str(r.get("feature_snapshot") or "") != "locked"]
    nights = sorted({str(r.get("_night")) for r in rows})
    add(f"nights   {len(nights)}  ({nights[0]} … {nights[-1]})")
    add(f"rows     {len(rows)}   locked={len(locked)}  not-locked={len(unlocked)}")
    add("")
    add("`locked` means apply_locked_features() overlaid the pre-game snapshot onto")
    add("the row. A row without it carries whatever the value was at GRADING time,")
    add("which for a form field is after the game it is supposed to predict.")
    add("")

    for label, subset in (("NOT LOCKED", unlocked), ("LOCKED", locked)):
        if not subset:
            continue
        res = scan(subset)
        add("-" * 78)
        add(f"{label}: {res['rows']} rows, base HR rate {100*res['base']:.1f}%, "
            f"{res['tested']} fields testable")
        if res["tested"] == 0:
            # Not the same statement as "nothing found", and printing them the
            # same way is how a scan that never ran gets quoted as a pass.
            add(f"  NOTHING WAS TESTED. No field clears the {MIN_ROWS}-row / {MIN_BUCKET}-row-zero-bucket")
            add("  floor on this subset, so this scan has said nothing about it either way.")
        elif not res["suspects"]:
            floor = smallest_detectable(res["rows"], res["base"])
            add("  no field's zero bucket is impossible at this sample.")
            if floor is not None:
                add(f"  Read that carefully: with {res['rows']} rows this scan could only have")
                add(f"  caught a zero bucket at or below {100*floor:.1f}%. It is not a clean bill.")
        else:
            add(f"  {'FIELD':<26} {'ZERO BUCKET':>16} {'EXPECTED':>9} {'log10 p':>9}")
            for s in res["suspects"]:
                add(f"  {s['field']:<26} {s['hits']:>4}/{s['n']:<5} {100*s['rate']:>5.2f}% "
                    f"{s['expected']:>8.1f} {s['log10p']:>9.1f}   LEAK")
            if not res["suspects"]:
                add("  none — no zero bucket is impossible")
        if res.get("watch"):
            add("")
            add("  Far below base, but a strong honest signal could produce it. Worth reading,")
            add("  not worth throwing a night away over:")
            for s in res["watch"]:
                add(f"  {s['field']:<26} {s['hits']:>4}/{s['n']:<5} {100*s['rate']:>5.2f}% "
                    f"{s['expected']:>8.1f} {s['log10p']:>9.1f}   watch")
        add("")
    add("=" * 78)
    return "\n".join(L)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Leak scan over the graded archive.")
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help="folder of graded_results_*.json (default: the repo's own)")
    ap.add_argument("--out", default="", help="write the findings as JSON here")
    args = ap.parse_args()
    text = report(args.dir)
    print(text)
    if args.out:
        rows = load_nights(args.dir)
        dirty = [r for r in rows if str(r.get("feature_snapshot") or "") != "locked"]
        locked = [r for r in rows if str(r.get("feature_snapshot") or "") == "locked"]
        payload = {
            "rows": len(rows),
            "locked_rows": len(locked),
            "not_locked": scan(dirty) if dirty else None,
            "locked": scan(locked) if locked else None,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
    # A found leak is a FAILING condition on the locked rows — that is the
    # regression this is scheduled to catch. A leak in the pre-fix archive is
    # history and must not fail the job forever.
    locked_rows = [r for r in load_nights(args.dir)
                   if str(r.get("feature_snapshot") or "") == "locked"]
    if locked_rows and scan(locked_rows)["suspects"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
