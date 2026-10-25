#!/usr/bin/env python3
"""
nhl_backtest.py -- LAMP v1 against naive baselines, on every NHL game since 2023.

    python bots/nhl_backtest.py [--cache DIR] [--seasons 2023 2024 2025]

Per-game rows come from the NHL stats API (skater summary + power play, goalie
summary), pulled in weekly chunks and cached. A player is scored on his earlier
games THIS season only (5+ of them); the opposing goalie is the one who started
that night (starters are public pregame; this is the one place the backtest knows
something the model would be told at the same time).

Reported, for "scores 1+ goal":
  top 1 / top 3 per game   -- best candidates in each game, both teams
  top 10 per night         -- what a board would show
vs three baselines: season goals per game, season shots per game, and a coin.
There are no historical NHL prices here, so this cannot say what any of it paid.
"""
import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_scorer as nhl   # noqa: E402

BASE = "https://api.nhle.com/stats/rest/en/{}?isAggregate=false&isGame=true&limit=-1&cayenneExp={}"
SEASON_START = {2023: "2023-10-01", 2024: "2024-10-01", 2025: "2025-10-01", 2026: "2026-10-01"}
SEASON_END = {2023: "2024-04-30", 2024: "2025-04-30", 2025: "2026-04-30",
              2026: (dt.date.today() - dt.timedelta(days=1)).isoformat()}      # the season to date; the last weekly file's name changes daily, so it is never stale


def pull(report, a, b, cache):
    path = Path(cache) / f"{report.replace('/', '_')}_{a}_{b}.json"
    if path.exists():
        return json.load(open(path))
    expr = f'gameDate>="{a}" and gameDate<="{b}" and gameTypeId=2'
    # curl, not urllib: the framework Python on some Macs has no CA bundle.
    raw = subprocess.run(["curl", "-s", "-m", "60", "--retry", "3", BASE.format(report, quote(expr))],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(raw)["data"]
    path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(path, "w"))
    return data


def weeks(a, b):
    d, end = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    while d <= end:
        e = min(d + dt.timedelta(days=6), end)
        yield d.isoformat(), e.isoformat()
        d = e + dt.timedelta(days=1)


def load_season(season, cache):
    jobs = [(r, a, b) for a, b in weeks(SEASON_START[season], SEASON_END[season])
            for r in ("skater/summary", "skater/powerplay", "goalie/summary")]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda j: (j[0], pull(j[0], j[1], j[2], cache)), jobs))
    out = defaultdict(list)
    for r, rows in res:
        out[r].extend(rows)
    return out


def auc_free_topk(rows, key, k, group):
    g = defaultdict(list)
    for r in rows:
        g[r[group]].append(r)
    hit = n = 0
    for v in g.values():
        top = sorted(v, key=lambda r: r[key], reverse=True)[:k]
        hit += sum(r["goal"] for r in top); n += len(top)
    return 100 * hit / n if n else float("nan"), n


def run(season, cache, min_prior=5):
    return run_data(load_season(season, cache), season, min_prior)


