"""Backtest the championship simulators against the last completed season (spec section 4.2).

    python bots/championship/backtest.py nfl            # writes bots/outputs/championship_backtest_nfl.json
    python bots/championship/backtest.py nhl|nba        # same, needs the NHL / ESPN hosts

Procedure, in this order, and the order is the point:
 1. FIT. Pick the regression prior N and the points for/against blend by minimising single-game log loss on the TWO
    seasons before the test season (walk-forward inside each season: a game is predicted from earlier games only).
 2. HOME RATE from the five seasons before the test season.
 3. TEST. Rebuild the standings and the remaining schedule at checkpoints of the test season using results through
    that point only, simulate, and score once against what happened: playoff field (Brier, log loss, calibration),
    the champion (share and rank the actual champion got; log loss against two baselines), and single-game log loss.
 The test season is never used to choose a constant.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
PRIORS = (2, 4, 6, 8, 12, 16, 24, 32)
BLENDS = (0.0, 0.25, 0.5, 0.75, 1.0)
EPS = 1e-6


def logloss_one(y: float, p: float) -> float:
    p = min(1 - EPS, max(EPS, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def brier(ps, ys) -> float:
    return float(np.mean([(p - y) ** 2 for p, y in zip(ps, ys)]))


def logloss(ps, ys) -> float:
    return float(np.mean([logloss_one(y, p) for p, y in zip(ps, ys)]))


def calibration(ps, ys, edges=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001)) -> list[dict]:
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, p in enumerate(ps) if lo <= p < hi]
        if idx:
            out.append({"bucket": f"{lo:.0%}-{min(hi, 1):.0%}", "n": len(idx),
                        "mean_prediction": round(float(np.mean([ps[i] for i in idx])), 4),
                        "actual_rate": round(float(np.mean([ys[i] for i in idx])), 4)})
    return out


# ── generic game-level fit: games = [(sort_key, home, away, home_units, home_pts, away_pts)] ─

def game_logloss(games: list[tuple], prior: float, blend: float, exponent: float, hwr: float) -> float:
    """Walk-forward: each game is predicted from the games that came BEFORE it that season (same-key games are
    predicted together from the earlier keys, so a round of simultaneous games never sees itself)."""
    games = sorted(games, key=lambda g: g[0])
    rec: dict = {}
    total, n = 0.0, 0
    i = 0
    while i < len(games):
        j = i
        while j < len(games) and games[j][0] == games[i][0]:
            j += 1
        batch = games[i:j]
        for _, h, a, hu, hp, ap in batch:
            sh = C.strength(*_r(rec, h), prior, blend, exponent)
            sa = C.strength(*_r(rec, a), prior, blend, exponent)
            total += logloss_one(hu, C.win_prob(sh, sa, hwr))
            n += 1
        for _, h, a, hu, hp, ap in batch:
            for t, u, pf, pa in ((h, hu, hp, ap), (a, 1 - hu, ap, hp)):
                r = rec.setdefault(t, [0.0, 0.0, 0.0, 0.0])
                r[0] += u; r[1] += 1; r[2] += pf; r[3] += pa
        i = j
    return total / max(n, 1)


def _r(rec, t):
    r = rec.get(t, (0.0, 0.0, 0.0, 0.0))
    return r[0], r[1], r[2], r[3]


def choose_constants(seasons_games: list[list[tuple]], exponent: float, hwr: float, priors=PRIORS, blends=BLENDS):
    grid = []
    for pr in priors:
        for bl in blends:
            ll = float(np.mean([game_logloss(g, pr, bl, exponent, hwr) for g in seasons_games]))
            grid.append({"prior": pr, "blend": bl, "game_logloss": round(ll, 5)})
    best = min(grid, key=lambda r: r["game_logloss"])
    return best, grid


# ── champion + field scoring ──────────────────────────────────────────────────

def score_checkpoint(rows: list[dict], field: set, champion: str, base_pct: dict) -> dict:
    abbrs = [r["abbr"] for r in rows]
    mk = [r["make_playoffs"] for r in rows]
    y = [1.0 if r["abbr"] in field else 0.0 for r in rows]
    ranked = sorted(rows, key=lambda r: -r["win_champion"])
    crank = next(i for i, r in enumerate(ranked, 1) if r["abbr"] == champion)
    share = next(r["win_champion"] for r in rows if r["abbr"] == champion)
    tot = sum(max(base_pct.get(a, 0.0), 0.0) for a in abbrs) or 1.0
    pct_share = max(base_pct.get(champion, 0.0), 0.0) / tot
    k = len(field)
    base_rate = k / len(rows)
    return {
        "make_playoffs": {"brier": round(brier(mk, y), 4), "logloss": round(logloss(mk, y), 4),
                          "brier_baseline_constant": round(brier([base_rate] * len(y), y), 4),
                          "logloss_baseline_constant": round(logloss([base_rate] * len(y), y), 4)},
        "champion": {"team": champion, "model_share": round(share, 4), "model_rank": crank,
                     "logloss_model": round(-math.log(max(share, EPS)), 4),
                     "logloss_uniform_field": round(-math.log(1.0 / k), 4),
                     "logloss_win_pct_only": round(-math.log(max(pct_share, EPS)), 4)},
        "_preds": {"p": mk, "y": y},
    }


def summarise(checkpoints: dict, constants: dict, hwr: float, game_ll_test: float, game_ll_baseline: float,
              season_label: str, sport: str) -> dict:
    ps, ys = [], []
    for cp in checkpoints.values():
        ps += cp["_preds"]["p"]; ys += cp["_preds"]["y"]
        del cp["_preds"]
    cal = calibration(ps, ys)
    mk_b, mk_ll = brier(ps, ys), logloss(ps, ys)
    champ_ll = float(np.mean([c["champion"]["logloss_model"] for c in checkpoints.values()]))
    uni = float(np.mean([c["champion"]["logloss_uniform_field"] for c in checkpoints.values()]))
    pct = float(np.mean([c["champion"]["logloss_win_pct_only"] for c in checkpoints.values()]))
    headline = (f"Backtested on {season_label}: playoff-field Brier {mk_b:.3f}; champion log loss {champ_ll:.2f} "
                f"(uniform {uni:.2f}, win% only {pct:.2f}); {len(ps)} team-checkpoints, one season, small sample.")
    return {
        "sport": sport, "test_season": season_label, "chosen_constants": constants, "home_win_rate": hwr,
        "game_logloss_test": round(game_ll_test, 5), "game_logloss_baseline_home_rate_only": round(game_ll_baseline, 5),
        "checkpoints": checkpoints,
        "pooled_make_playoffs": {"brier": round(mk_b, 4), "logloss": round(mk_ll, 4), "n": len(ps), "calibration": cal},
        "champion_mean_logloss": {"model": round(champ_ll, 4), "uniform_field": round(uni, 4), "win_pct_only": round(pct, 4)},
        "headline": headline,
        "caveat": "One test season, a handful of checkpoints, one champion per checkpoint: this cannot separate good "
                  "calibration from luck. Read the calibration table as a sanity check, not a proof.",
    }


def write(summary: dict, sport: str) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, f"championship_backtest_{sport}.json")
    with open(p, "w") as f:
        json.dump(summary, f, indent=1)
    return p


def save_params(sport: str, best: dict) -> None:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.json")
    cur = {}
    if os.path.exists(p):
        with open(p) as f:
            cur = json.load(f)
    cur[sport] = {"prior": best["prior"], "blend": best["blend"]}
    with open(p, "w") as f:
        json.dump(cur, f, indent=1)


# ── NFL adapter ───────────────────────────────────────────────────────────────

def run_nfl(test_season: int = 2025, sims: int = C.DEFAULT_SIMS) -> dict:
    import nfl as N
    fit_seasons = [test_season - 2, test_season - 1]
    hwr = N.fit_home_rate(list(range(test_season - 5, test_season)))
    df = N.load_schedule(list(range(test_season - 5, test_season + 1)))

    def games_of(season):
        reg = df[(df.season == season) & (df.game_type == "REG") & df.home_score.notna()]
        out = []
        for r in reg.itertuples():
            u = 1.0 if r.home_score > r.away_score else 0.5 if r.home_score == r.away_score else 0.0
            out.append((int(r.week), r.home_team, r.away_team, u, float(r.home_score), float(r.away_score)))
        return out

    best, grid = choose_constants([games_of(s) for s in fit_seasons], N.PYTH_EXPONENT, hwr)
    print(f"[backtest nfl] chosen on {fit_seasons}: prior {best['prior']}, blend {best['blend']}  (game log loss {best['game_logloss']})")

    teams = N.load_teams()
    cur = df[df.season == test_season]
    post = cur[cur.game_type.isin(["WC", "DIV", "CON", "SB"])]
    field = {x for x in list(post[post.game_type.isin(["WC", "DIV"])].home_team) + list(post[post.game_type.isin(["WC", "DIV"])].away_team)}
    sb = post[post.game_type == "SB"].iloc[0]
    champion = sb.home_team if sb.home_score > sb.away_score else sb.away_team
    assert len(field) == 14, field

    checkpoints = {}
    for wk in (4, 8, 12, 15):
        st = N.State(teams, cur[cur.game_type == "REG"], through_week=wk)
        rows = N.simulate(st, sims, seed=2025, prior=best["prior"], blend=best["blend"], home_win_rate=hwr)
        base = {t["abbr"]: (st.win_units[i] / st.games[i] if st.games[i] else 0.5) for i, t in enumerate(teams)}
        checkpoints[f"after_week_{wk}"] = score_checkpoint(rows, field, champion, base)
        print(f"[backtest nfl] week {wk}: {champion} share {checkpoints[f'after_week_{wk}']['champion']['model_share']:.3f}, "
              f"rank {checkpoints[f'after_week_{wk}']['champion']['model_rank']}")

    tg = games_of(test_season)
    ll_test = game_logloss(tg, best["prior"], best["blend"], N.PYTH_EXPONENT, hwr)
    ll_base = float(np.mean([logloss_one(g[3], hwr) for g in tg]))
    summary = summarise(checkpoints, {"prior": best["prior"], "blend": best["blend"], "fit_seasons": fit_seasons,
                                      "grid": grid}, hwr, ll_test, ll_base, str(test_season), "nfl")
    return summary, best


def main(argv=None) -> int:
    sport = (argv or sys.argv[1:] or ["nfl"])[0]
    if sport == "nfl":
        summary, best = run_nfl()
    else:
        import backtest_other
        summary, best = backtest_other.run(sport)
    p = write(summary, sport)
    save_params(sport, best)
    print(f"[backtest {sport}] wrote {p}")
    print(summary["headline"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
