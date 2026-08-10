#!/usr/bin/env python3
"""
🔬 BACKTEST ALL — everything the model claims, measured against the archive.

2026-08-09. Donovan: "just backtest everything and figure out what we need to
figure out."

The prompt for this was a miss of mine. The score shootout was built to read
the data branch and nothing else, so it saw 14 graded nights and reported "no
measurable difference" between the raw and ISO-adjusted orderings. Thirty-nine
more graded nights were sitting in his results folder the whole time. On the
combined archive the difference is real. A tool that only looks where it was
first pointed will keep producing confident answers from a tenth of the data.

So this is the sweep the shootout should have been: one script, one archive,
five questions, all of them able to answer "no".

  1  SEASON_POWER WEIGHT   he chose to fix the ordering inside hr_score_v2
                           rather than adjust site-side. Where does the weight
                           actually peak?
  2  THE OTHER MARKETS     hit_score, hrr_score, contact_score. Nobody has ever
                           checked whether these three sort at all.
  3  ISO BANDS             the multipliers in lib/scoring_additions.js were fit
                           on partial data. Re-fit on everything.
  4  EVERY CONTEXT STAT    top vs bottom decile HR rate for every numeric field
                           the archive publishes, with an interval on each.
  5  OUT OF SAMPLE         fit on the first 60% of nights, measure on the last
                           40%. Everything above is fit and measured on the
                           same data, which is how you talk yourself into noise.

WHAT THIS CANNOT TELL YOU, stated once and meant throughout: graded files hold
the bot's DESIGNATED PICKS, not the full slate. Every rate here is conditional
on the bot already having liked the hitter. That is the right frame for "does
the board sort" and the wrong one for "does the model find hitters".

Usage:
    python bots/backtest_all.py
    python bots/backtest_all.py --dir ~/Desktop/results
    python bots/backtest_all.py --only power markets
"""
from __future__ import annotations

import argparse
import glob
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


# ── plumbing ─────────────────────────────────────────────────────────────────
def num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def minmax(v: float | None, lo: float, hi: float) -> float:
    """The bot's own normaliser, copied from mlb_dashboard.minmax_norm."""
    if hi <= lo:
        return 0.5
    x = num(v)
    if x is None:
        x = lo
    return (max(lo, min(hi, x)) - lo) / (hi - lo)


def wilson(ok: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z = 1.96
    p = ok / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - m) / d, 100 * (c + m) / d)


def sign_test(a_wins: int, b_wins: int) -> float:
    """Two-sided binomial p on the decisive nights. See the note in
    score_shootout: the pooled intervals treat paired samples as independent
    and lose most of their power doing it."""
    n = a_wins + b_wins
    if n == 0:
        return 1.0
    w = max(a_wins, b_wins)
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(w, n + 1)) / (2 ** n))


