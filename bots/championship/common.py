"""Shared arithmetic for the NFL / NHL / NBA championship simulators (2026-10-14).

Spec: BOT-TASK-CHAMPIONSHIP-ODDS.md. MLB's bots/playoff_odds.py is the model for the
approach (regressed record -> log5 -> home edge as an odds multiplier -> play every
remaining game 10,000 times -> seed -> bracket -> tally) and is NOT imported or edited:
its win_probability / _play_series hard-wire MLB's home win rate, so the same arithmetic
lives here with the home rate as a parameter.

Everything a team is, to this model, is its record and its points for / against. No
injuries, no betting lines. Shares are 0-1, rounded to 4 places.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
from dataclasses import dataclass, field

import numpy as np

ROUNDS = ("win_round1", "win_round2", "win_round3", "win_round4")
DEFAULT_SIMS = 10000


# ── the arithmetic ───────────────────────────────────────────────────────────

def home_odds_ratio(home_win_rate: float) -> float:
    """Home edge as a multiplier on the odds: at an even matchup it reproduces the league's home win rate."""
    return home_win_rate / (1.0 - home_win_rate)


def win_prob(home: float, away: float, home_win_rate: float, neutral: bool = False) -> float:
    """P(home team wins): log5 on the two strengths, then the home odds multiplier (none at a neutral site)."""
    denom = home + away - 2.0 * home * away
    p = 0.5 if denom <= 0 else (home - home * away) / denom
    p = min(0.999, max(0.001, p))
    if neutral:
        return p
    odds = (p / (1.0 - p)) * home_odds_ratio(home_win_rate)
    return min(0.999, max(0.001, odds / (1.0 + odds)))


def win_prob_array(home: np.ndarray, away: np.ndarray, home_win_rate: float) -> np.ndarray:
    denom = home + away - 2.0 * home * away
    p = np.where(denom <= 0, 0.5, (home - home * away) / np.where(denom <= 0, 1.0, denom))
    p = np.clip(p, 0.001, 0.999)
    odds = (p / (1.0 - p)) * home_odds_ratio(home_win_rate)
    return np.clip(odds / (1.0 + odds), 0.001, 0.999)


def pythag(pf: float, pa: float, exponent: float) -> float:
    if pf <= 0 and pa <= 0:
        return 0.5
    a, b = max(pf, 0.0) ** exponent, max(pa, 0.0) ** exponent
    return a / (a + b) if (a + b) > 0 else 0.5


def strength(win_units: float, games: float, pf: float, pa: float, prior: float,
             blend: float, exponent: float) -> float:
    """Regressed record, optionally blended with the Pythagorean rate. Both are pulled toward .500 by `prior` games.

    win_units = wins (ties count 0.5; for the NHL, regulation + OT + shootout wins, so the league mean is .500).
    """
    rec = (win_units + 0.5 * prior) / (games + prior) if (games + prior) > 0 else 0.5
    if blend <= 0 or games <= 0:
        return rec
    py = (pythag(pf, pa, exponent) * games + 0.5 * prior) / (games + prior)
    return (1.0 - blend) * rec + blend * py


def play_single(rng: random.Random, hi: float, lo: float, home_win_rate: float, neutral: bool = False) -> bool:
    """One game, higher seed at home (unless neutral). True if the higher seed wins."""
    return rng.random() < win_prob(hi, lo, home_win_rate, neutral)


def play_series(rng: random.Random, hi: float, lo: float, length: int, home_pattern: list[bool],
                home_win_rate: float) -> bool:
    """True if the higher seed wins. `home_pattern[i]` is True when the higher seed is home in game i."""
    need = length // 2 + 1
    hw = lw = 0
    for i in range(length):
        at_home = home_pattern[i] if i < len(home_pattern) else True
        p = win_prob(hi, lo, home_win_rate) if at_home else 1.0 - win_prob(lo, hi, home_win_rate)
        if rng.random() < p:
            hw += 1
        else:
            lw += 1
        if hw == need or lw == need:
            break
    return hw > lw


BEST_OF_7_HOME = [True, True, False, False, True, False, True]   # 2-2-1-1-1


# ── the tally ────────────────────────────────────────────────────────────────

@dataclass
class Tally:
    """Counts across simulated seasons for ONE team."""
    make_playoffs: float = 0.0
    win_division: float = 0.0
    top_seed: float = 0.0
    proj_wins: float = 0.0
    play_in: float = 0.0
    seeds: dict = field(default_factory=dict)
    rounds: list = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    def seed(self, n: int) -> None:
        self.seeds[n] = self.seeds.get(n, 0) + 1


def r4(x: float) -> float:
    return round(float(x), 4)


def team_rows(meta: list[dict], tallies: dict, sims: int, seed_keys: int) -> list[dict]:
    """The published `teams[]`, sorted by champion share. `meta` rows carry the static fields (see each sport)."""
    out = []
    for m in meta:
        t = tallies[m["team_id"]]
        row = dict(m)
        row.update({
            "proj_wins": round(t.proj_wins / sims, 1),
            "make_playoffs": r4(t.make_playoffs / sims),
            "win_division": r4(t.win_division / sims),
            "top_seed": r4(t.top_seed / sims),
            "seeds": {str(k): r4(t.seeds.get(k, 0) / sims) for k in range(1, seed_keys + 1)},
        })
        for name, c in zip(ROUNDS, t.rounds):
            row[name] = r4(c / sims)
        row["win_champion"] = row["win_round4"]
        if t.play_in:
            row["play_in"] = r4(t.play_in / sims)
        out.append(row)
    out.sort(key=lambda r: (-r["win_champion"], -r["make_playoffs"], r["abbr"]))
    return out


