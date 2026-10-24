#!/usr/bin/env python3
"""
pitchtype_study.py -- does a hitter's record by PITCH TYPE, set against what the opposing starter throws, add to batter x pitcher?

    python bots/pitchtype_data.py CACHE      (once)
    python bots/pitchtype_study.py CACHE

For every hitter-game of 2025-26 (20+ prior games that season, an announced opposing starter), built only from batted balls BEFORE that date:
  batter overall      his home runs per batted ball, shrunk toward the league
  batter by type      his home runs per batted ball against each pitch type, shrunk toward (his overall x that type's league factor)
  pitcher mix         the share of batted balls this starter allows on each pitch type, shrunk toward the league mix
                      (the data holds batted balls, not every pitch: the mix is the mix of pitches that were HIT, a proxy for what he throws)
  pitcher overall     his home runs allowed per batted ball
  FIT                 sum over types of (mix x batter-by-type rate)  /  batter overall.   >1: his record suits this starter's mix; <1: it doesn't.
The same is done with HARD-HIT rate in place of HR (a steadier number than a rare event).
Test: logistic of "homered" on [his base HR rate, the starter's overall rate] (= batter x pitcher with no pitch type) vs the same + FIT. Fit on 2025, graded
once on 2026: log loss, best pick per game, top 10 per night; plus HR rate by FIT tercile against what his base rate predicted.
Approximations, said once: the starter is the announced probable; batted balls stand in for pitches; league type factors come from 2024.
"""
import json
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tune_models import fit_logistic, topk   # noqa: E402

K_B, K_BT, K_P, K_MIX = 40.0, 25.0, 60.0, 60.0


def load(cache):
    cache = Path(cache)
    bbe = sorted(pickle.load(open(cache / "bbe.pkl", "rb")), key=lambda r: r[0])
    prob = {int(k): v for k, v in json.load(open(cache / "probables.json")).items()}
    return bbe, prob


def hitter_games(cache, prob):
    out = []
    for f in Path(cache).glob("glog2_*.json"):
        season, pid = f.stem.split("_")[1:3]
        lg = sorted((g for g in json.load(open(f)) if g["ab"] > 0 and g.get("pk") in prob), key=lambda g: g["date"])
        hrs = 0
        for i, g in enumerate(lg):
            if i >= 20:
                pr = prob[g["pk"]]
                opp = pr["away_pitcher"] if g["home"] else pr["home_pitcher"]
                if opp:
                    out.append({"season": int(season), "date": g["date"], "pk": g["pk"], "b": int(pid), "p": opp, "y": int(g["hr"] >= 1),
                                "base": (hrs + 0.04 * 30) / (i + 30)})
            hrs += g["hr"]
    return sorted(out, key=lambda r: r["date"])


