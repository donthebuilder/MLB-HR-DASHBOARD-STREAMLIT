#!/usr/bin/env python3
"""
📏 SLATE EVAL — does hr_score work on EVERYBODY, not just on the picks?

2026-08-22. Every measurement of the HR model before this one was conditional
on the bot having picked the hitter. bots/missed_signals.py says so in its own
docstring: "graded rows are the bot's DESIGNATED PICKS, not the full slate, so
every number is conditional on the model having liked the hitter already."

That answers "given we picked him, was the ordering right." It cannot answer
"does the score work on everyone", because the pool it looks at was chosen by
the thing being tested.

THE JOIN THAT MAKES THE OTHER QUESTION ANSWERABLE:

    prediction_log_<run_id>.jsonl   every RATED player on the slate, with the
                                    score he carried, written before first
                                    pitch. Not just the picks.
    graded_results_<date>.json      hr_capture_report.caught_homer_entries +
      -> hr_capture_report          missed_homer_entries: EVERY home run hit
                                    on the slate, whether the model rated the
                                    player or not.

Left side is the model's opinion, right side is a home-run ledger. Neither is
the post-game slate snapshot that invalidated the component research, so
THIS JOIN CANNOT LEAK -- which is the whole reason it is worth having as a
standing tool rather than a one-off.

WHICH RUN'S SLATE. The slate is rebuilt all day and its roster churns: on
2026-08-21 it went 266 -> 269 players across 24 runs, and four of that day's
five "missed" home runs were hit by players who WERE on the morning board and
were dropped by a later rebuild. So "the slate" is ambiguous and the choice
matters. This tool prefers, in order:

    1. the run named by por_log_<date>.jsonl -- the PREDICTION OF RECORD, the
       run standing at first pitch. This is the honest answer.
    2. failing that, the EARLIEST retained run for the date -- closer to a
       pre-game board than the last rebuild of the night, and labelled as a
       fallback in the output so nobody reads it as the locked one.

Never the last run. That is the one that has already seen the evening.

Usage:
    python3 bots/slate_eval.py --dir public/data/current
    python3 bots/slate_eval.py --dir public/data/current --bands 20
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

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
MIN_DATES = 2
MIN_ROWS = 100


def _slate_runs(data_dir: Path) -> dict:
    """date -> {run_id: {player_id: hr_score}}, plus each run's generated_at."""
    runs: dict = defaultdict(dict)
    when: dict = {}
    for p in sorted(data_dir.glob("prediction_log_*.jsonl")):
        try:
            lines = p.read_text(encoding="utf-8").strip().split("\n")
        except Exception:
            continue
        if not lines:
            continue
        try:
            head = json.loads(lines[0])
        except Exception:
            continue
        run_id = head.get("run_id")
        if not run_id:
            continue
        when[run_id] = head.get("generated_at") or ""
        for ln in lines[1:]:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("prediction_type") != "slate_row":
                continue
            pid, date = r.get("player_id"), r.get("prediction_date")
            score = (r.get("scores") or {}).get("hr")
            if pid is None or not date or not isinstance(score, (int, float)):
                continue
            runs[date].setdefault(run_id, {})[pid] = float(score)
    return runs, when


