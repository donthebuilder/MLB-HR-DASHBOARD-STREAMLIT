"""NFL championship odds: the season, ten thousand times (spec: BOT-TASK-CHAMPIONSHIP-ODDS.md).

    python bots/championship/nfl.py --out public/data/current/nfl_playoff_odds.json

Reads nflverse schedules/results (nflreadpy, the bot's existing NFL source). A team is its record and its points
for/against; no injuries, no betting lines. Seven seeds per conference: the four division winners by record, then
the three best non-winners. Wild card 2v7 / 3v6 / 4v5 (the 1 seed has the bye), reseed for the divisional round
(1 plays the lowest survivor), conference championship, then a neutral-site Super Bowl. Ties in the standings are
broken by win percentage then a coin flip (head-to-head chains are not simulated). A tie game counts half a win.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

IN_SEASON_MONTHS = (9, 10, 11, 12, 1, 2)
MODEL_VERSION = "nfl-championship-v1"
PYTH_EXPONENT = 2.37
HOME_BOUNDS = (0.52, 0.56)
FIELD = 14
DEFAULTS = {"prior": 8.0, "blend": 0.0}


def params() -> dict:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.json")
    out = dict(DEFAULTS)
    if os.path.exists(p):
        with open(p) as f:
            out.update((json.load(f) or {}).get("nfl", {}))
    return out


# ── data ─────────────────────────────────────────────────────────────────────

def _canon(a: str) -> str:
    return {"LAR": "LA", "OAK": "LV", "SD": "LAC", "STL": "LA"}.get(a, a)


def load_teams() -> list[dict]:
    """The 32 clubs: abbr -> conference/division/name/id (nflverse team table, current clubs only)."""
    import nflreadpy
    t = nflreadpy.load_teams().to_pandas()
    keep = {"ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX",
            "KC", "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS"}
    t = t[t.team_abbr.isin(keep)]
    return [{"team_id": int(r.team_id), "abbr": r.team_abbr, "name": r.team_name, "conference": r.team_conf,
             "division": r.team_division} for r in t.itertuples()]


def load_schedule(seasons: list[int]):
    import nflreadpy
    df = nflreadpy.load_schedules(seasons).to_pandas()
    for c in ("home_team", "away_team"):
        df[c] = df[c].map(_canon)
    return df


def fit_home_rate(seasons: list[int]) -> float:
    df = load_schedule(seasons)
    reg = df[(df.game_type == "REG") & df.home_score.notna()]
    return C.fit_home_win_rate(list(zip(reg.home_score.astype(int), reg.away_score.astype(int))), *HOME_BOUNDS, "NFL")


class State:
    """What is known as of a moment: records, remaining regular-season games, and any playoff games already played."""

    def __init__(self, teams: list[dict], df, through_week: int | None = None):
        self.meta = teams
        self.idx = {t["abbr"]: i for i, t in enumerate(teams)}
        n = len(teams)
        reg = df[df.game_type == "REG"]
        played = reg[reg.home_score.notna()]
        if through_week is not None:
            played = played[played.week <= through_week]
            remaining = reg[reg.week > through_week]
        else:
            remaining = reg[reg.home_score.isna()]
        self.win_units = np.zeros(n); self.games = np.zeros(n); self.pf = np.zeros(n); self.pa = np.zeros(n)
        self.w = np.zeros(n, dtype=int); self.l = np.zeros(n, dtype=int); self.t = np.zeros(n, dtype=int)
        for r in played.itertuples():
            h, a = self.idx[r.home_team], self.idx[r.away_team]
            hs, as_ = int(r.home_score), int(r.away_score)
            for x, y, ps, pa in ((h, a, hs, as_), (a, h, as_, hs)):
                self.games[x] += 1; self.pf[x] += ps; self.pa[x] += pa
                if ps > pa:
                    self.win_units[x] += 1; self.w[x] += 1
                elif ps == pa:
                    self.win_units[x] += 0.5; self.t[x] += 1
                else:
                    self.l[x] += 1
        self.rem_home = np.array([self.idx[x] for x in remaining.home_team], dtype=int)
        self.rem_away = np.array([self.idx[x] for x in remaining.away_team], dtype=int)
        self.post = df[df.game_type.isin(["WC", "DIV", "CON", "SB"])] if through_week is None else df.iloc[0:0]
        self.remaining_count = len(remaining)

    @classmethod
    def from_arrays(cls, teams, win_units, games, pf, pa, rem_home, rem_away):
        """Build a state without a schedule table (tests and what-ifs). Arrays are per team, in `teams` order."""
        o = object.__new__(cls)
        o.meta, o.idx = teams, {t["abbr"]: i for i, t in enumerate(teams)}
        o.win_units, o.games, o.pf, o.pa = (np.array(x, float) for x in (win_units, games, pf, pa))
        o.w, o.l, o.t = (o.win_units.astype(int), (o.games - o.win_units).astype(int), np.zeros(len(teams), int))
        o.rem_home, o.rem_away = np.array(rem_home, int), np.array(rem_away, int)
        o.post, o.remaining_count = None, len(o.rem_home)
        return o

    def strengths(self, prior: float, blend: float) -> np.ndarray:
        return np.array([C.strength(self.win_units[i], self.games[i], self.pf[i], self.pa[i], prior, blend, PYTH_EXPONENT)
                         for i in range(len(self.meta))])


# ── the simulation ───────────────────────────────────────────────────────────

def _seed_conference(members: list[int], div_of: dict, wins: np.ndarray, jit: np.ndarray, games: float) -> list[int]:
    """Seed order for one conference: division winners (by record) 1-4, then the three best of the rest."""
    key = {i: wins[i] / games + jit[i] for i in members}
    winners = []
    for d in sorted({div_of[i] for i in members}):
        winners.append(max((i for i in members if div_of[i] == d), key=key.get))
    winners.sort(key=lambda i: -key[i])
    rest = sorted((i for i in members if i not in winners), key=lambda i: -key[i])[:3]
    return winners + rest


def simulate(state: State, sims: int = C.DEFAULT_SIMS, seed: int | None = 2026, prior: float | None = None,
             blend: float | None = None, home_win_rate: float = 0.54) -> list[dict]:
    p = params()
    prior = p["prior"] if prior is None else prior
    blend = p["blend"] if blend is None else blend
    n = len(state.meta)
    s = state.strengths(prior, blend)
    rng, nprng = C.seeds_rng(seed)
    div_of = {i: t["division"] for i, t in enumerate(state.meta)}
    conf = {c: [i for i, t in enumerate(state.meta) if t["conference"] == c] for c in ("AFC", "NFC")}
    p_home = C.win_prob_array(s[state.rem_home], s[state.rem_away], home_win_rate) if len(state.rem_home) else np.array([])
    tallies = {t["team_id"]: C.Tally() for t in state.meta}
    tid = [t["team_id"] for t in state.meta]
    base = state.win_units.copy()
    gp_total = 17.0

    known = _known_playoff(state)
    actual = _actual_seeds(state, s, conf)
    done = 0
    for wins, _ in C.sample_wins(base, state.rem_home, state.rem_away, p_home, sims, nprng):
        for row in wins:
            jit = nprng.random(n) * 1e-6
            champs = {}
            for cname, members in conf.items():
                seeds = actual.get(cname) or _seed_conference(members, div_of, row, jit, gp_total)
                winners = set(seeds[:4])
                for k, i in enumerate(seeds):
                    t = tallies[tid[i]]
                    t.make_playoffs += 1; t.seed(k + 1)
                    if i in winners:
                        t.win_division += 1
                    if k == 0:
                        t.top_seed += 1
                champs[cname] = _bracket(seeds, s, tallies, tid, rng, home_win_rate, known.get(cname))
            a, b = champs["AFC"], champs["NFC"]
            sb_known = known.get("SB")
            if sb_known is not None:
                w = sb_known
            else:
                hi, lo = (a, b) if s[a] >= s[b] else (b, a)
                w = hi if C.play_single(rng, s[hi], s[lo], home_win_rate, neutral=True) else lo
            tallies[tid[w]].rounds[3] += 1
            for i in range(n):
                tallies[tid[i]].proj_wins += row[i]
            done += 1
    # a team that reached the Super Bowl won round 3 (counted in _bracket); round 4 above
    rows = C.team_rows(_meta_rows(state, s), tallies, done, 7)
    for r in rows:
        C.monotone_rounds(r)
    return rows


def _actual_seeds(state: State, s: np.ndarray, conf: dict) -> dict:
    """Once the wild-card games are on the schedule the field is fixed: read the seeds from them instead of re-deriving
    tiebreakers. The bye is the best-rated club in the conference that is not in a wild-card game; the three home clubs
    are seeds 2-4 (ordered by strength) and each one's opponent sits in the mirrored seed (7, 6, 5)."""
    post = state.post
    if post is None or len(post) == 0:
        return {}
    wc = post[(post.game_type == "WC") & post.home_team.notna()]
    out = {}
    for cname, members in conf.items():
        games = [(state.idx[r.home_team], state.idx[r.away_team]) for r in wc.itertuples()
                 if r.home_team in state.idx and state.idx[r.home_team] in members]
        if len(games) != 3:
            continue
        in_wc = {x for g in games for x in g}
        bye = max((i for i in members if i not in in_wc), key=lambda i: (state.win_units[i] / max(state.games[i], 1), s[i]))
        games.sort(key=lambda g: -(state.win_units[g[0]] / max(state.games[g[0]], 1)))
        homes = [g[0] for g in games]
        aways = [g[1] for g in games]
        out[cname] = [bye] + homes + [aways[2], aways[1], aways[0]]
    return out


