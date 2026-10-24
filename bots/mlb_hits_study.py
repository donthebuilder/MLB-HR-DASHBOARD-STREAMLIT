#!/usr/bin/env python3
"""
mlb_hits_study.py -- a FULL-SLATE hit / H+R+RBI / total-bases / home-run model for every lineup starter, backtested chronologically.

    python bots/mlb_hits_data.py CACHE      (once, ~7,000 calls)
    python bots/mlb_hits_study.py CACHE

Population: every hitter in a posted starting lineup, 2024-26 regular season (the lineup is known ~3 hours before first pitch, so eligibility is pregame).
Fit on 2024-25, graded once on 2026. Everything is trailing -- games strictly before the game's date. Four outcomes:
    HIT  1+ hit      HRR  2+ hits+runs+RBI      TB2  2+ total bases      HR  1+ home run
Feature groups, added one at a time so each one's worth shows:
    his rate        his own rate of that outcome over 100 / 30 / 10 games, shrunk toward the league       (the plain-stat baseline is just the 100-game rate)
    + lineup        batting spot (1-9), home, plate appearances a game (spot sets how many looks he gets)
    + his profile   contact (BA), K%, BB%, ISO, total bases and HR per PA
    + the starter   opposing probable starter's hits, K%, BB%, HR per batter faced, runs, depth (outs a start), same-hand flag
    + the park      the venue's hit / total-base / HR factors from earlier games
Reported on 2026: log loss (lower is better), best pick per game, top 10 per night, and the hit rate of the top 5% / 10% / 20% / 40% of all hitter-games by
predicted chance -- the number to hold against the 60% / 75% targets.
"""
import bisect
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tune_models import fit_logistic, topk   # noqa: E402


def load(c):
    c = Path(c)
    games = {int(k): v for k, v in json.load(open(c / "games.json")).items()}
    people = {}
    for s in (2024, 2025, 2026):
        for p in json.load(open(c / f"people_{s}.json")):
            people[p["id"]] = p
    hit = defaultdict(list); pit = defaultdict(list)
    for f in c.glob("hit_*.json"):
        pid = int(f.stem.split("_")[2])
        hit[pid] += json.load(open(f))
    for f in c.glob("pit_*.json"):
        pid = int(f.stem.split("_")[2])
        pit[pid] += [x for x in json.load(open(f)) if x["started"]]
    return games, people, hit, pit


class Series:
    """A player's games in date order with prefix sums, so any trailing window is two lookups."""
    def __init__(self, rows, fields):
        rows = sorted((r for r in rows if r.get("pk")), key=lambda r: (r["date"], r["pk"]))
        self.dates = [r["date"] for r in rows]
        self.rows = rows
        self.cum = {f: [0.0] for f in fields}
        for r in rows:
            for f in fields:
                self.cum[f].append(self.cum[f][-1] + r[f])

    def before(self, date):
        return bisect.bisect_left(self.dates, date)

    def window(self, idx, n, f):
        lo = max(0, idx - n)
        return self.cum[f][idx] - self.cum[f][lo], idx - lo


HF = ["g", "hit", "ab", "h", "r", "rbi", "tb", "pa", "bb", "k", "hr", "hrr", "tb2"]
PF = ["start", "outs", "h", "bf", "k", "bb", "hr", "er"]


def prep_hit(rows):
    out = []
    for r in rows:
        if r["pa"] <= 0:
            continue
        out.append({**r, "g": 1, "hit": int(r["h"] >= 1), "hrr": int(r["h"] + r["r"] + r["rbi"] >= 2), "tb2": int(r["tb"] >= 2), "hr": int(r["hr"] >= 1) if False else r["hr"]})
    for r in out:
        r["hrr"] = int(r["h"] + r["r"] + r["rbi"] >= 2)
    return out


