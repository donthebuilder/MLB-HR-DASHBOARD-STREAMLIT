#!/usr/bin/env python3
"""
nfl_line_edge.py -- OUR probability vs THE BOOK'S LINE, on the real player-prop lines the NFL bot snapshots every day (data branch: nfl_odds_<date>.json,
{player_id: {market: [line, over price, implied]}}, since 2026-10-16).

    python bots/nfl/nfl_line_edge.py [--cache DIR] [--odds DIR] [--weeks 6 7]

For each market, thresholds are fitted as separate logistics (the same trailing features as nfl_lanes.py), then P(stat > line) is read off at the book's own line by
interpolating between thresholds. Fit on 2023-25 (and 2026 weeks BEFORE the graded one), graded on 2026 weeks with lines. Reported per market: lines graded, the share
that went over, the book's average implied probability, ours, log loss of each, and what betting the OVER whenever our edge >= 4 / 8 points would have returned at the
posted price (only the over is priced in the snapshot, so unders are not evaluated). A month of lines is a small sample: this is the measurement harness, and the same
code re-runs as the snapshots accumulate (the snapshots are also copied to bbe-history by nfl-odds-log.yml so they are never lost).
"""
import argparse, glob, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import nfl_scoring as S                 # noqa: E402
import line_tools as LT                 # noqa: E402
import nfl_lanes as L                   # noqa: E402
from tune_models import fit_logistic    # noqa: E402

