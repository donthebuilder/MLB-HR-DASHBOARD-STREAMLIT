#!/usr/bin/env python3
"""nfl_ladder.py -- the high-bar ladder (bot task 4B, 2026-10-11).

The weekly card ranks each market at ONE bar (REC_YDS 40, RUSH_YDS 50, REC 4,
PASS_YDS 225). The site's Card wants a straight at a HIGH rung: receiving or
rushing 100+, passing 300+, 7+ catches. The model scores a player's volume, not
a bar, so there is no per-bar model score and none is invented. What exists, and
is real, is how often he has actually cleared each rung:

  rate   the share of his last LADDER_GAMES logged games (this season, then
         last, newest first) in which he reached the bar
  n      how many games that is over -- a rate always travels with its n

Per market and per rung the ranked list is: players the market's own model
scores (right position, not on a bye, not flagged low-sample, at least
MIN_GAMES logged), ordered by rate, then the market's model score, then
player_id. A rung nobody has cleared in the window is an EMPTY list, not a
padded one.

NOT A PROBABILITY. `rate` is a count over a window and is stored on the leg as
a rate, never printed as a chance. The PRICE of a rung comes only from the
site's odds capture (odds_lines `<market>_alt`); nothing here prices anything.

Published as nfl_ladder.json under its own model_version (nfl_ladder_v1) --
a new file, not a key inside nfl_picks.json, so the published card (its rungs,
its bars, its archived weeks) is untouched. The 40+/50+/4+/225+ boards are NOT
relabelled: the lowest rung of each ladder is the card's own bar and is listed
for reference only.
"""
from __future__ import annotations

import math

MODEL_VERSION = "nfl_ladder_v1"
LADDER_GAMES = 16
MIN_GAMES = 5
DEPTH = 5

# market -> (log column, label, rungs). Lowest rung = the card's own bar.
MARKETS = {
    "REC_YDS":  ("g_recyd", "Receiving yards", [40, 60, 80, 100]),
    "RUSH_YDS": ("g_ruyd",  "Rushing yards",   [50, 60, 80, 100]),
    "PASS_YDS": ("g_payd",  "Passing yards",   [225, 250, 275, 300]),
    "REC":      ("g_rec",   "Receptions",      [4, 5, 6, 7]),
}


def _fin(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def window(log: list[dict], col: str, n: int = LADDER_GAMES) -> list[float]:
    """The values of `col` over his newest `n` logged games, newest last."""
    rows = [e for e in log or [] if isinstance(e, dict) and _fin(e.get(col))
            and _fin(e.get("s")) and _fin(e.get("w"))]
    rows.sort(key=lambda e: (e["s"], e["w"]))
    return [float(e[col]) for e in rows[-n:]]


def rate_at(vals: list[float], bar: float) -> "float | None":
    return round(sum(1 for v in vals if v >= bar) / len(vals), 3) if vals else None


def build(players: list[dict], logs: dict, depth: int = DEPTH) -> dict:
    """{market: {label, positions, rungs: [{bar, list: [...]}]}}"""
    out: dict = {}
    for key, (col, label, bars) in MARKETS.items():
        pool = []
        for p in players or []:
            sc = (p.get("scores") or {}).get(key)
            if p.get("on_bye") or p.get("low_sample") or not _fin(sc):
                continue
            vals = window(((logs or {}).get(p.get("player_id")) or {}).get("log") or [], col)
            if len(vals) < MIN_GAMES:
                continue
            pool.append((p, float(sc), vals))
        ladders = []
        for bar in bars:
            ranked = []
            for p, sc, vals in pool:
                r = rate_at(vals, bar)
                if r and r > 0:
                    ranked.append((-r, -sc, str(p.get("player_id")), p, sc, r, len(vals)))
            ranked.sort(key=lambda t: t[:3])
            ladders.append({"bar": bar, "rungs": [{
                "rank": i + 1, "player_id": p.get("player_id"), "name": p.get("name"),
                "team": p.get("team"), "opp": p.get("opp"), "position": p.get("position"),
                "rate": r, "n": n, "score": round(sc, 1),
                "questionable": bool(p.get("questionable")),
            } for i, (_, _, _, p, sc, r, n) in enumerate(ranked[:depth])]})
        out[key] = {"key": key, "label": label, "card_bar": bars[0], "ladder": ladders}
    return out
