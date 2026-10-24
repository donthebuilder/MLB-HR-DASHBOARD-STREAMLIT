#!/usr/bin/env python3
"""
blowout_study.py -- NBA: who loses minutes in a blowout, who gains them, and can we see it before tip?

    python bots/blowout_study.py [--cache DIR]

Uses the same cached box scores as buckets_backtest.py. "Star" = top two scorers on his
team by last-10 points who average 28+ minutes. "Bench" = 10-22 minutes. Both are read
from games BEFORE the one being measured. Pregame blowout risk is the gap in the two
teams' last-10 net ratings plus a 3-point home edge -- there are no historical NBA
spreads to use, so this is the stand-in; a real spread should do at least as well.

Part 1: what a blowout (final margin 20+) does to stars and bench, vs a close game (<10).
Part 2: how well the pregame gap sees a blowout coming.
Part 3: what the pregame gap does to a star's chance of 25+, with the favourite and underdog split.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402


def collect(cache, seasons):
    out = []                    # one dict per (player, game) for stars and bench
    games_meta = []             # one dict per team-game
    for s in seasons:
        games = sorted(bb.load(s, cache), key=lambda g: (g["date"], g["id"]))
        hist = defaultdict(list)        # pid -> [(min, pts)]
        team = defaultdict(list)        # team -> [(pts_for, pts_against)]
        for g in games:
            t = g["teams"]
            pts = [sum(p["pts"] for p in x["players"]) for x in t]
            margin = abs(pts[0] - pts[1])
            def net(a):
                h = team[a][-10:]
                return sum(f - c for f, c in h) / len(h) if h else None
            nets = [net(x["abbr"]) for x in t]
            for i, x in enumerate(t):
                if nets[0] is None or nets[1] is None:
                    continue
                gap = nets[i] - nets[1 - i] + (3.0 if x["home"] else -3.0)    # + = favoured
                roster = []
                for p in x["players"]:
                    h = hist[p["pid"]][-10:]
                    if len(h) >= 6:
                        roster.append((p, sum(m for m, _ in h) / len(h), sum(q for _, q in h) / len(h)))
                stars = sorted([r for r in roster if r[1] >= 28], key=lambda r: r[2], reverse=True)[:2]
                bench = [r for r in roster if 10 <= r[1] <= 22]
                games_meta.append({"gap": gap, "margin": margin})
                for role, rs in (("star", stars), ("bench", bench)):
                    for p, m10, q10 in rs:
                        out.append({"role": role, "gap": gap, "margin": margin, "min": p["min"], "min10": m10,
                                    "pts": p["pts"], "pts10": q10, "y25": int(p["pts"] >= 25)})
            for i, x in enumerate(t):
                team[x["abbr"]].append((pts[i], pts[1 - i]))
                for p in x["players"]:
                    if p["min"] > 0:
                        hist[p["pid"]].append((p["min"], p["pts"]))
    return out, games_meta


def avg(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    a = ap.parse_args()
    rows, metas = collect(a.cache, [2023, 2024])
    print(f"{len(metas)} team-games, {len(rows)} star/bench player-games\n")

    print("1. WHAT A BLOWOUT DOES (minutes and points vs his own last-10 average)")
    for role in ("star", "bench"):
        for label, lo, hi in (("close  (margin <10)", 0, 10), ("medium (10-19)", 10, 20), ("blowout (20+)", 20, 99)):
            rs = [r for r in rows if r["role"] == role and lo <= r["margin"] < hi]
            print(f"   {role:<5} {label:<20} n={len(rs):5}  minutes {avg([r['min'] for r in rs]):5.1f} (usual {avg([r['min10'] for r in rs]):5.1f})"
                  f"   points {avg([r['pts'] for r in rs]):5.1f} (usual {avg([r['pts10'] for r in rs]):5.1f})"
                  f"   25+ {100 * avg([r['y25'] for r in rs]):4.1f}%")

    print("\n2. CAN WE SEE IT COMING? P(final margin 20+) by pregame |gap|")
    for lo, hi in ((0, 3), (3, 6), (6, 10), (10, 99)):
        ms = [m for m in metas if lo <= abs(m["gap"]) < hi]
        print(f"   |gap| {lo:>2}-{hi if hi < 99 else '+':<3} n={len(ms):5}  blowout (20+) {100 * avg([m['margin'] >= 20 for m in ms]):4.1f}%   avg margin {avg([m['margin'] for m in ms]):4.1f}")

    print("\n3. A STAR'S 25+ RATE BY PREGAME GAP (+ = his team favoured)")
    for lo, hi in ((-99, -6), (-6, -2), (-2, 2), (2, 6), (6, 99)):
        rs = [r for r in rows if r["role"] == "star" and lo <= r["gap"] < hi]
        print(f"   gap {lo if lo > -99 else '-inf':>4} to {hi if hi < 99 else '+inf':<4} n={len(rs):5}  minutes {avg([r['min'] for r in rs]):5.1f}"
              f"   points {avg([r['pts'] for r in rs]):5.1f}   25+ {100 * avg([r['y25'] for r in rs]):4.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