def _bracket(seeds: list[int], s: np.ndarray, tallies, tid, rng, hwr, known) -> int:
    """Play one conference's bracket from seeds (list of 7 team indices, seed order). Returns the conference champion."""
    def hi_wins(a: int, b: int, rnd_known: set | None) -> int:
        """a is the higher seed (hosts). If `known` says who advanced, use it."""
        if rnd_known:
            if a in rnd_known and b not in rnd_known:
                return a
            if b in rnd_known and a not in rnd_known:
                return b
        return a if C.play_single(rng, s[a], s[b], hwr) else b

    k = known or {}
    bye = seeds[0]
    tallies[tid[bye]].rounds[0] += 1
    wc = [(seeds[1], seeds[6]), (seeds[2], seeds[5]), (seeds[3], seeds[4])]
    survivors = [bye]
    for a, b in wc:
        w = hi_wins(a, b, k.get("DIV"))
        tallies[tid[w]].rounds[0] += 1
        survivors.append(w)
    order = {t: seeds.index(t) for t in survivors}
    survivors.sort(key=lambda t: order[t])
    top, rest = survivors[0], survivors[1:]
    lowest = rest[-1]
    games = [(top, lowest), (rest[0], rest[1])]
    nxt = []
    for a, b in games:
        hi, lo = (a, b) if order[a] < order[b] else (b, a)
        w = hi_wins(hi, lo, k.get("CON"))
        tallies[tid[w]].rounds[1] += 1
        nxt.append(w)
    a, b = nxt
    hi, lo = (a, b) if order[a] < order[b] else (b, a)
    champ = hi_wins(hi, lo, k.get("SB_TEAMS"))
    tallies[tid[champ]].rounds[2] += 1
    return champ


