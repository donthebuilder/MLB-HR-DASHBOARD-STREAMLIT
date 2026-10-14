"""NHL championship odds: the season, ten thousand times (spec: BOT-TASK-CHAMPIONSHIP-ODDS.md).

    python bots/championship/nhl.py --out public/data/current/nhl_playoff_odds.json

Standings and schedule come from api-web.nhle.com (dated standings URLs only). A team is its win rate (regulation,
overtime and shootout wins over games played, so the league mean is .500), optionally blended with goals for/against;
no injuries, no betting lines. Every remaining game is played with log5 + home edge; an overtime/shootout rate fit on
recent seasons decides whether the loser still earns the point. Seeding: top three in each division plus the next two
in the conference by points (the wild cards); ties by points percentage, then regulation wins, then a coin flip.
Round 1: better division winner v lower wild card, other division winner v higher wild card, 2v3 in each division;
the bracket does not reseed. Best of seven, 2-2-1-1-1, the better regular-season record has home ice in the Final.

NOTE: written against the documented api-web shapes and unit-tested on fixtures; the real feed was not reachable from
the authoring sandbox, so the first real-data run is the check on the parser (see tests/test_championship.py FIXTURE).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

IN_SEASON_MONTHS = (10, 11, 12, 1, 2, 3, 4, 5, 6)
MODEL_VERSION = "nhl-championship-v1"
API = "https://api-web.nhle.com/v1"
PYTH_EXPONENT = 2.0
HOME_BOUNDS = (0.53, 0.55)
FIELD = 16
DEFAULTS = {"prior": 20.0, "blend": 0.0}
REG_GAMES = 82


def params() -> dict:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.json")
    out = dict(DEFAULTS)
    if os.path.exists(p):
        with open(p) as f:
            out.update((json.load(f) or {}).get("nhl", {}))
    return out


# ── feed ─────────────────────────────────────────────────────────────────────

def _get(path: str, timeout: int = 20):
    import requests
    r = requests.get(f"{API}{path}", timeout=timeout, headers={"User-Agent": "dash-network-bot"})
    r.raise_for_status()
    return r.json()


def parse_standings(doc: dict) -> list[dict]:
    """api-web /standings/<date> -> one row per club."""
    rows = []
    for r in doc.get("standings") or []:
        ab = (r.get("teamAbbrev") or {}).get("default")
        rows.append({
            "abbr": ab,
            "name": (r.get("teamName") or {}).get("default") or ab,
            "conference": "East" if r.get("conferenceAbbrev") == "E" else "West",
            "division": r.get("divisionName") or r.get("divisionAbbrev"),
            "gp": int(r.get("gamesPlayed", 0)), "w": int(r.get("wins", 0)), "l": int(r.get("losses", 0)),
            "otl": int(r.get("otLosses", 0)), "pts": int(r.get("points", 0)),
            "rw": int(r.get("regulationWins", 0)), "gf": int(r.get("goalFor", 0)), "ga": int(r.get("goalAgainst", 0)),
            "clinch": (r.get("clinchIndicator") or ""),
        })
    return rows


def parse_games(doc: dict) -> list[dict]:
    """api-web schedule payload (either /schedule/<date> weeks or /club-schedule-season) -> flat games."""
    games = []
    blocks = doc.get("gameWeek")
    if blocks is None:
        blocks = [{"date": g.get("gameDate"), "games": [g]} for g in doc.get("games", [])]
    for b in blocks:
        for g in b.get("games", []):
            h, a = g.get("homeTeam") or {}, g.get("awayTeam") or {}
            hs, as_ = h.get("score"), a.get("score")
            state = g.get("gameState") or ""
            final = state in ("OFF", "FINAL") and hs is not None and as_ is not None
            games.append({
                "id": g.get("id"), "type": int(g.get("gameType", 2)), "date": b.get("date") or g.get("gameDate"),
                "home": h.get("abbrev"), "away": a.get("abbrev"), "home_id": h.get("id"), "away_id": a.get("id"),
                "hs": hs if final else None, "as": as_ if final else None,
                "ot": (g.get("gameOutcome") or {}).get("lastPeriodType") in ("OT", "SO") if final else None,
            })
    return games


def fetch_season_games(season_start_year: int, abbrs: list[str]) -> list[dict]:
    """Every game of a season (regular + playoffs) from each club's season schedule, de-duplicated by game id."""
    seen: dict = {}
    for ab in abbrs:
        try:
            doc = _get(f"/club-schedule-season/{ab}/{season_start_year}{season_start_year + 1}")
        except Exception:
            continue
        for g in parse_games(doc):
            seen.setdefault(g["id"], g)
    return list(seen.values())


