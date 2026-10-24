#!/usr/bin/env python3
"""
raymond_case.py -- why was Lucas Raymond PropFinder's #1 on Thu Oct 22, 2026 (DET @ VAN), and where was our model?

    python bots/nhl_live.py 2026-10-22 --out BOARD.json
    python bots/raymond_case.py BOARD.json [DAY] [PLAYER]

PART 1  our view that morning: nhl_live's board for the day (nhl_goal_v2, calibrated).
PART 2  the legs the market-research notes say PropFinder had and our goal score did not -- the opposing defence's goals allowed to HIS POSITION (L / R / C / D)
        and the power-play time the opponent gives up -- rebuilt from public game logs, with where Raymond and the other top names stand on them.
PART 3  do those legs help in general? Add them to the tuned NHL model, fit on 2023-24, graded on 2025-26: log loss, best pick per game, top 10 per night,
        and where the augmented model would have put Raymond that night.
"""
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as nb      # noqa: E402
import nhl_live as live        # noqa: E402
import ev_log                  # noqa: E402
import injury_status as I      # noqa: E402
from tune_models import fit_logistic, topk   # noqa: E402

board_path = sys.argv[1]
day = sys.argv[2] if len(sys.argv) > 2 else "2026-10-22"
who = sys.argv[3] if len(sys.argv) > 3 else "Lucas Raymond"
HOME = Path.home()
SCRATCH = HOME / ".cache/dash_nhl"
GROUP = {"L": "F", "R": "F", "C": "F", "D": "D"}


def legs_tables(data):
    """per defending team: date-ordered [(date, {pos: goals allowed}, pp_minutes_given)]"""
    pp = {(r["playerId"], r["gameId"]): (r.get("ppTimeOnIce") or 0) / 60.0 for r in data["skater/powerplay"]}
    per = defaultdict(lambda: defaultdict(lambda: [None, defaultdict(float), 0.0]))      # defending team -> game -> [date, goals by pos, pp given]
    for r in data["skater/summary"]:
        if r["positionCode"] == "G":
            continue
        e = per[r["opponentTeamAbbrev"]][r["gameId"]]
        e[0] = r["gameDate"]
        e[1][r["positionCode"]] += r["goals"]
        e[2] += pp.get((r["playerId"], r["gameId"]), 0.0)
    return {t: sorted(((e[0], dict(e[1]), e[2]) for e in g.values()), key=lambda x: x[0]) for t, g in per.items()}


def leg_values(tables, opp, pos, date, n=20):
    """(his position's goals allowed per game by `opp` relative to the league's, pp minutes `opp` gives up relative to the league's), games strictly before `date`."""
    def last(t):
        return [x for x in tables.get(t, []) if x[0] < date][-n:]
    mine = last(opp)
    if len(mine) < 5:
        return 0.0, 0.0
    allg = [x for t in tables for x in last(t)]
    dv = sum(x[1].get(pos, 0) for x in mine) / len(mine) - sum(x[1].get(pos, 0) for x in allg) / len(allg)
    pg = sum(x[2] for x in mine) / len(mine) - sum(x[2] for x in allg) / len(allg)
    return dv, pg


# ------------------------------------------------------------------ PART 1
board = json.load(open(board_path))["players"]
order = sorted(board, key=lambda r: (r["p_goal_adj"], r["p_goal_raw"]), reverse=True)
me = next(r for r in order if I.key(r["name"]) == I.key(who))
print(f"PART 1 -- OUR MODEL, {day}: {me['name']} ({me['team']} @ {me['opponent']}, {me['pos']})")
print(f"   calibrated P(goal) {100 * me['p_goal']:.1f}%  (raw {100 * me['p_goal_raw']:.1f}%), #{order.index(me) + 1} of {len(order)} skaters on the slate, tier {me['tier']}")
print(f"   his inputs: {me['goals_per_gm']} goals/gm, {me['shots_per_gm']} shots/gm, {me['toi_min']} min, opposing goalie {me['opp_goalie']}")
print("   top eight by our model: " + ", ".join(f"{r['name']} {100 * r['p_goal']:.0f}%" for r in order[:8]))
actual = ev_log.stats_nhl(day).get(I.key(who), {})
print(f"   what happened: {me['name']} scored {actual.get('Anytime goal')} goal(s), {actual.get('Shots on goal')} shots\n")

