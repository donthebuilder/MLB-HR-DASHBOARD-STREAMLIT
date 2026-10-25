#!/usr/bin/env python3
"""
team_models.py -- MONEYLINE and SPREAD models for NBA, NHL and NFL, graded against the closing line.

    python bots/team_models.py nba|nhl|nfl [--cache DIR] [--save]

Per game (oldest first, only information from before the game):
  ELO     team ratings updated by margin of victory, home-field edge, a pull toward the mean at each new season (tuned on the training seasons only)
  FORM    average margin over the last 10 games, rest days, back-to-back, home
  MODEL   ridge regression of the home margin on (Elo gap, form gap, rest gap, back-to-backs); win probability = Normal(expected margin, residual sd)
Fit on the earlier seasons, graded once on the held-out one (NBA/NHL: 2025-26; NFL: 2025 then 2026 weeks to date). Reported against the CLOSING line on the same
games: win-probability log loss (model vs the book's no-vig moneyline), spread error (model vs the book's spread), and what happens when the model and the book
disagree: how often the model's side covers / wins. The closing line is the hardest benchmark there is; a model that is close to it is a good model.
--save writes models/<sport>_team_v1.json (ratings as of today, sd, regression weights) for the live games.
"""
import argparse, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_lines as G              # noqa: E402
from tune_models import fit_logistic    # noqa: E402

CFG = {"nba": {"hfa": (60, 90, 120), "k": (8, 12, 18, 25), "scale": 12.0, "revert": 0.25},
       "nhl": {"hfa": (20, 40, 60), "k": (4, 8, 12), "scale": 2.2, "revert": 0.35},
       "nfl": {"hfa": (30, 48, 65), "k": (10, 16, 24), "scale": 13.5, "revert": 0.33}}


# ---------------------------------------------------------------- data: one list of games per sport, oldest first
def games_nba(cache):
    import buckets_backtest as bb
    ld = Path(cache).parent / "nba_lines"
    ld.mkdir(parents=True, exist_ok=True)
    allg = [g for s in (2023, 2024, 2025, 2026) for g in bb.load(s, cache)]
    G.fetch_closing("nba", [g["id"] for g in allg], ld)                 # parallel; each line is cached on disk once fetched
    out = []
    for s in (2023, 2024, 2025, 2026):
        for g in bb.load(s, cache):
            a, b = g["teams"]
            h, aw = (a, b) if a["home"] else (b, a)
            hp, ap = sum(p["pts"] for p in h["players"]), sum(p["pts"] for p in aw["players"])
            ln = G.close_line("nba", g["id"], ld) if ld.exists() else None
            out.append({"season": s, "date": g["date"], "id": g["id"], "home": h["abbr"], "away": aw["abbr"], "hs": hp, "as": ap, "line": ln})
    return sorted(out, key=lambda x: (x["date"], x["id"]))


def games_nhl(cache):
    import nhl_backtest as nb
    if not (Path(cache) / "espn_nhl_games.json").exists():
        import nhl_buildout
        nhl_buildout.pull(Path(cache))                                   # date|away|home -> ESPN event id (for the closing lines)
    ids = json.load(open(Path(cache) / "espn_nhl_games.json")) if (Path(cache) / "espn_nhl_games.json").exists() else {}
    out = []
    (Path(cache) / "nhl_lines").mkdir(parents=True, exist_ok=True)
    lines = G.fetch_closing("nhl", list(ids.values()), Path(cache) / "nhl_lines") if ids else {}
    for s in (2023, 2024, 2025, 2026):
        d = nb.load_season(s, cache)
        by = defaultdict(lambda: {"goals": defaultdict(float), "home": None, "date": None})
        for r in d["skater/summary"]:
            x = by[r["gameId"]]
            x["goals"][r["teamAbbrev"]] += r["goals"]; x["date"] = r["gameDate"]
            if r.get("homeRoad") == "H":
                x["home"] = r["teamAbbrev"]
        for gid, x in by.items():
            if x["home"] is None or len(x["goals"]) != 2:
                continue
            away = [t for t in x["goals"] if t != x["home"]][0]
            eid = ids.get(f"{x['date']}|{away}|{x['home']}")
            ln = lines.get(eid) if eid else None
            out.append({"season": s, "date": x["date"], "id": gid, "home": x["home"], "away": away, "hs": x["goals"][x["home"]], "as": x["goals"][away], "line": ln})
    return sorted(out, key=lambda x: (x["date"], x["id"]))


def games_nfl(cache):
    import nflreadpy as nfl
    out = []
    for r in nfl.load_schedules([2023, 2024, 2025, 2026]).iter_rows(named=True):
        if r["game_type"] != "REG" or r["home_score"] is None:
            continue
        ln = {"spread_home": -float(r["spread_line"]), "ml_home": r["home_moneyline"], "ml_away": r["away_moneyline"], "total": r["total_line"]} if r["spread_line"] is not None else None
        out.append({"season": r["season"], "date": str(r["gameday"]), "id": r["game_id"], "home": r["home_team"], "away": r["away_team"], "hs": r["home_score"], "as": r["away_score"], "line": ln,
                    "rest_h": r.get("home_rest"), "rest_a": r.get("away_rest")})
    return sorted(out, key=lambda x: (x["date"], x["id"]))


