"""NHL and NBA backtests (same procedure as backtest.run_nfl; see backtest.py for the order of operations).

    python bots/championship/backtest.py nhl      # test season 2025-26
    python bots/championship/backtest.py nba

Needs api-web.nhle.com (NHL) or site.api.espn.com (NBA). Written without access to either host, so the first run on a
network that has them is also the first real test of the parsers; it fails loudly (assertions on field size, team
count) rather than scoring a wrong field.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B  # noqa: E402
import common as C  # noqa: E402


def _games_tuples(games: list[dict], date_key=lambda g: g["date"]):
    out = []
    for g in games:
        if g["type"] != 2 or g["hs"] is None:
            continue
        u = 1.0 if g["hs"] > g["as"] else 0.5 if g["hs"] == g["as"] else 0.0
        out.append((str(date_key(g))[:10], g["home"], g["away"], u, float(g["hs"]), float(g["as"])))
    return out


def _champion_and_field(games: list[dict], expect_field: int):
    post = sorted((g for g in games if g["type"] == 3 and g["hs"] is not None), key=lambda g: str(g["date"]))
    assert post, "no completed playoff games found for the test season"
    field = {g["home"] for g in post} | {g["away"] for g in post}
    assert len(field) == expect_field, f"playoff field has {len(field)} clubs, expected {expect_field}: {sorted(field)}"
    last = post[-1]
    return field, (last["home"] if last["hs"] > last["as"] else last["away"])


def run(sport: str):
    if sport == "nhl":
        return _run_nhl()
    if sport == "nba":
        return _run_nba()
    raise SystemExit(f"unknown sport {sport}")


# ── NHL ──────────────────────────────────────────────────────────────────────

def _run_nhl(test_year: int = 2025, sims: int = C.DEFAULT_SIMS):
    import nhl as H
    now_rows = H.parse_standings(H._get("/standings/now"))
    abbrs = [r["abbr"] for r in now_rows]
    by_year = {y: H.fetch_season_games(y, abbrs) for y in range(test_year - 5, test_year + 1)}
    hist = [g for y in range(test_year - 5, test_year) for g in by_year[y] if g["type"] == 2 and g["hs"] is not None]
    hwr = C.fit_home_win_rate([(g["hs"], g["as"]) for g in hist], *H.HOME_BOUNDS, "NHL")
    ot_rate = float(np.mean([bool(g["ot"]) for g in hist]))
    best, grid = B.choose_constants([_games_tuples(by_year[y]) for y in (test_year - 2, test_year - 1)], H.PYTH_EXPONENT, hwr)
    print(f"[backtest nhl] chosen: prior {best['prior']}, blend {best['blend']} (game log loss {best['game_logloss']})")

    test = by_year[test_year]
    field, champion = _champion_and_field(test, 16)
    end = max(str(g["date"]) for g in test if g["type"] == 2)[:10]
    meta = {r["abbr"]: r for r in H.parse_standings(H._get(f"/standings/{end}"))}
    ids = {}
    for g in test:
        ids[g["home"]] = g["home_id"]; ids[g["away"]] = g["away_id"]
    reg = sorted((g for g in test if g["type"] == 2), key=lambda g: str(g["date"]))
    checkpoints = {}
    for label, median_gp in (("after_20_games", 20), ("after_40_games", 40), ("after_60_games", 60), ("10_to_go", 72)):
        played_n, counts, cut = 0, {}, None
        for g in reg:                                    # the date by which the median club has played `median_gp`
            if g["hs"] is None:
                continue
            for t in (g["home"], g["away"]):
                counts[t] = counts.get(t, 0) + 1
            if sorted(counts.values())[len(counts) // 2] >= median_gp and len(counts) >= 30:
                cut = str(g["date"])[:10]; break
        rows, remaining = _nhl_state(reg, cut, meta)
        st = H.State(rows, remaining, ids)
        out = H.simulate(st, sims, seed=2025, prior=best["prior"], blend=best["blend"], home_win_rate=hwr, ot_rate=ot_rate)
        base = {r["abbr"]: (r["w"] / r["gp"] if r["gp"] else 0.5) for r in rows}
        checkpoints[label] = B.score_checkpoint(out, field, champion, base)
        print(f"[backtest nhl] {label} (through {cut}): {champion} share {checkpoints[label]['champion']['model_share']:.3f}")
    tg = _games_tuples(test)
    ll_test = B.game_logloss(tg, best["prior"], best["blend"], H.PYTH_EXPONENT, hwr)
    ll_base = float(np.mean([B.logloss_one(g[3], hwr) for g in tg]))
    summary = B.summarise(checkpoints, {"prior": best["prior"], "blend": best["blend"],
                                        "fit_seasons": [test_year - 2, test_year - 1], "grid": grid},
                          hwr, ll_test, ll_base, f"{test_year}-{str(test_year + 1)[2:]}", "nhl")
    return summary, best


def _nhl_state(reg: list[dict], cut: str, meta: dict):
    acc: dict = {}
    remaining = []
    for g in reg:
        if g["hs"] is not None and str(g["date"])[:10] <= cut:
            for t, gf, ga, home in ((g["home"], g["hs"], g["as"], True), (g["away"], g["as"], g["hs"], False)):
                a = acc.setdefault(t, {"gp": 0, "w": 0, "l": 0, "otl": 0, "pts": 0, "rw": 0, "gf": 0, "ga": 0})
                a["gp"] += 1; a["gf"] += gf; a["ga"] += ga
                if gf > ga:
                    a["w"] += 1; a["pts"] += 2; a["rw"] += 0 if g["ot"] else 1
                elif g["ot"]:
                    a["otl"] += 1; a["pts"] += 1
                else:
                    a["l"] += 1
        else:
            remaining.append(g)
    rows = []
    for t, a in acc.items():
        m = meta[t]
        rows.append({"abbr": t, "name": m["name"], "conference": m["conference"], "division": m["division"], "clinch": "", **a})
    return rows, remaining


# ── NBA ──────────────────────────────────────────────────────────────────────

def _run_nba(test_year: int = 2025, sims: int = C.DEFAULT_SIMS):
    import nba as N
    by_year = {y: N.fetch_games(dt.date(y, 10, 1), dt.date(y + 1, 6, 30)) for y in range(test_year - 5, test_year + 1)}
    hist = [g for y in range(test_year - 5, test_year) for g in by_year[y] if g["type"] == 2 and g["hs"] is not None]
    hwr = C.fit_home_win_rate([(g["hs"], g["as"]) for g in hist], *N.HOME_BOUNDS, "NBA")
    best, grid = B.choose_constants([_games_tuples(by_year[y]) for y in (test_year - 2, test_year - 1)], N.PYTH_EXPONENT, hwr)
    print(f"[backtest nba] chosen: prior {best['prior']}, blend {best['blend']} (game log loss {best['game_logloss']})")

    test = by_year[test_year]
    field, champion = _champion_and_field(test, 16)
    final = N.parse_standings(N._get(N.STANDINGS, {"season": test_year + 1}))
    conf_of = {r["abbr"]: r for r in final}
    reg = sorted((g for g in test if g["type"] == 2), key=lambda g: str(g["date"]))
    checkpoints = {}
    for label, median_gp in (("after_20_games", 20), ("after_40_games", 40), ("after_60_games", 60), ("10_to_go", 72)):
        counts, cut = {}, None
        for g in reg:
            if g["hs"] is None:
                continue
            for t in (g["home"], g["away"]):
                counts[t] = counts.get(t, 0) + 1
            if len(counts) >= 30 and sorted(counts.values())[len(counts) // 2] >= median_gp:
                cut = str(g["date"])[:10]; break
        acc, remaining = {}, []
        for g in reg:
            if g["hs"] is not None and str(g["date"])[:10] <= cut:
                for t, pf, pa in ((g["home"], g["hs"], g["as"]), (g["away"], g["as"], g["hs"])):
                    a = acc.setdefault(t, {"w": 0, "l": 0, "pf": 0.0, "pa": 0.0})
                    a["pf"] += pf; a["pa"] += pa
                    a["w" if pf > pa else "l"] += 1
            else:
                remaining.append(g)
        rows = [{"team_id": conf_of[t]["team_id"], "abbr": t, "name": conf_of[t]["name"], "conference": conf_of[t]["conference"],
                 "division": None, "clinch": "", **a} for t, a in acc.items()]
        st = N.State(rows, remaining)
        out = N.simulate(st, sims, seed=2025, prior=best["prior"], blend=best["blend"], home_win_rate=hwr)
        base = {r["abbr"]: (r["w"] / max(r["w"] + r["l"], 1)) for r in rows}
        checkpoints[label] = B.score_checkpoint(out, field, champion, base)
        print(f"[backtest nba] {label} (through {cut}): {champion} share {checkpoints[label]['champion']['model_share']:.3f}")
    tg = _games_tuples(test)
    ll_test = B.game_logloss(tg, best["prior"], best["blend"], N.PYTH_EXPONENT, hwr)
    ll_base = float(np.mean([B.logloss_one(g[3], hwr) for g in tg]))
    summary = B.summarise(checkpoints, {"prior": best["prior"], "blend": best["blend"],
                                        "fit_seasons": [test_year - 2, test_year - 1], "grid": grid},
                          hwr, ll_test, ll_base, f"{test_year}-{str(test_year + 1)[2:]}", "nba")
    return summary, best