# ------------------------------------------------------------------ PART 2
d25 = nb.load_season(2025, str(SCRATCH))
d26 = live.season_data(day, str(HOME / ".cache/dash_nhl_live"))
data = {k: d25[k] + d26[k] for k in d25}
tables = legs_tables(data)
opp = me["opponent"]
pos = me["pos"]
ranks = {}
for t in tables:
    ranks[t] = leg_values(tables, t, pos, day)
rank_dv = sorted(ranks, key=lambda t: ranks[t][0], reverse=True).index(opp) + 1
rank_pg = sorted(ranks, key=lambda t: ranks[t][1], reverse=True).index(opp) + 1
print(f"PART 2 -- PropFinder's legs, rebuilt")
print(f"   {opp}'s defence against {pos}s (last 20 games): {ranks[opp][0]:+.3f} goals a game vs the league's average for {pos}s -> #{rank_dv} softest of {len(ranks)}")
print(f"   {opp}'s penalty habit: gives opponents {ranks[opp][1]:+.2f} power-play minutes a game vs the league -> #{rank_pg} most generous of {len(ranks)}")
ppavg = defaultdict(list)
pp_by = {(r["playerId"], r["gameId"]): (r.get("ppTimeOnIce") or 0) / 60.0 for r in data["skater/powerplay"]}
names = {}
for r in sorted(data["skater/summary"], key=lambda r: r["gameDate"]):
    if r["positionCode"] != "G" and r["gameDate"] < day:
        ppavg[r["playerId"]].append(pp_by.get((r["playerId"], r["gameId"]), 0.0)); names[r["playerId"]] = (r["skaterFullName"], r["teamAbbrev"], r["positionCode"])
pp10 = {pid: sum(v[-10:]) / len(v[-10:]) for pid, v in ppavg.items() if len(v) >= 5}
pid_me = next(pid for pid, (n, t, p) in names.items() if I.key(n) == I.key(who))
rk = sorted(pp10, key=lambda p: pp10[p], reverse=True).index(pid_me) + 1
print(f"   his own power-play time: {pp10[pid_me]:.2f} min a game over the last 10 -> #{rk} of {len(pp10)} skaters")
print("   the same legs for the eight names our model ranked highest that day:")
for r in order[:8]:
    pid = next((p for p, (n, t, ps) in names.items() if I.key(n) == I.key(r["name"])), None)
    if pid:
        dv, pg = leg_values(tables, r["opponent"], r["pos"], day)
        print(f"      {r['name']:<22}{r['team']} vs {r['opponent']}   opp {r['pos']}-defence {dv:+.3f}   opp PP given {pg:+.2f} min   own PP {pp10.get(pid, 0):.2f} min")
print()

# ------------------------------------------------------------------ PART 3
rows = []
for s in (2023, 2024, 2025):
    d = nb.load_season(s, str(SCRATCH))
    tb = legs_tables(d)
    for r in nb.run_data(d, s):
        dv, pg = leg_values(tb, r["opp"], r["pos"], r["night"])
        r["f"] = {**r["f"], "opp_pos_def": dv, "opp_pp_given": pg}
        rows.append(r)
tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
names_all = sorted(rows[0]["f"])
base_cols = [c for c in names_all if c not in ("opp_pos_def", "opp_pp_given")]
print("PART 3 -- do those legs help in general? (fit on 2023-24, graded on 2025-26, {} skater-games)".format(len(te)))
fits = {}
for label, cols in (("our model as it is", base_cols), ("+ opponent defence vs his position", base_cols + ["opp_pos_def"]),
                    ("+ opponent PP minutes given", base_cols + ["opp_pp_given"]), ("+ both", base_cols + ["opp_pos_def", "opp_pp_given"])):
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
    print(f"   {label:<38} log loss {ll:.5f}   best/game {topk(te, '_z', 'y', 1, 'gk'):5.1f}%   top10/night {topk(te, '_z', 'y', 10, 'night'):5.1f}%"
          + "".join(f"   {c} {x:+.3f}" for c, x in zip(cols, w[1:]) if c in ("opp_pos_def", "opp_pp_given")))
    fits[label] = (cols, mu, sd, w)
print()
print("   (the live board is built from nhl_goal_v2, so the augmented fit is not re-run per skater: the weights above say how much the two legs would move a player. They barely move anyone.)")
