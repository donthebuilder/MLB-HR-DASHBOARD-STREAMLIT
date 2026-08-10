#!/usr/bin/env python3
"""
🥊 SCORE SHOOTOUT — which ordering actually put the homers on top?

2026-08-09. Donovan, after the site moved off the ISO-adjusted ranking:
"ranked in order, I thought the raw score was more correct."

That is an empirical claim and neither of us should be settling it by opinion.
This settles it against the graded archive.

WHY THE ORIGINAL AUDIT MIGHT HAVE MISSED WHAT HE'S SEEING
---------------------------------------------------------
The 2026-08-04 finding — ISO bands separating home-run rate 8.2% to 22.2%
while score quartiles moved only ~4.7 points — measured SEPARATION ACROSS THE
WHOLE POOL. That is the right test for "does ISO carry signal". It is NOT the
test for "does multiplying by ISO improve the TOP OF THE BOARD", and the top is
the only part anybody reads.

Those two can disagree, and here is the mechanism:

  · the adjustment DRAGS DOWN high-score, thin-ISO bats — some of whom are
    high-score for good reasons the ISO band knows nothing about (a favourable
    park, a leaking arm, a pitch-type exploit)
  · and it PUSHES UP low-score, big-ISO bats, who are cheap to promote because
    ISO is a season-long trait and says nothing about tonight

A rule that improves average separation over 3,973 slots can still make the top
20 worse. So this measures where it matters: at the top.

WHAT IT COMPARES, on identical rows
  raw       the bot's published hr_score, which the site now ranks on
  adjusted  hr_score × the measured HR rate of the hitter's ISO band — the
            site's old ranking, reproduced from lib/scoring_additions.js
  iso       season ISO alone, as the control the audit implied was strongest
  random    shuffled, as the floor — if a real ordering can't beat noise on
            this sample, the sample is too small to conclude anything

HOW IT SCORES THEM
  · HR rate in the top 10 / 20 / 50 of each night's board, averaged over nights
  · plus the pooled rate with a 95% Wilson interval, because two orderings
    three points apart on 400 picks is not a difference
  · and a HEAD-TO-HEAD count: on how many individual nights did each ordering
    put more homers in the top 20 than the other

An honest run can conclude "no measurable difference", and on this sample size
that is the most likely honest answer. It says so when it happens.

CAVEAT STATED UP FRONT, because it bounds everything below: graded files
contain the bot's DESIGNATED PICKS, not the full slate. So this measures how
well each ordering sorts the picks the bot already made — not how well it would
have sorted all 260 hitters. It is the right question for the board's top,
which is drawn from those picks, and the wrong question for the whole pool.

Usage:
    python bots/score_shootout.py
    python bots/score_shootout.py --top 20 --days 38
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
PUBLIC_CURRENT = REPO_ROOT / "public" / "data" / "current"
DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

# The site's old adjustment, reproduced exactly so the comparison is fair.
# Band floors and their measured relative HR rate, from the 2026-08-04 audit
# (lib/scoring_additions.js). Interpolated between floors, as the site did.
ISO_BANDS = [(0.000, 0.56), (0.130, 0.78), (0.170, 1.06), (0.230, 1.52)]


def num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def iso_mult(iso: float | None) -> float:
    """The site's old multiplier: linear between the measured band floors."""
    if iso is None:
        return 1.0
    if iso <= ISO_BANDS[0][0]:
        return ISO_BANDS[0][1]
    for (lo, ml), (hi, mh) in zip(ISO_BANDS, ISO_BANDS[1:]):
        if iso <= hi:
            span = hi - lo
            return ml if span <= 0 else ml + (mh - ml) * ((iso - lo) / span)
    return ISO_BANDS[-1][1]


def wilson(ok: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 100.0)
    z = 1.96
    p = ok / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den
    return (max(0.0, (mid - half) * 100), min(100.0, (mid + half) * 100))


RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


def fetch_archive(days: int) -> list[tuple[str, dict]]:
    """
    Pull the graded nights straight from the data branch.

    This exists because the checkout you run this from is the SCRIPTS branch —
    the graded files live on `data`, so a local run finds an empty folder and
    concludes there is no archive. Rather than make someone clone a second
    branch and copy files around to answer one question, the tool fetches what
    it needs. Walks backwards from today; a missing date is a night that wasn't
    graded, not an error.
    """
    out = []
    today = dt.date.today()
    misses = 0
    for i in range(days * 2):                 # look back further than we need,
        if len(out) >= days:                  # since off-days leave gaps
            break
        d = (today - dt.timedelta(days=i)).isoformat()
        url = f"{RAW}/graded_results_{d}.json"
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                out.append((d, json.loads(r.read().decode())))
            print(f"  · {d}", flush=True)
        except Exception:
            misses += 1
            if misses > 12 and not out:
                print("  ! nothing found on the data branch — is it reachable?")
                break
    out.reverse()
    return out


