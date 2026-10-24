#!/usr/bin/env python3
"""
nhl_live.py -- tonight's NHL board: EVERY skater on tonight's teams, scored, with his stats, long shots included.

    python bots/nhl_live.py [YYYY-MM-DD] [--out FILE]

Same features as the backtest (nhl_backtest.run_data): tonight's games are appended to this season's logs as zero-stat
rows and run through the exact code path the 128,000-game backtest used. The tuned model (models/nhl_goal_v2.json,
fit on 2023-26, honest holdout in tune_models.py) turns them into a goal probability.

Per skater: goal probability (tuned), his chance of playing (injury report), the product of the two, the v1 LAMP score,
shots/goals per game, ice time, power-play time, the opposing probable goalie, and the book's moneyline and total.
Tier = thirds of tonight's probability: TOP / MIDDLE / LONG SHOT. Nobody is dropped for being a long shot.

Not in this board yet: numerology flags (needs jersey + birthdate per skater; numerology_lab.flag() takes them).
"""
import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_backtest as nb      # noqa: E402
import lineups                 # noqa: E402
import injury_status           # noqa: E402

ESPN_TO_NHL = {"NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}
SYNTH = 9_000_000_000


def _calibrate(model, p):
    """raw model chance -> chance as graded on the holdout (isotonic, models/*.json 'calibration')."""
    pl = model.get("platt")
    if pl:                                   # smooth, monotone, no plateau (tune_models.py)
        zraw = math.log(p / (1 - p))
        q = 1 / (1 + math.exp(-(pl["a"] + pl["b"] * zraw)))
        tail = pl.get("tail")
        return tail["t"] + (q - tail["t"]) * tail["slope"] if tail and q > tail["t"] else q
    c = model.get("calibration")
    if not c:
        return p
    xs, ys = c["x"], c["y"]
    if p <= xs[0]:
        return ys[0] * p / xs[0] if xs[0] else p
    if p >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if p <= xs[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (p - xs[i - 1]) / (xs[i] - xs[i - 1]) if xs[i] > xs[i - 1] else ys[i]
    return p


def tonight(day):
    j = nb.json.loads(nb.subprocess.run(["curl", "-s", "-m", "30", f"https://api-web.nhle.com/v1/schedule/{day}"], capture_output=True, text=True).stdout)
    games = []
    for w in j.get("gameWeek", []):
        if w["date"] == day:
            games += [{"id": g["id"], "away": g["awayTeam"]["abbrev"], "home": g["homeTeam"]["abbrev"], "start": g["startTimeUTC"]} for g in w["games"]]
    return games


def season_data(day, cache):
    start = f"{day[:4]}-10-01" if int(day[5:7]) >= 9 else f"{int(day[:4]) - 1}-10-01"
    end = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    out = defaultdict(list)
    for a, b in nb.weeks(start, end):
        for r in ("skater/summary", "skater/powerplay", "goalie/summary"):
            out[r].extend(nb.pull(r, a, b, cache))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--out", default=None)
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nhl_live"))
    a = ap.parse_args()
    day = a.day
    games = tonight(day)
    if not games:
        print(f"no NHL games on {day}")
        return 0
    data = season_data(day, a.cache)
    espn = {}
    for g in lineups.espn("nhl", day):
        h, w = g["sides"].get("home"), g["sides"].get("away")
        if h and w:
            espn[(ESPN_TO_NHL.get(w["team"], w["team"]), ESPN_TO_NHL.get(h["team"], h["team"]))] = (g, w, h)
    goalie_ids = {}
    for r in sorted(data["goalie/summary"], key=lambda r: r["gameDate"]):
        goalie_ids[r["goalieFullName"]] = r["playerId"]

    recent = defaultdict(dict)                   # team -> pid -> latest skater row (players seen in the last 10 team games)
    by_team_games = defaultdict(set)
    for r in sorted(data["skater/summary"], key=lambda r: (r["gameDate"], r["gameId"])):
        if r["positionCode"] != "G":
            by_team_games[r["teamAbbrev"]].add(r["gameId"])
    keep = {t: set(sorted(gs)[-10:]) for t, gs in by_team_games.items()}
    for r in data["skater/summary"]:
        if r["positionCode"] != "G" and r["gameId"] in keep.get(r["teamAbbrev"], ()):
            recent[r["teamAbbrev"]][r["playerId"]] = r

    synth_ids, meta = set(), {}
    for n, g in enumerate(games):
        gid = SYNTH + n
        synth_ids.add(gid)
        pair = espn.get((g["away"], g["home"]))
        meta[gid] = {"nhl_game": g["id"], "start": g["start"], "espn": pair}
        for side, team, opp in (("R", g["away"], g["home"]), ("H", g["home"], g["away"])):
            for pid, r in recent[team].items():
                data["skater/summary"].append({"playerId": pid, "gameId": gid, "gameDate": day, "teamAbbrev": team, "opponentTeamAbbrev": opp,
                                               "homeRoad": side, "goals": 0, "shots": 0, "ppGoals": 0, "timeOnIcePerGame": 60.0,
                                               "positionCode": r["positionCode"], "skaterFullName": r["skaterFullName"]})
            if pair:   # the opposing probable goalie, as the starter for this game
                gname = (pair[2] if side == "R" else pair[1]).get("probable_goalie")   # goalie facing this team = the OTHER side's
                gid_player = goalie_ids.get(gname)
                if gid_player:
                    data["goalie/summary"].append({"playerId": gid_player, "gameId": gid, "gameDate": day, "gamesStarted": 1, "saves": 0,
                                                   "shotsAgainst": 0, "opponentTeamAbbrev": team, "goalieFullName": gname})

    feats = [r for r in nb.run_data(data, int(day[:4]), min_prior=5) if r["game"] in synth_ids]
    model = json.load(open(Path(__file__).resolve().parent / "models" / "nhl_goal_v2.json"))
    inj = injury_status.fetch("nhl")
    rows = []
    for r in feats:
        z = model["intercept"] + sum(w * (r["f"][n] - m) / s for n, w, m, s in zip(model["features"], model["weights"], model["mu"], model["sd"]))
        p_raw = 1.0 / (1.0 + math.exp(-z))
        p = _calibrate(model, p_raw)
        mt = meta[r["game"]]
        pair = mt["espn"]
        rows.append({"name": r["name"], "team": r["team"], "opponent": r["opp"], "home": bool(r["f"]["is_home"]), "pos": r["pos"],
                     "model_version": model["model_version"], "p_goal": round(p, 4), "p_goal_raw": round(p_raw, 4), "lamp_v1": r["lamp"],
                     "goals_per_gm": round(r["base_goals"], 3), "shots_per_gm": round(r["base_shots"], 2), "toi_min": round(r["f"]["toi"], 1),
                     "pp_pts": r["f"]["pp_pts"], "back_to_back": bool(r["f"]["b2b"]), "teammates_out_goals": round(r["f"]["teammates_out_goals"], 2),
                     "opp_goalie": ((pair[2] if not r["f"]["is_home"] else pair[1]).get("probable_goalie") if pair else None),
                     "opp_goalie_status": "probable (ESPN)" if pair else "unknown",
                     "book_ml_total": (pair[0].get("spread"), pair[0].get("total")) if pair else None, "start_utc": mt["start"]})
    injury_status.apply(rows, inj)
    for r in rows:
        r["p_goal_adj"] = round(r["p_goal"] * r["p_play"], 4)
    order = sorted(r["p_goal"] for r in rows)
    lo, hi = order[len(order) // 3], order[2 * len(order) // 3]
    for r in rows:
        r["tier"] = "TOP" if r["p_goal"] >= hi else "MIDDLE" if r["p_goal"] >= lo else "LONG SHOT"
    rows.sort(key=lambda r: (r["p_goal_adj"], r["p_goal_raw"]), reverse=True)   # the calibrated top block has ties; raw breaks them
    out = Path(a.out) if a.out else Path(__file__).resolve().parent / "outputs" / "nhl_board_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"date": day, "model_version": model["model_version"], "players": rows}, indent=1))
    print(f"{len(games)} games, {len(rows)} skaters -> {out}")
    print(f"{'player':<22}{'team':<5}{'vs':<5}{'pos':<4}{'p(goal)':>8}{'plays':>7}{'adj':>7}{'g/gm':>6}{'sh/gm':>7}{'toi':>6}  tier / goalie")
    for r in rows[:12]:
        print(f"{r['name'][:21]:<22}{r['team']:<5}{r['opponent']:<5}{r['pos']:<4}{100 * r['p_goal']:>7.1f}%{100 * r['p_play']:>6.0f}%{100 * r['p_goal_adj']:>6.1f}%{r['goals_per_gm']:>6.2f}{r['shots_per_gm']:>7.1f}{r['toi_min']:>6.1f}  {r['tier']} / {r['opp_goalie']}")
    ls = [r for r in rows if r["tier"] == "LONG SHOT"]
    print(f"... {len(ls)} long shots kept on the board, e.g. " + ", ".join(f"{r['name']} {100 * r['p_goal']:.1f}%" for r in ls[:4]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