def main():
    cache = sys.argv[1]
    bbe, prob = load(cache)
    rows = hitter_games(cache, prob)
    # league factors from 2024 only (before any graded game)
    lg_bip = defaultdict(int); lg_hr = defaultdict(int); lg_hh = defaultdict(int); n24 = hr24 = hh24 = 0
    for d, b, p, t, hr, hh, ev in bbe:
        if d < "2025-01-01" and t:
            lg_bip[t] += 1; lg_hr[t] += hr; lg_hh[t] += hh; n24 += 1; hr24 += hr; hh24 += hh
    L, LH = hr24 / n24, hh24 / n24
    fac = {t: ((lg_hr[t] + 5 * L) / (lg_bip[t] + 5)) / L for t in lg_bip if lg_bip[t] >= 200}
    facH = {t: ((lg_hh[t] + 5 * LH) / (lg_bip[t] + 5)) / LH for t in lg_bip if lg_bip[t] >= 200}
    types = sorted(fac)
    lg_mix = {t: lg_bip[t] / sum(lg_bip[x] for x in types) for t in types}
    print(f"{len(bbe)} batted balls; league HR/BIP {100 * L:.2f}%, hard-hit {100 * LH:.1f}%; {len(types)} pitch types; {len(rows)} hitter-games\n")

    b_n = defaultdict(int); b_hr = defaultdict(int); b_hh = defaultdict(int)
    bt_n = defaultdict(int); bt_hr = defaultdict(int); bt_hh = defaultdict(int)
    p_n = defaultdict(int); p_hr = defaultdict(int); pt_n = defaultdict(int)
    i = 0
    out = []
    for r in rows:
        while i < len(bbe) and bbe[i][0] < r["date"]:
            d, b, p, t, hr, hh, ev = bbe[i]; i += 1
            b_n[b] += 1; b_hr[b] += hr; b_hh[b] += hh
            p_n[p] += 1; p_hr[p] += hr
            if t in lg_mix:
                bt_n[(b, t)] += 1; bt_hr[(b, t)] += hr; bt_hh[(b, t)] += hh; pt_n[(p, t)] += 1
        b, p = r["b"], r["p"]
        if b_n[b] < 30 or p_n[p] < 30:
            continue
        hb = (b_hr[b] + K_B * L) / (b_n[b] + K_B)
        hhb = (b_hh[b] + K_B * LH) / (b_n[b] + K_B)
        pf = ((p_hr[p] + K_P * L) / (p_n[p] + K_P)) / L
        tot = sum(pt_n[(p, t)] for t in types)
        mix = {t: (pt_n[(p, t)] + K_MIX * lg_mix[t]) / (tot + K_MIX) for t in types}
        fit = sum(mix[t] * ((bt_hr[(b, t)] + K_BT * hb * fac[t]) / (bt_n[(b, t)] + K_BT)) for t in types) / hb
        fitH = sum(mix[t] * ((bt_hh[(b, t)] + K_BT * hhb * facH[t]) / (bt_n[(b, t)] + K_BT)) for t in types) / hhb
        out.append({**r, "pf": pf, "fit": fit, "fitH": fitH})
    print(f"{len(out)} hitter-games with a history on both sides ({sum(r['season'] == 2025 for r in out)} in 2025, {sum(r['season'] == 2026 for r in out)} in 2026)\n")

    for r in out:
        r["lb"] = math.log(r["base"] / (1 - r["base"])); r["lpf"] = math.log(r["pf"]); r["lfit"] = math.log(r["fit"]); r["lfitH"] = math.log(r["fitH"])
        r["game"] = (r["date"], r["pk"]); r["night"] = r["date"]
    tr = [r for r in out if r["season"] == 2025]; te = [r for r in out if r["season"] == 2026]

    print("1. DOES A GOOD FIT HAVE MORE HOME RUNS THAN HIS BASE RATE SAYS?  (2025-26, by FIT tercile of the HR version / the hard-hit version)")
    for label, key in (("HR fit", "fit"), ("hard-hit fit", "fitH")):
        cuts = np.quantile([r[key] for r in out], [1 / 3, 2 / 3])
        cells = []
        for name, f in (("worst third", lambda v: v <= cuts[0]), ("middle", lambda v: cuts[0] < v <= cuts[1]), ("best third", lambda v: v > cuts[1])):
            rs = [r for r in out if f(r[key])]
            cells.append(f"{name} {100 * sum(r['y'] for r in rs) / len(rs):.1f}% vs predicted {100 * sum(r['base'] for r in rs) / len(rs):.1f}% (n={len(rs)})")
        print(f"   {label:<13} " + "   |   ".join(cells))

    print("\n2. DOES FIT ADD TO BATTER x PITCHER?  (fit on 2025, graded on 2026)")
    base_cols = ["lb", "lpf"]
    for label, cols in (("batter x pitcher (no pitch type)", base_cols), ("+ HR pitch-type fit", base_cols + ["lfit"]), ("+ hard-hit pitch-type fit", base_cols + ["lfitH"]),
                        ("+ both", base_cols + ["lfit", "lfitH"])):
        X = lambda rs: np.array([[r[c] for c in cols] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([r["y"] for r in tr], float), l2=5.0)
        z = ((X(te) - mu) / sd) @ w[1:] + w[0]
        ll = -sum(math.log(min(1 - 1e-6, max(1e-6, 1 / (1 + math.exp(-zz))))) if r["y"] else math.log(min(1 - 1e-6, max(1e-6, 1 - 1 / (1 + math.exp(-zz))))) for r, zz in zip(te, z)) / len(te)
        for r, zz in zip(te, z):
            r["_z"] = float(zz)
        print(f"   {label:<34} log loss {ll:.5f}   best/game {topk(te, '_z', 'y', 1, 'game'):5.1f}%   top10/night {topk(te, '_z', 'y', 10, 'night'):5.1f}%   weights " + ", ".join(f"{c} {x:+.3f}" for c, x in zip(cols, w[1:])))
    print(f"\n   graded on {len(te)} hitter-games (2026), base HR rate {100 * sum(r['y'] for r in te) / len(te):.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
