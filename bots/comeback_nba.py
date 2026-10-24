#!/usr/bin/env python3
"""
comeback_nba.py -- close games on the book, then a big hole in the game: how often does the club come back?

    python bots/comeback_nba.py [--cache DIR] [--lines DIR]

2,463 NBA games, 2023-24 and 2024-25, each with its closing spread (game_lines.py) and its quarter scores.
State = the margin after a quarter (Q1, Q2 = halftime, Q3). For a club that is TRAILING by a given amount:
    wins             the final margin is above zero
    gets inside 10   it loses by 9 or fewer (or wins): "covers +10" at that moment
    cuts it 10+      its deficit shrinks by at least 10 points by the final buzzer
    fair price       the break-even American odds on the club winning, from the measured rate
Grouped by the PREGAME spread: close games (the book had it within 5 either way) vs the rest.
A fair price is not a live price: live moneylines are not kept anywhere we can read, so compare the fair price
with what the book offers in the moment. `lines.py snapshot` saves tonight's pregame lines; live quotes would need their own feed.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402
import game_lines as G          # noqa: E402


def fair_odds(p):
    if p <= 0:
        return "n/a"
    if p >= 1:
        return "-inf"
    return f"+{round(100 * (1 - p) / p):,}" if p < 0.5 else f"-{round(100 * p / (1 - p)):,}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    ap.add_argument("--lines", default=None)
    a = ap.parse_args()
    lines_dir = Path(a.lines) if a.lines else Path(a.cache).parent / "nba_lines"

    states = defaultdict(list)          # (quarter, deficit bucket, pregame group) -> [final margin of the trailing club]
    used = 0
    for season in (2023, 2024):
        for g in bb.load(season, a.cache):
            t = g["teams"]
            ln = G.close_line("nba", g["id"], lines_dir)
            if not ln or ln.get("spread_home") is None or any(len(x.get("lines") or []) < 4 for x in t):
                continue
            used += 1
            group = "close (book within 5)" if abs(ln["spread_home"]) <= 5 else "not close (book 5+)"
            own, opp = t[0]["lines"], t[1]["lines"]
            final = sum(own) - sum(opp)
            for q in (1, 2, 3):
                m = sum(own[:q]) - sum(opp[:q])
                if m == 0:
                    continue
                trailing_final = final if m < 0 else -final
                deficit = abs(m)
                for lo, hi, lab in ((10, 14, "down 10-14"), (15, 19, "down 15-19"), (20, 99, "down 20+")):
                    if lo <= deficit <= hi:
                        states[(q, lab, group)].append((trailing_final, deficit))
    print(f"{used} games with a closing spread and quarter scores\n")
    for q, qname in ((2, "AT HALFTIME"), (3, "AFTER THREE QUARTERS")):
        print(qname)
        print(f"   {'trailing club is':<22}{'pregame':<24}{'n':>6}{'wins':>8}{'inside 10':>11}{'cuts it 10+':>13}   fair price to win")
        for lab in ("down 10-14", "down 15-19", "down 20+"):
            for group in ("close (book within 5)", "not close (book 5+)"):
                xs = states.get((q, lab, group), [])
                if len(xs) < 25:
                    continue
                n = len(xs)
                w = sum(f > 0 for f, d in xs) / n
                inside = sum(f > -10 for f, d in xs) / n
                cut = sum(f - (-d) >= 10 for f, d in xs) / n
                print(f"   {lab:<22}{group:<24}{n:>6}{100 * w:>7.1f}%{100 * inside:>10.1f}%{100 * cut:>12.1f}%   {fair_odds(w)}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