def _known_playoff(state: State) -> dict:
    """Winners already decided by played playoff games: {conf: {"DIV": {teams that reached the DIV round}, ...}, "SB": idx}.
    A round's advancers are the teams that appear in the next round's games, so nothing is guessed."""
    post = state.post
    if post is None or len(post) == 0:
        return {}
    conf_of = {i: t["conference"] for i, t in enumerate(state.meta)}
    out: dict = {}
    def teams_in(gt):
        g = post[(post.game_type == gt) & post.home_team.notna()]
        return {state.idx[x] for x in list(g.home_team) + list(g.away_team) if x in state.idx}
    for cname in ("AFC", "NFC"):
        d = out.setdefault(cname, {})
        div, con, sb = teams_in("DIV"), teams_in("CON"), teams_in("SB")
        if div:
            d["DIV"] = {i for i in div if conf_of[i] == cname}
        if con:
            d["CON"] = {i for i in con if conf_of[i] == cname}
        if sb:
            d["SB_TEAMS"] = {i for i in sb if conf_of[i] == cname}
    sbg = post[(post.game_type == "SB") & post.home_score.notna()]
    if len(sbg):
        r = sbg.iloc[0]
        out["SB"] = state.idx[r.home_team] if r.home_score > r.away_score else state.idx[r.away_team]
    return out