def monotone_rounds(row: dict) -> None:
    """Counts are tallied from one bracket so they are monotone by construction; this is the guard."""
    vals = [row[n] for n in ROUNDS]
    assert all(vals[i] >= vals[i + 1] - 1e-9 for i in range(3)), (row["abbr"], vals)


def payload(*, sport_label: str, season: int, sims: int, model_version: str, method: str,
            home_win_rate: float, teams: list[dict], as_of: str | None = None, note: str = "",
            status: str = "regular_season") -> dict:
    return {
        "schema": 1,
        "season": season,
        "as_of": as_of or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "sims": sims,
        "model_version": model_version,
        "status": status,
        "method": method,
        "note": note,
        "home_win_rate": round(home_win_rate, 4),
        "teams": teams,
    }


# ── the validator: the site's reader rules (section 1 of the spec), in Python ─

def validate(doc: dict, n_teams: int, field_size: int) -> list[str]:
    """Return a list of breaches; empty = the site's reader would accept the file."""
    bad: list[str] = []
    if not str(doc.get("method") or "").strip():
        bad.append("method is empty")
    if not (isinstance(doc.get("sims"), int) and doc["sims"] > 0):
        bad.append("sims must be > 0")
    teams = doc.get("teams") or []
    if len(teams) != n_teams:
        bad.append(f"expected {n_teams} teams, got {len(teams)}")
    share_keys = ("make_playoffs", "win_division", "top_seed", *ROUNDS, "win_champion")
    for t in teams:
        for k in share_keys:
            v = t.get(k)
            if v is None:
                continue
            if not (isinstance(v, (int, float)) and 0.0 <= v <= 1.0):
                bad.append(f"{t.get('abbr')}.{k}={v} outside [0,1]")
        for k, v in (t.get("seeds") or {}).items():
            if not (0.0 <= v <= 1.0):
                bad.append(f"{t.get('abbr')}.seeds[{k}]={v} outside [0,1]")
        r = [t.get(n) for n in ROUNDS]
        if all(isinstance(x, (int, float)) for x in r) and any(r[i] < r[i + 1] - 1e-9 for i in range(3)):
            bad.append(f"{t.get('abbr')}: rounds not monotone {r}")
        if t.get("win_round4") is not None and abs(t["win_round4"] - t.get("win_champion", -1)) > 1e-9:
            bad.append(f"{t.get('abbr')}: win_round4 != win_champion")
    total = sum(t.get("win_champion", 0.0) for t in teams)
    if abs(total - 1.0) > 0.05:
        bad.append(f"win_champion sums to {total:.4f}")
    mk = sum(t.get("make_playoffs", 0.0) for t in teams)
    if abs(mk - field_size) > 0.05 * max(field_size, 1):
        bad.append(f"make_playoffs sums to {mk:.2f}, field is {field_size}")
    return bad


# ── file handling ────────────────────────────────────────────────────────────

def write_json(doc: dict, out: str) -> None:
    import os
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, separators=(",", ":"), sort_keys=False)
    os.replace(tmp, out)


def carry_forward(out: str, note: str) -> bool:
    """Feed down: keep yesterday's file unchanged (same as_of), add `note`. Returns True if a file was carried."""
    import os
    if not os.path.exists(out):
        return False
    with open(out) as f:
        doc = json.load(f)
    doc["note"] = note
    write_json(doc, out)
    return True


def fit_home_win_rate(results: list[tuple[int, int]], lo: float, hi: float, label: str) -> float:
    """results = [(home_score, away_score)]. Ties count half. Raises if outside the sanity bounds (a data bug)."""
    n = len(results)
    if n == 0:
        raise ValueError(f"{label}: no games to fit the home win rate")
    w = sum(1.0 if h > a else 0.5 if h == a else 0.0 for h, a in results)
    rate = w / n
    if not (lo <= rate <= hi):
        raise ValueError(f"{label}: home win rate {rate:.4f} outside sanity bounds [{lo}, {hi}]")
    return round(rate, 4)


def sample_wins(base: np.ndarray, home_idx: np.ndarray, away_idx: np.ndarray, p_home: np.ndarray,
                sims: int, nprng: np.random.Generator, chunk: int = 2000):
    """Yield (final_wins[chunk, n], home_won[chunk, G]) for the remaining games, in chunks (memory)."""
    n = len(base)
    done = 0
    while done < sims:
        c = min(chunk, sims - done)
        if len(p_home):
            hw = nprng.random((c, len(p_home))) < p_home          # True = home team wins
        else:
            hw = np.zeros((c, 0), dtype=bool)
        wins = np.tile(base.astype(np.float64), (c, 1))
        for j in range(c):
            if len(p_home):
                wins[j] += np.bincount(home_idx[hw[j]], minlength=n) + np.bincount(away_idx[~hw[j]], minlength=n)
        yield wins, hw
        done += c


def in_season(months: tuple, today: dt.date | None = None) -> bool:
    """A sport out of season does not run (spec section 3): its last file stays, and its as_of says how old it is."""
    return (today or dt.date.today()).month in months


def seeds_rng(seed: int | None):
    return random.Random(seed), np.random.default_rng(seed)
