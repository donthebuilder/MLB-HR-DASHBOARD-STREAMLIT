#!/usr/bin/env python3
"""
nba_line_edge.py -- OUR probability vs THE BOOK'S LINE for NBA player props (points, rebounds, assists, threes, points+rebounds, points+assists), on the daily prop
snapshots the NBA bot publishes (nba_odds_<date>.json: {player name: {market: [line, over price, implied]}}).

    python bots/nba_line_edge.py [--cache DIR] [--odds DIR]

Same method as nfl_line_edge.py: a logistic per threshold (the model's own trailing features), then P(stat > line) read off at the book's line. Fit on 2023-26 games BEFORE
the graded night's season-to-date (2023-25 for the 2026 games graded here); graded on every 2026 game that has a pregame line. Reports lines graded, share over, the book's
implied probability, ours, log loss of each, and the return of betting the OVER at the posted price when our edge is >= 4 / 8 points. Small sample until the odds log grows.
"""
import argparse, glob, json, math, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb       # noqa: E402
import game_lines as G              # noqa: E402
import injury_status                # noqa: E402
from nba_live import athlete        # noqa: E402
from tune_models import fit_logistic    # noqa: E402

MK = {
    "player_points": (lambda r: r["raw"]["pts"], list(range(6, 42, 2)), ["pts"]),
    "player_rebounds": (lambda r: r["raw"]["reb"], list(range(1, 15)), ["reb"]),
    "player_assists": (lambda r: r["raw"]["ast"], list(range(1, 13)), ["ast"]),
    "player_threes": (lambda r: r["raw"]["tpm"], list(range(1, 8)), ["3pm"]),
    "player_pts_reb": (lambda r: r["raw"]["pts"] + r["raw"]["reb"], list(range(8, 52, 3)), ["pts", "reb"]),
    "player_pts_ast": (lambda r: r["raw"]["pts"] + r["raw"]["ast"], list(range(8, 52, 3)), ["pts", "ast"]),
}


def featvec(r, parts):
    out = {}
    for p in parts:
        f = r["f"] if p == "pts" else (r.get("lanes") or {}).get(p, {}).get("f")
        if f is None:
            return None
        out.update({f"{p}:{k}": v for k, v in f.items()})
    return out


def p_over(ths, probs, line):
    k = math.floor(line) + 1
    pr = np.minimum.accumulate(np.array(probs))
    return float(pr[0] if k <= ths[0] else pr[-1] if k >= ths[-1] else np.interp(k, ths, pr))


def profit(price):
    return price / 100.0 if price > 0 else 100.0 / -price


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba")); ap.add_argument("--odds", required=True); a = ap.parse_args()
    ld = Path(a.cache).parent / "nba_lines"
    hist = [r for s in (2023, 2024, 2025) for r in bb.run(s, a.cache) if r.get("lanes")]
    gs = sorted(bb.load(2025, a.cache) + bb.load(2026, a.cache), key=lambda g: (g["date"], g["id"]))
    ids26 = {g["id"] for g in bb.load(2026, a.cache)}
    cur = [r for r in bb.run_games(gs, 2026, lambda gid: G.close_line("nba", gid, ld) if ld.exists() else None, only_ids=ids26) if r.get("lanes")]
    print(f"{len(hist)} training player-games (2023-25); {len(cur)} 2026 player-games with features")
    import line_tools as LT
    oc = LT.open_close(a.odds, "nba", lambda d: d.get("date"), injury_status.key)
    lines = {k: {"open": v["open"][1], "close": v["close"][1], "moved": v["open"][0] != v["close"][0]} for k, v in oc.items()}
    names = {}
    for r in cur:
        if r["pid"] not in names:
            names[r["pid"]] = injury_status.key(athlete(r["pid"], a.cache)["name"] or "")
    res = []
    for om, (out, ths, parts) in MK.items():
        tr = [(featvec(r, parts), out(r)) for r in hist]
        tr = [(f, y) for f, y in tr if f is not None]
        keys = sorted(tr[0][0]); X = np.array([[f[k] for k in keys] for f, _ in tr], float); y = np.array([v for _, v in tr], float)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        W = [fit_logistic((X - mu) / sd, (y >= t).astype(float), l2=20.0) for t in ths]
        for r in cur:
            oc_ = lines.get((r["night"], names.get(r["pid"]), om))
            fv = featvec(r, parts)
            if not oc_ or fv is None:
                continue
            ln = oc_["close"]
            x = (np.array([fv[k] for k in keys], float) - mu) / sd
            probs = [1 / (1 + math.exp(-(x @ w[1:] + w[0]))) for w in W]
            yv = out(r)
            if yv == ln[0]:
                continue
            of = LT.fair_over(oc_["open"][1], oc_["open"][3] if len(oc_["open"]) > 3 else None)[0]
            cf = LT.fair_over(ln[1], ln[3] if len(ln) > 3 else None)[0]
            res.append({"market": om, "line": ln[0], "price": ln[1], "implied": ln[2] / 100.0, "open_fair": of, "close_fair": cf, "model": p_over(ths, probs, ln[0]), "over": int(yv > ln[0])})
    print(f"\n{'market':<18}{'n':>5}{'went over':>11}{'book implied':>14}{'our p':>8}{'logloss book/ours':>20}   over bets at edge >=4 / >=8 pts (n, hit, ROI)")
    for om in MK:
        rs = [r for r in res if r["market"] == om]
        if len(rs) < 15:
            print(f"{om:<18}{len(rs):>5}  (too few to read)"); continue
        y = np.array([r["over"] for r in rs], float); pb = np.clip([r["implied"] for r in rs], 1e-3, 1 - 1e-3); pm = np.clip([r["model"] for r in rs], 1e-3, 1 - 1e-3)
        ll = lambda p: -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
        bets = []
        for thr in (0.04, 0.08):
            sel = [r for r in rs if r["model"] - r["implied"] >= thr]
            bets.append(f"{len(sel)}, {100 * sum(r['over'] for r in sel) / len(sel):.0f}%, {100 * sum((profit(r['price']) if r['over'] else -1.0) for r in sel) / len(sel):+.0f}%" if sel else "0")
        print(f"{om:<18}{len(rs):>5}{100 * y.mean():>10.0f}%{100 * np.mean(pb):>13.1f}%{100 * np.mean(pm):>7.1f}%{ll(pb):>12.3f}/{ll(pm):.3f}   {' | '.join(bets)}")
    print("\nClosing-line value (picks at >= 4 pts of edge vs the opening price):")
    for om in MK:
        c = LT.clv_summary([r for r in res if r["market"] == om])
        print(f"  {om:<18}" + (f"{c['n']} picks, market moved our way {100 * c['moved_our_way']:.0f}% / against {100 * c['moved_against']:.0f}%, average CLV {c['avg_clv_pts']:+.1f} pts" if c else "no picks with an opening and a closing price yet"))
    json.dump(res, open(Path(a.odds) / "nba_line_edge_results.json", "w"))


if __name__ == "__main__":
    main()
