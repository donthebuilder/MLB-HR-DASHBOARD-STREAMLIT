#!/usr/bin/env python3
"""
watchlist_check.py -- your TUDDY watchlist (the CSV the site exports) against OUR model and the book's real line, on the market you PINNED for each player.

    python bots/nfl/watchlist_check.py WATCHLIST_CSV ODDS_DIR [--season 2026 --week 7] [--cache DIR]

The watchlist has the pinned market per player (TD / REC_YDS / REC / RUSH_YDS / PASS_YDS), TUDDY's own score and status (CALLED / ON THE BOARD / NOT ON THE BOARD). For each row this finds the book's
line and price for that market (the latest pregame snapshot in ODDS_DIR) and gives OUR chance of going over it (threshold models fit on every earlier game, week 7 never seen), the book's implied chance and
the gap. TD = anytime TD (our fitted probability vs the price). Lines are the book's, not necessarily the one you took.
"""
import argparse, csv, json, math, sys, unicodedata, re
from pathlib import Path
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import nfl_lanes as L                 # noqa: E402
import nfl_line_edge as E             # noqa: E402
import nfl_features as F              # noqa: E402
import nfl_scoring as S               # noqa: E402
import nfl_more as NM                 # noqa: E402
from tune_models import fit_logistic  # noqa: E402

MARKET = {"REC_YDS": ("player_reception_yds", "REC_YDS", E.MK["player_reception_yds"][2]), "REC": ("player_receptions", "REC", E.MK["player_receptions"][2]),
          "RUSH_YDS": ("player_rush_yds", "RUSH_YDS", E.MK["player_rush_yds"][2]), "PASS_YDS": ("player_pass_yds", "PASS_YDS", E.MK["player_pass_yds"][2]),
          "RUSH_ATT": ("player_rush_attempts", "RUSH_ATT", E.MK["player_rush_attempts"][2]), "TD": ("player_anytime_td", "TD1", [1])}
norm = lambda s: " ".join(re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()).replace(" jr", "").replace(" iii", "").split())


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("watchlist"); ap.add_argument("odds"); ap.add_argument("--season", type=int, default=2026); ap.add_argument("--week", type=int, default=7)
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl")); a = ap.parse_args()
    wl = list(csv.DictReader(open(a.watchlist)))
    lines = E.load_lines(a.odds, {a.week})
    NM.register(); T = NM.augment(a.cache)
    slate, _, _ = F.upcoming_rows(a.season, a.week)
    slate = NM.__dict__.get("with_extras", lambda s, c: s)(slate, a.cache) if False else slate
    ex = NM.live_extras(a.cache)
    d0 = S.derive(slate).with_columns([pl.col("player_id").map_elements(lambda p, k=k: (ex.get(p) or {}).get(k, 0.0), return_dtype=pl.Float64).alias(k) for k in ("f_int", "f_sacks", "f_longest")])
    names = {norm(r.get("player_display_name") or r.get("player_name")): r["player_id"] for r in d0.iter_rows(named=True)}
    cache = {}
    def probs(mk, ths):
        if mk in cache:
            return cache[mk]
        cols = L.feats(mk)
        D = {s: L.prep(T[s], mk) for s in T}
        D = {s: x.filter(pl.col("f_gp") >= 3) if "f_gp" in x.columns else x for s, x in D.items()}
        tr = pl.concat([D[s] for s in D if s < a.season] + [D[a.season].filter(pl.col("week") < a.week)], how="diagonal_relaxed")
        Xtr = L.matrix(tr, cols); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        ytr = tr.with_columns(L.OUT[mk].alias("_y"))["_y"].to_numpy().astype(float)
        ws = [fit_logistic((Xtr - mu) / sd, (ytr >= t).astype(float), l2=20.0) for t in ths]
        sl = d0.filter(pl.col("position").is_in(L.MARKETS[mk]["pos"]))
        Xs = (L.matrix(sl, cols) - mu) / sd
        P = np.array([1 / (1 + np.exp(-(Xs @ w[1:] + w[0]))) for w in ws]).T
        cache[mk] = {r["player_id"]: P[i] for i, r in enumerate(sl.iter_rows(named=True))}
        return cache[mk]
    print(f"{'player':<20}{'pos':<4}{'game':<10}{'pinned':<9}{'status':<17}{'TUDDY':>6} | {'book line':>9}{'price':>7}{'implied':>9} | {'ours':>6}{'gap':>7}")
    out = []
    for r in wl:
        pid = names.get(norm(r["Player"]))
        pm = r["Pinned market"]
        if pm not in MARKET or not pid:
            print(f"{r['Player']:<20}{r['Pos']:<4}{r['Game']:<10}{pm:<9}{r['Status']:<17}{r['Pinned score']:>6} | (not scored: {'player not on the slate table' if not pid else 'market has no line model'})"); continue
        om, mk, ths = MARKET[pm]
        P = probs(mk, ths).get(pid)
        oc = lines.get((a.week, pid, om))
        if P is None:
            print(f"{r['Player']:<20}{r['Pos']:<4}{r['Game']:<10}{pm:<9}{r['Status']:<17}{r['Pinned score']:>6} | (no model row: position outside the market)"); continue
        if pm == "TD":
            ours = float(P[0])
        elif oc:
            ours = E.p_over(ths, P, oc["close"][0])
        else:
            ours = None
        if oc:
            line, price, imp = oc["close"][0], oc["close"][1], oc["close"][2] / 100.0
            gap = f"{100 * (ours - imp):+.1f}" if ours is not None else "-"
            print(f"{r['Player']:<20}{r['Pos']:<4}{r['Game']:<10}{pm:<9}{r['Status']:<17}{r['Pinned score']:>6} | {line:>9g}{price:>+7d}{100 * imp:>8.1f}% | {100 * ours:>5.1f}%{gap:>7}")
            out.append({"player": r["Player"], "market": pm, "line": line, "price": price, "implied": imp, "ours": ours, "status": r["Status"]})
        else:
            print(f"{r['Player']:<20}{r['Pos']:<4}{r['Game']:<10}{pm:<9}{r['Status']:<17}{r['Pinned score']:>6} | {'no line':>9}{'':>7}{'':>9} | " + (f"{100 * ours:>5.1f}% (bar {'1+ TD' if pm == 'TD' else ''})" if ours is not None else ""))
    if out:
        gaps = [o["ours"] - o["implied"] for o in out if o["ours"] is not None]
        by = {}
        for o in out:
            by.setdefault(o["status"], []).append(o["ours"] - o["implied"])
        print(f"\n{len(gaps)} priced pins: average gap (ours - book) {100 * np.mean(gaps):+.1f} pts; ours above the book on {sum(g > 0 for g in gaps)}")
        for k, v in by.items():
            print(f"  {k:<17}{len(v):>3} pins, average gap {100 * np.mean(v):+.1f}")


if __name__ == "__main__":
    main()
