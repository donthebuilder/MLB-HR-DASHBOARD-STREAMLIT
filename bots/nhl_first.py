#!/usr/bin/env python3
"""
nhl_first.py -- FIRST GOAL of the game: best player, usage, early usage, or matchup?

    python bots/nhl_first.py pull  [--cache DIR]     (once: NHL play-by-play, 2023-25, compact first_<gameId>.json)
    python bots/nhl_first.py study [--cache DIR]

Per game from the play-by-play: who scored the game's FIRST goal, and every shot attempt (on goal, missed, blocked, goal) in the first 6:00 of the first period
by shooter -- the "first shifts" usage. Study rows: every regular skater (10+ minutes a game, 8+ games) who played; outcome = he scored the first goal (random
is roughly 1 in 25). Families, alone and together, fit on 2023-24 and graded on 2025-26:
  best player   goals per game        usage   shots per game, ice time        early usage  his share of his team's first-6:00 attempts
  history       how often he has scored the game's first goal              matchup   how often the opponent concedes the first goal, home ice
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as nb   # noqa: E402
from tune_models import fit_logistic   # noqa: E402

PBP = "https://api-web.nhle.com/v1/gamecenter/{}/play-by-play"
ATTEMPT = {"shot-on-goal", "missed-shot", "blocked-shot", "goal"}


def secs(t):
    try:
        m, s = str(t).split(":")
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def one(gid, cache):
    f = Path(cache) / f"nhlfirst_{gid}.json"
    if f.exists():
        return json.load(open(f))
    for _ in range(3):
        raw = subprocess.run(["curl", "-s", "-m", "60", PBP.format(gid)], capture_output=True, text=True).stdout
        if raw.startswith("{"):
            d = json.loads(raw)
            break
    else:
        return None
    first, early = None, defaultdict(int)
    for p in d.get("plays", []):
        k = p.get("typeDescKey")
        det = p.get("details") or {}
        per = (p.get("periodDescriptor") or {}).get("number")
        t = secs(p.get("timeInPeriod"))
        if k == "goal" and first is None:
            first = {"pid": det.get("scoringPlayerId"), "team_id": det.get("eventOwnerTeamId")}
        if per == 1 and t is not None and t <= 6 * 60 and k in ATTEMPT:
            who = det.get("scoringPlayerId") if k == "goal" else det.get("shootingPlayerId")
            if who:
                early[str(who)] += 1
    out = {"id": gid, "first": first, "early": dict(early), "home": (d.get("homeTeam") or {}).get("abbrev"), "away": (d.get("awayTeam") or {}).get("abbrev"),
           "home_id": (d.get("homeTeam") or {}).get("id"), "away_id": (d.get("awayTeam") or {}).get("id")}
    f.write_text(json.dumps(out))
    return out


def games_by_season(cache):
    out = {}
    for s in (2023, 2024, 2025):
        data = nb.load_season(s, cache)
        out[s] = data
    return out


def pull(cache):
    ids = []
    for s, data in games_by_season(cache).items():
        ids += sorted({r["gameId"] for r in data["skater/summary"]})
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda g: one(g, cache), ids))
    print(f"{sum(1 for r in res if r)} of {len(ids)} games, {sum(1 for r in res if r and r['first'])} with a goal")


def study(cache):
    rows = []
    cache = Path(cache)
    for season, data in games_by_season(str(cache)).items():
        per_game = defaultdict(list)
        for r in data["skater/summary"]:
            if r["positionCode"] != "G" and r.get("timeOnIcePerGame"):
                per_game[r["gameId"]].append(r)
        order = sorted(per_game, key=lambda g: (min(x["gameDate"] for x in per_game[g]), g))
        P = defaultdict(list); T = defaultdict(list)
        for gid in order:
            f = cache / f"nhlfirst_{gid}.json"
            fd = json.load(open(f)) if f.exists() else None
            rs = per_game[gid]
            teams = defaultdict(list)
            for r in rs:
                teams[r["teamAbbrev"]].append(r)
            if fd and fd.get("first") and fd["first"]["pid"] and len(teams) == 2:
                ab_list = list(teams)
                for ab, mem in teams.items():
                    opp = [x for x in ab_list if x != ab][0]
                    t_hist, o_hist = T[ab], T[opp]
                    team_early = sum(sum(x["early"] for x in P[r["playerId"]][-10:]) for r in mem) or 1
                    for r in mem:
                        h = P[r["playerId"]]
                        if len(h) < 8 or sum(x["toi"] for x in h[-10:]) / len(h[-10:]) < 10:
                            continue
                        h10 = h[-10:]
                        rows.append({"season": season, "game": gid, "night": r["gameDate"], "y": int(fd["first"]["pid"] == r["playerId"]),
                                     "gpg": sum(x["g"] for x in h) / len(h), "spg": sum(x["s"] for x in h) / len(h), "toi": sum(x["toi"] for x in h10) / len(h10),
                                     "early_pg": sum(x["early"] for x in h10) / len(h10), "early_share": sum(x["early"] for x in h10) / team_early,
                                     "first_rate": sum(x["first"] for x in h) / len(h),
                                     "team_scores_first": (sum(x["scored_first"] for x in t_hist[-20:]) / len(t_hist[-20:])) if t_hist else 0.5,
                                     "opp_concedes_first": (sum(x["conceded_first"] for x in o_hist[-20:]) / len(o_hist[-20:])) if o_hist else 0.5,
                                     "is_home": float(r.get("homeRoad") == "H")})
            if fd and fd.get("first"):
                first_team = fd["first"]["team_id"]
                for ab, mem in teams.items():
                    my_id = fd["home_id"] if (mem[0].get("homeRoad") == "H") else fd["away_id"]
                    T[ab].append({"scored_first": int(first_team == my_id), "conceded_first": int(first_team != my_id)})
            for r in rs:
                P[r["playerId"]].append({"g": r["goals"], "s": r["shots"], "toi": r["timeOnIcePerGame"] / 60.0,
                                         "early": (fd or {}).get("early", {}).get(str(r["playerId"]), 0),
                                         "first": int(bool(fd and fd.get("first") and fd["first"]["pid"] == r["playerId"]))})
    tr = [r for r in rows if r["season"] in (2023, 2024)]; te = [r for r in rows if r["season"] == 2025]
    games = defaultdict(list)
    for r in te:
        games[r["game"]].append(r)
    games = {k: v for k, v in games.items() if len(v) >= 15}
    pool = sum(len(v) for v in games.values()) / len(games)
    print(f"{len(rows)} skater-games; fit on {len(tr)} (2023-24), graded on {len(games)} games of 2025-26 (about {pool:.0f} regular skaters a game, random {100 / pool:.1f}%)\n")

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
    evaluate("best player (goals per game)", key="gpg")
    evaluate("usage (shots per game)", key="spg")
    evaluate("usage (ice time)", key="toi")
    evaluate("EARLY usage (first-6:00 share)", key="early_share")
    evaluate("his first-goal history", key="first_rate")
    evaluate("all player terms", cols=["gpg", "spg", "toi", "early_pg", "early_share", "first_rate"])
    evaluate("all player terms + matchup", cols=["gpg", "spg", "toi", "early_pg", "early_share", "first_rate", "opp_concedes_first", "team_scores_first", "is_home"])
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "study"])
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nhl"))
    a = ap.parse_args()
    pull(a.cache) if a.cmd == "pull" else study(a.cache)
