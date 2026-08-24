#!/usr/bin/env python3
"""
DUE SCORE — kill or rebuild?
============================

The roadmap question: does "he is due" earn its place, or is it the gambler's
fallacy wearing a lab coat?

TWO THINGS SHARE THE NAME, and separating them is most of the work.

  hr_due_score   PURE dueness. Published on every slate row and shown on the
                 site. max(0, expected_HRs_in_his_recent_PA_window - HRs he
                 actually hit) * 25, expected at his own season rate. This is
                 exactly the "he's owed one" quantity.

  due_score()    The MODEL function feeding pick ranking, pools and pairs.
                 Only 0.15 of it is that gap and 0.07 is "he has not homered
                 lately". The other 0.78 is recent CONTACT QUALITY -- 350ft
                 rate, 375ft rate, ideal-contact rate, barrel rate, hard-hit
                 rate, season ISO. It is a hotness score wearing a dueness
                 name, and it would test as predictive on the strength of its
                 contact-quality majority while telling us nothing about
                 dueness at all.

So the question is whether the DUENESS PART carries its own weight.

THIS STUDY CANNOT ANSWER IT YET, AND SAYS SO
--------------------------------------------
The first run of it produced a beautiful, damning table: hitters with no home
run in their last five games homered 0 times in 217 rows. That is not a cold
streak, that is a tautology. `last5_hr` counts his last five GAMES, and the
value in the graded file was refreshed AFTER the game, so tonight is one of the
five -- a man who homered tonight has last5_hr >= 1 by construction.

hr_due_score is built from that same field (recent_hr falls back to last5_hr),
so it inherits the contamination. Every night carrying hr_due_score is a night
carrying the leak, which means the entire overlap is unusable.

The fix -- apply_locked_features() overlaying the pre-game snapshot -- reaches
rows stamped feature_snapshot="locked", first seen 2026-08-22. Those are the
only nights this study will ever quote from, it refuses to mix them with the
rest, and it prints how many it has. See bots/leak_scan.py, which is the
general form of the check and is run first here.

WHAT THE DENOMINATOR IS
-----------------------
Graded rows are the bot's OWN designated picks, not every bat on the slate. The
base rate is around 15-20%, not the ~5% a random hitter runs. Nothing here
transfers to the full slate.
"""
from __future__ import annotations

import glob
import json
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), "public", "data", "current")

try:
    from leak_scan import scan as leak_scan, load_nights
except ImportError:  # running as a module from the repo root
    from bots.leak_scan import scan as leak_scan, load_nights  # type: ignore

# Below this many clean rows the study states its finding as "not yet", never
# as a verdict. 400 is roughly five full slates; at a ~16% base that is ~64
# home runs, which is the least that can separate a real effect from noise in
# quartiles.
MIN_CLEAN_ROWS = 400


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def wilson(k: int, n: int) -> Tuple[float, float]:
    """95% Wilson interval -- never the normal approximation, which on buckets
    this small produces intervals running past 100%."""
    if n == 0:
        return (0.0, 0.0)
    z = 1.959963985
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    lo, hi = centre - half, centre + half
    # At k=0 the two terms are algebraically identical and the bound is exactly
    # zero; in floating point they differ by ~1e-18, which prints as 0.0 but
    # fails an equality test and, worse, would read as "the true rate is at
    # least something" on the one bucket where the whole point is that it is
    # not. Same at the top end. Pinned rather than rounded away.
    if k == 0:
        lo = 0.0
    if k == n:
        hi = 1.0
    return (max(0.0, lo), min(1.0, hi))


def homered(r: Dict[str, Any]) -> bool:
    return _f(r.get("actual_hr")) > 0


