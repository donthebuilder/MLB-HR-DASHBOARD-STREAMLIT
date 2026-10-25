#!/usr/bin/env python3
"""
nfl_first.py -- FIRST TOUCHDOWN of the game: the TD model's best player, usage, EARLY usage, or the matchup?

    python bots/nfl/nfl_first.py [--cache DIR]

Per game from nflverse play-by-play: who scored the game's FIRST touchdown, and every carry and target in the first 20 offensive plays (both teams, the
opening drives) by player. Candidates = the RB / WR / TE rows nfl_features.build() already has (3+ games that season); outcome = he scored the first TD
(random is about 1 in 40). Families, alone and together, fit on 2023-24 and graded on 2025:
  best player   nfl_td_v2's score (the live anytime-TD model: red-zone volume, implied total, touches, xTD, snaps)
  usage         trailing touches (targets + carries) per game
  early usage   trailing early touches per game and his share of his team's early touches
  history       how often he has scored the game's first TD
  matchup       how often the opponent has conceded the first TD of its games, home field
NOTE: nfl_td_v2's weights were tuned on 2024-25, so on this test it has seen the seasons it is graded against; read its row as its best case.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nfl_features as F          # noqa: E402
import nfl_td_v3_backtest as T    # noqa: E402
import nfl_tuddy_backtest as NT   # noqa: E402
from nfl_scoring import MODELS, derive   # noqa: E402
from tune_models import fit_logistic     # noqa: E402

EARLY_PLAYS = 20


def pbp_facts(season):
    p = F._pbp(season).filter(pl.col("season_type") == "REG")
    if "order_sequence" in p.columns:
        p = p.sort(["game_id", "order_sequence"])
    else:
        p = p.sort(["game_id", "play_id"])
    first, early, order = {}, defaultdict(lambda: defaultdict(int)), {}
    for gid, g in p.group_by("game_id", maintain_order=True):
        gid = gid[0] if isinstance(gid, tuple) else gid
        td = g.filter((pl.col("touchdown") == 1) & pl.col("td_player_id").is_not_null()).head(1)
        if td.height:
            r = td.row(0, named=True)
            first[gid] = {"pid": r["td_player_id"], "team": r["td_team"]}
        offense = g.filter(pl.col("play_type").is_in(["run", "pass"])).head(EARLY_PLAYS)
        for r in offense.iter_rows(named=True):
            who = r["rusher_player_id"] if r["play_type"] == "run" else r["receiver_player_id"]
            if who:
                early[gid][who] += 1
        order[gid] = int(g["week"][0])
    return first, early


def build_rows(cache, seasons=(2023, 2024, 2025), live=None):
    """live = {"season": 2026, "week": 7, "slate": nfl_features.upcoming_rows(...)[0]}: also returns y=None rows for that coming week (flag live=True)."""
    class a:      # noqa: N801  (keeps the body below unchanged)
        pass
    a.cache = cache
    V2 = MODELS["TD"]["w"]
    rows = []
    for season in seasons:
        tbl = NT.season_table(season, a.cache)
        first, early = pbp_facts(season)
        d = derive(tbl).filter(pl.col("position").is_in(MODELS["TD"]["pos"]), pl.col("season_type") == "REG")
        d = T._score(d, V2).rename({"score": "v2score"})
        base = d.select("player_id", "week", "team", "opponent_team", "game_id", "position", "v2score", "f_touches", "is_home", "implied_total", "spread", pl.col("f_gp")).to_dicts()
        ph = defaultdict(list); th = defaultdict(list)
        by_week = defaultdict(list)
        for r in base:
            by_week[r["week"]].append(r)
        if live and live["season"] == season:
            ld = derive(live["slate"]).filter(pl.col("position").is_in(MODELS["TD"]["pos"]))
            ld = T._score(ld, V2).rename({"score": "v2score"})
            for r in ld.select("player_id", "week", "team", "opponent_team", "position", "v2score", "f_touches", "is_home", "implied_total", "spread", pl.col("f_gp")).to_dicts():
                r["game_id"] = f"{season}_{int(live['week']):02d}_" + "_".join(sorted([r["team"], r["opponent_team"]])); r["live"] = True
                by_week[int(live["week"])].append(r)
        for wk in sorted(by_week):
            games_this_week = {r["game_id"] for r in by_week[wk]}
            team_early = defaultdict(float)
            for gid in games_this_week:
                for r in by_week[wk]:
                    if r["game_id"] == gid:
                        team_early[(gid, r["team"])] += early[gid].get(r["player_id"], 0)
            for r in by_week[wk]:
                h = ph[r["player_id"]]
                gid = r["game_id"]
                if len(h) >= 3 and (gid in first or r.get("live")):
                    h4 = h[-4:]
                    team_tot = sum(x["team_early"] for x in h4) or 1
                    opp_h = th[r["opponent_team"]]
                    rows.append({"season": season, "game": gid, "week": wk, "y": int(gid in first and first[gid]["pid"] == r["player_id"]), "live": bool(r.get("live")), "v2": r["v2score"] or 0.0, "touches": r["f_touches"] or 0.0,
                                 "early_pg": sum(x["early"] for x in h4) / len(h4), "early_share": sum(x["early"] for x in h4) / team_tot,
                                 "first_rate": sum(x["first"] for x in h) / len(h),
                                 "opp_concedes_first": (sum(x["conceded"] for x in opp_h[-8:]) / len(opp_h[-8:])) if opp_h else 0.5, "is_home": float(r["is_home"] or 0), "implied_total": float(r.get("implied_total") or 0.0), "spread": float(r.get("spread") or 0.0), "pid": r["player_id"], "team": r["team"], "position": r.get("position")})
            for r in by_week[wk]:
                gid = r["game_id"]
                ph[r["player_id"]].append({"early": early[gid].get(r["player_id"], 0), "team_early": team_early[(gid, r["team"])],
                                           "first": int(gid in first and first[gid]["pid"] == r["player_id"])})
            for gid in games_this_week:
                if gid in first:
                    for r in by_week[wk]:
                        if r["game_id"] == gid and r["team"] not in {x for x in ()}:
                            pass
                teams = {r["team"] for r in by_week[wk] if r["game_id"] == gid}
                for t in teams:
                    th[t].append({"conceded": int(gid in first and first[gid]["team"] != t)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nfl"))
    a = ap.parse_args()
    rows = build_rows(a.cache)
    tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
    games = defaultdict(list)
    for r in te:
        games[r["game"]].append(r)
    games = {k: v for k, v in games.items() if len(v) >= 15}
    pool = sum(len(v) for v in games.values()) / len(games)
    print(f"{len(rows)} player-games; fit on {len(tr)} (2023-24), graded on {len(games)} games of 2025 (about {pool:.0f} candidates a game, random {100 / pool:.1f}%)\n")

    def evaluate(label, key=None, cols=None):
        if cols:
            X = lambda rs: np.array([[r[c] for c in cols] for r in rs], float)
            Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            w = fit_logistic((Xtr - mu) / sd, np.array([r["y"] for r in tr], float), l2=20.0)
            for r, z in zip(te, ((X(te) - mu) / sd) @ w[1:] + w[0]):
                r["_s"] = float(z)
            note = "  weights: " + ", ".join(f"{c} {x:+.2f}" for c, x in sorted(zip(cols, w[1:]), key=lambda t: -abs(t[1]))[:4])
        else:
            for r in te:
                r["_s"] = r[key]
            note = ""
        t = [0, 0, 0]
        for v in games.values():
            order = sorted(v, key=lambda r: r["_s"], reverse=True)
            t[0] += order[0]["y"]; t[1] += sum(r["y"] for r in order[:3]); t[2] += sum(r["y"] for r in order[:5])
        n = len(games)
        print(f"   {label:<36} top 1: {100 * t[0] / n:5.1f}%   top 3: {100 * t[1] / n:5.1f}%   top 5: {100 * t[2] / n:5.1f}%{note}")
    evaluate("best player (nfl_td_v2 score)", key="v2")
    evaluate("usage (touches per game)", key="touches")
    evaluate("EARLY usage (first 20 plays, share)", key="early_share")
    evaluate("early touches per game", key="early_pg")
    evaluate("his first-TD history", key="first_rate")
    evaluate("v2 + early usage", cols=["v2", "early_pg", "early_share"])
    evaluate("v2 + early usage + history", cols=["v2", "early_pg", "early_share", "first_rate"])
    evaluate("all of that + matchup", cols=["v2", "early_pg", "early_share", "first_rate", "opp_concedes_first", "is_home"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
