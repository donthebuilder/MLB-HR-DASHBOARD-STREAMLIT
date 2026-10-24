#!/usr/bin/env python3
"""
nba_first_study.py -- FIRST BASKET: is it the best player, the usage, the early usage, or the matchup?

    python bots/nba_first.py pull --seasons 2023 2024 2025      (once)
    python bots/nba_first_study.py [--cache DIR]

One row per pre-game STARTER (the ten eligible men), the outcome = he scored the game's first basket (base rate ~10%). Everything trailing, within the season
(8+ starts). Four families, each tested alone and together on the later season:
  best player      points per game
  usage            share of his team's shot attempts (the site's buckets-first-v1 reads this)
  early usage      share of his team's attempts in the FIRST 4:00 of games -- "who gets the first possessions"
  history          how often he has scored his team's first basket / the game's first basket
  matchup          how often the opposing team has allowed the first basket (team level), and the book's spread when closing lines exist
Fit on 2023-24, graded once on 2025-26. Reported: how often the model's top starter per game scored the first basket (random = 10%), top 3 per game, and the
weights. Same fit-early / grade-late rules as every other lane.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402
from tune_models import fit_logistic   # noqa: E402


def build(cache):
    rows = []
    cache = Path(cache)
    for season in (2023, 2024, 2025):
        games = sorted(bb.load(season, str(cache)), key=lambda g: (g["date"], g["id"]))
        P = defaultdict(list)                    # pid -> trailing per-game dicts (only games he played)
        T = defaultdict(list)                    # team -> trailing: {early_total, fga_total, first_scored_by_team, first_allowed}
        for g in games:
            f = cache / f"first_{g['id']}.json"
            fd = json.load(open(f)) if f.exists() else None
            teams = {t["abbr"]: t for t in g["teams"]}
            if fd and fd.get("first") and fd.get("starters"):
                first_pid = fd["first"]["pid"]
                for ab, t in teams.items():
                    opp = [x for x in teams if x != ab][0]
                    ts, to = T[ab], T[opp]
                    for pid in fd["starters"].get(ab, []):
                        h = [x for x in P[pid]]
                        if len(h) < 8:
                            continue
                        h10 = h[-10:]
                        team_fga = sum(x["tfga"] for x in h) or 1
                        team_early = sum(x["tearly"] for x in h) or 1
                        rows.append({"season": season, "game": g["id"], "night": g["date"], "pid": pid, "team": ab, "y": int(first_pid == pid),
                                     "ppg": sum(x["pts"] for x in h) / len(h), "mpg": sum(x["min"] for x in h10) / len(h10),
                                     "fga_share": sum(x["fga"] for x in h) / team_fga,
                                     "early_share": sum(x["early"] for x in h) / team_early,
                                     "early_per_game": sum(x["early"] for x in h) / len(h),
                                     "first_rate": sum(x["scored_first"] for x in h) / len(h),
                                     "team_first_rate": sum(x["scored_team_first"] for x in h) / len(h),
                                     "opp_allows_first": (sum(x["allowed_first"] for x in to[-20:]) / len(to[-20:])) if to else 0.5,
                                     "is_home": float(t["home"])})
            # update trailing state AFTER the game
            for ab, t in teams.items():
                opp = [x for x in teams if x != ab][0]
                tfga = sum(p["fga"] for p in t["players"])
                tearly = sum((fd or {}).get("early", {}).get(p["pid"], 0) for p in t["players"])
                first_pid = ((fd or {}).get("first") or {}).get("pid")
                mine_first = first_pid in {p["pid"] for p in t["players"]}
                tid = (fd or {}).get("team_ids", {}).get(ab)
                tf = ((fd or {}).get("team_first") or {}).get(tid)
                T[ab].append({"allowed_first": int(first_pid is not None and not mine_first and first_pid in {p["pid"] for p in teams[opp]["players"]})})
                for p in t["players"]:
                    if p["min"] > 0:
                        P[p["pid"]].append({"pts": p["pts"], "min": p["min"], "fga": p["fga"], "tfga": tfga, "early": (fd or {}).get("early", {}).get(p["pid"], 0),
                                            "tearly": tearly, "scored_first": int(first_pid == p["pid"]), "scored_team_first": int(tf == p["pid"])})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    a = ap.parse_args()
    rows = build(a.cache)
    tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
    print(f"{len(rows)} starter-games; fit on {len(tr)} (2023-24), graded on {len(te)} (2025-26); base rate {100 * sum(r['y'] for r in te) / len(te):.1f}% (10 starters a game)\n")
    games = defaultdict(list)
    for r in te:
        games[r["game"]].append(r)
    games = {k: v for k, v in games.items() if len(v) >= 8}

    def evaluate(label, key=None, cols=None):
        if cols:
            X = lambda rs: np.array([[r[c] for c in cols] for r in rs], float)
            Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            w = fit_logistic((Xtr - mu) / sd, np.array([r["y"] for r in tr], float), l2=20.0)
            for r, z in zip(te, ((X(te) - mu) / sd) @ w[1:] + w[0]):
                r["_s"] = float(z)
            note = "  weights: " + ", ".join(f"{c} {x:+.2f}" for c, x in sorted(zip(cols, w[1:]), key=lambda t: -abs(t[1]))[:4])
        else:
            for r in te:
                r["_s"] = r[key]
            note = ""
        t1 = t3 = 0
        for v in games.values():
            order = sorted(v, key=lambda r: r["_s"], reverse=True)
            t1 += order[0]["y"]; t3 += sum(r["y"] for r in order[:3])
        n = len(games)
        print(f"   {label:<38} top starter scored it {100 * t1 / n:5.1f}%   top 3 starters: {100 * t3 / n:5.1f}% (random 30%){note}")
    print(f"GRADED ON {len(games)} games of 2025-26 (the ten starters; the first basket went to a starter {100 * sum(sum(r['y'] for r in v) for v in games.values()) / len(games):.0f}% of the time)")
    evaluate("best player (points per game)", key="ppg")
    evaluate("usage (share of team shots)", key="fga_share")
    evaluate("EARLY usage (first 4:00 shot share)", key="early_share")
    evaluate("his own first-basket history", key="first_rate")
    evaluate("usage + early usage", cols=["fga_share", "early_share"])
    evaluate("all player terms", cols=["ppg", "mpg", "fga_share", "early_share", "early_per_game", "first_rate", "team_first_rate"])
    evaluate("all player terms + matchup", cols=["ppg", "mpg", "fga_share", "early_share", "early_per_game", "first_rate", "team_first_rate", "opp_allows_first", "is_home"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