def clean_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only rows whose features were overlaid from the pre-game snapshot."""
    return [r for r in rows if str(r.get("feature_snapshot") or "") == "locked"]


def rate_line(label: str, rows: List[Dict[str, Any]], width: int = 28) -> str:
    n = len(rows)
    if n == 0:
        return f"{label:<{width}} {'—':>5}"
    k = sum(1 for r in rows if homered(r))
    lo, hi = wilson(k, n)
    return f"{label:<{width}} {n:>5} {100*k/n:>7.1f}%   {100*lo:>5.1f}–{100*hi:<5.1f}"


def buckets(rows: List[Dict[str, Any]], key: Callable[[Dict[str, Any]], float],
            n: int) -> List[Tuple[str, List[Dict[str, Any]]]]:
    ranked = sorted(rows, key=key)
    size = max(1, len(ranked) // n)
    out = []
    for i in range(n):
        lo = i * size
        hi = len(ranked) if i == n - 1 else (i + 1) * size
        chunk = ranked[lo:hi]
        if chunk:
            out.append((f"{key(chunk[0]):.1f}–{key(chunk[-1]):.1f}", chunk))
    return out


def report(dirpath: str = DEFAULT_DIR) -> str:
    rows = load_nights(dirpath)
    L: List[str] = []
    add = L.append
    add("=" * 78)
    add("DUE SCORE — DOES DUENESS EARN ITS PLACE?")
    add("=" * 78)
    if not rows:
        add(f"No graded files under {dirpath}. Nothing to say.")
        return "\n".join(L)

    dirty = [r for r in rows if str(r.get("feature_snapshot") or "") != "locked"]
    clean = clean_rows(rows)
    nights_clean = sorted({str(r.get("_night")) for r in clean})
    nights_all = sorted({str(r.get("_night")) for r in rows})

    add(f"archive        {len(rows)} rows over {len(nights_all)} nights "
        f"({nights_all[0]} … {nights_all[-1]})")
    add(f"usable         {len(clean)} rows over {len(nights_clean)} nights "
        + (f"({nights_clean[0]} … {nights_clean[-1]})" if nights_clean else "(none)"))
    add("")

    # ── THE GATE ────────────────────────────────────────────────────────────
    res = leak_scan(dirty) if dirty else {"suspects": []}
    leaked = {s["field"] for s in res.get("suspects", [])}
    if leaked:
        add("WHY MOST OF THE ARCHIVE IS UNUSABLE")
        add("-" * 78)
        for s in res["suspects"]:
            add(f"  {s['field']:<22} zero bucket homered {s['hits']}/{s['n']} "
                f"({100*s['rate']:.2f}%) against {s['expected']:.0f} expected — "
                f"log10 p {s['log10p']:.1f}")
        add("")
        add("  These are post-game values standing in for pre-game ones. hr_due_score is")
        add("  built from last5_hr, so every night carrying the score also carries the")
        add("  leak. Studying dueness on them would measure the leak and call it baseball.")
        add("")

    if len(clean) < MIN_CLEAN_ROWS:
        add("VERDICT: NOT YET — and this is the honest answer, not a hedge.")
        add("-" * 78)
        add(f"  {len(clean)} clean rows against the {MIN_CLEAN_ROWS} this study needs before it will")
        add("  quote a number. At roughly a 16% base that is about 64 home runs, which is")
        add("  the least that can separate a real quartile effect from noise.")
        add("")
        add("  Re-run it after about "
            f"{max(1, math.ceil((MIN_CLEAN_ROWS - len(clean)) / 85))} more graded nights. Nothing else is needed:")
        add("  the snapshot overlay is already live, so every night from here arrives clean.")
        add("")
        add("  WHAT IS ALREADY KNOWN WITHOUT THE DATA, from reading due_score() itself:")
        add("  0.78 of its weight is recent contact quality — 350ft rate, 375ft rate,")
        add("  ideal-contact rate, barrel rate, hard-hit rate, season ISO. Whatever the")
        add("  dueness terms turn out to be worth, a score that is three-quarters hotness")
        add("  is misnamed, and the two halves should be measured apart before either is")
        add("  defended. That much needs no archive at all.")
        add("=" * 78)
        return "\n".join(L)

    # ── THE STUDY, once there is enough clean data to run it ────────────────
    add(f"{'BOARD':<28} {'N':>5} {'HR RATE':>8}   {'95% CI':>12}")
    add("-" * 78)
    add(rate_line("every graded pick", clean))
    add("")
    add("1. hr_due_score — the published 'he is owed one' number")
    add("-" * 78)
    add(rate_line("on pace or ahead (0)", [r for r in clean if _f(r.get("hr_due_score")) <= 0]))
    pos = [r for r in clean if _f(r.get("hr_due_score")) > 0]
    add(rate_line("overdue (> 0)", pos))
    if pos:
        add("")
        for label, chunk in buckets(pos, lambda r: _f(r.get("hr_due_score")), 4):
            add(rate_line(f"  overdue {label}", chunk))
    add("")
    add("2. Within hr_score terciles — does dueness split what the model already knows?")
    add("-" * 78)
    for tlabel, tchunk in buckets(clean, lambda r: _f(r.get("hr_score")), 3):
        add(rate_line(f"hr_score {tlabel}", tchunk))
        add(rate_line("    …on pace", [r for r in tchunk if _f(r.get("hr_due_score")) <= 0]))
        add(rate_line("    …overdue", [r for r in tchunk if _f(r.get("hr_due_score")) > 0]))
    add("")
    add("=" * 78)
    return "\n".join(L)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Does dueness earn its place?")
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help="folder of graded_results_*.json (default: the repo's own)")
    args = ap.parse_args()
    print(report(args.dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
