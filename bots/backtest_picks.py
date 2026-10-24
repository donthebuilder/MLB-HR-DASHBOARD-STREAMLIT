#!/usr/bin/env python3
"""
backtest_picks.py -- who the board called in each game, against who was there.

    python bots/backtest_picks.py --dir <graded dir> [--dir <more>] [--odds <dir>]

Answers, for MLB home runs:
  1. How often is a CALLED pick not the best player in his game (by hr_score),
     and how does it do when he isn't?
  2. If MOONSHOT v5 had picked each game's player instead of hr_score, how many
     games change, who wins those games, and what did it pay?
  3. Return, not just hits: flat $1 on each strategy's pick at the price the
     book had that night (odds_YYYY-MM-DD.json), for the picks that were priced.

Rules: a hitter with 0 AB and 0 BB is void, not a miss. Spray-log entries on or
after the game date are dropped before v5 sees them. The graded files hold the
bot's designated players (about 5-7 a game), not the whole lineup, so "best in
the game" means best among the players the bot already put on the board.
"""
import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_v5 import rows_of, v5_score   # noqa: E402

DATE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
CALLED = {"TOP", "HR"}          # the roles that are a home-run call


def load_nights(dirs):
    nights = {}
    for d in dirs:
        for f in sorted(glob.glob(str(Path(d) / "graded_results_*.json"))):
            m = DATE.search(f)
            if m:
                nights[m.group(1)] = json.load(open(f))      # later dirs win
    return nights


def load_odds(dirs):
    out = {}
    for d in dirs:
        for f in glob.glob(str(Path(d) / "odds_2*.json")):
            doc = json.load(open(f))
            for pid, mk in (doc.get("rows") or {}).items():
                hr = mk.get("batter_home_runs")
                if hr and hr[0] == 0.5:
                    out[(doc["date"], str(pid))] = hr[1]
    return out


def dollars(american):
    return american / 100 if american > 0 else 100 / -american


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--odds", action="append", default=[])
    a = ap.parse_args()
    nights = load_nights(a.dir)
    odds = load_odds(a.odds or a.dir)

    games = []          # one dict per game: its de-duplicated, non-void players
    for night, doc in sorted(nights.items()):
        by_game = defaultdict(dict)
        for r in rows_of(doc):
            if r.get("got_hr") is None or r.get("hr_score") in (None, ""):
                continue
            if not (r.get("actual_ab") or r.get("actual_bb")):
                continue                                    # void
            p = by_game[r["game_pk"]].setdefault(r["player_id"], {
                "pid": str(r["player_id"]), "hr_score": float(r["hr_score"]), "hit": bool(r["got_hr"]),
                "roles": set(), "v5": None, "night": night, "price": odds.get((night, str(r["player_id"])))})
            p["roles"].add(r.get("pick_type") or r.get("game_pick_role"))
            if p["v5"] is None:
                try:
                    p["v5"] = v5_score(r, night)
                except Exception:
                    pass
        for gp, ps in by_game.items():
            if len(ps) >= 2:
                games.append(list(ps.values()))

    print(f"{len(nights)} nights, {len(games)} games with 2+ board players, "
          f"{sum(len(g) for g in games)} players, {sum(p['hit'] for g in games for p in g)} homered\n")

    # 1 -- called picks vs the best player in the game
    print("1. CALLED PICKS (TOP / HR roles) vs the best hr_score in their game")
    best = notbest = [0, 0]
    best, notbest = [0, 0], [0, 0]
    rank_hits = defaultdict(lambda: [0, 0])
    ret = {"best": [0, 0.0], "notbest": [0, 0.0], "all": [0, 0.0]}      # [bets, net]

    def book(key, p):
        if p["price"] is not None:
            ret[key][0] += 1
            ret[key][1] += dollars(p["price"]) if p["hit"] else -1.0

    for g in games:
        for p in g:
            book("all", p)
        order = sorted(g, key=lambda p: p["hr_score"], reverse=True)
        for i, p in enumerate(order):
            if p["roles"] & CALLED:
                tgt = best if i == 0 else notbest
                tgt[0] += p["hit"]; tgt[1] += 1
                rank_hits[min(i + 1, 4)][0] += p["hit"]; rank_hits[min(i + 1, 4)][1] += 1
                book("best" if i == 0 else "notbest", p)
    tot = best[1] + notbest[1]
    print(f"   called picks: {tot}; best in his game: {best[1]} ({pct(best[1], tot)}), "
          f"NOT the best: {notbest[1]} ({pct(notbest[1], tot)})")
    print(f"   hit rate when best {pct(*best)}   when NOT best {pct(*notbest)}")
    for k in sorted(rank_hits):
        print(f"   called pick ranked {'4+' if k == 4 else k} in his game: {pct(*rank_hits[k])} of {rank_hits[k][1]}")

    for label, key in (("called, best in game", "best"), ("called, NOT best", "notbest"),
                       ("every board player", "all")):
        n, net = ret[key]
        print(f"   return, {label:22} {n:4} priced bets   ROI {100 * net / n if n else 0:+6.1f}%")

    # 2 -- v5 vs hr_score, game by game
    print("\n2. MOONSHOT v5 vs hr_score: each model's #1 in every game")
    both = [g for g in games if sum(p["v5"] is not None for p in g) >= 2]
    old_h = new_h = same = diff = old_diff = new_diff = 0
    miss = 0
    for g in both:
        sc = [p for p in g if p["v5"] is not None]
        o = max(sc, key=lambda p: p["hr_score"]); n = max(sc, key=lambda p: p["v5"])
        old_h += o["hit"]; new_h += n["hit"]
        if o is n:
            same += 1
        else:
            diff += 1; old_diff += o["hit"]; new_diff += n["hit"]
        if any(p["hit"] for p in sc) and not n["hit"]:
            miss += 1
    print(f"   {len(both)} games scored by both; same pick in {same}, different in {diff} ({pct(diff, len(both))})")
    print(f"   #1 hit rate   hr_score {pct(old_h, len(both))}   v5 {pct(new_h, len(both))}")
    print(f"   games where they DISAGREE ({diff}):  hr_score's pick hit {old_diff},  v5's pick hit {new_diff}")

    # 3 -- return at the price the book had
    print("\n3. RETURN, flat $1 on each model's #1 per game (priced picks only)")
    for name, key in (("hr_score", "hr_score"), ("v5", "v5")):
        n = w = 0
        net = 0.0
        for g in both:
            sc = [p for p in g if p["v5"] is not None]
            p = max(sc, key=lambda p: p[key])
            if p["price"] is None:
                continue
            n += 1
            if p["hit"]:
                w += 1; net += dollars(p["price"])
            else:
                net -= 1
        print(f"   {name:9} priced {n:3} of {len(both)} games   hit {w}   net {net:+.2f} on {n}   ROI {100 * net / n if n else 0:+.1f}%")
    allp = [(p["price"], p["hit"]) for g in both for p in g if p["price"] is not None]
    if allp:
        net = sum(dollars(pr) if h else -1 for pr, h in allp)
        print(f"   every priced board player: {len(allp)} bets, ROI {100 * net / len(allp):+.1f}%  (the baseline to beat)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
