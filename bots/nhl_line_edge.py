#!/usr/bin/env python3
"""
nhl_line_edge.py -- OUR anytime-goal probability vs THE BOOK'S PRICE (and its movement), on the daily NHL prop snapshots (nhl_odds_<date>.json: player_anytime_goal / player_first_goal, price only).

    python bots/nhl_line_edge.py [--cache DIR] [--odds DIR]

Rows are nhl_backtest.run_data for the 2026 season to date (history carried over from 2025), scored with models/nhl_goal_v2.json (its fit includes 2026 to date, so this is the
in-sample-ish read; the prospective version is the shadow log). Reported for anytime goal: the book's implied probability (vig included: only the yes price is posted) vs ours vs what happened,
the return of backing the yes whenever we are 4 / 8 points above the price, and closing-line value (open vs close, line_tools.open_close).
"""
import argparse, json, math, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as nb           # noqa: E402
import injury_status                # noqa: E402
import line_tools as LT             # noqa: E402
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nhl")); ap.add_argument("--odds", required=True); a = ap.parse_args()
    d25, d26 = nb.load_season(2025, a.cache), nb.load_season(2026, a.cache)
    comb = defaultdict(list)
    for k in set(d25) | set(d26):
        comb[k] = list(d25.get(k, [])) + list(d26.get(k, []))
    rows = [r for r in nb.run_data(comb, 2026) if r["night"] >= "2026-10-01"]
    model = json.load(open(Path(__file__).resolve().parent / "models" / "nhl_goal_v2.json"))
    import nhl_live
    oc = LT.open_close(a.odds, "nhl", lambda d: d.get("date"), injury_status.key)
    res = []
    for r in rows:
        k = (r["night"], injury_status.key(r["name"] or ""), "player_anytime_goal")
        if k not in oc:
            continue
        z = model["intercept"] + sum(w * (r["f"][n] - m) / s for n, w, m, s in zip(model["features"], model["weights"], model["mu"], model["sd"]))
        p = nhl_live._calibrate(model, 1 / (1 + math.exp(-z)))
        o, c = oc[k]["open"][1], oc[k]["close"][1]
        res.append({"p": p, "implied": c[2] / 100.0, "price": c[1], "open_implied": LT.implied_prob(o[1]), "close_implied": LT.implied_prob(c[1]), "goal": r["goal"], "moved": oc[k]["open"][0] != oc[k]["close"][0]})
    if len(res) < 30:
        print(f"{len(res)} priced skater-games matched so far (too few)"); return
    y = np.array([r["goal"] for r in res], float); pm = np.array([r["p"] for r in res]); pb = np.array([r["implied"] for r in res])
    ll = lambda p: -np.mean(y * np.log(np.clip(p, 1e-4, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-4, 1)))
    print(f"NHL anytime goal, 2026 to date: {len(res)} priced skater-games; went in {100 * y.mean():.1f}%; book implied {100 * pb.mean():.1f}% (vig included); ours {100 * pm.mean():.1f}%; log loss book {ll(pb):.4f} vs ours {ll(pm):.4f}")
    for thr in (0.04, 0.08):
        sel = [r for r in res if r["p"] - r["implied"] >= thr]
        if sel:
            profit = sum((r["price"] / 100.0 if r["goal"] else -1.0) if r["price"] > 0 else ((100.0 / -r["price"]) if r["goal"] else -1.0) for r in sel)
            print(f"  back the yes when we are >= {100 * thr:.0f} pts above the price: {len(sel)} bets, went in {100 * np.mean([r['goal'] for r in sel]):.1f}% (they imply {100 * np.mean([r['implied'] for r in sel]):.1f}%), return {100 * profit / len(sel):+.0f}% per $1")
    mv = [(1 if r["p"] > r["open_implied"] else -1) * (r["close_implied"] - r["open_implied"]) for r in res if r["open_implied"] and abs(r["p"] - r["open_implied"]) >= 0.04]
    if mv:
        print(f"  closing-line value (picks >= 4 pts from the opening price): {len(mv)} picks, market moved our way {100 * np.mean(np.array(mv) > 0):.0f}% / against {100 * np.mean(np.array(mv) < 0):.0f}%, average {100 * np.mean(mv):+.2f} pts (opening is the file's first price; older files only have one price a day)")


if __name__ == "__main__":
    main()