class State:
    def __init__(self, rows: list[dict], remaining: list[dict], ids: dict | None = None):
        self.rows = rows
        self.meta = rows
        self.idx = {r["abbr"]: i for i, r in enumerate(rows)}
        n = len(rows)
        self.gp = np.array([r["gp"] for r in rows], float)
        self.wins = np.array([r["w"] for r in rows], float)
        self.pts = np.array([r["pts"] for r in rows], float)
        self.rw = np.array([r["rw"] for r in rows], float)
        self.gf = np.array([r["gf"] for r in rows], float)
        self.ga = np.array([r["ga"] for r in rows], float)
        self.rem_home = np.array([self.idx[g["home"]] for g in remaining if g["home"] in self.idx and g["away"] in self.idx], int)
        self.rem_away = np.array([self.idx[g["away"]] for g in remaining if g["home"] in self.idx and g["away"] in self.idx], int)
        self.ids = ids or {}
        self.remaining_count = len(self.rem_home)

    def strengths(self, prior: float, blend: float) -> np.ndarray:
        return np.array([C.strength(self.wins[i], self.gp[i], self.gf[i], self.ga[i], prior, blend, PYTH_EXPONENT)
                         for i in range(len(self.rows))])


# ── simulation ───────────────────────────────────────────────────────────────

def _qualify(members: list[int], division: dict, key: dict) -> tuple[list[int], list[int], list[int]]:
    """-> (div_winners ordered best first, their division 2/3 lists per division, wild cards ordered best first)."""
    divs = sorted({division[i] for i in members})
    top3 = {d: sorted((i for i in members if division[i] == d), key=lambda i: -key[i])[:3] for d in divs}
    in_top3 = {i for d in divs for i in top3[d]}
    wc = sorted((i for i in members if i not in in_top3), key=lambda i: -key[i])[:2]
    return divs, top3, wc