# ---------------------------------------------------------------- elo + form features
def build(games, sport, k, hfa):
    cfg = CFG[sport]
    elo = defaultdict(lambda: 1500.0); last = {}; hist = defaultdict(list); cur = None
    rows = []
    for g in games:
        if g["season"] != cur:
            for t in list(elo):
                elo[t] = 1500 + (elo[t] - 1500) * (1 - cfg["revert"])
            cur = g["season"]
        h, a = g["home"], g["away"]
        gap = elo[h] - elo[a] + hfa
        def rest(t):
            if t not in last:
                return 7.0
            import datetime as dt
            return float(min(10, (dt.date.fromisoformat(g["date"]) - dt.date.fromisoformat(last[t])).days))
        rh, ra = (g.get("rest_h") or rest(h), g.get("rest_a") or rest(a)) if sport == "nfl" else (rest(h), rest(a))
        f10 = lambda t: sum(hist[t][-10:]) / len(hist[t][-10:]) if hist[t] else 0.0
        rows.append({**g, "elo_gap": gap, "form_gap": f10(h) - f10(a), "rest_gap": rh - ra, "b2b_h": float(rh <= 1), "b2b_a": float(ra <= 1), "ngames": min(len(hist[h]), len(hist[a])),
                     "margin": g["hs"] - g["as"], "win": int(g["hs"] > g["as"]) if g["hs"] != g["as"] else 0.5})
        m = g["hs"] - g["as"]
        exp = 1 / (1 + 10 ** (-gap / 400)); res = 1.0 if m > 0 else 0.0 if m < 0 else 0.5
        mult = ((abs(m) + 3) ** 0.8) / (7.5 + 0.006 * abs(gap)) if sport != "nhl" else (abs(m) + 1) ** 0.6
        d = k * mult * (res - exp)
        elo[h] += d; elo[a] -= d
        hist[h].append(m); hist[a].append(-m)
        last[h] = last[a] = g["date"]
    return rows, elo


def novig_home(ln):
    if ln and ln.get("ml_home") and ln.get("ml_away"):
        try:
            return G.novig(ln["ml_home"], ln["ml_away"])
        except Exception:    # noqa: BLE001
            return None
    return None


