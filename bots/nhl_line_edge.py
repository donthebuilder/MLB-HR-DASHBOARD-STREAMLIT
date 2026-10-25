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
import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as nb           # noqa: E402
import injury_status                # noqa: E402
import line_tools as LT             # noqa: E402
from collections import defaultdict


LANE_MK = {"player_points": ("pts", "points", [1, 2, 3, 4]), "player_assists": ("ast", "assists", [1, 2, 3]), "player_shots_on_goal": ("sog", "shots", list(range(1, 9)))}


def p_over_line(ths, probs, line):
    k = math.floor(line) + 1
    pr = np.minimum.accumulate(np.array(probs))
    return float(pr[0] if k <= ths[0] else pr[-1] if k >= ths[-1] else np.interp(k, ths, pr))


def lane_markets(a, rows, oc_all, fit_rows):
    """points / assists / shots-on-goal against the book's lines: a logistic per threshold on the lane's own features, P(over the line) read at the book's number."""
    from tune_models import fit_logistic
    out = []
    for om, (lane, rawk, ths) in LANE_MK.items():
        tr = [r for r in fit_rows if r.get("lanes")]
        names = sorted(tr[0]["lanes"][lane]["f"])
        X = np.array([[r["lanes"][lane]["f"][n] for n in names] for r in tr], float); mu, sd = X.mean(0), X.std(0) + 1e-9
        W = [fit_logistic((X - mu) / sd, np.array([float(r["raw"][rawk] >= t) for r in tr]), l2=20.0) for t in ths]
        for r in rows:
            k = (r["night"], injury_status.key(r["name"] or ""), om)
            if k not in oc_all or not r.get("lanes"):
                continue
            x = (np.array([r["lanes"][lane]["f"][n] for n in names], float) - mu) / sd
            P = [1 / (1 + math.exp(-(x @ w[1:] + w[0]))) for w in W]
            o, c = oc_all[k]["open"][1], oc_all[k]["close"][1]
            line = c[0]
            y = r["raw"][rawk]
            if y == line:
                continue
            fair_close = LT.fair_over(c[1], c[3] if len(c) > 3 else None)[0]
            fair_open = LT.fair_over(o[1], o[3] if len(o) > 3 else None)[0]
            out.append({"market": om, "line": line, "price": c[1], "implied": c[2] / 100.0, "model": p_over_line(ths, P, line), "over": int(y > line), "open_fair": fair_open, "close_fair": fair_close})
    return out