def simulate(state: State, sims: int = C.DEFAULT_SIMS, seed: int | None = 2026, prior: float | None = None,
             blend: float | None = None, home_win_rate: float = 0.54, ot_rate: float = 0.23) -> list[dict]:
    p = params()
    prior = p["prior"] if prior is None else prior
    blend = p["blend"] if blend is None else blend
    n = len(state.rows)
    s = state.strengths(prior, blend)
    rng, nprng = C.seeds_rng(seed)
    division = {i: r["division"] for i, r in enumerate(state.rows)}
    conf = {c: [i for i, r in enumerate(state.rows) if r["conference"] == c] for c in ("East", "West")}
    ids = [i for i in range(n)]
    tallies = {i: C.Tally() for i in ids}
    G = len(state.rem_home)
    p_home = C.win_prob_array(s[state.rem_home], s[state.rem_away], home_win_rate) if G else np.array([])
    done = 0
    chunk = 1000
    while done < sims:
        c = min(chunk, sims - done)
        hw = nprng.random((c, G)) < p_home if G else np.zeros((c, 0), bool)
        ot = nprng.random((c, G)) < ot_rate if G else np.zeros((c, 0), bool)
        for j in range(c):
            pts = state.pts.copy(); rw = state.rw.copy(); wins = state.wins.copy()
            if G:
                h, a, w, o = state.rem_home, state.rem_away, hw[j], ot[j]
                hp = np.where(w, 2, np.where(o, 1, 0)); ap = np.where(w, np.where(o, 1, 0), 2)
                pts += np.bincount(h, weights=hp, minlength=n) + np.bincount(a, weights=ap, minlength=n)
                rw += np.bincount(h, weights=(w & ~o).astype(float), minlength=n) + np.bincount(a, weights=(~w & ~o).astype(float), minlength=n)
                wins += np.bincount(h, weights=w.astype(float), minlength=n) + np.bincount(a, weights=(~w).astype(float), minlength=n)
            jit = nprng.random(n) * 1e-9
            gp_full = float(REG_GAMES)
            key = {i: pts[i] / (2 * gp_full) + rw[i] * 1e-6 + jit[i] for i in range(n)}
            champs = {}
            for cname, members in conf.items():
                champs[cname] = _conference(members, division, key, s, tallies, rng, home_win_rate, pts)
            a_, b_ = champs["East"], champs["West"]
            hi, lo = (a_, b_) if key[a_] >= key[b_] else (b_, a_)
            champ = hi if C.play_series(rng, s[hi], s[lo], 7, C.BEST_OF_7_HOME, home_win_rate) else lo
            tallies[champ].rounds[3] += 1
            for i in range(n):
                tallies[i].proj_wins += wins[i]
            done += 1
    rows = C.team_rows(_meta_rows(state, s), {r["team_id"]: tallies[i] for i, r in enumerate(_meta_rows(state, s))}, done, 8)
    for r in rows:
        C.monotone_rounds(r)
    return rows


def _conference(members, division, key, s, tallies, rng, hwr, pts) -> int:
    divs, top3, wc = _qualify(members, division, key)
    winners = sorted((top3[d][0] for d in divs), key=lambda i: -key[i])
    a_win, b_win = winners[0], winners[1]            # a_win has the better record
    field = [i for d in divs for i in top3[d]] + wc
    # conference seeds: 1-2 division winners, then everyone else by record
    order = winners + sorted((i for i in field if i not in winners), key=lambda i: -key[i])
    for k, i in enumerate(order):
        t = tallies[i]
        t.make_playoffs += 1; t.seed(k + 1)
        if i in winners:
            t.win_division += 1
        if k == 0:
            t.top_seed += 1
    wc_hi, wc_lo = wc[0], wc[1]

    def series(x: int, y: int) -> int:
        hi, lo = (x, y) if key[x] >= key[y] else (y, x)
        return hi if C.play_series(rng, s[hi], s[lo], 7, C.BEST_OF_7_HOME, hwr) else lo

    # division brackets: winner v wild card, and 2v3 in each division
    opp_of = {a_win: wc_lo, b_win: wc_hi}
    div_champs = []
    for d in divs:
        w = top3[d][0]
        r1a = series(w, opp_of[w]); tallies[r1a].rounds[0] += 1
        r1b = series(top3[d][1], top3[d][2]); tallies[r1b].rounds[0] += 1
        r2 = series(r1a, r1b); tallies[r2].rounds[1] += 1
        div_champs.append(r2)
    champ = series(div_champs[0], div_champs[1]); tallies[champ].rounds[2] += 1
    return champ


def _meta_rows(state: State, s: np.ndarray) -> list[dict]:
    rows = []
    for i, r in enumerate(state.rows):
        rows.append({"team_id": state.ids.get(r["abbr"], i + 1), "abbr": r["abbr"], "name": r["name"],
                     "conference": r["conference"], "division": r["division"], "wins": r["w"], "losses": r["l"],
                     "otl": r["otl"], "strength": round(float(s[i]), 4),
                     "race": {"clinch": {"x": "x", "y": "y", "z": "z", "p": "z", "e": ""}.get(str(r["clinch"]).lower(), ""),
                              "eliminated": str(r["clinch"]).lower() == "e", "seed": 0}})
    return rows


