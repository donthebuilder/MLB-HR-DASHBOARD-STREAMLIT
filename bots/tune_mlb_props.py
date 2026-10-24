#!/usr/bin/env python3
"""
tune_mlb_props.py -- hits, H+R+RBI and contact: fit on earlier nights, grade on later ones.

    python bots/tune_mlb_props.py --dir <graded dir> [--dir <more>]

Each market is a logistic regression over every pregame numeric field the graded rows carry
(about 80% coverage or better), fitted on the first 70% of nights and graded on the last 30% --
chronological, never shuffled. Outcome fields (actual_*, got_*, hrr_*, designed_*, hr_events) are
excluded by name. Reported against the shipped score for the same market:
  top 1 / 3 per game and top 5 / 10 / 15 per night, among the players the bot already put on the board.

Markets: HIT = 1+ hit (hit_score), HRR = 2+ hits+runs+RBI (hrr_score), CONTACT = 2+ total bases (contact_score).
A hitter with 0 AB and 0 BB is void. Nothing here touches a live score.
"""
import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

DATE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
EXCLUDE = re.compile(r"^(actual_|got_|hrr_total|hrr_2_plus|hrr_3_plus|designed_|hr_events|is_final|game_status|merged|rank$|games_since)", re.I)
MARKETS = {
    "HIT":     ("hit_score",     lambda r: bool(r.get("got_base_hit"))),
    "HRR":     ("hrr_score",     lambda r: bool(r.get("hrr_2_plus"))),
    "CONTACT": ("contact_score", lambda r: (r.get("actual_tb") or 0) >= 2),
}


def rows_of(d):
    if isinstance(d, list):
        return d
    for k in ("graded_slots", "results", "rows"):
        if isinstance(d.get(k), list) and d[k] and isinstance(d[k][0], dict):
            return d[k]
    return []


def load(dirs):
    nights = {}
    for d in dirs:
        for f in sorted(glob.glob(str(Path(d) / "graded_results_*.json"))):
            m = DATE.search(f)
            if m:
                nights[m.group(1)] = rows_of(json.load(open(f)))
    out = []
    for night, rs in sorted(nights.items()):
        seen = {}
        for r in rs:
            if r.get("hr_score") in (None, "") or not (r.get("actual_ab") or r.get("actual_bb")):
                continue
            seen.setdefault((r["game_pk"], r["player_id"]), r)
        for (gp, pid), r in seen.items():
            r = dict(r); r["_night"] = night
            out.append(r)
    return out


def fit_logistic(X, y, l2=20.0, iters=40):
    Xb = np.hstack([np.ones((len(X), 1)), X]); w = np.zeros(Xb.shape[1])
    pen = np.eye(len(w)) * l2; pen[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        step = np.linalg.solve((Xb * (p * (1 - p))[:, None]).T @ Xb + pen, Xb.T @ (p - y) + pen @ w)
        w -= step
        if np.abs(step).max() < 1e-7:
            break
    return w


def topk(rows, key, y, k, group):
    g = defaultdict(list)
    for r in rows:
        g[r[group]].append(r)
    hit = n = 0
    for v in g.values():
        for r in sorted(v, key=lambda r: r[key], reverse=True)[:k]:
            hit += r[y]; n += 1
    return 100 * hit / n if n else float("nan"), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", required=True)
    a = ap.parse_args()
    rows = load(a.dir)
    nights = sorted({r["_night"] for r in rows})
    cut = nights[int(len(nights) * 0.7)]
    print(f"{len(rows)} board players, {len(nights)} nights; fit on nights before {cut}, graded on {cut} onward\n")

    # features: numeric, pregame, mostly present
    cands = defaultdict(int)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not EXCLUDE.search(k) and not k.startswith("_"):
                cands[k] += 1
    feats = sorted(k for k, c in cands.items() if c >= 0.8 * len(rows))
    print(f"{len(feats)} pregame numeric fields used\n")

    for mk, (score_field, did) in MARKETS.items():
        pool = [r for r in rows if r.get(score_field) not in (None, "")]
        for r in pool:
            r["y"] = int(did(r)); r["shipped"] = float(r[score_field]); r["game"] = (r["_night"], r["game_pk"])
        tr = [r for r in pool if r["_night"] < cut]; te = [r for r in pool if r["_night"] >= cut]
        if len(tr) < 500 or len(te) < 300:
            print(f"{mk}: too little data"); continue
        med = {f: float(np.median([r[f] for r in tr if isinstance(r.get(f), (int, float))] or [0])) for f in feats}
        def mat(rs):
            return np.array([[r[f] if isinstance(r.get(f), (int, float)) else med[f] for f in feats] for r in rs], float)
        Xtr, Xte = mat(tr), mat(te)
        mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([r["y"] for r in tr], float))
        for r, z in zip(te, ((Xte - mu) / sd) @ w[1:] + w[0]):
            r["tuned"] = float(z)
        base = 100 * sum(r["y"] for r in te) / len(te)
        print(f"{mk}  (holdout n={len(te)}, base rate on the board {base:.1f}%)")
        for label, key in (("shipped", "shipped"), ("tuned", "tuned")):
            t1, _ = topk(te, key, "y", 1, "game"); t3, _ = topk(te, key, "y", 3, "game")
            t5, _ = topk(te, key, "y", 5, "_night"); t10, _ = topk(te, key, "y", 10, "_night"); t15, _ = topk(te, key, "y", 15, "_night")
            print(f"   {label:<8} top1/game {t1:5.1f}%  top3/game {t3:5.1f}%   top5/night {t5:5.1f}%  top10/night {t10:5.1f}%  top15/night {t15:5.1f}%")
        top = sorted(zip(feats, w[1:]), key=lambda t: -abs(t[1]))[:6]
        print("   biggest weights: " + ", ".join(f"{n} {c:+.2f}" for n, c in top) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