def espn_first_scorers(nights):
    """{(night, away NHL abbr, home NHL abbr): first goal scorer display name} from ESPN's play-by-play (scoring plays put in game order: period, then clock counting down)."""
    import subprocess
    cv = {"NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}
    get = lambda u: json.loads(subprocess.run(["curl", "-s", "-m", "40", u], capture_output=True, text=True).stdout or "{}")
    out = {}
    for n in nights:
        for e in get(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={n.replace('-', '')}").get("events", []):
            if not e["status"]["type"]["completed"]:
                continue
            c = {x["homeAway"]: cv.get(x["team"]["abbreviation"], x["team"]["abbreviation"]) for x in e["competitions"][0]["competitors"]}
            plays = [p for p in get(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={e['id']}").get("plays", []) if p.get("scoringPlay")]
            def ck(p):
                m = re.match(r"(\d+):(\d+)", (p.get("clock") or {}).get("displayValue", "0:0"))
                return ((p.get("period") or {}).get("number") or 9, -(int(m.group(1)) * 60 + int(m.group(2))) if m else 0)
            if plays:
                p = sorted(plays, key=ck)[0]
                out[(n, c["away"], c["home"])] = ((p.get("participants") or [{}])[0].get("athlete") or {}).get("displayName")
    return out


def first_goal_test(rows, oc_all, model, nhl_live):
    """First goal of the game: P(player scores it) = his goal rate's share of every skater's in the game x P(any goal). Compared with the book's price (long shots, +500 and up)."""
    by_game = {}
    for r in rows:
        z = model["intercept"] + sum(w * (r["f"][n] - m) / s for n, w, m, s in zip(model["features"], model["weights"], model["mu"], model["sd"]))
        p = nhl_live._calibrate(model, 1 / (1 + math.exp(-z)))
        r["_lam"] = -math.log(1 - min(p, 0.95))
        by_game.setdefault(r["game"], []).append(r)
    nights = sorted({r["night"] for r in rows if (r["night"], injury_status.key(r["name"] or ""), "player_first_goal") in oc_all})
    first = espn_first_scorers(nights)
    res = []
    for g, rs in by_game.items():
        tot = sum(r["_lam"] for r in rs)
        night = rs[0]["night"]
        teams = {r["team"] for r in rs}
        scorer = next((v for (n, a_, h_), v in first.items() if n == night and {a_, h_} == teams), None)
        if not scorer:
            continue
        for r in rs:
            k = (night, injury_status.key(r["name"] or ""), "player_first_goal")
            if k not in oc_all:
                continue
            c = oc_all[k]["close"][1]
            res.append({"p": r["_lam"] / tot * (1 - math.exp(-tot)), "implied": c[2] / 100.0, "price": c[1], "won": int(injury_status.key(scorer) == injury_status.key(r["name"] or ""))})
    return res


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
    fit_rows = []
    for s_ in (2023, 2024, 2025):
        fit_rows += nb.run(s_, a.cache)
    lm = lane_markets(a, rows, oc, fit_rows)
    if lm:
        print(f"\nNHL lines (points / assists / shots on goal), 2026 to date, fit on 2023-25: market, n, went over, book implied, ours, log loss book/ours, over bets at 4+ pts (n, hit, ROI), CLV")
        for om in LANE_MK:
            rs = [r for r in lm if r["market"] == om]
            if len(rs) < 20:
                print(f"  {om:<22}{len(rs)} lines (too few)"); continue
            y = np.array([r["over"] for r in rs], float); pb = np.clip([r["implied"] for r in rs], 1e-3, 1 - 1e-3); pm = np.clip([r["model"] for r in rs], 1e-3, 1 - 1e-3)
            ll_ = lambda p: -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
            sel = [r for r in rs if r["model"] - r["implied"] >= 0.04]
            prof = sum(((r["price"] / 100.0 if r["price"] > 0 else 100.0 / -r["price"]) if r["over"] else -1.0) for r in sel)
            c = LT.clv_summary(rs)
            print(f"  {om:<22}{len(rs):>4}  over {100 * y.mean():4.0f}%  book {100 * np.mean(pb):4.1f}%  ours {100 * np.mean(pm):4.1f}%  logloss {ll_(pb):.3f}/{ll_(pm):.3f}  bets {len(sel)}" + (f", {100 * np.mean([r['over'] for r in sel]):.0f}% hit, {100 * prof / len(sel):+.0f}%" if sel else "") +
                  (f"  CLV {c['avg_clv_pts']:+.1f} pts ({c['n']} picks)" if c else ""))
    try:
        fg = first_goal_test(rows, oc_first := oc, model, nhl_live)
    except Exception as exc:     # noqa: BLE001
        fg = []; print(f"first goal test skipped ({type(exc).__name__}: {exc})")
    if len(fg) >= 50:
        y = np.array([r["won"] for r in fg], float); pm = np.array([r["p"] for r in fg]); pb = np.array([r["implied"] for r in fg])
        sel = [r for r in fg if r["p"] - r["implied"] >= 0.01]
        prof = sum((r["price"] / 100.0 if r["won"] else -1.0) for r in sel)
        print(f"\nNHL first goal, 2026 to date: {len(fg)} priced skater-games ({int(y.sum())} first goals); book implied {100 * pb.mean():.2f}% on average (vig and long-shot shading), ours {100 * pm.mean():.2f}%, actual {100 * y.mean():.2f}%")
        print(f"  backing the first goal when we are 1+ point above the price: {len(sel)} bets, {sum(r['won'] for r in sel)} hit, return {100 * prof / max(1, len(sel)):+.0f}% per $1")
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
