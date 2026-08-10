#!/usr/bin/env python3
"""
🕵️ MISSED SIGNALS — what predicts a home run AFTER the model has spoken.

2026-08-09, Donovan: "there has to be a way to find missed signals, things that
we miss on HR hitters."

There is, and it is not the scan we have been running. Every audit so far has
asked: "does this field separate homers?" That question has a fatal flaw for
this purpose — season_iso separates homers beautifully, and hr_score already
knows about season_iso. Finding it again tells you nothing you can act on. A
univariate scan mostly rediscovers the model's own inputs and ranks them by how
loudly they shout.

THE RIGHT QUESTION IS CONDITIONAL: among hitters the model scored THE SAME,
does this field still separate the ones who went deep from the ones who didn't?

That is a residual test, and only a residual can be a missed signal. If a field
still sorts inside a score band, the model is not using it — or is using it
wrongly — and that is free information sitting on the table. If it goes flat
inside the band, the model has already priced it, however impressive it looked
in a whole-pool scan.

    WHOLE-POOL (what we did)          WITHIN-BAND (what this does)
    "does ISO predict homers?"        "among hitters the model rated 70-80,
    yes, hugely — and the model        does ISO STILL predict homers?"
    already uses it. Useless.          if yes, the model is underusing it.

HOW IT WORKS
  1. Split every graded pick into hr_score bands (deciles of the score, so
     each band holds hitters the model considered interchangeable).
  2. Inside each band, split the field at its median and compare HR rates.
  3. Pool the within-band differences across bands — this is a stratified
     comparison, the same trick a Mantel-Haenszel test uses, and it removes
     the model's own opinion from the comparison entirely.
  4. Report the lift with a confidence interval and a permutation p-value.

WHY A PERMUTATION TEST. ~170 fields get tested, so about 8 will clear p<0.05
by chance alone. The permutation shuffles the outcome WITHIN each band a
thousand times and asks how often a lift this big appears by luck, and the
report applies a Benjamini-Hochberg correction on top. Without that this tool
would confidently hand back eight fantasies every run.

WHAT IT CANNOT DO, stated plainly:
  · Graded rows are the bot's DESIGNATED PICKS, not the full slate. Every
    number is conditional on the bot having liked the hitter already.
  · Correlated fields will surface together. season_slg and hr_per_pa are two
    views of one thing; if both rank, that is one finding, not two.
  · It finds ASSOCIATION inside a band, not causation, and not a weight. A
    field near the top is a candidate for the blend, and the way to confirm it
    is to re-run the real pipeline with it, not to trust this number.

Usage:
    python3 bots/missed_signals.py --dir ~/Desktop/results
    python3 bots/missed_signals.py --bands 8 --min-n 300 --outcome hr
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

DEFAULT_DIRS = [
    Path.home() / "Desktop" / "results",
    Path.home() / "results",
    Path(__file__).resolve().parent.parent / "public" / "data" / "current",
]

# Fields that are outcomes, or restatements of the score itself. Testing the
# score against itself inside its own band is meaningless, and testing an
# outcome is circular.
BLOCK_PREFIX = ("actual_", "got_", "hrr_")
BLOCK_EXACT = {
    "hr_score", "hr_score_v2", "hr_score_legacy", "hr_score_old", "hr_score_pure",
    "hr_score_delta", "overall_score", "overall_score_legacy", "player_id",
    "game_pk", "jersey_number", "is_final", "rank",
}


def num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def wilson(ok: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z = 1.96
    p = ok / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - m) / d, 100 * (c + m) / d)


def rows_of(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(k)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


def load(dirs: list[Path]) -> list[dict]:
    seen_dates: set[str] = set()
    out: list[dict] = []
    for d in dirs:
        if not d or not d.is_dir():
            continue
        for p in sorted(d.glob("graded_results_*.json")):
            m = DATE_RE.search(p.name)
            if not m or m.group(1) in seen_dates:
                continue
            try:
                rows = rows_of(json.loads(p.read_text()))
            except Exception:
                continue
            seen, keep = set(), []
            for r in rows:
                pid = r.get("player_id")
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                if (num(r.get("actual_ab")) or 0) <= 0:
                    continue                       # never batted: not asked
                if num(r.get("hr_score")) is None:
                    continue                       # no model opinion to hold fixed
                keep.append(r)
            if len(keep) >= 10:
                seen_dates.add(m.group(1))
                out += keep
    return out


OUTCOMES = {
    "hr":  ("homered",            lambda r: 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0),
    "hit": ("got a base hit",     lambda r: 1 if (num(r.get("actual_hits")) or 0) >= 1 else 0),
    "tb":  ("2+ total bases",     lambda r: 1 if (num(r.get("actual_tb")) or 0) >= 2 else 0),
}


def band_edges(vals: list[float], k: int) -> list[float]:
    """k quantile cuts of the model's own score."""
    s = sorted(vals)
    return [s[int(len(s) * i / k)] for i in range(1, k)]