def _meta_rows(state: State, s: np.ndarray) -> list[dict]:
    rows = []
    for i, t in enumerate(state.meta):
        rows.append({"team_id": t["team_id"], "abbr": t["abbr"], "name": t["name"], "conference": t["conference"],
                     "division": t["division"], "wins": int(state.w[i]), "losses": int(state.l[i]), "ties": int(state.t[i]),
                     "strength": round(float(s[i]), 4)})
    return rows


METHOD = ("Monte Carlo of every remaining regular-season game, then the NFL bracket. A team is its record{blend}, "
          "regressed toward .500 by {prior:g} games; one game is log5 with home field applied as an odds multiplier "
          "(home win rate {hwr:.3f}, fit on {fit}); the Super Bowl is at a neutral site. Seeds: four division winners, "
          "then three wild cards; 2v7, 3v6, 4v5, the 1 seed has the bye, then the divisional round reseeds. Ties in the "
          "standings are broken by win percentage then a coin flip (no head-to-head chains). A tie game counts half a win. "
          "No injuries, weather, rest or betting lines.{bt}")


def build(season: int, sims: int = C.DEFAULT_SIMS, seed: int | None = 2026) -> dict:
    p = params()
    fit_seasons = list(range(season - 5, season))
    hwr = fit_home_rate(fit_seasons)
    teams = load_teams()
    df = load_schedule([season])
    st = State(teams, df)
    rows = simulate(st, sims, seed, home_win_rate=hwr)
    status = "regular_season"
    if len(st.post):
        status = "complete" if (st.post.game_type == "SB").any() and st.post[st.post.game_type == "SB"].home_score.notna().all() else "playoffs"
    bt = ""
    bpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs", "championship_backtest_nfl.json")
    if os.path.exists(bpath):
        try:
            with open(bpath) as f:
                bt = " " + (json.load(f).get("headline") or "")
        except Exception:
            bt = ""
    method = METHOD.format(blend=(f" blended {p['blend']:.0%} with points for/against" if p["blend"] else ""),
                           prior=p["prior"], hwr=hwr, fit=f"{fit_seasons[0]}-{fit_seasons[-1]}", bt=bt)
    note = f"{st.remaining_count} regular-season games left when this ran."
    doc = C.payload(sport_label="nfl", season=season, sims=sims, model_version=MODEL_VERSION, method=method,
                    home_win_rate=hwr, teams=rows, note=note, status=status)
    bad = C.validate(doc, 32, FIELD)
    if bad:
        raise ValueError("NFL file failed validation: " + "; ".join(bad[:5]))
    return doc


def current_season() -> int:
    import datetime as dt
    d = dt.date.today()
    return d.year if d.month >= 3 else d.year - 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--sims", type=int, default=C.DEFAULT_SIMS)
    ap.add_argument("--force", action="store_true", help="run out of season")
    a = ap.parse_args(argv)
    if not a.force and not C.in_season(IN_SEASON_MONTHS):
        print("[nfl_championship] out of season: leaving the last file" )
        return 0
    season = a.season or current_season()
    try:
        doc = build(season, a.sims)
    except Exception as e:  # feed down or a validation breach: never publish a partial list
        print(f"[nfl_championship] {type(e).__name__}: {e}")
        if C.carry_forward(a.out, f"Feed unavailable on this run ({type(e).__name__}); showing the previous run."):
            print("[nfl_championship] carried the previous file forward")
        return 0
    C.write_json(doc, a.out)
    top = doc["teams"][0]
    print(f"[nfl_championship] {a.out}: season {season}, status {doc['status']}, top {top['abbr']} {top['win_champion']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