# odds market -> (our market key, outcome column expr, thresholds, positions)
MK = {
    "player_reception_yds": ("REC_YDS", lambda r: r["receiving_yards"], [3, 6, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100]),
    "player_receptions": ("REC", lambda r: r["receptions"], [1, 2, 3, 4, 5, 6, 7, 8]),
    "player_rush_yds": ("RUSH_YDS", lambda r: r["rushing_yards"], [3, 6, 10, 15, 20, 30, 40, 50, 60, 70, 80, 100]),
    "player_rush_attempts": ("RUSH_ATT", lambda r: r["carries"], [1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20]),
    "player_pass_yds": ("PASS_YDS", lambda r: r["passing_yards"], [150, 175, 200, 225, 250, 275, 300, 325]),
    "player_kicking_points": ("KICK_PTS", lambda r: (r["fg_made"] or 0) * 3 + (r["pat_made"] or 0), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
}


def american_profit(price):
    return price / 100.0 if price > 0 else 100.0 / -price


def load_lines(odds_dir, weeks):
    """{(week, player, market): {"open": [...], "close": [...]}}: first and last pregame snapshot (line_tools.open_close)."""
    import line_tools as LT
    oc = LT.open_close(odds_dir, "nfl", lambda d: d.get("week") if d.get("week") in weeks else None, lambda k: k)
    return {k: {"open": v["open"][1], "close": v["close"][1]} for k, v in oc.items()}


def p_over(ths, probs, line):
    k = math.floor(line) + 1                           # x.5 line: over = stat >= ceil(line)
    pr = np.minimum.accumulate(np.array(probs))
    if k <= ths[0]:
        return float(pr[0])
    if k >= ths[-1]:
        return float(pr[-1])
    return float(np.interp(k, ths, pr))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl")); ap.add_argument("--odds", required=True); ap.add_argument("--weeks", nargs="+", type=int, default=[6, 7])
    a = ap.parse_args()
    lines = load_lines(a.odds, set(a.weeks))
    T = {s: L.table(s, a.cache) for s in (2023, 2024, 2025, 2026)}
    print(f"{len(lines)} pregame lines in weeks {a.weeks}")
    results = []
    for om, (mk, out, ths) in MK.items():
        cols = L.feats(mk)
        D = {s: L.prep(T[s], mk) for s in T}
        D = {s: d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d for s, d in D.items()}
        test26 = D[2026]
        for wk in a.weeks:
            te = test26.filter(pl.col("week") == wk)
            if not te.height:
                continue
            tr = pl.concat([D[2023], D[2024], D[2025]] + ([test26.filter(pl.col("week") < wk)] if wk > 1 else []), how="diagonal_relaxed")
            Xtr = L.matrix(tr, cols); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            Xte = (L.matrix(te, cols) - mu) / sd
            ytr = tr.with_columns(L.OUT[mk].alias("_y"))["_y"].to_numpy().astype(float)
            probs = []
            for t in ths:
                w = fit_logistic((Xtr - mu) / sd, (ytr >= t).astype(float), l2=20.0)
                probs.append(1 / (1 + np.exp(-(Xte @ w[1:] + w[0]))))
            probs = np.array(probs).T                                  # rows x thresholds
            for i, r in enumerate(te.iter_rows(named=True)):
                oc = lines.get((wk, r["player_id"], om))
                if not oc:
                    continue
                ln = oc["close"]
                line, price, implied = ln[0], ln[1], ln[2]
                under = ln[3] if len(ln) > 3 else None
                of = LT.fair_over(oc["open"][1], oc["open"][3] if len(oc["open"]) > 3 else None)[0]
                cf, devigged = LT.fair_over(price, under)
                y = out(r)
                if y is None or y == line:
                    continue
                results.append({"market": om, "week": wk, "line": line, "price": price, "under_price": under, "implied": implied / 100.0, "open_fair": of, "close_fair": cf, "devigged": devigged,
                                "line_moved": oc["close"][0] != oc["open"][0], "model": p_over(ths, probs[i], line), "over": int(y > line)})
    # anytime TD (price only) and longest reception (a line), from the new-market models
    import nfl_more as NM
    NM.register(); TA = NM.augment(a.cache)
    for om, mk, ths, outf in (("player_anytime_td", "TD1", [1], lambda r: (r["rushing_tds"] or 0) + (r["receiving_tds"] or 0)),
                              ("player_longest_reception", "LONG_REC", [8, 12, 16, 20, 25, 30, 40, 50, 60], lambda r: r["longest_rec"])):
        cols = L.feats(mk)
        D = {s_: L.prep(TA[s_], mk) for s_ in TA}
        D = {s_: d.filter(pl.col("f_gp") >= 3) if "f_gp" in d.columns else d for s_, d in D.items()}
        for wk in a.weeks:
            te = D[2026].filter(pl.col("week") == wk)
            if not te.height:
                continue
            tr = pl.concat([D[2023], D[2024], D[2025]] + [D[2026].filter(pl.col("week") < wk)], how="diagonal_relaxed")
            Xtr = L.matrix(tr, cols); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            Xte = (L.matrix(te, cols) - mu) / sd
            ytr = tr.with_columns(L.OUT[mk].alias("_y"))["_y"].to_numpy().astype(float)
            probs = np.array([1 / (1 + np.exp(-(Xte @ fit_logistic((Xtr - mu) / sd, (ytr >= t).astype(float), l2=20.0)[1:] + fit_logistic((Xtr - mu) / sd, (ytr >= t).astype(float), l2=20.0)[0]))) for t in ths]).T
            for i, r in enumerate(te.iter_rows(named=True)):
                oc = lines.get((wk, r["player_id"], om))
                if not oc:
                    continue
                ln = oc["close"]; line, price, implied = ln[0], ln[1], ln[2]
                y = outf(r)
                if y is None or y == line:
                    continue
                of = LT.fair_over(oc["open"][1], oc["open"][3] if len(oc["open"]) > 3 else None)[0]
                cf, devigged = LT.fair_over(price, ln[3] if len(ln) > 3 else None)
                pm = float(probs[i][0]) if om == "player_anytime_td" else p_over(ths, probs[i], line)
                results.append({"market": om, "week": wk, "line": line, "price": price, "implied": implied / 100.0, "open_fair": of, "close_fair": cf, "model": pm, "over": int(y > line if om != "player_anytime_td" else y >= 1)})
    print()
    print(f"{'market':<24}{'n':>5}{'went over':>11}{'book implied':>14}{'our p':>8}{'logloss book/ours':>20}   over bets at edge >=4pts / >=8pts (n, hit, ROI)")
    for om in list(MK) + ["player_anytime_td", "player_longest_reception"]:
        rs = [r for r in results if r["market"] == om]
        if len(rs) < 15:
            print(f"{om:<24}{len(rs):>5}  (too few to read)"); continue
        y = np.array([r["over"] for r in rs], float); pb = np.clip([r["implied"] for r in rs], 1e-3, 1 - 1e-3); pm = np.clip([r["model"] for r in rs], 1e-3, 1 - 1e-3)
        ll = lambda p: -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
        bets = []
        for thr in (0.04, 0.08):
            sel = [r for r in rs if r["model"] - r["implied"] >= thr]
            prof = sum((american_profit(r["price"]) if r["over"] else -1.0) for r in sel)
            bets.append(f"{len(sel)}, {100 * sum(r['over'] for r in sel) / len(sel):.0f}%, {100 * prof / len(sel):+.0f}%" if sel else "0")
        print(f"{om:<24}{len(rs):>5}{100 * y.mean():>10.0f}%{100 * np.mean(pb):>13.1f}%{100 * np.mean(pm):>7.1f}%{ll(pb):>12.3f}/{ll(pm):.3f}   {' | '.join(bets)}")
    print("\nClosing-line value: our over/under picks at >= 4 pts of edge vs the OPENING fair price (needs the over and under prices; lines saved before 2026-10-24 have only the over):")
    for om in list(MK) + ["player_anytime_td", "player_longest_reception"]:
        c = LT.clv_summary([r for r in results if r["market"] == om])
        print(f"  {om:<24}" + (f"{c['n']} picks, market moved our way {100 * c['moved_our_way']:.0f}% / against {100 * c['moved_against']:.0f}%, average CLV {c['avg_clv_pts']:+.1f} pts" if c else "no picks with an opening and a closing price yet"))
    json.dump(results, open(Path(a.odds) / "line_edge_results.json", "w"))


if __name__ == "__main__":
    main()