def ll(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def Phi(x):
    return 0.5 * (1 + np.vectorize(math.erf)(x / math.sqrt(2)))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("sport", choices=CFG); ap.add_argument("--cache", default=None); ap.add_argument("--save", action="store_true"); a = ap.parse_args()
    sport = a.sport
    cache = a.cache or str(Path.home() / {"nba": ".cache/dash_nba", "nhl": ".cache/dash_nhl", "nfl": ".cache/dash_nfl"}[sport])
    games = {"nba": games_nba, "nhl": games_nhl, "nfl": games_nfl}[sport](cache)
    cfg = CFG[sport]
    tr_s = [2023, 2024]
    # Elo parameters: chosen on 2024 given 2023 only (never on the held-out season)
    best = None
    for k in cfg["k"]:
        for hfa in cfg["hfa"]:
            rows, _ = build(games, sport, k, hfa)
            sel = [r for r in rows if r["season"] == 2024 and r["ngames"] >= 5 and r["win"] != 0.5]
            p = 1 / (1 + 10 ** (-np.array([r["elo_gap"] for r in sel]) / 400)); score = ll(p, np.array([r["win"] for r in sel], float))
            if best is None or score < best[0]:
                best = (score, k, hfa)
    _, k, hfa = best
    rows, elo = build(games, sport, k, hfa)
    cols = ["elo_gap", "form_gap", "rest_gap", "b2b_h", "b2b_a"]
    X = lambda rs: np.array([[r[c] for c in cols] for r in rs], float)
    tr = [r for r in rows if r["season"] in tr_s and r["ngames"] >= 5]
    Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    A = np.hstack([np.ones((len(tr), 1)), (Xtr - mu) / sd]); y = np.array([r["margin"] for r in tr], float)
    w = np.linalg.solve(A.T @ A + 5.0 * np.eye(A.shape[1]), A.T @ y)
    resid_sd = float(np.std(y - A @ w))
    print(f"{sport.upper()}: Elo K={k}, home edge {hfa} (chosen on 2024 from 2023); margin model fit on {len(tr)} games of 2023-24, residual sd {resid_sd:.2f}")
    for test in ([2025, 2026] if sport == "nfl" else [2025, 2026]):
        te = [r for r in rows if r["season"] == test and r["ngames"] >= 5 and r["hs"] != r["as"]]
        if not te:
            continue
        # for the 2026 look, refit on everything before it
        if test == 2026:
            tr2 = [r for r in rows if r["season"] < 2026 and r["ngames"] >= 5]
            X2 = X(tr2); mu2, sd2 = X2.mean(0), X2.std(0) + 1e-9
            A2 = np.hstack([np.ones((len(tr2), 1)), (X2 - mu2) / sd2]); y2 = np.array([r["margin"] for r in tr2], float)
            w_, m_, s_ = np.linalg.solve(A2.T @ A2 + 5.0 * np.eye(A2.shape[1]), A2.T @ y2), mu2, sd2; rsd = float(np.std(y2 - A2 @ w_))
        else:
            w_, m_, s_, rsd = w, mu, sd, resid_sd
        Xt = X(te); em = np.hstack([np.ones((len(te), 1)), (Xt - m_) / s_]) @ w_
        yw = np.array([1.0 if r["margin"] > 0 else 0.0 for r in te]); pm = Phi(em / rsd)
        pe = 1 / (1 + 10 ** (-np.array([r["elo_gap"] for r in te]) / 400))
        print(f"\n  graded {test}{'-' + str(test + 1)[2:] if sport != 'nfl' else ''}: {len(te)} games, home win rate {100 * yw.mean():.1f}%")
        print(f"    win accuracy  margin model {100 * np.mean((pm > .5) == (yw == 1)):.1f}%   Elo {100 * np.mean((pe > .5) == (yw == 1)):.1f}%   home always {100 * yw.mean():.1f}%")
        print(f"    log loss      margin model {ll(pm, yw):.4f}   Elo {ll(pe, yw):.4f}   coin {math.log(2):.4f}")
        with_line = [(i, r) for i, r in enumerate(te) if r.get("line") and r["line"].get("spread_home") is not None]
        if with_line and sport == "nhl":
            # the NHL book spread is a +/-1.5 puck line, not an expected margin, so a spread comparison would be meaningless: compare win probability only
            ix = [i for i, r in with_line if novig_home(r["line"])]
            pb = np.array([novig_home(te[i]["line"]) for i in ix])
            print(f"    vs CLOSING moneyline ({len(ix)} games): win log loss  model {ll(pm[ix], yw[ix]):.4f}   book {ll(pb, yw[ix]):.4f}   (NHL spread = fixed puck line; not compared)")
        elif with_line:
            ix = [i for i, _ in with_line]
            ln_m = np.array([-r["line"]["spread_home"] for _, r in with_line]); act = np.array([te[i]["margin"] for i in ix], float)
            pb = np.array([novig_home(r["line"]) if novig_home(r["line"]) else np.nan for _, r in with_line])
            ok = ~np.isnan(pb)
            print(f"    vs CLOSING line ({len(ix)} games): spread error (mean abs)  model {np.mean(np.abs(em[ix] - act)):.2f}   book {np.mean(np.abs(ln_m - act)):.2f}"
                  f"   | win log loss  model {ll(pm[ix][ok], yw[ix][ok]):.4f}   book {ll(pb[ok], yw[ix][ok]):.4f}")
            blend = 0.5 * em[ix] + 0.5 * ln_m
            print(f"    blend of model and book, spread error {np.mean(np.abs(blend - act)):.2f}   (below the book's {np.mean(np.abs(ln_m - act)):.2f} means the model adds something)")
            diff = em[ix] - ln_m
            for thr in (sum(sorted(np.abs(diff))[:len(diff) // 2]) and np.median(np.abs(diff)), np.quantile(np.abs(diff), 0.8)):
                sel = np.abs(diff) >= thr
                if sel.sum() >= 20:
                    cover = (np.sign(diff[sel]) * (act[sel] - ln_m[sel]) > 0).mean(); push = (act[sel] == ln_m[sel]).mean()
                    print(f"    where the model differs from the book by >= {thr:.1f} pts ({int(sel.sum())} games): the model's side covered {100 * cover:.1f}% (pushes {100 * push:.1f}%)")
        o = np.argsort(pm); q = len(o) // 6
        print("    calibration by sixth (pred>actual %): " + " ".join(f"{100 * pm[o[i * q:(i + 1) * q if i < 5 else None]].mean():.0f}>{100 * yw[o[i * q:(i + 1) * q if i < 5 else None]].mean():.0f}" for i in range(6)))
    if a.save:
        rows_all = [r for r in rows if r["ngames"] >= 5]
        Xa = X(rows_all); amu, asd = Xa.mean(0), Xa.std(0) + 1e-9
        Aa = np.hstack([np.ones((len(rows_all), 1)), (Xa - amu) / asd]); ya = np.array([r["margin"] for r in rows_all], float)
        wa = np.linalg.solve(Aa.T @ Aa + 5.0 * np.eye(Aa.shape[1]), Aa.T @ ya)
        out = Path(__file__).resolve().parent / "models" / f"{sport}_team_v1.json"
        out.write_text(json.dumps({"model_version": f"{sport}_team_v1", "elo": {"k": k, "hfa": hfa, "revert": cfg["revert"], "ratings": {t: round(v, 1) for t, v in elo.items()}},
                                   "features": cols, "mu": amu.tolist(), "sd": asd.tolist(), "weights": wa.tolist(), "resid_sd": float(np.std(ya - Aa @ wa)),
                                   "fit_on": f"{len(rows_all)} games, 2023-{'26 to date'}"}, indent=1))
        print(f"\n  saved {out}")


if __name__ == "__main__":
    main()
