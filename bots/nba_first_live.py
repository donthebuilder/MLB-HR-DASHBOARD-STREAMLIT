#!/usr/bin/env python3
"""
nba_first_live.py -- tonight's FIRST BASKET board: a probability for each projected starter, top 3 per game flagged.

    python bots/nba_first_live.py [YYYY-MM-DD] [--out FILE]

Same features as nba_first_lane.py (models/nba_first_v1.json). Two differences from the backtest, both forced by this being the live path:
  * STARTERS are projected (the five who started most of the team's last 5 games) until the real ones are on the box score at tip.
  * Trailing history runs ACROSS the season boundary (last season's games count) because the backtest's 8-game within-season minimum would leave the
    board empty for the first month. Rows with fewer than 8 games of the current season are marked `carried_from_last_season`.
"""
import argparse, datetime as dt, json, math, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb       # noqa: E402
import game_lines as G              # noqa: E402
import lineups                      # noqa: E402
import nba_first as nf              # noqa: E402
from nba_live import athlete, parse_spread   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=dt.date.today().isoformat()); ap.add_argument("--out", default=None)
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba")); a = ap.parse_args()
    day = a.day
    tonight = [g for g in lineups.espn("nba", day) if g["status"] != "Final"]
    if not tonight:
        print(f"no NBA games left on {day}"); return 0
    model = json.load(open(Path(__file__).resolve().parent / "models" / "nba_first_v1.json"))
    games = []
    for s in (2025, 2026):
        games += [g for g in bb.load(s, a.cache) if g["date"] < day]
    games.sort(key=lambda g: (g["date"], g["id"]))
    with ThreadPoolExecutor(12) as ex:
        fds = list(ex.map(lambda g: nf.one_game(g["id"], a.cache), games))
    P, T = defaultdict(list), defaultdict(list)
    recent_starts = defaultdict(list)                     # team -> [starters of each game], oldest first
    season_games = defaultdict(int)                       # pid -> games this season (date >= season start)
    start26 = bb.RANGE[2026][0]
    for g, fd in zip(games, fds):
        teams = {t["abbr"]: t for t in g["teams"]}
        first_pid = ((fd or {}).get("first") or {}).get("pid")
        for ab, t in teams.items():
            opp = [x for x in teams if x != ab][0]
            tfga = sum(p["fga"] for p in t["players"])
            tearly = sum((fd or {}).get("early", {}).get(p["pid"], 0) for p in t["players"])
            mine = {p["pid"] for p in t["players"]}
            tid = (fd or {}).get("team_ids", {}).get(ab)
            tf = ((fd or {}).get("team_first") or {}).get(tid)
            T[ab].append({"allowed_first": int(first_pid is not None and first_pid not in mine and first_pid in {p["pid"] for p in teams[opp]["players"]})})
            recent_starts[ab].append(((fd or {}).get("starters") or {}).get(ab, []))
            for p in t["players"]:
                if p["min"] > 0:
                    P[p["pid"]].append({"pts": p["pts"], "min": p["min"], "fga": p["fga"], "tfga": tfga, "early": (fd or {}).get("early", {}).get(p["pid"], 0), "tearly": tearly,
                                        "scored_first": int(first_pid == p["pid"]), "scored_team_first": int(tf == p["pid"])})
                    if g["date"] >= start26:
                        season_games[p["pid"]] += 1
    out_rows = []
    for g in tonight:
        away, home = g["sides"]["away"]["team"], g["sides"]["home"]["team"]
        sp = parse_spread(g.get("spread"), home, away)
        total = float(g.get("total") or 0.0)
        gr = []
        for ab, is_home in ((away, False), (home, True)):
            opp = away if is_home else home
            cnt = defaultdict(int)
            for st in recent_starts[ab][-5:]:
                for pid in st:
                    cnt[pid] += 1
            starters = [pid for pid, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:5]]
            to = T[opp]
            for pid in starters:
                h = P[pid]
                if len(h) < 8:
                    continue
                h10 = h[-10:]; tf_ = sum(x["tfga"] for x in h) or 1; te_ = sum(x["tearly"] for x in h) or 1
                fav = 0.0 if sp is None else (-sp if is_home else sp)
                f = {"ppg": sum(x["pts"] for x in h) / len(h), "mpg": sum(x["min"] for x in h10) / len(h10), "fga_share": sum(x["fga"] for x in h) / tf_,
                     "early_share": sum(x["early"] for x in h) / te_, "early_per_game": sum(x["early"] for x in h) / len(h), "first_rate": sum(x["scored_first"] for x in h) / len(h),
                     "team_first_rate": sum(x["scored_team_first"] for x in h) / len(h),
                     "opp_allows_first": (sum(x["allowed_first"] for x in to[-20:]) / len(to[-20:])) if to else 0.5, "is_home": float(is_home),
                     "book_fav": fav, "book_total": total}
                z = model["intercept"] + sum(w * (f[n] - m) / s for n, w, m, s in zip(model["features"], model["weights"], model["mu"], model["sd"]))
                gr.append({"pid": pid, "name": athlete(pid, a.cache)["name"], "team": ab, "opponent": opp, "z": z, "carried_from_last_season": season_games[pid] < 8, "ppg": round(f["ppg"], 1),
                           "early_per_game": round(f["early_per_game"], 2), "start_utc": g.get("start_utc")})
        if gr:
            e = np.exp([r["z"] for r in gr]); pr = e / (1 + e)
            pr = pr / pr.sum() * model["starter_share"]
            for r, p in zip(gr, pr):
                r["p_first_basket"] = round(float(p), 4); r.pop("z")
            for i, r in enumerate(sorted(gr, key=lambda r: -r["p_first_basket"])):
                r["top3_in_game"] = i < 3
            out_rows += gr
    out_rows.sort(key=lambda r: -r["p_first_basket"])
    out = Path(a.out) if a.out else Path(__file__).resolve().parent / "outputs" / "nba_first_board_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"date": day, "model_version": model["model_version"], "starters": "projected: most starts in the team's last 5 games", "players": out_rows}, indent=1))
    print(f"{len(tonight)} games, {len(out_rows)} projected starters -> {out}")
    for r in out_rows[:10]:
        print(f"{(r['name'] or '?')[:22]:<23}{r['team']:<5}vs {r['opponent']:<5}{100 * r['p_first_basket']:5.1f}%  {'top3' if r['top3_in_game'] else ''}{'  (carried from last season)' if r['carried_from_last_season'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