def band_of(v: float, edges: list[float]) -> int:
    i = 0
    for e in edges:
        if v >= e:
            i += 1
    return i


def stratified_lift(pairs_by_band: dict[int, list[tuple[float, int]]]) -> tuple[float, int, int, int, int]:
    """
    Within each band, split at the band's own median and compare HR rates.

    Pooling the halves ACROSS bands rather than pooling the raw rows is the
    whole point: it compares like with like, so the model's own ranking cannot
    leak into the answer.
    """
    hi_ok = hi_n = lo_ok = lo_n = 0
    for _b, pairs in pairs_by_band.items():
        if len(pairs) < 20:
            continue
        pairs = sorted(pairs, key=lambda x: x[0])
        # a field with one repeated value inside a band cannot split it
        if pairs[0][0] == pairs[-1][0]:
            continue
        h = len(pairs) // 2
        lo, hi = pairs[:h], pairs[h:]
        lo_ok += sum(y for _v, y in lo); lo_n += len(lo)
        hi_ok += sum(y for _v, y in hi); hi_n += len(hi)
    if not lo_n or not hi_n:
        return (0.0, 0, 0, 0, 0)
    return (100 * hi_ok / hi_n - 100 * lo_ok / lo_n, hi_ok, hi_n, lo_ok, lo_n)


