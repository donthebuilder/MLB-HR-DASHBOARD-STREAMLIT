#!/usr/bin/env python3
"""nfl_gamelog.py — per-game outcomes and hit rates against a line.

This is the prop-research primitive: not "he averages 4.6 receptions" but
"he cleared 4.5 in 7 of his last 10, and here is the bar for every one of
those games." An average hides the shape — two 12-catch games and eight
2-catch games average the same as ten 4-catch games and are not the same bet.

Produces, per player:
  · a chronological game log carrying every market's value
  · hit rates over L5 / L10 / L20 / this season / prior season, per market,
    against that market's default bar

The site draws the bars from the log, so changing the line re-grades in the
browser without another bot run — which is the whole point of shipping the
log rather than a precomputed percentage.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

# market -> (column expression name, default bar)
MARKET_VALUE = {
    "TD":       ("g_td",    1),
    "REC_YDS":  ("g_recyd", 40),
    "REC":      ("g_rec",   4),
    "RUSH_YDS": ("g_ruyd",  50),
    "RUSH_ATT": ("g_car",   12),
    "PASS_YDS": ("g_payd",  225),
    "KICK_PTS": ("g_kick",  6),
}

WINDOWS = {"l5": 5, "l10": 10, "l20": 20}

_WD = {"Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed", "Thursday": "Thu",
       "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun"}


@functools.lru_cache(maxsize=6)
def _sched(season: int) -> pl.DataFrame:
    return nfl.load_schedules(seasons=[season]).filter(pl.col("game_type") == "REG")


def game_context(season: int) -> dict:
    """{(week, team): per-game facts}, from the nflverse schedule.

    Each fact comes straight from a schedule column (verified present in
    nflreadpy 0.1.5: gameday, weekday, location, home/away_score, home/away_rest,
    roof, surface). Nothing is guessed: a column that is null stays absent.

      d   game date, YYYY-MM-DD (the game's own date)
      h   1 home, 0 away; ABSENT at a neutral site (location == "Neutral")
      wd  kickoff weekday, 3 letters
      rf  roof     (outdoors / dome / closed / open)
      sf  surface  (grass / fieldturf / ...)
      r   result for THIS team, "W" / "L" / "T"; absent until the game has a score
      rs  rest days before the game (schedule home_rest / away_rest)
    """
    try:
        sc = _sched(season)
    except Exception:
        return {}
    out: dict = {}
    for g in sc.iter_rows(named=True):
        hs, as_ = g.get("home_score"), g.get("away_score")
        for side in ("home", "away"):
            team = g.get(f"{side}_team")
            if not team or g.get("week") is None:
                continue
            c: dict = {}
            if g.get("gameday"):
                c["d"] = str(g["gameday"])
            if g.get("location") != "Neutral":
                c["h"] = 1 if side == "home" else 0
            if g.get("weekday") in _WD:
                c["wd"] = _WD[g["weekday"]]
            if g.get("roof"):
                c["rf"] = g["roof"]
            if g.get("surface"):
                c["sf"] = g["surface"]
            if hs is not None and as_ is not None:
                mine, theirs = (hs, as_) if side == "home" else (as_, hs)
                c["r"] = "W" if mine > theirs else ("L" if mine < theirs else "T")
            rest = g.get(f"{side}_rest")
            if rest is not None:
                c["rs"] = int(rest)
            out[(int(g["week"]), team)] = c
    return out


@functools.lru_cache(maxsize=6)
def _weekly(season: int) -> pl.DataFrame:
    return (nfl.load_player_stats(seasons=[season], summary_level="week")
              .filter(pl.col("season_type") == "REG"))


def _shape(season: int) -> pl.DataFrame:
    d = _weekly(season)
    cols = ["receiving_tds", "rushing_tds", "receiving_yards", "receptions",
            "rushing_yards", "carries", "passing_yards", "fg_made", "pat_made", "targets"]
    for c in cols:
        if c in d.columns:
            d = d.with_columns(pl.col(c).fill_null(0))
        else:
            d = d.with_columns(pl.lit(0).alias(c))
    return d.select([
        "player_id", "player_display_name", "position", "team", "opponent_team", "week",
        (pl.col("receiving_tds") + pl.col("rushing_tds")).alias("g_td"),
        pl.col("receiving_yards").alias("g_recyd"),
        pl.col("receptions").alias("g_rec"),
        pl.col("rushing_yards").alias("g_ruyd"),
        pl.col("carries").alias("g_car"),
        pl.col("passing_yards").alias("g_payd"),
        (pl.col("fg_made") * 3 + pl.col("pat_made")).alias("g_kick"),
        pl.col("targets").alias("g_tgt"),
        pl.lit(season).alias("season"),
    ])


def build(seasons: list[int], min_games: int = 3) -> dict:
    """{player_id: {"log": [...], "rates": {market: {window: [hits, n]}}}}"""
    frames = []
    for s in seasons:
        try:
            frames.append(_shape(s))
        except Exception:
            continue
    if not frames:
        return {}
    d = pl.concat(frames).sort(["player_id", "season", "week"])
    ctx = {s_: game_context(s_) for s_ in seasons}

    out: dict = {}
    for pid, grp in d.group_by("player_id", maintain_order=True):
        pid = pid[0] if isinstance(pid, tuple) else pid
        rows = grp.to_dicts()
        if len(rows) < min_games:
            continue

        log = [{
            "s": int(r["season"]), "w": int(r["week"]),
            "opp": r["opponent_team"], "tm": r["team"],
            # ADDITIVE (2026-10-06): date / home-away / weekday / roof / surface /
            # result / rest, from the schedule. Old keys above are unchanged.
            **ctx.get(int(r["season"]), {}).get((int(r["week"]), r["team"]), {}),
            **{MARKET_VALUE[m][0]: round(float(r[MARKET_VALUE[m][0]] or 0), 1)
               for m in MARKET_VALUE},
        } for r in rows]

        rates: dict = {}
        for m, (col, bar) in MARKET_VALUE.items():
            vals = [float(r[col] or 0) for r in rows]
            if not any(vals):
                continue          # he doesn't play this market at all
            block = {}
            for wname, n in WINDOWS.items():
                tail = vals[-n:]
                if tail:
                    block[wname] = [sum(1 for v in tail if v >= bar), len(tail)]
            # per-season, so "'24-'25 vs '25-'26" reads like the reference
            for s in seasons:
                sv = [float(r[col] or 0) for r in rows if int(r["season"]) == s]
                if sv:
                    block[str(s)] = [sum(1 for v in sv if v >= bar), len(sv)]
            rates[m] = block

        out[pid] = {"log": log, "rates": rates}
    return out


if __name__ == "__main__":
    import json
    g = build([2024, 2025])
    print(f"{len(g)} players with logs")
    wk = nfl.load_player_stats(seasons=[2025], summary_level="week")
    who = wk.filter(pl.col("player_display_name") == "Bijan Robinson")["player_id"].to_list()
    if who:
        b = g[who[0]]
        print("games:", len(b["log"]))
        print("TD rates:", b["rates"]["TD"])
        print("REC_YDS rates:", b["rates"]["REC_YDS"])
        print("last 3 games:", json.dumps(b["log"][-3:])[:300])
