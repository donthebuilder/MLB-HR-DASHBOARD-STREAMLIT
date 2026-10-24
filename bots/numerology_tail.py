#!/usr/bin/env python3
"""
numerology_tail.py -- Donovan's hypothesis, stated before it is tested:

    "Alignment doesn't change whether a player does the normal thing -- it shows up in the OUT-OF-BODY nights."
    i.e. players with several numbers lined up with the game date have more EXTREME games than their usual rate says.

EXTREME = MLB 2+ home runs · NHL 2+ goals · NFL 2+ TDs · NBA 35+ points.
ALIGNED = the count of a player's five numbers (jersey, birth day, life path, next number, name) equal to the game date's
root: 0, 1, 2, 3+ . Hypothesis: extreme-game rate rises with that count, in every sport.

Controls: a player's extreme rate depends on how good he is, so each row is compared with the extreme rate of players
at the SAME usual scoring rate (deciles), not with a flat average.
Design: seasons are split in half in time order. The first half is DISCOVERY, the second is CONFIRMATION. A pattern counts
only if it points the same way in both, and the confirmation half is read once.

    python bots/numerology_tail.py mlb|nhl|nfl|nba
Also prints the plain table people ask for: how often the NORMAL event (HR / goal / TD / 25+) happens at each alignment count,
beside the base rate at that count -- a "15-40% hit rate" only means something against that column.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numerology_lab as nl      # noqa: E402
import angle_engine as eng       # noqa: E402

LABEL = {"mlb": ("home run", "2+ HR"), "nhl": ("goal", "2+ goals"), "nfl": ("touchdown", "2+ TD"), "nba": ("25+ points", "35+ pts")}


def hits_of(r):
    dr = nl.digits_root(r["date"])
    return sum(1 for v in r["ax"].values() if v and v == dr)


def deltas(rows, field):
    """field minus the average of that field among players in the same decile of usual rate."""
    order = sorted(r["rate"] for r in rows)
    cuts = [order[len(order) * i // 10] for i in range(1, 10)]
    dec = defaultdict(list)
    for r in rows:
        r["_dec"] = sum(r["rate"] >= c for c in cuts)
        dec[r["_dec"]].append(r[field])
    avg = {k: sum(v) / len(v) for k, v in dec.items()}
    for r in rows:
        r["dx"] = 100 * (r[field] - avg[r["_dec"]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=list(LABEL))
    ap.add_argument("--cache", default=None)
    ap.add_argument("--seasons", nargs="+", type=int, default=None)
    a = ap.parse_args()
    cache = a.cache or str(Path.home() / f".cache/dash_{a.sport}")
    if a.sport == "mlb":
        rows = nl.collect_mlb(cache, tuple(a.seasons or (2021, 2022, 2023, 2024, 2025, 2026)))
    else:
        rows = {"nhl": nl.collect_nhl, "nfl": nl.collect_nfl, "nba": nl.collect_nba}[a.sport](cache)
    normal, tail = LABEL[a.sport]
    seasons = sorted({r["season"] for r in rows})
    half = len(seasons) // 2
    disc, conf = set(seasons[:half]), set(seasons[half:])
    print(f"{a.sport.upper()}: {len(rows)} player-games; discovery seasons {sorted(disc)}, confirmation {sorted(conf)}")
    for r in rows:
        r["k"] = min(3, hits_of(r))
    deltas(rows, "x")

    print(f"\nTHE PLAIN TABLE -- by how many of his numbers equal today's root")
    print(f"   {'aligned':<9}{'n':>8}{normal + ' rate':>16}{tail + ' rate':>14}   (base across everyone: {normal} {100 * sum(r['y'] for r in rows) / len(rows):.1f}%, {tail} {100 * sum(r['x'] for r in rows) / len(rows):.2f}%)")
    for k in range(4):
        rs = [r for r in rows if r["k"] == k]
        print(f"   {('3+' if k == 3 else k):<9}{len(rs):>8}{100 * sum(r['y'] for r in rs) / len(rs):>15.1f}%{100 * sum(r['x'] for r in rs) / len(rs):>13.2f}%")

    print(f"\nTHE HYPOTHESIS: extreme-game rate ({tail}) vs same-quality players, by alignment count (pp; + = more extreme games)")
    print(f"   {'aligned':<9}{'DISCOVERY n':>12}{'effect':>9}{'t':>6}   {'CONFIRMATION n':>15}{'effect':>9}{'t':>6}   verdict")
    for k in (1, 2, 3):
        res = []
        for name, S in (("d", disc), ("c", conf)):
            w = [r for r in rows if r["season"] in S and (r["k"] >= k if k == 3 else r["k"] == k)]
            wo = [r for r in rows if r["season"] in S and r["k"] == 0]
            d, t = eng.welch([r["dx"] for r in w], [r["dx"] for r in wo])
            res.append((len(w), d, t))
        (nd, dd, td), (nc, dc, tc) = res
        same = dd * dc > 0 and abs(tc) >= 2
        print(f"   {('3+' if k == 3 else k):<9}{nd:>12}{dd:>+8.2f}p{td:>6.1f}   {nc:>15}{dc:>+8.2f}p{tc:>6.1f}   {'HOLDS' if same else 'does not hold'}")
    # STACKED: alignment ON TOP of the stats -- split by how strong he already is, then by alignment count
    order = sorted(r["rate"] for r in rows)
    c1, c2 = order[len(order) // 3], order[2 * len(order) // 3]
    print(f"\nALIGNMENT ON TOP OF THE STATS (all seasons): {normal} rate / {tail} rate, by how strong he already is and how many numbers align")
    print(f"   {'his usual rate':<18}{'0 aligned':>22}{'1 aligned':>22}{'2+ aligned':>22}")
    for label, f in (("bottom third", lambda r: r["rate"] <= c1), ("middle third", lambda r: c1 < r["rate"] <= c2), ("top third", lambda r: r["rate"] > c2)):
        cells = []
        for sel in (lambda r: r["k"] == 0, lambda r: r["k"] == 1, lambda r: r["k"] >= 2):
            rs = [r for r in rows if f(r) and sel(r)]
            cells.append(f"{100 * sum(r['y'] for r in rs) / len(rs):5.1f}% / {100 * sum(r['x'] for r in rs) / len(rs):4.2f}% (n={len(rs)})" if rs else "n/a")
        print(f"   {label:<18}" + "".join(f"{c:>22}" for c in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