METHOD = ("Monte Carlo of every remaining regular-season game, then the Stanley Cup bracket. A team is its win rate "
          "(regulation, overtime and shootout wins over games played){blend}, regressed toward .500 by {prior:g} games; one "
          "game is log5 with home ice applied as an odds multiplier (home win rate {hwr:.3f}, fit on {fit}); a "
          "{ot:.0%} overtime/shootout rate decides whether the loser still earns a point. Seeding: top three in each "
          "division plus two wild cards per conference; ties by points percentage, then regulation wins, then a coin flip. "
          "The bracket does not reseed; best of seven, 2-2-1-1-1; the better record has home ice in the Final. No injuries, "
          "goalies, rest or betting lines.{bt}")


def build(season: int, sims: int = C.DEFAULT_SIMS, seed: int | None = 2026, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    p = params()
    rows = parse_standings(_get(f"/standings/{today.isoformat()}"))
    if len(rows) != 32:
        raise ValueError(f"standings returned {len(rows)} clubs, expected 32")
    abbrs = [r["abbr"] for r in rows]
    games = fetch_season_games(season, abbrs)
    if any(g["type"] == 3 and g["hs"] is not None for g in games):
        raise RuntimeError("playoff games are being played: in-playoff state is not built yet; carrying the last file")
    reg = [g for g in games if g["type"] == 2]
    remaining = [g for g in reg if g["hs"] is None]
    ids = {}
    for g in games:
        if g["home"]:
            ids[g["home"]] = g["home_id"]
        if g["away"]:
            ids[g["away"]] = g["away_id"]
    # home / overtime rates from the five seasons before this one
    hist = []
    for y in range(season - 5, season):
        hist += [g for g in fetch_season_games(y, abbrs) if g["type"] == 2 and g["hs"] is not None]
    # each game appears once (de-duplicated by id inside fetch_season_games)
    hwr = C.fit_home_win_rate([(g["hs"], g["as"]) for g in hist], *HOME_BOUNDS, "NHL")
    ot_rate = float(np.mean([bool(g["ot"]) for g in hist]))
    st = State(rows, remaining, ids)
    out = simulate(st, sims, seed, home_win_rate=hwr, ot_rate=ot_rate)
    bt = ""
    bpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs", "championship_backtest_nhl.json")
    if os.path.exists(bpath):
        try:
            with open(bpath) as f:
                bt = " " + (json.load(f).get("headline") or "")
        except Exception:
            bt = ""
    method = METHOD.format(blend=(f", blended {p['blend']:.0%} with goals for/against" if p["blend"] else ""), prior=p["prior"],
                           hwr=hwr, fit=f"{season - 5}-{season - 1}", ot=ot_rate, bt=bt)
    doc = C.payload(sport_label="nhl", season=season, sims=sims, model_version=MODEL_VERSION, method=method,
                    home_win_rate=hwr, teams=out, note=f"{st.remaining_count} regular-season games left when this ran.")
    bad = C.validate(doc, 32, FIELD)
    if bad:
        raise ValueError("NHL file failed validation: " + "; ".join(bad[:5]))
    return doc


def current_season() -> int:
    d = dt.date.today()
    return d.year if d.month >= 8 else d.year - 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--sims", type=int, default=C.DEFAULT_SIMS)
    ap.add_argument("--force", action="store_true", help="run out of season")
    a = ap.parse_args(argv)
    if not a.force and not C.in_season(IN_SEASON_MONTHS):
        print("[nhl_championship] out of season: leaving the last file" )
        return 0
    season = a.season or current_season()
    try:
        doc = build(season, a.sims)
    except Exception as e:
        print(f"[nhl_championship] {type(e).__name__}: {e}")
        if C.carry_forward(a.out, f"Feed unavailable on this run ({type(e).__name__}); showing the previous run."):
            print("[nhl_championship] carried the previous file forward")
        return 0
    C.write_json(doc, a.out)
    top = doc["teams"][0]
    print(f"[nhl_championship] {a.out}: season {season}, top {top['abbr']} {top['win_champion']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