def _locked_run_ids(data_dir: Path) -> dict:
    """date -> the run_id(s) named as prediction_of_record, most common first."""
    out: dict = defaultdict(lambda: defaultdict(int))
    for p in sorted(data_dir.glob("por_log_*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            d, rid = r.get("prediction_date"), r.get("run_id")
            if d and rid:
                out[d][rid] += 1
    return {d: sorted(v, key=lambda k: -v[k]) for d, v in out.items()}


def _homered(data_dir: Path) -> dict:
    """date -> set(player_id who hit at least one HR anywhere on the slate)."""
    out: dict = defaultdict(set)
    caps: dict = {}
    for p in sorted(data_dir.glob("graded_results_*.json")):
        m = DATE_RE.search(p.name)
        if not m:
            continue
        try:
            rep = (json.loads(p.read_text(encoding="utf-8")) or {}).get("hr_capture_report") or {}
        except Exception:
            continue
        if not rep:
            continue
        caps[m.group(1)] = (rep.get("total_hrs_on_slate") or 0,
                            rep.get("caught_hrs_on_sheet") or 0)
        for key in ("caught_homer_entries", "missed_homer_entries"):
            for e in rep.get(key) or []:
                if e.get("player_id") is not None:
                    out[m.group(1)].add(e["player_id"])
    return out, caps


def auc(pos: list, neg: list) -> float:
    """P(a random HR hitter outscored a random non-HR hitter). 0.5 = coin flip."""
    if not pos or not neg:
        return float("nan")
    wins = ties = 0
    for a in pos:
        for b in neg:
            if a > b:
                wins += 1
            elif a == b:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def bootstrap_auc_ci(rows: list, rng: random.Random, iters: int = 2000) -> tuple:
    n = len(rows)
    out = []
    for _ in range(iters):
        s = [rows[rng.randrange(n)] for _ in range(n)]
        p = [x for x, y in s if y]
        q = [x for x, y in s if not y]
        if p and q:
            out.append(auc(p, q))
    if not out:
        return (float("nan"), float("nan"))
    out.sort()
    return (out[int(0.025 * len(out))], out[int(0.975 * len(out))])


def permutation_p(by_date: dict, observed: float, rng: random.Random, iters: int = 2000) -> float:
    """Shuffle went_yard WITHIN each date.

    Stratifying by date matters: it means a "some nights are homer nights"
    effect cannot manufacture the result. (Measured separately and found not to
    exist -- league HR/game disperses at chi2/df 1.38, consistent with Poisson
    -- but the null should not depend on that having been checked.)
    """
    hits = 0
    for _ in range(iters):
        pos, neg = [], []
        for _d, rows in by_date.items():
            ys = [y for _x, y in rows]
            rng.shuffle(ys)
            for (x, _old), y in zip(rows, ys):
                (pos if y else neg).append(x)
        if auc(pos, neg) >= observed:
            hits += 1
    return (hits + 1) / (iters + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="public/data/current")
    ap.add_argument("--bands", type=int, default=10, help="score band width")
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    d = Path(os.path.expanduser(a.dir))

    runs, when = _slate_runs(d)
    locked = _locked_run_ids(d)
    homered, caps = _homered(d)

    dates = sorted(set(runs) & set(homered))
    if not dates:
        print(f"no date has BOTH a prediction_log and a graded hr_capture_report in {d}.")
        print("prediction_log dates:", sorted(runs) or "none")
        print("capture-report dates:", sorted(homered) or "none")
        return 1

    by_date, chosen = {}, {}
    for date in dates:
        rid = next((r for r in locked.get(date, []) if r in runs[date]), None)
        how = "locked (prediction_of_record)"
        if rid is None:
            rid = min(runs[date], key=lambda r: when.get(r) or "")
            how = "EARLIEST retained run — fallback, not the locked one"
        chosen[date] = (rid, how, len(runs[date]))
        by_date[date] = [(s, 1 if pid in homered[date] else 0)
                         for pid, s in runs[date][rid].items()]

    rows = [r for v in by_date.values() for r in v]
    k = sum(y for _x, y in rows)

    print(f"\n📏 SLATE EVAL — hr_score against the WHOLE rated slate, not just the picks")
    print(f"   {len(rows)} rated player-games over {len(dates)} date(s), {k} homered "
          f"({100*k/len(rows):.2f}%)\n")
    for date in dates:
        rid, how, nruns = chosen[date]
        print(f"   {date}: {len(by_date[date]):>4} rated · run {rid[:28]:<28} [{how}]"
              + (f" · {nruns} runs retained" if nruns > 1 else ""))

    if len(dates) < MIN_DATES or len(rows) < MIN_ROWS:
        print(f"\n   NOT ENOUGH YET — need >= {MIN_DATES} dates and >= {MIN_ROWS} rows.")
        print("   prediction_log retention is what gates this; it grows one slate a night.")
        return 0

    print(f"\n   {'hr_score band':<16}{'n':>7}{'HR':>6}{'rate':>9}")
    w = a.bands
    top = int(max(x for x, _y in rows) // w) + 1
    for b in range(top):
        g = [y for x, y in rows if b * w <= x < (b + 1) * w]
        if not g:
            continue
        print(f"   {str(b*w)+'-'+str((b+1)*w-1):<16}{len(g):>7}{sum(g):>6}{100*sum(g)/len(g):>8.2f}%")

    pos = [x for x, y in rows if y]
    neg = [x for x, y in rows if not y]
    A = auc(pos, neg)
    lo, hi = bootstrap_auc_ci(rows, rng, iters=min(a.iters, 2000))
    p = permutation_p(by_date, A, rng, iters=a.iters)
    print(f"\n   AUC {A:.3f}   95% CI [{lo:.3f}, {hi:.3f}]   "
          f"date-stratified permutation p = {p:.4f}")
    print("   AUC is P(a hitter who homered outscored one who didn't). 0.5 is a coin flip.")

    srt = sorted(rows, key=lambda r: -r[0])
    base = 100 * k / len(rows)
    print()
    for kk in (10, 25, 50, 100):
        if kk <= len(srt):
            r = 100 * sum(y for _x, y in srt[:kk]) / kk
            print(f"   top {kk:>3} by hr_score: {r:>6.2f}% homered   ({r/base:.1f}x the {base:.2f}% base rate)")

    tot = sum(t for t, _c in caps.values())
    cau = sum(c for _t, c in caps.values())
    if tot:
        print(f"\n   CAPTURE, all {len(caps)} graded dates: {tot} home runs on the slates, "
              f"{cau} ({100*cau/tot:.1f}%) by a rated player.")
        print("   Treat the shortfall as a CEILING, not a coverage estimate: the capture report")
        print("   compares against the currently-published slate, so a man who was on the board")
        print("   at lock time and dropped by a later rebuild is counted as never rated. See")
        print("   claude/moonshot-full-slate-validation.md §5.")

    print("\n   WHAT THIS DOES NOT SAY: it is not calibration (a 0-100 score is not a")
    print("   probability -- this tests ORDERING only), and it says nothing about whether any")
    print("   individual weight is sized right, which is a different question entirely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