def load_nights(days: int | None) -> list[tuple[str, list[dict]]]:
    files = []
    for p in PUBLIC_CURRENT.glob("graded_results_*.json"):
        m = DATE_RE.search(p.name)
        if m:
            files.append((m.group(1), p))
    files.sort()
    if days:
        files = files[-days:]

    raw_nights = []
    for date, p in files:
        try:
            raw_nights.append((date, json.loads(p.read_text())))
        except Exception:
            continue

    # Nothing on disk? Go and get it. See fetch_archive().
    if not raw_nights:
        print(f"no graded files in {PUBLIC_CURRENT} — fetching from the data branch:")
        raw_nights = fetch_archive(days or 45)

    out = []
    for date, payload in raw_nights:
        rows, seen = [], set()
        for r in payload.get("graded_slots") or payload.get("results") or []:
            pid = r.get("player_id")
            if pid is None or pid in seen:
                continue                       # one row per player per night
            seen.add(pid)
            if (num(r.get("actual_ab")) or 0) <= 0:
                continue                       # void: he never batted
            hr_score = num(r.get("hr_score"))
            if hr_score is None:
                continue
            rows.append({
                "raw": hr_score,
                "iso": num(r.get("season_iso")),
                "hr": 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0,
            })
        if len(rows) >= 10:                    # a night too thin to rank is skipped
            out.append((date, rows))
    return out


RANKERS = {
    "raw":      lambda r: r["raw"],
    "adjusted": lambda r: r["raw"] * iso_mult(r["iso"]),
    "iso":      lambda r: (r["iso"] if r["iso"] is not None else -1),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--top", type=int, nargs="*", default=[10, 20, 50])
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)

    nights = load_nights(a.days)
    if not nights:
        print("no graded archive found — nothing to compare")
        return 0

    names = list(RANKERS) + ["random"]
    pooled = {k: {n: [0, 0] for n in a.top} for k in names}   # name -> topN -> [hr, n]
    per_night = {k: {n: [] for n in a.top} for k in names}

    for _date, rows in nights:
        for name in names:
            if name == "random":
                order = rows[:]
                random.shuffle(order)
            else:
                order = sorted(rows, key=RANKERS[name], reverse=True)
            for n in a.top:
                cut = order[:n]
                if len(cut) < min(n, 5):
                    continue
                hits = sum(r["hr"] for r in cut)
                pooled[name][n][0] += hits
                pooled[name][n][1] += len(cut)
                per_night[name][n].append(hits / len(cut))

    total_rows = sum(len(r) for _, r in nights)
    total_hr = sum(sum(x["hr"] for x in r) for _, r in nights)
    base = 100 * total_hr / total_rows if total_rows else 0
    print(f"🥊 SCORE SHOOTOUT — {len(nights)} graded nights, {total_rows} picks that batted, "
          f"{total_hr} homers ({base:.1f}% base rate)\n")

    for n in a.top:
        print(f"── top {n} of each night ──")
        rows_out = []
        for name in names:
            ok, tot = pooled[name][n]
            if not tot:
                continue
            lo, hi = wilson(ok, tot)
            rows_out.append((100 * ok / tot, name, ok, tot, lo, hi))
        rows_out.sort(reverse=True)
        for pct, name, ok, tot, lo, hi in rows_out:
            print(f"   {name:<9} {pct:5.1f}%  ({ok}/{tot})   95% CI {lo:.1f}–{hi:.1f}")

        # Do the two real candidates actually differ?
        r_ok, r_tot = pooled["raw"][n]
        a_ok, a_tot = pooled["adjusted"][n]
        if r_tot and a_tot:
            rlo, rhi = wilson(r_ok, r_tot)
            alo, ahi = wilson(a_ok, a_tot)
            if rlo > ahi:
                verdict = "RAW is measurably better here"
            elif alo > rhi:
                verdict = "ADJUSTED is measurably better here"
            else:
                verdict = ("no measurable difference — the intervals overlap, so this sample "
                           "cannot separate them")
            print(f"   → {verdict}")

        # Head to head, night by night: less powerful than the pooled test but
        # it answers "does one of them win more often", which is what an eye
        # test on a nightly board is actually picking up on.
        rn, an = per_night["raw"][n], per_night["adjusted"][n]
        if rn and an and len(rn) == len(an):
            raw_win = sum(1 for x, y in zip(rn, an) if x > y)
            adj_win = sum(1 for x, y in zip(rn, an) if y > x)
            tie = len(rn) - raw_win - adj_win
            print(f"   night-by-night: raw won {raw_win}, adjusted won {adj_win}, tied {tie}")
        print()

    print("Graded files hold the bot's DESIGNATED PICKS, not the full slate, so this measures how "
          "well each ordering sorts the picks the bot already made. That is the right question for "
          "the top of the board and the wrong one for the whole pool.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
