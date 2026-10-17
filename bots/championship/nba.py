"""NBA championship odds: the season, ten thousand times (spec: BOT-TASK-CHAMPIONSHIP-ODDS.md).

    python bots/championship/nba.py --out public/data/current/nba_playoff_odds.json

Standings and the remaining schedule come from ESPN (the same host the site's lib/nba/api.js reads). A team is its
record, optionally blended with points for/against; no injuries, no betting lines. Per conference: seeds 1-6 go
straight in, 7-10 play in (7v8: the winner is the 7 seed; 9v10: the loser is out; the loser of 7v8 hosts the winner of
9v10 for the 8 seed), then the fixed bracket 1v8, 4v5, 3v6, 2v7 with no reseeding. Best of seven, 2-2-1-1-1; the better
record has home court in the Finals. Standings ties are a coin flip (no head-to-head chains).

Seeds published: 1-6 = finishing seed, 7 and 8 = bracket seed after the play-in (so seeds 1-8 sum to make_playoffs),
9 and 10 = finishing 9th or 10th (play-in only; overlaps with 8 for a team that wins its way in).

NOTE: written against ESPN's documented JSON shapes and unit-tested on fixtures; the host was not reachable from the
authoring sandbox, so the first real-data run is the check on the parser.
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
MODEL_VERSION = "nba-championship-v1"
STANDINGS = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
PYTH_EXPONENT = 14.3
HOME_BOUNDS = (0.56, 0.60)
FIELD = 16
DEFAULTS = {"prior": 20.0, "blend": 0.0}
REG_GAMES = 82


def params() -> dict:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.json")
    out = dict(DEFAULTS)
    if os.path.exists(p):
        with open(p) as f:
            out.update((json.load(f) or {}).get("nba", {}))
    return out


def _get(url: str, params_: dict | None = None, timeout: int = 25):
    import requests
    r = requests.get(url, params=params_, timeout=timeout, headers={"User-Agent": "dash-network-bot"})
    r.raise_for_status()
    return r.json()


def _stat(stats: list, name: str, default=0.0):
    for s in stats or []:
        if s.get("name") == name or s.get("abbreviation") == name:
            v = s.get("value")
            return float(v) if v is not None else default
    return default


def parse_standings(doc: dict) -> list[dict]:
    rows = []
    for ch in doc.get("children") or []:
        conf = "East" if "east" in (ch.get("name") or "").lower() else "West"
        for e in (ch.get("standings") or {}).get("entries") or []:
            t = e.get("team") or {}
            st = e.get("stats") or []
            rows.append({
                "team_id": int(t.get("id")) if str(t.get("id", "")).isdigit() else None,
                "abbr": t.get("abbreviation"), "name": t.get("displayName") or t.get("name"),
                "conference": conf, "division": None,
                "w": int(_stat(st, "wins")), "l": int(_stat(st, "losses")),
                "pf": _stat(st, "pointsFor"), "pa": _stat(st, "pointsAgainst"),
                "clinch": (_stat(st, "clincher", 0.0) and "") or "",
            })
    return rows


def parse_events(doc: dict) -> list[dict]:
    games = []
    for ev in doc.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        home = next((c for c in cs if c.get("homeAway") == "home"), None)
        away = next((c for c in cs if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        done = bool(((comp.get("status") or ev.get("status") or {}).get("type") or {}).get("completed"))
        stype = int((ev.get("season") or {}).get("type", 2))
        games.append({
            "id": ev.get("id"), "type": stype, "date": ev.get("date"),
            "home": (home.get("team") or {}).get("abbreviation"), "away": (away.get("team") or {}).get("abbreviation"),
            "hs": int(float(home["score"])) if done and home.get("score") not in (None, "") else None,
            "as": int(float(away["score"])) if done and away.get("score") not in (None, "") else None,
        })
    return games


def fetch_games(start: dt.date, end: dt.date) -> list[dict]:
    """Scoreboard by month-sized date ranges (one request each), de-duplicated by event id."""
    seen: dict = {}
    d = start
    while d <= end:
        e = min(end, (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
        doc = _get(SCOREBOARD, {"dates": f"{d:%Y%m%d}-{e:%Y%m%d}", "limit": 1000})
        for g in parse_events(doc):
            seen.setdefault(g["id"], g)
        d = e + dt.timedelta(days=1)
    return list(seen.values())


class State:
    def __init__(self, rows: list[dict], remaining: list[dict]):
        self.rows = rows
        self.idx = {r["abbr"]: i for i, r in enumerate(rows)}
        self.w = np.array([r["w"] for r in rows], float)
        self.l = np.array([r["l"] for r in rows], float)
        self.gp = self.w + self.l
        self.pf = np.array([r["pf"] for r in rows], float)
        self.pa = np.array([r["pa"] for r in rows], float)
        ok = [g for g in remaining if g["home"] in self.idx and g["away"] in self.idx]
        self.rem_home = np.array([self.idx[g["home"]] for g in ok], int)
        self.rem_away = np.array([self.idx[g["away"]] for g in ok], int)
        self.remaining_count = len(ok)

    def strengths(self, prior: float, blend: float) -> np.ndarray:
        return np.array([C.strength(self.w[i], self.gp[i], self.pf[i], self.pa[i], prior, blend, PYTH_EXPONENT)
                         for i in range(len(self.rows))])


def simulate(state: State, sims: int = C.DEFAULT_SIMS, seed: int | None = 2026, prior: float | None = None,
             blend: float | None = None, home_win_rate: float = 0.58) -> list[dict]:
    p = params()
    prior = p["prior"] if prior is None else prior
    blend = p["blend"] if blend is None else blend
    n = len(state.rows)
    s = state.strengths(prior, blend)
    rng, nprng = C.seeds_rng(seed)
    conf = {c: [i for i, r in enumerate(state.rows) if r["conference"] == c] for c in ("East", "West")}
    meta = _meta_rows(state, s)
    tallies = {i: C.Tally() for i in range(n)}
    p_home = C.win_prob_array(s[state.rem_home], s[state.rem_away], home_win_rate) if len(state.rem_home) else np.array([])
    done = 0
    for wins, _ in C.sample_wins(state.w, state.rem_home, state.rem_away, p_home, sims, nprng):
        for row in wins:
            key = row + nprng.random(n) * 1e-6
            champs = {}
            for cname, members in conf.items():
                champs[cname] = _conference(members, key, s, tallies, rng, home_win_rate)
            a, b = champs["East"], champs["West"]
            hi, lo = (a, b) if key[a] >= key[b] else (b, a)
            champ = hi if C.play_series(rng, s[hi], s[lo], 7, C.BEST_OF_7_HOME, home_win_rate) else lo
            tallies[champ].rounds[3] += 1
            for i in range(n):
                tallies[i].proj_wins += row[i]
            done += 1
    rows = C.team_rows(meta, {m["team_id"]: tallies[i] for i, m in enumerate(meta)}, done, 10)
    for r in rows:
        r.pop("win_division", None)
        C.monotone_rounds(r)
    return rows


def _conference(members: list[int], key: np.ndarray, s: np.ndarray, tallies, rng, hwr) -> int:
    order = sorted(members, key=lambda i: -key[i])
    for k, i in enumerate(order):
        if k >= 6:
            tallies[i].play_in += 1
        if k >= 8:
            tallies[i].seeds[k + 1] = tallies[i].seeds.get(k + 1, 0) + 1     # finished 9th / 10th
    if order:
        tallies[order[0]].top_seed += 1

    def one(hi: int, lo: int) -> int:
        return hi if C.play_single(rng, s[hi], s[lo], hwr) else lo

    s7, s8, s9, s10 = order[6], order[7], order[8], order[9]
    w78 = one(s7, s8)
    l78 = s8 if w78 == s7 else s7
    w910 = one(s9, s10)
    last = one(l78, w910)                             # the loser of 7v8 is the higher seed, so it hosts
    bracket = order[:6] + [w78, last]                 # bracket seeds 1..8
    for k, i in enumerate(bracket):
        t = tallies[i]
        t.make_playoffs += 1
        t.seed(k + 1)
    def series(x: int, y: int) -> int:
        hi, lo = (x, y) if key[x] >= key[y] else (y, x)
        return hi if C.play_series(rng, s[hi], s[lo], 7, C.BEST_OF_7_HOME, hwr) else lo
    r1 = {}
    for hi_k, lo_k in ((0, 7), (3, 4), (2, 5), (1, 6)):
        w = series(bracket[hi_k], bracket[lo_k]); tallies[w].rounds[0] += 1; r1[hi_k] = w
    top = series(r1[0], r1[3]); tallies[top].rounds[1] += 1
    bot = series(r1[1], r1[2]); tallies[bot].rounds[1] += 1
    champ = series(top, bot); tallies[champ].rounds[2] += 1
    return champ


def _meta_rows(state: State, s: np.ndarray) -> list[dict]:
    rows = []
    for i, r in enumerate(state.rows):
        rows.append({"team_id": r["team_id"] if r["team_id"] is not None else i + 1, "abbr": r["abbr"], "name": r["name"],
                     "conference": r["conference"], "division": r.get("division"), "wins": r["w"], "losses": r["l"],
                     "strength": round(float(s[i]), 4), "race": {"clinch": "", "eliminated": False, "seed": 0}})
    return rows


METHOD = ("Monte Carlo of every remaining regular-season game, then the play-in and the NBA bracket. A team is its "
          "record{blend}, regressed toward .500 by {prior:g} games; one game is log5 with home court applied as an odds "
          "multiplier (home win rate {hwr:.3f}, fit on {fit}). Seeds 1-6 go straight in; 7-10 play in (7v8, 9v10, then the "
          "loser of 7v8 hosts the winner of 9v10 for the 8 seed); bracket 1v8, 4v5, 3v6, 2v7 with no reseeding; best of "
          "seven, 2-2-1-1-1; the better record has home court in the Finals. Standings ties are a coin flip. Seeds 1-8 are "
          "bracket seeds (they sum to the chance of making the field); 9 and 10 are finishing places in the play-in. No "
          "injuries, rest or betting lines.{bt}")


def build(season: int, sims: int = C.DEFAULT_SIMS, seed: int | None = 2026, today: dt.date | None = None) -> dict:
    """`season` = the year the season STARTS (2026 = 2026-27)."""
    today = today or dt.date.today()
    p = params()
    rows = parse_standings(_get(STANDINGS, {"season": season + 1}))
    if len(rows) != 30:
        raise ValueError(f"standings returned {len(rows)} clubs, expected 30")
    games = fetch_games(dt.date(season, 10, 1), dt.date(season + 1, 4, 30))
    if any(g["type"] == 3 and g["hs"] is not None for g in games):
        raise RuntimeError("playoff games are being played: in-playoff state is not built yet; carrying the last file")
    remaining = [g for g in games if g["type"] == 2 and g["hs"] is None]
    hist = []
    for y in range(season - 5, season):
        hist += [g for g in fetch_games(dt.date(y, 10, 1), dt.date(y + 1, 4, 30)) if g["type"] == 2 and g["hs"] is not None]
    hwr = C.fit_home_win_rate([(g["hs"], g["as"]) for g in hist], *HOME_BOUNDS, "NBA")
    st = State(rows, remaining)
    out = simulate(st, sims, seed, home_win_rate=hwr)
    bt = ""
    bpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs", "championship_backtest_nba.json")
    if os.path.exists(bpath):
        try:
            with open(bpath) as f:
                bt = " " + (json.load(f).get("headline") or "")
        except Exception:
            bt = ""
    method = METHOD.format(blend=(f", blended {p['blend']:.0%} with points for/against" if p["blend"] else ""), prior=p["prior"],
                           hwr=hwr, fit=f"{season - 5}-{season - 1}", bt=bt)
    doc = C.payload(sport_label="nba", season=season, sims=sims, model_version=MODEL_VERSION, method=method,
                    home_win_rate=hwr, teams=out, note=f"{st.remaining_count} regular-season games left when this ran.")
    bad = C.validate(doc, 30, FIELD)
    if bad:
        raise ValueError("NBA file failed validation: " + "; ".join(bad[:5]))
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
        print("[nba_championship] out of season: leaving the last file" )
        return 0
    season = a.season or current_season()
    try:
        doc = build(season, a.sims)
    except Exception as e:
        print(f"[nba_championship] {type(e).__name__}: {e}")
        if C.carry_forward(a.out, f"Feed unavailable on this run ({type(e).__name__}); showing the previous run."):
            print("[nba_championship] carried the previous file forward")
        return 0
    C.write_json(doc, a.out)
    top = doc["teams"][0]
    print(f"[nba_championship] {a.out}: season {season}, top {top['abbr']} {top['win_champion']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
