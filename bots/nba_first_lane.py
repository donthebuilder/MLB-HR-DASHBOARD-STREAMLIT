#!/usr/bin/env python3
"""
nba_first_lane.py -- NBA FIRST BASKET as a calibrated probability for every starter, plus a top-3-per-game flag.

    python bots/nba_first_lane.py [--cache DIR]

Rows are nba_first_study.build (the ten pre-game starters, trailing within the season, 8+ games), plus the book's closing spread and total for the game.
Fit on 2023-24, graded once on 2025-26. A first basket is scored by exactly one player, so after the logistic each game's probabilities are rescaled to sum
to the share of first baskets a starter takes (about 96%): that is what makes the numbers comparable with a price. Reports the top pick / top 3 per game
against the plain usage and points-per-game picks, log loss, and calibration by band, then saves models/nba_first_v1.json.
"""
import argparse, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nba_first_study as fs        # noqa: E402
import game_lines as G              # noqa: E402
from tune_models import fit_logistic   # noqa: E402

COLS = ["ppg", "mpg", "fga_share", "early_share", "early_per_game", "first_rate", "team_first_rate", "opp_allows_first", "is_home", "book_fav", "book_total"]


def with_lines(rows, lines_dir):
    cache = {}
    for r in rows:
        if r["game"] not in cache:
            cache[r["game"]] = G.close_line("nba", r["game"], lines_dir) if lines_dir.exists() else None
        ln = cache[r["game"]]
        ok = bool(ln and ln.get("spread_home") is not None)
        r["book_fav"] = (-ln["spread_home"] if r["is_home"] else ln["spread_home"]) if ok else 0.0
        r["book_total"] = float(ln.get("total") or 0.0) if ok else 0.0
    return rows


def rescale(rows, z, share):
    p = 1 / (1 + np.exp(-z))
    by = defaultdict(list)
    for i, r in enumerate(rows):
        by[r["game"]].append(i)
    out = np.zeros(len(rows))
    for ix in by.values():
        out[ix] = p[ix] / p[ix].sum() * share
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba")); a = ap.parse_args()
    rows = with_lines(fs.build(a.cache), Path(a.cache).parent / "nba_lines")
    tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
    share = sum(r["y"] for r in tr) / (len(tr) / 10.0)          # first baskets that went to a starter, per game
    X = lambda rs: np.array([[r[c] for c in COLS] for r in rs], float)
    Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = fit_logistic((Xtr - mu) / sd, np.array([r["y"] for r in tr], float), l2=20.0)
    z = ((X(te) - mu) / sd) @ w[1:] + w[0]
    p = rescale(te, z, share)
    y = np.array([r["y"] for r in te], float)
    print(f"{len(te)} starter-games graded (2025-26); base {100 * y.mean():.1f}% per starter; fit {len(tr)} (2023-24); starters took {100 * share:.0f}% of first baskets")
    games = defaultdict(list)
    for i, r in enumerate(te):
        games[r["game"]].append(i)
    games = {k: v for k, v in games.items() if len(v) >= 8}
    def pick(score):
        t1 = t3 = 0
        for ix in games.values():
            o = sorted(ix, key=lambda i: score[i], reverse=True)
            t1 += y[o[0]]; t3 += y[o[:3]].sum()
        n = len(games); return 100 * t1 / n, 100 * t3 / n
    for label, sc in (("points per game", np.array([r["ppg"] for r in te])), ("usage (FGA share)", np.array([r["fga_share"] for r in te])), ("fitted model", p)):
        a1, a3 = pick(sc); print(f"  {label:<20} top pick {a1:5.1f}%   top 3 per game {a3:5.1f}%  (random 10% / 30%)")
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    print(f"  fitted log loss {-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)):.4f} vs flat {-np.mean(y * math.log(y.mean()) + (1 - y) * math.log(1 - y.mean())):.4f}")
    o = np.argsort(p); k = len(o) // 8
    print("  calibration by eighth (pred>actual %): " + " ".join(f"{100 * p[o[i * k:(i + 1) * k if i < 7 else None]].mean():.1f}>{100 * y[o[i * k:(i + 1) * k if i < 7 else None]].mean():.1f}" for i in range(8)))
    print("  weights: " + ", ".join(f"{c} {v:+.2f}" for c, v in sorted(zip(COLS, w[1:]), key=lambda t: -abs(t[1]))[:6]))
    # production: all seasons, share from all, saved
    Xa = X(rows); amu, asd = Xa.mean(0), Xa.std(0) + 1e-9
    aw = fit_logistic((Xa - amu) / asd, np.array([r["y"] for r in rows], float), l2=20.0)
    ash = sum(r["y"] for r in rows) / (len(rows) / 10.0)
    out = Path(__file__).resolve().parent / "models" / "nba_first_v1.json"
    out.write_text(json.dumps({"model_version": "nba_first_v1", "target": "first basket, pre-game starters", "features": COLS, "mu": amu.tolist(), "sd": asd.tolist(),
                               "intercept": float(aw[0]), "weights": aw[1:].tolist(), "starter_share": ash, "fit_on": f"{len(rows)} starter-games, 2023-25"}, indent=1))
    print(f"  saved {out}")


if __name__ == "__main__":
    main()
