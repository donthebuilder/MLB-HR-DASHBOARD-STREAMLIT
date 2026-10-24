#!/usr/bin/env python3
"""
nhl_buildout.py -- the legs PropFinder-style models lean on, tested properly on three seasons of NHL.

    python bots/nhl_buildout.py pull   [--cache DIR]      (once: ESPN closing totals and moneylines for every 2023-25 game)
    python bots/nhl_buildout.py study  [--cache DIR]

Legs, each added to the tuned nhl_goal_v2 features (fit 2023-24, graded once on 2025-26, 42,815 skater-games):
  BOOK     the closing total and the club's moneyline win chance (implied scoring environment: a 6.5 total with a heavy favourite is a different night)
  DvP      the opposing defence's goals / SHOTS allowed to HIS position over its last 10, 20 or 40 games vs the league (shots are steadier than goals)
  PP       the power-play minutes the opponent gives up
Reported: log loss, best pick per game, top 10 per night, and the weight each leg gets.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as nb      # noqa: E402
import game_lines as G         # noqa: E402
from tune_models import fit_logistic, topk   # noqa: E402

ESPN_TO_NHL = {"NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}
SB = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={}"


def pull(cache):
    import subprocess
    cache = Path(cache)
    out = cache / "espn_nhl_games.json"
    games = json.load(open(out)) if out.exists() else {}
    days = []
    for s in (2023, 2024, 2025):
        a, b = date.fromisoformat(nb.SEASON_START[s]), date.fromisoformat(nb.SEASON_END[s])
        days += [(a + timedelta(i)).strftime("%Y%m%d") for i in range((b - a).days + 1)]

    def one(day):
        raw = subprocess.run(["curl", "-s", "-m", "30", SB.format(day)], capture_output=True, text=True).stdout
        res = {}
        try:
            for e in json.loads(raw).get("events", []):
                comp = e["competitions"][0]
                ab = {c["homeAway"]: ESPN_TO_NHL.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in comp["competitors"]}
                et = (date.fromisoformat(e["date"][:10]) if e["date"][11:13] >= "05" else date.fromisoformat(e["date"][:10]) - timedelta(days=1)).isoformat()
                res[f"{et}|{ab['away']}|{ab['home']}"] = e["id"]
        except Exception:
            pass
        return res
    with ThreadPoolExecutor(12) as ex:
        for r in ex.map(one, days):
            games.update(r)
    out.write_text(json.dumps(games))
    ids = list(games.values())
    lines = G.fetch_closing("nhl", ids, cache / "nhl_lines")
    print(f"{len(games)} ESPN games, {sum(1 for v in lines.values() if v and v.get('total') is not None)} with a closing total and moneyline")


def legs_tables(data):
    pp = {(r["playerId"], r["gameId"]): (r.get("ppTimeOnIce") or 0) / 60.0 for r in data["skater/powerplay"]}
    per = defaultdict(lambda: defaultdict(lambda: [None, defaultdict(float), defaultdict(float), 0.0]))
    for r in data["skater/summary"]:
        if r["positionCode"] == "G":
            continue
        e = per[r["opponentTeamAbbrev"]][r["gameId"]]
        e[0] = r["gameDate"]; e[1][r["positionCode"]] += r["goals"]; e[2][r["positionCode"]] += r["shots"]; e[3] += pp.get((r["playerId"], r["gameId"]), 0.0)
    return {t: sorted(((e[0], dict(e[1]), dict(e[2]), e[3]) for e in g.values()), key=lambda x: x[0]) for t, g in per.items()}


def dvp(tables, opp, pos, date_, n, idx):
    mine = [x for x in tables.get(opp, []) if x[0] < date_][-n:]
    if len(mine) < 5:
        return 0.0
    allg = [x for t in tables for x in [y for y in tables[t] if y[0] < date_][-n:]]
    return sum(x[idx].get(pos, 0) for x in mine) / len(mine) - sum(x[idx].get(pos, 0) for x in allg) / len(allg)


def study(cache):
    cache = Path(cache)
    games = json.load(open(cache / "espn_nhl_games.json"))
    rows = []
    for s in (2023, 2024, 2025):
        d = nb.load_season(s, str(cache))
        tb = legs_tables(d)
        # sampling the league average every row is slow: precompute per (date) lazily
        for r in nb.run_data(d, s):
            home, away = (r["team"], r["opp"]) if r["f"]["is_home"] else (r["opp"], r["team"])
            eid = games.get(f"{r['night']}|{away}|{home}")
            ln = G.close_line("nhl", eid, cache / "nhl_lines") if eid else None
            if ln and ln.get("total") is not None and ln.get("ml_home") and ln.get("ml_away"):
                ph = G.novig(ln["ml_home"], ln["ml_away"])
                pw = ph if r["f"]["is_home"] else 1 - ph
                bk = {"bk_total": float(ln["total"]), "bk_win_logit": math.log(pw / (1 - pw))}
                has = 1.0
            else:
                bk, has = {"bk_total": 6.0, "bk_win_logit": 0.0}, 0.0
            r["f"] = {**r["f"], **bk, "bk_has": has}
            rows.append((r, tb))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "study"])
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nhl"))
    a = ap.parse_args()
    if a.cmd == "pull":
        pull(a.cache)
        return 0
    pairs = study(a.cache)
    print(f"{len(pairs)} skater-games, {sum(r['f']['bk_has'] for r, _ in pairs) / len(pairs) * 100:.0f}% with closing lines\n")
    # DvP variants, computed once per (opponent, pos, date, window, metric)
    memo = {}
    def leg(tb, r, n, idx):
        k = (id(tb), r["opp"], r["pos"], r["night"], n, idx)
        if k not in memo:
            memo[k] = dvp(tb, r["opp"], r["pos"], r["night"], n, idx)
        return memo[k]
    rows = []
    for r, tb in pairs:
        r["f"]["dvp_goals20"] = leg(tb, r, 20, 1); r["f"]["dvp_shots20"] = leg(tb, r, 20, 2)
        r["f"]["dvp_shots10"] = leg(tb, r, 10, 2); r["f"]["dvp_shots40"] = leg(tb, r, 40, 2)
        rows.append(r)
    tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
    base = [c for c in sorted(rows[0]["f"]) if not c.startswith(("bk_", "dvp_"))]
    variants = [("our model as it is", base), ("+ closing total & win chance", base + ["bk_total", "bk_win_logit", "bk_has"]),
                ("+ DvP goals (20 games)", base + ["dvp_goals20"]), ("+ DvP SHOTS (10 games)", base + ["dvp_shots10"]), ("+ DvP SHOTS (20 games)", base + ["dvp_shots20"]),
                ("+ DvP SHOTS (40 games)", base + ["dvp_shots40"]),
                ("+ closing lines + DvP shots 20", base + ["bk_total", "bk_win_logit", "bk_has", "dvp_shots20"])]
    print(f"graded on {len(te)} skater-games of 2025-26 (fit on {len(tr)})")
    for label, cols in variants:
        X = lambda rs: np.array([[r["f"][c] for c in cols] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([r["goal"] for r in tr], float))
        z = ((X(te) - mu) / sd) @ w[1:] + w[0]
        ll = 0.0
        for r, zz in zip(te, z):
            p = min(1 - 1e-6, max(1e-6, 1 / (1 + math.exp(-zz))))
            ll -= math.log(p) if r["goal"] else math.log(1 - p)
            r["_z"], r["y"], r["gk"] = float(zz), r["goal"], (r["night"], r["game"])
        ll /= len(te)
        extra = "".join(f"  {c} {x:+.3f}" for c, x in zip(cols, w[1:]) if c.startswith(("bk_total", "bk_win", "dvp_")))
        print(f"   {label:<34} log loss {ll:.5f}  best/game {topk(te, '_z', 'y', 1, 'gk'):5.1f}%  top10/night {topk(te, '_z', 'y', 10, 'night'):5.1f}%{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
