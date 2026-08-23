#!/usr/bin/env python3
"""
🎯 PRECISION STUDY — would FEWER picks have been BETTER picks?

Donovan, 2026-08-23:

    "lets focus on precsion and instead of coverage ... i just feel like now
    theres hell picks to every games got the top and a hr pick idk. i thinbk
    that good for high hr days but this season in general has seen some not so
    good days. ... i was thinking what about the 4 best bets then from dividing
    up the picks top hit hrr bases whatever, what would the socring look like
    if we did that over the time — if bad or not good just forget that idea."

That last clause is the whole brief, and it is the right instinct: MEASURE IT,
and if the number says no, the idea dies with a receipt attached. So this
script changes nothing. It reads the graded archive and answers one question:

    On the nights already played, if the board had published only the best N
    picks instead of every per-game designation, what would the record be?

WHY THE COMPARISON IS LEGITIMATE, WHICH IS THE HARD PART
========================================================
Ranking a HIT pick against an HR pick is the mistake this repo has already
paid for once. From lib/verdict.js on the site:

    hit_score simply runs hotter than hr_score, so sorting a mixed board on
    "each card's own score" puts a 1+HIT card at 80 above the game's best bat
    at 65 and calls it an ordering.

Four markets, four scales, no shared zero. So nothing here compares raw
scores. Every pick is converted to its PERCENTILE WITHIN ITS OWN MARKET ON
ITS OWN NIGHT, and the cross-market board is ranked on that. "92nd percentile
of tonight's HIT picks" and "92nd percentile of tonight's HR picks" are the
same statement about two different things, which is exactly what a mixed board
needs and what a raw score can never provide.

AND EACH PICK IS GRADED ON ITS OWN BAR
======================================
An HR pick is asked for a home run; a HIT pick is asked for a base hit; a
CONTACT pick is asked for two total bases. Grading a shortened board on "did
he homer" would compare a 21% market against a 70% one and manufacture
whatever answer the market mix happened to produce. This uses `designed_hit`,
the same per-market bar bots/live_results_tracker.py grades every night on.

ONE PROPERTY OF PERCENTILE RANKING WORTH KNOWING
================================================
A market with MORE picks has more entries packed near the 100th percentile —
twenty HR picks give you 95th, 90th and 85th before a four-pick HIT market
offers anything below 75th. So the top of a cross-market board leans toward
the market the bot designates most, which is HR (708 of 2048 picks on the
archive this was written against). That is not a bug to hide: it means the
short boards below are NOT quietly dodging home runs, and the lift they show
is therefore harder to explain away, not easier. The lane mix is printed so
the reader can check it rather than take that on trust.

WHAT THIS CANNOT ANSWER, STATED UP FRONT
========================================
The graded archive holds the bot's PICKS, not the full slate. So this measures
"of the hitters we already designated, would the top few have been enough" —
which is precisely the fewer-picks-higher-bar question — and it CANNOT measure
whether a better four existed among the hitters the bot never designated. That
needs the prediction_log join bots/slate_eval.py built for exactly this reason,
and it is a separate study.

It also cannot tell you the RIGHT number of picks in a vacuum: fewer picks is
always at least as accurate if the ordering carries any signal at all, because
you are dropping the model's own least-confident calls. The question worth
asking is whether the lift is big enough to be worth the coverage, and how
fast it decays. So the output prints the whole curve, not one number.

Usage:
    python3 bots/precision_study.py
    python3 bots/precision_study.py --dir /tmp/data-checkout/public/data/current
    python3 bots/precision_study.py --out /tmp/precision.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive import describe, load_local, rows_of  # noqa: E402

# The board sizes to simulate, plus the literal shape Donovan described.
BOARD_SIZES = [4, 6, 8, 10, 15, 20, 30]

# pick_type -> the lane it belongs to for the "one of each" board. TOP15 is a
# slate-wide HR board rather than a per-game slot, so it rides with HR.
LANE_OF = {
    "TOP": "TOP", "TOP15": "HR", "HR": "HR",
    "HIT": "HIT", "HRR": "HRR", "CONTACT": "BASES", "TB": "BASES",
}
QUOTA_LANES = ["TOP", "HR", "HIT", "HRR", "BASES"]
# The site already ships this exact board: components/BotPicksStrip.js — "🎯 The
# Four" — is one pick per market on HR / HIT / HRR / CONTACT, ranked on that
# market's own score. For a single pick per lane, "highest score in the lane"
# and "highest percentile in the lane" select the same hitter, so the number
# this row produces is THE FOUR'S OWN RECORD and can be printed beside it.
# TOP is excluded here precisely because The Four excludes it.
FOUR_LANES = ["HR", "HIT", "HRR", "BASES"]

# Which score each pick type was actually selected on. Same map the site's
# verdict registry uses, and the reason it exists is the same: a pick always
# wears its own market's score.
SCORE_FIELD = {
    "TOP": "top_board_score_v2", "TOP15": "hr_score", "HR": "hr_score",
    "HIT": "hit_score", "HRR": "hrr_score", "CONTACT": "contact_score",
    "TB": "contact_score",
}


def _f(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _score_of(row: dict, pick_type: str):
    """The score this pick was chosen on, with a documented fallback.

    TOP's own field (top_board_score_v2) is newer than parts of the archive,
    so a night that predates it falls back to overall_score and then hr_score.
    A pick whose score cannot be recovered at all is DROPPED from the night
    rather than given a zero — a zero would sort it to the bottom of the
    percentile ranking and quietly make every short board look better.
    """
    field = SCORE_FIELD.get(pick_type)
    for key in ([field] if field else []) + ["overall_score", "hr_score"]:
        if not key:
            continue
        v = _f(row.get(key))
        if v is not None and v > 0:
            return v
    return None


def _wilson(hits: int, n: int) -> tuple:
    """95% Wilson interval. A 3-of-4 night is 75% and means nothing; the
    interval is what stops a short board from reading as a finding."""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    z = 1.96
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (100 * max(0.0, centre - half), 100 * min(1.0, centre + half))


def night_board(rows: list) -> list:
    """One graded night as a ranked, cross-market-comparable list.

    Each entry: (pct, lane, hit, pick_type, name). `pct` is the pick's
    percentile WITHIN ITS OWN MARKET on this night — see the module docstring
    for why nothing is compared on a raw score.
    """
    by_market = defaultdict(list)
    entries = []
    for r in rows:
        pt = str(r.get("pick_type", "")).upper().strip()
        if pt not in LANE_OF:
            continue
        hit = r.get("designed_hit")
        if hit is None:
            continue                      # ungraded row: not evidence either way
        score = _score_of(r, pt)
        if score is None:
            continue
        entries.append({"pt": pt, "lane": LANE_OF[pt], "score": score,
                        "hit": 1 if int(hit) else 0,
                        "name": str(r.get("name", "")), "id": r.get("player_id")})
        by_market[pt].append(score)
    if not entries:
        return []
    for k in by_market:
        by_market[k].sort()
    out = []
    for e in entries:
        arr = by_market[e["pt"]]
        # fraction of this market's picks at or below him, 0-100
        lo, hi = 0, len(arr)
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] <= e["score"]:
                lo = mid + 1
            else:
                hi = mid
        e["pct"] = (100.0 * lo) / len(arr)
        out.append(e)
    out.sort(key=lambda e: (-e["pct"], -e["score"]))
    return out


def _one_per(board: list, lanes: list) -> list:
    seen, out = set(), []
    for e in board:                       # already ranked
        if e["lane"] not in lanes or e["lane"] in seen:
            continue
        seen.add(e["lane"])
        out.append(e)
        if len(seen) >= len(lanes):
            break
    return out


def quota_board(board: list) -> list:
    """The literal idea: the single best pick in each lane, one per lane.

    Not "the best 4 overall" — Donovan described DIVIDING UP the picks by type
    ("top hit hrr bases whatever"), which is a different board: it guarantees
    one of each market rather than letting a hot lane take all four slots. Both
    are simulated because they answer different questions, and conflating them
    is how a study ends up measuring the one nobody asked about.
    """
    return _one_per(board, QUOTA_LANES)


def study(nights: dict) -> dict:
    boards = {}
    for date in sorted(nights):
        b = night_board(rows_of(nights[date]))
        if b:
            boards[date] = b
    if not boards:
        return {"nights": 0}

    # ── THE LANE MIX IS THE TRAP, SO IT IS PRICED IN ────────────────────────
    # The full board is HR-heavy by construction (an HR pick per game plus the
    # TOP15 slate board), and HR is by far the hardest bar on the site — 21.8%
    # against 74.3% for 1+ hit on the archive this was written against. Any
    # short board that happens to hold proportionally fewer HR picks scores
    # higher for that reason ALONE, before a single thing is said about the
    # ordering. A study that reported that as "precision works" would be
    # measuring its own market mix.
    #
    # So every board gets a MIX-MATCHED BASELINE: take the full board's own
    # per-lane hit rate, and weight those rates by the lanes THIS board
    # actually selected. That is what a board of this exact shape would have
    # scored with no ordering skill at all. The gap between the board and its
    # own baseline is the only number here that is about the ranking.
    lane_hits, lane_n = defaultdict(int), defaultdict(int)
    for b in boards.values():
        for e in b:
            lane_hits[e["lane"]] += e["hit"]
            lane_n[e["lane"]] += 1
    lane_rate = {k: (100.0 * lane_hits[k] / lane_n[k]) for k in lane_n if lane_n[k]}

    def tally(pick_fn):
        hits = tot = 0
        per_night = []
        mix = defaultdict(int)
        for date, b in boards.items():
            sel = pick_fn(b)
            if not sel:
                continue
            h = sum(e["hit"] for e in sel)
            hits += h
            tot += len(sel)
            for e in sel:
                mix[e["lane"]] += 1
            per_night.append({"date": date, "hits": h, "n": len(sel)})
        base = (sum(lane_rate.get(k, 0.0) * v for k, v in mix.items()) / tot) if tot else None
        pct = (100.0 * hits / tot) if tot else None
        return {"hits": hits, "n": tot, "pct": pct,
                "baseline": base,
                "skill": (pct - base) if (pct is not None and base is not None) else None,
                "mix": dict(sorted(mix.items(), key=lambda kv: -kv[1])),
                "ci": _wilson(hits, tot), "nights": len(per_night)}

    out = {
        "nights": len(boards),
        "dates": [min(boards), max(boards)],
        "full": tally(lambda b: b),
        "sizes": {str(k): tally(lambda b, k=k: b[:k]) for k in BOARD_SIZES},
        "quota": tally(quota_board),
        "four": tally(lambda b: _one_per(b, FOUR_LANES)),
        "by_lane": {},
    }
    out["by_lane"] = {k: {"hits": lane_hits[k], "n": lane_n[k], "pct": lane_rate.get(k)}
                      for k in sorted(lane_n)}
    out["top4_lane_mix"] = out["sizes"]["4"]["mix"]
    return out


def report(res: dict, provenance: str) -> str:
    L = []
    add = L.append
    add("=" * 74)
    add("🎯 PRECISION STUDY — would fewer picks have been better picks?")
    add("=" * 74)
    add(provenance)
    if not res.get("nights"):
        add("")
        add("NO GRADED NIGHTS FOUND. Nothing to study — this is a refusal, not a")
        add("result. Point --dir at a checkout of the data branch, or set")
        add("MOONSHOT_ARCHIVE_DIRS to a folder of graded_results_*.json.")
        return "\n".join(L)

    full = res["full"]
    add(f"nights: {res['nights']}  ({res['dates'][0]} .. {res['dates'][1]})")
    add("")
    add("Every pick graded on ITS OWN bar (designed_hit), ranked across markets")
    add("by percentile within its own market on its own night.")
    add("")
    def line(label, s):
        if not s or not s["n"]:
            return
        raw = (s["pct"] - full["pct"]) if (s["pct"] is not None and full["pct"] is not None) else 0.0
        add(f"{label:<22}{s['n']:>7}{(s['pct'] or 0):>8.1f}%{(s['baseline'] or 0):>10.1f}%"
            f"{(s['skill'] or 0):>+9.1f}{raw:>+9.1f}   {s['ci'][0]:.0f}–{s['ci'][1]:.0f}")

    add(f"{'BOARD':<22}{'PICKS':>7}{'RATE':>9}{'MIX BASE':>10}{'SKILL':>9}{'RAW':>9}   95% CI")
    add("-" * 74)
    line("every designation", full)
    for k in BOARD_SIZES:
        line(f"top {k} a night", res["sizes"][str(k)])
    line("best of each lane", res["quota"])
    line("THE FOUR (as shipped)", res.get("four"))
    add("")
    add("MIX BASE is what a board of THIS EXACT LANE SHAPE would have scored")
    add("with no ordering skill at all, using the full board's own per-lane")
    add("rates. SKILL is the board minus its own baseline — the only column")
    add("here that is about the ranking. RAW is the naive comparison against")
    add("the whole board, and it flatters every short board by however much")
    add("less HR it happens to hold.")
    add("")
    add("PER-LANE RATES ON THE FULL BOARD — the reason MIX BASE exists.")
    for lane, v in res["by_lane"].items():
        add(f"  {lane:<8}{v['n']:>6} picks{(v['pct'] or 0):>8.1f}%")
    add("")
    add("WHAT THE TOP 4 ACTUALLY ARE, by lane: "
        + ", ".join(f"{k} {v}" for k, v in res["top4_lane_mix"].items()))
    add("")
    add("-" * 74)
    add("HOW TO READ THIS")
    add("-" * 74)
    add("Read the SKILL column, not RAW. A shorter board is EXPECTED to score")
    add("higher for two reasons that are not skill: it drops the model's own")
    add("least-confident calls, and it usually holds less HR — the hardest bar")
    add("on the site. MIX BASE prices the second one out; SKILL is what is left.")
    add("The findings are:")
    add("  1. how big SKILL is, against an interval that says whether it is real;")
    add("  2. how fast it decays as the board grows — that is where the honest")
    add("     cut-off lives;")
    add("  3. whether the top of the board is one market wearing a disguise (the")
    add("     lane mix above), in which case a 'precision' board is really a")
    add("     base-hit board and should be called one.")
    add("")
    add("This measures the bot's OWN picks. It cannot see the hitters it never")
    add("designated, so it cannot tell you a better four existed among them —")
    add("that is the prediction_log join in bots/slate_eval.py, a separate study.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="", help="extra folder of graded_results_*.json")
    ap.add_argument("--out", default="", help="write the findings as JSON here")
    a = ap.parse_args()

    extra = [Path(a.dir)] if a.dir else None
    found, notes = load_local(Path(__file__).resolve().parent.parent, extra)
    res = study(found)
    text = report(res, describe(found, notes))
    print(text)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