def main():
    cache = sys.argv[1]
    games, people, hitlogs, pitlogs = load(cache)
    H = {pid: Series(prep_hit(rs), HF) for pid, rs in hitlogs.items()}
    P = {pid: Series([{**r, "start": 1} for r in rs], PF) for pid, rs in pitlogs.items()}
    # league priors from 2024 only
    tot = defaultdict(float)
    for s in H.values():
        for r in s.rows:
            if r["date"] < "2025-01-01":
                for f in HF:
                    tot[f] += r[f]
    L = {f: tot[f] / tot["g"] for f in ("hit", "hrr", "tb2")}
    L.update({"ba": tot["h"] / tot["ab"], "k": tot["k"] / tot["pa"], "bb": tot["bb"] / tot["pa"], "iso": (tot["tb"] - tot["h"]) / tot["ab"], "tbpa": tot["tb"] / tot["pa"],
              "hrpa": tot["hr"] / tot["pa"], "pa": tot["pa"] / tot["g"]})
    ptot = defaultdict(float)
    for s in P.values():
        for r in s.rows:
            if r["date"] < "2025-01-01":
                for f in PF:
                    ptot[f] += r[f]
    LP = {"h": ptot["h"] / ptot["bf"], "k": ptot["k"] / ptot["bf"], "bb": ptot["bb"] / ptot["bf"], "hr": ptot["hr"] / ptot["bf"], "er": ptot["er"] / ptot["start"], "outs": ptot["outs"] / ptot["start"]}
    print(f"league 2024: hit {100 * L['hit']:.1f}% of hitter-games, H+R+RBI 2+ {100 * L['hrr']:.1f}%, 2+ TB {100 * L['tb2']:.1f}%, BA {L['ba']:.3f}, {L['pa']:.2f} PA a game\n")

    def sh(s, n, w, prior):
        return (s + w * prior) / (n + w)

    venue = defaultdict(lambda: defaultdict(float)); lg = defaultdict(float)
    rows = []
    for pk in sorted(games, key=lambda k: (games[k]["date"], k)):
        g = games[pk]
        if len(g["home_lineup"]) != 9 or len(g["away_lineup"]) != 9 or not g["home_sp"] or not g["away_sp"]:
            continue
        date = g["date"]
        v = venue[g["venue"]]
        pf = {}
        for f, key in (("h", "hit"), ("tb", "tb"), ("hr", "hr")):
            pf[f] = ((v[f] + 3000 * (lg[f] / lg["pa"] if lg["pa"] else 0.0) ) / (v["pa"] + 3000)) / (lg[f] / lg["pa"]) if lg["pa"] > 5000 and lg[f] else 1.0
        for side, lineup, opp_sp, home in (("home", g["home_lineup"], g["away_sp"], 1), ("away", g["away_lineup"], g["home_sp"], 0)):
            ps = P.get(opp_sp)
            if ps:
                pidx = ps.before(date)
                feats_p = {}
                for n, tag in ((10, "10"), (30, "30")):
                    bf, k = ps.window(pidx, n, "bf")[0], ps.window(pidx, n, "k")[0]
                nst = min(pidx, 30)
                bf30 = ps.window(pidx, 30, "bf")[0]
                pitch = {"p_h": sh(ps.window(pidx, 30, "h")[0], bf30, 100, LP["h"]) / LP["h"], "p_k": sh(ps.window(pidx, 30, "k")[0], bf30, 100, LP["k"]) / LP["k"],
                         "p_bb": sh(ps.window(pidx, 30, "bb")[0], bf30, 100, LP["bb"]) / LP["bb"], "p_hr": sh(ps.window(pidx, 30, "hr")[0], bf30, 100, LP["hr"]) / LP["hr"],
                         "p_er": sh(ps.window(pidx, 30, "er")[0], nst, 4, LP["er"]) / LP["er"], "p_outs": sh(ps.window(pidx, 10, "outs")[0], min(pidx, 10), 3, LP["outs"]) / LP["outs"]}
            else:
                pitch = {"p_h": 1.0, "p_k": 1.0, "p_bb": 1.0, "p_hr": 1.0, "p_er": 1.0, "p_outs": 1.0}
            thr = (people.get(opp_sp) or {}).get("throws")
            for spot, pid in enumerate(lineup, 1):
                s = H.get(pid)
                if not s:
                    continue
                idx = s.before(date)
                rec = next((r for r in s.rows[idx:idx + 3] if r["pk"] == pk), None)       # his line in THIS game (may be 3 games ahead on a doubleheader)
                if rec is None or idx < 30:
                    continue
                f = {}
                for out in ("hit", "hrr", "tb2"):
                    f[f"{out}100"] = sh(s.window(idx, 100, out)[0], s.window(idx, 100, "g")[0], 20, L[out])
                    f[f"{out}30"] = sh(s.window(idx, 30, out)[0], s.window(idx, 30, "g")[0], 10, L[out])
                    f[f"{out}10"] = sh(s.window(idx, 10, out)[0], s.window(idx, 10, "g")[0], 5, L[out])
                ab, pa = s.window(idx, 100, "ab")[0], s.window(idx, 100, "pa")[0]
                f["ba"] = sh(s.window(idx, 100, "h")[0], ab, 80, L["ba"]); f["k"] = sh(s.window(idx, 100, "k")[0], pa, 80, L["k"]); f["bb"] = sh(s.window(idx, 100, "bb")[0], pa, 80, L["bb"])
                f["iso"] = sh(s.window(idx, 100, "tb")[0] - s.window(idx, 100, "h")[0], ab, 80, L["iso"]); f["tbpa"] = sh(s.window(idx, 100, "tb")[0], pa, 80, L["tbpa"])
                f["hrpa"] = sh(s.window(idx, 100, "hr")[0], pa, 150, L["hrpa"])
                f["pa_pg"] = sh(s.window(idx, 30, "pa")[0], s.window(idx, 30, "g")[0], 10, L["pa"])
                f["spot"] = float(spot); f["spot2"] = float(spot * spot); f["home"] = float(home)
                bats = (people.get(pid) or {}).get("bats")
                f["same_hand"] = float(bool(bats and thr and bats == thr))
                f.update(pitch)
                f["pk_h"], f["pk_tb"], f["pk_hr"] = pf["h"], pf["tb"], pf["hr"]
                rows.append({"season": int(date[:4]), "date": date, "game": pk, "pid": pid, "f": f,
                             "y": {"hit": int(rec["h"] >= 1), "hrr": int(rec["h"] + rec["r"] + rec["rbi"] >= 2), "tb2": int(rec["tb"] >= 2), "hr": int(rec["hr"] >= 1)}})
        for side, lineup in (("home", g["home_lineup"]), ("away", g["away_lineup"])):
            for pid in lineup:
                s = H.get(pid)
                if not s:
                    continue
                idx = s.before(date)
                rec = next((r for r in s.rows[idx:idx + 3] if r["pk"] == pk), None)
                if rec:
                    for f_, key in (("h", "h"), ("tb", "tb"), ("hr", "hr"), ("pa", "pa")):
                        venue[g["venue"]][f_] += rec[key]; lg[f_] += rec[key]
    print(f"{len(rows)} lineup hitter-games ({sum(r['season'] == 2024 for r in rows)} in 2024, {sum(r['season'] == 2025 for r in rows)} in 2025, {sum(r['season'] == 2026 for r in rows)} in 2026)\n")
    tr = [r for r in rows if r["season"] in (2024, 2025)]; te = [r for r in rows if r["season"] == 2026]
    for r in te:
        r["game_key"] = (r["date"], r["game"])
    GROUPS = [("his rate only (the plain stat)", lambda o: [f"{o}100"]), ("his rate, 3 windows", lambda o: [f"{o}100", f"{o}30", f"{o}10"]),
              ("+ lineup spot, home, PA", lambda o: [f"{o}100", f"{o}30", f"{o}10", "spot", "spot2", "home", "pa_pg"]),
              ("+ his profile", lambda o: [f"{o}100", f"{o}30", f"{o}10", "spot", "spot2", "home", "pa_pg", "ba", "k", "bb", "iso", "tbpa", "hrpa"]),
              ("+ the opposing starter", lambda o: [f"{o}100", f"{o}30", f"{o}10", "spot", "spot2", "home", "pa_pg", "ba", "k", "bb", "iso", "tbpa", "hrpa", "p_h", "p_k", "p_bb", "p_hr", "p_er", "p_outs", "same_hand"]),
              ("+ the park (full)", lambda o: [f"{o}100", f"{o}30", f"{o}10", "spot", "spot2", "home", "pa_pg", "ba", "k", "bb", "iso", "tbpa", "hrpa", "p_h", "p_k", "p_bb", "p_hr", "p_er", "p_outs", "same_hand", "pk_h", "pk_tb", "pk_hr"])]
    LABEL = {"hit": "1+ HIT", "hrr": "2+ H+R+RBI", "tb2": "2+ TOTAL BASES", "hr": "1+ HOME RUN"}
    for out in ("hit", "hrr", "tb2", "hr"):
        base = 100 * sum(r["y"][out] for r in te) / len(te)
        print(f"{LABEL[out]}   (graded on {len(te)} lineup hitter-games of 2026, base rate {base:.1f}%)")
        res = None
        for label, cols_f in GROUPS:
            if out == "hr":
                cols = [c.replace("hr100", "hrpa") if False else c for c in cols_f("tb2")]
                cols = [c for c in cols if not c.startswith(("tb2",))] + ["hrpa"] if "tb2100" in cols else cols
                cols = list(dict.fromkeys(cols))
            else:
                cols = cols_f(out)
            X = lambda rs: np.array([[r["f"][c] for c in cols] for r in rs], float)
            Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            w = fit_logistic((Xtr - mu) / sd, np.array([r["y"][out] for r in tr], float), l2=10.0)
            z = ((X(te) - mu) / sd) @ w[1:] + w[0]
            ll = 0.0
            for r, zz in zip(te, z):
                p = min(1 - 1e-6, max(1e-6, 1 / (1 + math.exp(-zz))))
                ll -= math.log(p) if r["y"][out] else math.log(1 - p)
                r["_z"], r["y1"], r["night"] = float(zz), r["y"][out], r["date"]
            ll /= len(te)
            t1, t10 = topk(te, "_z", "y1", 1, "game_key"), topk(te, "_z", "y1", 10, "night")
            order = sorted(te, key=lambda r: r["_z"], reverse=True)
            tiers = "  ".join(f"top {int(q * 100)}% {100 * sum(r['y1'] for r in order[:int(len(order) * q)]) / int(len(order) * q):.1f}%" for q in (0.05, 0.10, 0.20, 0.40))
            print(f"   {label:<32} log loss {ll:.5f}  best/game {t1:5.1f}%  top10/night {t10:5.1f}%   {tiers}")
            res = (cols, w)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