def permutation_p(pairs_by_band, observed: float, iters: int, rng: random.Random) -> float:
    """
    Shuffle the OUTCOME within each band and see how often chance beats what we
    measured. Within-band shuffling preserves the band structure, so the null
    is "this field carries nothing the model doesn't already have" rather than
    the much weaker "this field carries nothing at all".
    """
    hits = 0
    shuffled = {b: [v for v, _y in p] for b, p in pairs_by_band.items()}
    ys = {b: [y for _v, y in p] for b, p in pairs_by_band.items()}
    for _ in range(iters):
        fake = {}
        for b, vals in shuffled.items():
            yy = ys[b][:]
            rng.shuffle(yy)
            fake[b] = list(zip(vals, yy))
        if abs(stratified_lift(fake)[0]) >= abs(observed):
            hits += 1
    return (hits + 1) / (iters + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", default=None)
    ap.add_argument("--bands", type=int, default=6,
                    help="how many hr_score bands to hold fixed (more = stricter)")
    ap.add_argument("--min-n", type=int, default=400)
    ap.add_argument("--outcome", default="hr", choices=list(OUTCOMES))
    ap.add_argument("--iters", type=int, default=1000)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    dirs = [Path(os.path.expanduser(d)) for d in (a.dir or [])] + DEFAULT_DIRS
    rows = load(dirs)
    if len(rows) < 500:
        print(f"only {len(rows)} graded picks found — need ~500. Checked: "
              + ", ".join(str(d) for d in dirs))
        return 1

    what, outcome = OUTCOMES[a.outcome]
    base = sum(outcome(r) for r in rows)
    scores = [num(r["hr_score"]) for r in rows]
    edges = band_edges(scores, a.bands)
    bands = [band_of(num(r["hr_score"]), edges) for r in rows]

    print(f"\n🕵️  MISSED SIGNALS — {len(rows)} graded picks, {base} {what} "
          f"({100*base/len(rows):.1f}%)")
    print(f"   Holding the model's own opinion fixed in {a.bands} hr_score bands, then asking")
    print(f"   whether each field STILL separates {what} inside a band.\n")
    counts = defaultdict(int)
    for b in bands:
        counts[b] += 1
    print("   band sizes: " + " ".join(f"{counts[b]}" for b in sorted(counts)))
    print(f"   band cuts:  " + " ".join(f"{e:.0f}" for e in edges) + "\n")

    # collect every numeric field, banded
    by_field: dict[str, dict[int, list[tuple[float, int]]]] = defaultdict(lambda: defaultdict(list))
    for r, b in zip(rows, bands):
        y = outcome(r)
        for k, v in r.items():
            if k in BLOCK_EXACT or k.startswith(BLOCK_PREFIX):
                continue
            f = num(v)
            if f is not None:
                by_field[k][b].append((f, y))

    results = []
    for k, banded in by_field.items():
        n = sum(len(v) for v in banded.values())
        if n < a.min_n:
            continue
        if len({v for pairs in banded.values() for v, _ in pairs}) < 3:
            continue
        lift, hok, hn, lok, ln = stratified_lift(banded)
        if not hn or not ln:
            continue
        results.append({"field": k, "n": n, "lift": lift,
                        "hi": 100 * hok / hn, "lo": 100 * lok / ln,
                        "hn": hn, "ln": ln, "banded": banded})

    # Permutation only for the plausible ones — 1,000 shuffles x 170 fields is
    # a lot of arithmetic to spend on fields that measured nothing.
    results.sort(key=lambda r: -abs(r["lift"]))
    for r in results[:40]:
        r["p"] = permutation_p(r["banded"], r["lift"], a.iters, rng)
    for r in results[40:]:
        r["p"] = 1.0

    # Benjamini-Hochberg. ~170 fields means ~8 false positives at p<0.05, so a
    # tool that reported raw p-values would invent a discovery every run.
    tested = sorted([r for r in results if r["p"] < 1.0], key=lambda r: r["p"])
    m = len(tested)
    for i, r in enumerate(tested, start=1):
        r["q"] = min(1.0, r["p"] * m / i)
    for i in range(len(tested) - 2, -1, -1):
        tested[i]["q"] = min(tested[i]["q"], tested[i + 1]["q"])
    for r in results:
        r.setdefault("q", 1.0)

    print(f"   {'field':<34}{'n':>6}{'top half':>10}{'bottom':>9}{'lift':>8}{'p':>8}{'q':>8}")
    shown = sorted(results, key=lambda r: -abs(r["lift"]))[:a.top]
    for r in shown:
        star = " ***" if r["q"] < 0.05 else (" *" if r["p"] < 0.05 else "")
        print(f"   {r['field']:<34}{r['n']:>6}{r['hi']:>9.1f}%{r['lo']:>8.1f}%"
              f"{r['lift']:>+8.1f}{r['p']:>8.3f}{r['q']:>8.3f}{star}")

    real = [r for r in results if r["q"] < 0.05]
    print(f"\n   *** survives multiple-testing correction (q<0.05): {len(real)} of {len(results)} fields")
    if real:
        print("   These are the ones the model is NOT already using — or is using backwards:")
        for r in sorted(real, key=lambda x: -abs(x["lift"])):
            d = "higher is better" if r["lift"] > 0 else "LOWER is better — check the sign in the blend"
            print(f"     · {r['field']}  {r['lift']:+.1f} pts within band  ({d})")
    else:
        print("   Nothing clears the bar. That is a real result and the most likely one:")
        print("   it means the model has already priced everything the archive publishes,")
        print("   and the next gain has to come from a NEW field rather than a re-weight.")

    print("\n   CAVEATS, all load-bearing:")
    print("   · graded rows are the bot's designated picks, so every rate is conditional")
    print("     on the model having liked the hitter already.")
    print("   · correlated fields surface together — slg and hr_per_pa are one finding.")
    print("   · this finds association inside a band, not a weight. Confirm anything here")
    print("     by re-running the real pipeline, not by trusting this number.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
