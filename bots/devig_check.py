#!/usr/bin/env python3
"""
devig_check.py -- is the price the book offers fair, and does our score find the ones it gets wrong?

    python bots/devig_check.py --dir <graded dir> [--dir ...]  [--odds <odds dir>]

PLAIN-ENGLISH DEVIG
  A book's price is not the true chance; it has the book's cut ("vig" / overround) baked in. +400 on a home run
  implies 20.0% -- but if the book's prices for the whole market add up to 106%, the book really thinks about 18.9%.
  Taking that cut out is "de-vigging", and what's left is the book's FAIR chance. A bet has positive expected value
  (+EV) only when the real chance is higher than the fair chance by more than the cut:  EV = p x payout - (1 - p).
  Two ways to get the fair chance:
    two-way market (over AND under both priced): divide each implied chance by the two added together.
    one-sided market (home-run YES only): no 'no' side to normalise against, so use sharp books' price (Pinnacle/Circa) or
        the market's own measured overround -- measured here from every settled bet, which is the honest way when only YES is posted.

THIS SCRIPT answers three questions on the settled history we have (home-run props, 2026-08-15 on):
  1. HOW BIG IS THE CUT?  Average implied chance vs how often it really happened, overall and by price band.
  2. IS THE MARKET OTHERWISE RIGHT?  Hit rate by price band against that band's de-vigged chance.
  3. DOES OUR SCORE BEAT IT?  Among priced hitters we ranked, ROI by our score tier, against betting everyone.
"""
import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import miss_autopsy as m   # noqa: E402

BANDS = [("-101 or shorter", -10**9, -101), ("-100 to +150", -100, 150), ("+151 to +400", 151, 400), ("+401 to +900", 401, 900), ("+901 and up", 901, 10**9)]


def payout(american):
    return american / 100 if american > 0 else 100 / -american


def implied(american):
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--odds", action="append", default=[])
    a = ap.parse_args()
    nights = m.load(a.dir)
    odds = {}
    for d in (a.odds or a.dir):
        for f in glob.glob(str(Path(d) / "odds_2*.json")):
            doc = json.load(open(f))
            for pid, mk in (doc.get("rows") or {}).items():
                hr = mk.get("batter_home_runs")
                if hr and hr[0] == 0.5:
                    odds[(doc["date"], str(pid))] = hr[1]
    rows = []
    for night, rs in nights.items():
        seen = set()
        for r in rs:
            pid = str(r["player_id"])
            if (night, pid) in odds and pid not in seen and r.get("got_hr") is not None and (r.get("actual_ab") or r.get("actual_bb")):
                seen.add(pid)
                rows.append({"night": night, "price": odds[(night, pid)], "y": int(bool(r["got_hr"])), "score": float(r["hr_score"]) if r.get("hr_score") not in (None, "") else None,
                             "board": r.get("pick_type") in ("TOP15", "TOP", "HR")})
    if not rows:
        print("no priced + graded home-run rows found")
        return 1
    n = len(rows)
    avg_imp = sum(implied(r["price"]) for r in rows) / n
    hit = sum(r["y"] for r in rows) / n
    cut = avg_imp / hit if hit else float("nan")
    print(f"{n} priced, graded home-run bets across {len({r['night'] for r in rows})} nights (board hitters only)\n")
    print("1. THE CUT")
    print(f"   the book's prices add up to an average chance of {100 * avg_imp:.1f}%; they actually homered {100 * hit:.1f}% -> the cut is about {100 * (cut - 1):.0f}% on top of the true chance")

    print("\n2. IS THE MARKET OTHERWISE RIGHT? (by price band)")
    print(f"   {'price':<18}{'n':>6}{'book says':>11}{'fair (cut out)':>16}{'happened':>10}{'flat ROI':>10}")
    for label, lo, hi in BANDS:
        rs = [r for r in rows if lo <= r["price"] <= hi]
        if len(rs) < 15:
            continue
        imp = sum(implied(r["price"]) for r in rs) / len(rs)
        h = sum(r["y"] for r in rs) / len(rs)
        net = sum(payout(r["price"]) if r["y"] else -1 for r in rs)
        print(f"   {label:<18}{len(rs):>6}{100 * imp:>10.1f}%{100 * imp / cut:>15.1f}%{100 * h:>9.1f}%{100 * net / len(rs):>9.1f}%")

    print("\n3. DOES OUR SCORE BEAT THE PRICE? (priced hitters we scored, by our hr_score tier; fair chance uses the cut above)")
    sc = sorted((r for r in rows if r["score"] is not None), key=lambda r: r["score"])
    k = len(sc) // 3
    print(f"   {'our tier':<20}{'n':>6}{'happened':>10}{'fair price says':>17}{'flat ROI':>10}{'  (se)':>8}")
    for label, chunk in (("bottom third", sc[:k]), ("middle third", sc[k:2 * k]), ("top third", sc[2 * k:]), ("on our TOP/HR/TOP15 board", [r for r in sc if r["board"]])):
        if not chunk:
            continue
        h = sum(r["y"] for r in chunk) / len(chunk)
        imp = sum(implied(r["price"]) for r in chunk) / len(chunk)
        rets = [payout(r["price"]) if r["y"] else -1.0 for r in chunk]
        mean = sum(rets) / len(rets)
        se = (sum((x - mean) ** 2 for x in rets) / max(1, len(rets) - 1)) ** 0.5 / len(rets) ** 0.5
        print(f"   {label:<20}{len(chunk):>6}{100 * h:>9.1f}%{100 * imp / cut:>16.1f}%{100 * mean:>9.1f}%{100 * se:>7.1f}%")
    print("\n   an ROI inside about +/- 2 standard errors of zero is not a finding. These are the numbers a few hundred more bets would settle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