def run_data(data, season, min_prior=5):
    """Features and scores for every skater-game in `data` ({report: rows}). nhl_live.py calls this with tonight's
    games appended as zero-stat rows, so the live board and the backtest share one feature code path."""
    pp = {(r["playerId"], r["gameId"]): r for r in data["skater/powerplay"]}
    by_player = defaultdict(list)
    for r in data["skater/summary"]:
        if r["positionCode"] == "G" or not r.get("timeOnIcePerGame"):
            continue
        p = pp.get((r["playerId"], r["gameId"]), {})
        by_player[r["playerId"]].append({
            "date": r["gameDate"], "game": r["gameId"], "team": r["teamAbbrev"], "opp": r["opponentTeamAbbrev"],
            "shots": r["shots"], "goals": r["goals"], "pp_goals": r["ppGoals"], "assists": r.get("assists") or 0, "points": r.get("points") or 0,
            "pp_points": r.get("ppPoints") or 0,
            "toi_min": r["timeOnIcePerGame"] / 60.0, "pp_toi_min": (p.get("ppTimeOnIce") or 0) / 60.0,
            "home": r.get("homeRoad") == "H", "pos": r["positionCode"], "name": r.get("skaterFullName")})
    goalies = defaultdict(list)                      # goalie id -> starts, oldest first
    starter = {}                                     # (gameId, goalie's opponent team) -> goalie id
    for r in sorted(data["goalie/summary"], key=lambda r: (r["gameDate"], r["gameId"])):
        if not r.get("gamesStarted"):
            continue
        goalies[r["playerId"]].append({"date": r["gameDate"], "saves": r["saves"], "shots_against": r["shotsAgainst"]})
        starter[(r["gameId"], r["opponentTeamAbbrev"])] = r["playerId"]
    team_sa = defaultdict(lambda: defaultdict(float))        # team -> gameId -> shots against
    team_dates = {}
    for rows in by_player.values():
        for x in rows:
            team_sa[x["opp"]][x["game"]] += x["shots"]
            team_dates[(x["opp"], x["game"])] = x["date"]

    # team -> sorted [(date, game)] and game -> {pid: (toi_min, goals)}: for "teammates out" and games missed
    tgames = defaultdict(dict)
    for pid, rows in by_player.items():
        for x in rows:
            tgames[x["team"]].setdefault(x["game"], {"date": x["date"], "pl": {}})["pl"][pid] = (x["toi_min"], x["goals"])
    order = {t: sorted(gs, key=lambda gid: (gs[gid]["date"], gid)) for t, gs in tgames.items()}
    pos_in = {t: {gid: i for i, gid in enumerate(o)} for t, o in order.items()}
    out_memo = {}

    def teammates_out(team, gid):
        k = (team, gid)
        if k not in out_memo:
            i = pos_in[team][gid]
            cut = (dt.date.fromisoformat(tgames[team][gid]["date"]) - dt.timedelta(days=60)).isoformat()      # not last season's roster (the offseason turns it over)
            recent = [tgames[team][q]["pl"] for q in order[team][max(0, i - 10):i] if tgames[team][q]["date"] >= cut]
            tonight = tgames[team][gid]["pl"]
            cnt, toi, gl = defaultdict(int), defaultdict(float), defaultdict(float)
            for gm in recent:
                for q, (t_, g_) in gm.items():
                    cnt[q] += 1; toi[q] += t_; gl[q] += g_
            # regulars (5+ of the last 10, 12+ min) not in tonight's lineup
            out_memo[k] = sum(gl[q] / cnt[q] for q in cnt if cnt[q] >= 5 and toi[q] / cnt[q] >= 12 and q not in tonight)
        return out_memo[k]

    out = []
    for pid, rows in by_player.items():
        rows.sort(key=lambda x: x["date"])
        cg = cs = ca = cp = 0
        hist = []
        for x in rows:
            if len(hist) >= min_prior:
                gid = starter.get((x["game"], x["team"]))      # goalie facing this skater's team? opp = skater's opponent
                # goalie who defended against this skater is the starter for x["opp"] -> keyed (game, skater team)
                gh = [g for g in goalies.get(gid, []) if g["date"] < x["date"]] if gid else None
                sa = [v for gm, v in sorted(team_sa[x["opp"]].items(), key=lambda kv: team_dates[(x["opp"], kv[0])])
                      if team_dates[(x["opp"], gm)] < x["date"]]
                hh = [dict(h) for h in hist]
                hh[-1]["season_goals"], hh[-1]["season_shots"] = cg, cs
                s = nhl.score_player(hh, gh, sa)
                gap = (dt.date.fromisoformat(x["date"]) - dt.date.fromisoformat(hist[-1]["date"])).days
                f = {k: s[k] for k in ("sog_pts", "pp_pts", "es_pts", "quality_pts", "stability_pts")}
                f.update({"goalie_sv": s.get("goalie_sv_pct", nhl.LG_SAVE_PCT),
                          "opp_sa": sum(sa[-10:]) / len(sa[-10:]) if sa else 30.0,
                          "is_home": float(x["home"]), "b2b": float(gap <= 1), "rest": float(min(gap, 5)),
                          "is_d": float(x["pos"] == "D"), "base_goals": cg / len(hist), "base_shots": cs / len(hist),
                          "toi": sum(h["toi_min"] for h in hist[-10:]) / len(hist[-10:]),
                          "teammates_out_goals": teammates_out(x["team"], x["game"]),
                          "games_missed": float(min(10, max(0, pos_in[x["team"]][x["game"]] - pos_in[x["team"]][hist[-1]["game"]] - 1)))
                          if hist[-1]["game"] in pos_in[x["team"]] else 0.0})      # 0 right after a trade
                l10 = hist[-10:]
                lane_base = {"toi": f["toi"], "pp_toi": sum(h["pp_toi_min"] for h in l10) / len(l10), "is_d": f["is_d"], "is_home": f["is_home"], "b2b": f["b2b"],
                             "opp_sa": f["opp_sa"], "goalie_sv": f["goalie_sv"], "teammates_out_goals": f["teammates_out_goals"], "games_missed": f["games_missed"]}
                lanes = {}
                for ln, key, thr in (("pts", "points", 1), ("ast", "assists", 1), ("sog", "shots", 3)):
                    season_rate = (ca if key == "assists" else cp if key == "points" else cs) / len(hist)
                    l10_rate = sum(h[key] for h in l10) / len(l10)
                    l3_rate = sum(h[key] for h in hist[-3:]) / len(hist[-3:])
                    lanes[ln] = {"y": int(x[key] >= thr), "avg": season_rate,
                                 "f": dict(lane_base, season=season_rate, l10=l10_rate, l3=l3_rate, season_goals=cg / len(hist), pp_pts=f["pp_pts"], sog_season=cs / len(hist))}
                out.append({"night": x["date"], "game": x["game"], "pid": pid, "goal": int(x["goals"] >= 1), "lanes": lanes,
                            "lamp": s["final_score"], "scorer_only": s["scorer_score"],
                            "base_goals": cg / len(hist), "base_shots": cs / len(hist),
                            "rand": ((pid * 2654435761 + x["game"]) % 1000) / 1000, "f": f, "season": season,
                            "name": x.get("name"), "team": x["team"], "opp": x["opp"], "pos": x["pos"], "gp": len(hist)})
            hist.append(x)
            cg += x["goals"]; cs += x["shots"]; ca += x["assists"]; cp += x["points"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nhl"))
    ap.add_argument("--seasons", nargs="+", type=int, default=[2023, 2024, 2025])
    a = ap.parse_args()
    allrows = []
    for s in a.seasons:
        rows = run(s, a.cache)
        base = 100 * sum(r["goal"] for r in rows) / len(rows)
        print(f"\n{s}-{str(s + 1)[2:]}: {len(rows)} skater-games, base goal rate {base:.1f}%")
        allrows += rows
        for label, key in (("LAMP v1", "lamp"), ("scorer only (no goalie)", "scorer_only"),
                           ("season goals/gm", "base_goals"), ("season shots/gm", "base_shots"), ("coin", "rand")):
            t1, _ = auc_free_topk(rows, key, 1, "game"); t3, _ = auc_free_topk(rows, key, 3, "game")
            t10, _ = auc_free_topk(rows, key, 10, "night")
            print(f"  {label:<24} top1/game {t1:5.1f}%   top3/game {t3:5.1f}%   top10/night {t10:5.1f}%")
    print("\nPOOLED")
    for label, key in (("LAMP v1", "lamp"), ("scorer only (no goalie)", "scorer_only"),
                       ("season goals/gm", "base_goals"), ("season shots/gm", "base_shots"), ("coin", "rand")):
        t1, n1 = auc_free_topk(allrows, key, 1, "game"); t3, _ = auc_free_topk(allrows, key, 3, "game")
        t10, n10 = auc_free_topk(allrows, key, 10, "night")
        print(f"  {label:<24} top1/game {t1:5.1f}% (n={n1})   top3/game {t3:5.1f}%   top10/night {t10:5.1f}% (n={n10})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