def slots(payload: Any) -> list[dict]:
    """Four shapes live in the archive; see score_shootout._slots."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(k)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


def load(dirs: list[Path]) -> list[tuple[str, list[dict]]]:
    by_date: dict[str, list[dict]] = {}
    for d in dirs:
        if not d or not d.is_dir():
            continue
        for p in sorted(d.glob("graded_results_*.json")):
            m = DATE_RE.search(p.name)
            if not m or m.group(1) in by_date:
                continue
            try:
                rows = slots(json.loads(p.read_text()))
            except Exception:
                continue
            keep, seen = [], set()
            for r in rows:
                pid = r.get("player_id")
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                if (num(r.get("actual_ab")) or 0) <= 0:
                    continue          # never batted — void, not a loss
                keep.append(r)
            if len(keep) >= 10:
                by_date[m.group(1)] = keep
    return sorted(by_date.items())


# ── reconstructing season_power ──────────────────────────────────────────────
# From mlb_dashboard.py line ~6473, copied exactly:
#   season_power = 100 * (0.50*minmax(max(season_iso, split_iso), 0.08, 0.38)
#                       + 0.30*minmax(hr_per_pa, 0.015, 0.085)
#                       + 0.20*minmax(season_slg, 0.330, 0.700))
# It has to be reconstructed because the graded rows never stored the component
# itself — only the finished hr_score. Every input IS stored, so this is a
# recomputation rather than a guess, but it is still a reconstruction and the
# report says so wherever it matters.
def season_power(r: dict) -> float | None:
    iso = num(r.get("season_iso"))
    throws = str(r.get("pitcher_throws") or "").upper()
    split = num(r.get("iso_vs_lhp")) if throws.startswith("L") else num(r.get("iso_vs_rhp"))
    best_iso = max([x for x in (iso, split) if x is not None], default=None)
    if best_iso is None:
        return None

    hpp = num(r.get("hr_per_pa"))
    if hpp is None:
        hr, pa = num(r.get("season_hr")), num(r.get("season_pa"))
        hpp = hr / pa if hr is not None and pa else None

    # SLG = AVG + ISO. That is the DEFINITION of isolated power, not an
    # estimate — verified against 3,511 archive rows carrying all three, where
    # the maximum disagreement was exactly 0.000000. Worth reconstructing
    # rather than skipping: 26 of 58 graded nights publish season_avg and
    # season_iso but no season_slg, and without this the sweep silently ran on
    # 32 nights while the header said 58.
    slg = num(r.get("season_slg"))
    if slg is None:
        avg = num(r.get("season_avg"))
        if avg is not None:
            slg = avg + best_iso

    # RENORMALISE OVER WHAT IS PRESENT rather than treating an absent component
    # as zero. hr_per_pa needs season_pa, which those same 26 nights never
    # published and which cannot be derived from anything else on the row. The
    # old code returned None and threw the night away; scoring 0.0 for it would
    # have been worse, quietly ranking every hitter on a legacy night as
    # powerless. Weights are rescaled across the terms that exist, so a night
    # with two of three components produces a comparable 0-100 number.
    parts = [(0.50, minmax(best_iso, 0.08, 0.38))]
    if hpp is not None:
        parts.append((0.30, minmax(hpp, 0.015, 0.085)))
    if slg is not None:
        parts.append((0.20, minmax(slg, 0.330, 0.700)))
    if len(parts) < 2:
        return None                    # ISO alone is not season_power
    w = sum(p[0] for p in parts)
    return 100 * sum(p[0] * p[1] for p in parts) / w


# Re-weighting the blend without re-running the pipeline. hr_raw = Σ wᵢxᵢ, so
# moving Δ into season_power and taking it proportionally from everything else:
#
#   new = (raw − w·sp)·(1 − w − Δ)/(1 − w) + (w + Δ)·sp
#
# APPROXIMATION, NAMED: this uses the published hr_score in place of hr_raw.
# hr_score is hr_raw after clipping and a weak-spot bonus, so the two are close
# but not identical, and the substitution slightly compresses the simulated
# effect. It is good enough to locate a peak and NOT good enough to publish the
# peak's exact height — which is why the recommendation below is a direction
# and a re-run instruction, not a number to paste into MODEL_WEIGHTS.
W_SP = 0.12


def reweighted(hr_score: float, sp: float, new_w: float) -> float:
    rest = (hr_score - W_SP * sp) * (1 - new_w) / (1 - W_SP)
    return rest + new_w * sp


# ── the shared measurement ───────────────────────────────────────────────────
def top_n_rate(nights, key, outcome, n=20):
    """Pooled hit rate in each night's top N under an ordering, plus the
    per-night vector for the paired test."""
    ok = tot = 0
    per = []
    for _d, rows in nights:
        usable = [r for r in rows if key(r) is not None]
        if len(usable) < min(n, 10):
            per.append(None)
            continue
        cut = sorted(usable, key=key, reverse=True)[:n]
        hits = sum(outcome(r) for r in cut)
        ok += hits
        tot += len(cut)
        per.append(hits / len(cut))
    return ok, tot, per


def paired(per_a, per_b):
    a = b = 0
    for x, y in zip(per_a, per_b):
        if x is None or y is None:
            continue
        if x > y:
            a += 1
        elif y > x:
            b += 1
    return a, b


def line(label, ok, tot, extra=""):
    lo, hi = wilson(ok, tot)
    print(f"   {label:<26}{100*ok/tot:5.1f}%  ({ok}/{tot})   95% CI {lo:.1f}–{hi:.1f} {extra}")


# ── 1. the season_power weight ───────────────────────────────────────────────
def q_power(nights, top=20):
    print("═" * 78)
    print("1. SEASON_POWER WEIGHT — where does the ordering actually peak?")
    print("═" * 78)
    print("Donovan chose to carry the ISO signal inside hr_score_v2 rather than")
    print("adjust it on the site. This sweeps the weight and reports the shape.\n")

    have = [(d, [r for r in rows
                 if num(r.get("hr_score")) is not None and season_power(r) is not None])
            for d, rows in nights]
    have = [(d, r) for d, r in have if len(r) >= 20]
    print(f"   {len(have)} of {len(nights)} nights carry every input season_power needs\n")
    if len(have) < 10:
        print("   not enough — skipping\n")
        return None

    got_hr = lambda r: 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0
    results = []
    for w in [0.00, 0.06, 0.12, 0.18, 0.24, 0.30, 0.40, 0.50, 0.70, 1.00]:
        key = lambda r, w=w: reweighted(num(r["hr_score"]), season_power(r), w)
        ok, tot, per = top_n_rate(have, key, got_hr, top)
        results.append((w, ok, tot, per))

    base_per = next(p for w, _o, _t, p in results if w == W_SP)
    print(f"   ordering the top {top} by hr_score re-weighted, current weight is {W_SP}\n")
    for w, ok, tot, per in results:
        mark = "  ← today" if w == W_SP else ""
        a, b = paired(per, base_per)
        sig = "" if w == W_SP else f"   vs today: {a}W-{b}L, p={sign_test(a, b):.3f}"
        line(f"season_power {w:.2f}", ok, tot, mark + sig)

    best = max(results, key=lambda x: x[1] / x[2])
    print(f"\n   peak at weight {best[0]:.2f}")
    if best[0] == W_SP:
        print("   → the current weight is already the best of those tested. Leave it.")
    else:
        a, b = paired(best[3], base_per)
        p = sign_test(a, b)
        print(f"   → {best[0]:.2f} beat today's {W_SP} on {a} nights and lost on {b}, p={p:.3f}")
        if p < 0.05:
            print("   → real. Worth changing in MODEL_WEIGHTS.")
        else:
            print("   → NOT significant. The peak is where the noise happens to sit;")
            print("     a sweep always produces a winner. Do not move the weight on this.")
    print("\n   CAVEAT: simulated by re-weighting the published hr_score, which is")
    print("   hr_raw after clipping and the weak-spot bonus. Direction is reliable,")
    print("   the exact height is not. Confirm any change by re-running the real")
    print("   pipeline over the archive before it ships.\n")
    return results


# ── 2. the other three markets ───────────────────────────────────────────────
def q_markets(nights, top=20):
    print("═" * 78)
    print("2. THE OTHER THREE MARKETS — do hit, hrr and contact sort at all?")
    print("═" * 78)
    print("The shootout only ever tested HR. These three have shipped on the site")
    print("since the beginning without anyone checking them once.\n")

    tests = [
        ("hit_score",     "got a base hit",   lambda r: 1 if (num(r.get("actual_hits")) or 0) >= 1 else 0),
        ("hrr_score",     "scored or drove in", lambda r: 1 if (num(r.get("hrr_total")) or 0) >= 1 else 0),
        ("contact_score", "2+ total bases",   lambda r: 1 if (num(r.get("actual_tb")) or 0) >= 2 else 0),
        ("overall_score", "homered",          lambda r: 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0),
    ]
    out = {}
    for field, what, outcome in tests:
        usable = [(d, [r for r in rows if num(r.get(field)) is not None]) for d, rows in nights]
        usable = [(d, r) for d, r in usable if len(r) >= 20]
        if len(usable) < 8:
            print(f"   {field}: only {len(usable)} usable nights — skipping\n")
            continue
        key = lambda r, f=field: num(r[f])
        ok, tot, per = top_n_rate(usable, key, outcome, top)

        rnd = random.Random(7)
        rok = rtot = 0
        rper = []
        for _d, rows in usable:
            sh = rows[:]
            rnd.shuffle(sh)
            cut = sh[:top]
            h = sum(outcome(r) for r in cut)
            rok += h
            rtot += len(cut)
            rper.append(h / len(cut))
        base = sum(outcome(r) for _d, rows in usable for r in rows)
        baseN = sum(len(rows) for _d, rows in usable)

        print(f"   ── {field} → {what} ({len(usable)} nights) ──")
        line(f"top {top} by {field}", ok, tot)
        line(f"random {top}", rok, rtot)
        line("all designated picks", base, baseN)
        a, b = paired(per, rper)
        p = sign_test(a, b)
        lift = 100 * ok / tot - 100 * rok / rtot
        # A verdict has to be able to say no. The first version graded on
        # `lift > 0`, which called a +0.4pt / p=1.000 result "leans but
        # unproven" — flattering language for a score that finished level with
        # a shuffle on 16 of 32 nights. If it cannot beat random, say so.
        if p < 0.05 and lift > 0:
            verdict = "SORTS — real"
        elif lift >= 1.5 and p < 0.25:
            verdict = "leans, needs more nights"
        else:
            verdict = "DOES NOT SORT — level with a shuffle"
        print(f"   {'lift vs random':<26}{lift:+5.1f} pts   beat the shuffle on "
              f"{a} of {a+b} decisive nights, p={p:.3f}   → {verdict}\n")
        out[field] = (lift, p)
    return out


# ── 3. the ISO bands ─────────────────────────────────────────────────────────
def q_iso(nights):
    print("═" * 78)
    print("3. ISO BANDS — re-fit on everything")
    print("═" * 78)
    print("The multipliers in lib/scoring_additions.js came from a partial")
    print("archive. Same band floors, recomputed.\n")
    floors = [0.000, 0.130, 0.170, 0.230]
    buckets = defaultdict(lambda: [0, 0])
    for _d, rows in nights:
        for r in rows:
            iso = num(r.get("season_iso"))
            if iso is None:
                continue
            b = max(f for f in floors if iso >= f)
            buckets[b][1] += 1
            buckets[b][0] += 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0
    tot_ok = sum(v[0] for v in buckets.values())
    tot_n = sum(v[1] for v in buckets.values())
    if not tot_n:
        print("   no ISO data\n")
        return
    base = tot_ok / tot_n
    OLD = {0.000: 0.56, 0.130: 0.78, 0.170: 1.06, 0.230: 1.52}
    print(f"   overall HR rate across {tot_n} graded picks: {100*base:.1f}%\n")
    print(f"   {'band':<14}{'n':>6}{'HR rate':>10}{'95% CI':>16}{'new mult':>11}{'shipped':>10}")
    for f in floors:
        ok, n = buckets[f]
        if not n:
            continue
        lo, hi = wilson(ok, n)
        mult = (ok / n) / base
        print(f"   ISO ≥ {f:.3f}{n:>8}{100*ok/n:>9.1f}%{f'{lo:.1f}–{hi:.1f}':>16}"
              f"{mult:>11.2f}{OLD[f]:>10.2f}")
    print("\n   These are the numbers the site's old adjustment was built on. If the")
    print("   season_power route wins in section 1, they become documentation")
    print("   rather than code — the bot carries the signal and the site does not.\n")


# ── 4. every context stat ────────────────────────────────────────────────────
def q_signals(nights, min_n=400):
    print("═" * 78)
    print("4. EVERY CONTEXT STAT — top vs bottom decile HR rate")
    print("═" * 78)
    print("Every numeric field in the archive, ranked by how far apart its best")
    print("and worst deciles land. An interval on each, because a 10-point gap")
    print("on 200 picks is a coin landing heads twice.\n")

    vals = defaultdict(list)
    for _d, rows in nights:
        for r in rows:
            hr = 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0
            for k, v in r.items():
                if k.startswith("actual_") or k.startswith("got_") or k.startswith("hrr_"):
                    continue                      # outcomes, not inputs
                if isinstance(v, bool):
                    vals[k].append((1.0 if v else 0.0, hr))
                elif isinstance(v, (int, float)):
                    f = num(v)
                    if f is not None:
                        vals[k].append((f, hr))

    rows_out = []
    for k, pairs in vals.items():
        if len(pairs) < min_n:
            continue
        distinct = len({p[0] for p in pairs})
        if distinct < 3:
            continue
        pairs.sort(key=lambda x: x[0])
        d = max(1, len(pairs) // 10)
        lowc, highc = pairs[:d], pairs[-d:]
        lo_ok, hi_ok = sum(p[1] for p in lowc), sum(p[1] for p in highc)
        l1, l2 = wilson(lo_ok, len(lowc))
        h1, h2 = wilson(hi_ok, len(highc))
        lift = 100 * hi_ok / len(highc) - 100 * lo_ok / len(lowc)
        sep = h1 > l2 or l1 > h2          # intervals actually clear each other
        rows_out.append((abs(lift), lift, k, len(pairs), 100 * hi_ok / len(highc),
                         100 * lo_ok / len(lowc), sep))
    rows_out.sort(reverse=True)

    print(f"   {'field':<34}{'n':>7}{'top dec':>9}{'bot dec':>9}{'lift':>8}   sep?")
    for _a, lift, k, n, hi, lo, sep in rows_out[:26]:
        print(f"   {k:<34}{n:>7}{hi:>8.1f}%{lo:>8.1f}%{lift:>+8.1f}   {'yes' if sep else '·'}")
    strong = [r for r in rows_out if r[6]]
    print(f"\n   {len(strong)} of {len(rows_out)} fields separate with non-overlapping intervals.")
    print("   'sep? ·' means the gap is inside the noise — it is not evidence of")
    print("   anything, however large the lift column looks.")
    print("\n   READ THIS BEFORE ACTING: these are UNIVARIATE and heavily correlated.")
    print("   season_iso, season_slg and hr_per_pa are three views of one thing and")
    print("   will all rank high together. A field near the top is a candidate for")
    print("   the blend, not a proven addition to it. And every one is measured on")
    print("   hitters the bot already designated, so the range is narrow.\n")
    return rows_out


# ── 5. out of sample ─────────────────────────────────────────────────────────
def q_holdout(nights, top=20):
    print("═" * 78)
    print("5. OUT OF SAMPLE — does any of it survive on nights it never saw?")
    print("═" * 78)
    print("Everything above is fit and measured on the same nights, which is how")
    print("a person talks themselves into noise. Fit on the first 60% of the")
    print("archive, measure on the last 40%.\n")
    cut = int(len(nights) * 0.6)
    train, test = nights[:cut], nights[cut:]
    if len(test) < 6:
        print("   too few nights to hold any out\n")
        return
    print(f"   fit:  {len(train)} nights  {train[0][0]} .. {train[-1][0]}")
    print(f"   test: {len(test)} nights  {test[0][0]} .. {test[-1][0]}\n")

    got_hr = lambda r: 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0
    # the ISO bands, fit on train only
    floors = [0.000, 0.130, 0.170, 0.230]
    b = defaultdict(lambda: [0, 0])
    for _d, rows in train:
        for r in rows:
            iso = num(r.get("season_iso"))
            if iso is None:
                continue
            f = max(x for x in floors if iso >= x)
            b[f][1] += 1
            b[f][0] += got_hr(r)
    tk = sum(v[0] for v in b.values()) / max(1, sum(v[1] for v in b.values()))
    mult = {f: ((b[f][0] / b[f][1]) / tk if b[f][1] and tk else 1.0) for f in floors}
    print("   bands fit on the training half: "
          + ", ".join(f"≥{f:.3f}→{mult[f]:.2f}" for f in floors) + "\n")

    def adj(r):
        s, iso = num(r.get("hr_score")), num(r.get("season_iso"))
        if s is None:
            return None
        return s * (mult[max(x for x in floors if iso >= x)] if iso is not None else 1.0)

    raw = lambda r: num(r.get("hr_score"))
    isok = lambda r: num(r.get("season_iso"))
    for label, key in (("raw hr_score", raw), ("ISO-adjusted (train-fit)", adj), ("season ISO alone", isok)):
        ok, tot, per = top_n_rate(test, key, got_hr, top)
        if tot:
            line(label, ok, tot)
    rok, rtot, rper = top_n_rate(test, raw, got_hr, top)
    aok, atot, aper = top_n_rate(test, adj, got_hr, top)
    a, bb = paired(aper, rper)
    print(f"\n   on nights the bands never saw: adjusted won {a}, raw won {bb}, "
          f"p={sign_test(a, bb):.3f}")
    print("   This is the only number in the whole report that was not fit on")
    print("   itself. If it disagrees with section 1, believe this one.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", default=None)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--only", nargs="*", default=None,
                    help="power markets iso signals holdout")
    a = ap.parse_args()

    dirs = [Path(os.path.expanduser(d)) for d in a.dir] if a.dir else []
    dirs += DEFAULT_DIRS
    nights = load(dirs)
    if not nights:
        print("no graded archive found in: " + ", ".join(str(d) for d in dirs))
        return 1
    picks = sum(len(r) for _d, r in nights)
    hrs = sum(1 for _d, rows in nights for r in rows if (num(r.get("actual_hr")) or 0) >= 1)
    print(f"\n🔬 BACKTEST — {len(nights)} graded nights, {nights[0][0]} .. {nights[-1][0]}")
    print(f"   {picks} picks that batted, {hrs} homers ({100*hrs/picks:.1f}% base rate)")
    print("   Every rate below is conditional on the bot having designated the")
    print("   hitter. This measures the board's sorting, not the model's finding.\n")

    want = set(a.only or ["power", "markets", "iso", "signals", "holdout"])
    if "power" in want:
        q_power(nights, a.top)
    if "markets" in want:
        q_markets(nights, a.top)
    if "iso" in want:
        q_iso(nights)
    if "signals" in want:
        q_signals(nights)
    if "holdout" in want:
        q_holdout(nights, a.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
