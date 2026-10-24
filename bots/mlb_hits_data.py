#!/usr/bin/env python3
"""
mlb_hits_data.py -- the full-slate data for the MLB hit / H+R+RBI / total-bases models. Pulls once into CACHE_DIR.

    python bots/mlb_hits_data.py CACHE_DIR

  games.json       {gamePk: {date, venue, home_team, away_team, home_lineup[9 ids in batting order], away_lineup[...], home_sp, away_sp}}   2024-26 regular season
  hit_<season>_<pid>.json   each hitter's game log: date, pk, home, pa, ab, h, r, rbi, tb, bb, k, hr, 2b, 3b
  pit_<season>_<pid>.json   each pitcher's game log: date, pk, started, outs, h, bf, k, bb, hr, er
  people_<season>.json      id, name, bat side, pitch hand
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path


def jget(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "60", "--retry", "2", url], capture_output=True, text=True).stdout)


def schedule(c):
    out = Path(c) / "games.json"
    if out.exists():
        return
    games = {}
    spans = [(date(2024, 3, 20), date(2024, 9, 30)), (date(2025, 3, 18), date(2025, 9, 28)), (date(2026, 3, 25), date(2026, 9, 27))]
    days = [(a + timedelta(i)).isoformat() for a, b in spans for i in range((b - a).days + 1)]

    def one(day):
        res = {}
        j = jget(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}&hydrate=lineups,probablePitcher")
        for dd in j.get("dates", []):
            for g in dd["games"]:
                if g.get("gameType") != "R" or g["status"]["abstractGameState"] != "Final":
                    continue
                lu = g.get("lineups") or {}
                t = g["teams"]
                res[g["gamePk"]] = {"date": day, "venue": (g.get("venue") or {}).get("id"), "home_team": t["home"]["team"]["id"], "away_team": t["away"]["team"]["id"],
                                    "home_lineup": [p["id"] for p in lu.get("homePlayers", [])], "away_lineup": [p["id"] for p in lu.get("awayPlayers", [])],
                                    "home_sp": (t["home"].get("probablePitcher") or {}).get("id"), "away_sp": (t["away"].get("probablePitcher") or {}).get("id")}
        return res
    with ThreadPoolExecutor(12) as ex:
        for r in ex.map(one, days):
            games.update(r)
    out.write_text(json.dumps(games))
    print(f"schedule: {len(games)} games, {sum(1 for g in games.values() if len(g['home_lineup']) == 9 and len(g['away_lineup']) == 9)} with both lineups, "
          f"{sum(1 for g in games.values() if g['home_sp'] and g['away_sp'])} with both starters")


def people(c, season):
    f = Path(c) / f"people_{season}.json"
    if not f.exists():
        ps = jget(f"https://statsapi.mlb.com/api/v1/sports/1/players?season={season}")["people"]
        f.write_text(json.dumps([{"id": p["id"], "name": p.get("fullName"), "bats": (p.get("batSide") or {}).get("code"), "throws": (p.get("pitchHand") or {}).get("code"),
                                  "pos": (p.get("primaryPosition") or {}).get("abbreviation")} for p in ps]))
    return json.load(open(f))


def logs(c):
    c = Path(c)
    games = json.load(open(c / "games.json"))
    for season in (2024, 2025, 2026):
        ps = people(c, season)
        lineup_ids = set()
        sp_ids = set()
        for g in games.values():
            if g["date"].startswith(str(season)):
                lineup_ids.update(g["home_lineup"]); lineup_ids.update(g["away_lineup"])
                for k in ("home_sp", "away_sp"):
                    if g[k]:
                        sp_ids.add(g[k])

        def hit(pid):
            f = c / f"hit_{season}_{pid}.json"
            if f.exists():
                return
            try:
                sp = jget(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season={season}")["stats"][0]["splits"]
            except Exception:
                sp = []
            f.write_text(json.dumps([{"date": x["date"], "pk": (x.get("game") or {}).get("gamePk"), "home": bool(x.get("isHome")), "pa": x["stat"].get("plateAppearances", 0),
                                      "ab": x["stat"].get("atBats", 0), "h": x["stat"].get("hits", 0), "r": x["stat"].get("runs", 0), "rbi": x["stat"].get("rbi", 0),
                                      "tb": x["stat"].get("totalBases", 0), "bb": x["stat"].get("baseOnBalls", 0), "k": x["stat"].get("strikeOuts", 0),
                                      "hr": x["stat"].get("homeRuns", 0)} for x in sp]))

        def pit(pid):
            f = c / f"pit_{season}_{pid}.json"
            if f.exists():
                return
            try:
                sp = jget(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=pitching&season={season}")["stats"][0]["splits"]
            except Exception:
                sp = []
            f.write_text(json.dumps([{"date": x["date"], "pk": (x.get("game") or {}).get("gamePk"), "started": x["stat"].get("gamesStarted", 0), "outs": x["stat"].get("outs", 0),
                                      "h": x["stat"].get("hits", 0), "bf": x["stat"].get("battersFaced", 0), "k": x["stat"].get("strikeOuts", 0), "bb": x["stat"].get("baseOnBalls", 0),
                                      "hr": x["stat"].get("homeRuns", 0), "er": x["stat"].get("earnedRuns", 0)} for x in sp]))
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(hit, sorted(lineup_ids)))
            list(ex.map(pit, sorted(sp_ids)))
        print(f"{season}: {len(lineup_ids)} lineup hitters, {len(sp_ids)} starters")


if __name__ == "__main__":
    c = sys.argv[1]
    Path(c).mkdir(parents=True, exist_ok=True)
    schedule(c)
    logs(c)
    print("done")
