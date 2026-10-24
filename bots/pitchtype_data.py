#!/usr/bin/env python3
"""
pitchtype_data.py -- pull what the pitch-type matchup study needs, once, into a cache directory.

    python bots/pitchtype_data.py CACHE_DIR

  bbe.pkl        every batted ball since 2024-02 from the bbe-history branch: (date, batter, pitcher, pitch_type, is_hr, is_hard_hit, ev)
  probables.json {gamePk: {date, home_team, away_team, home_pitcher, away_pitcher}}   probable starters (statsapi schedule, 2025-26)
  glog2_*.json   each hitter's game log WITH gamePk, home/away, opponent team (2025-26): date, gamePk, isHome, ab, hr, h
"""
import json
import pickle
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def curl_json(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "60", "--retry", "2", url], capture_output=True, text=True).stdout)


def bbe(cache):
    out = Path(cache) / "bbe.pkl"
    if out.exists():
        return
    names = subprocess.run(["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", "origin/bbe-history", "public/data/current/bbe_history/"],
                           capture_output=True, text=True).stdout.split()
    rows = []
    p = subprocess.Popen(["git", "-C", str(REPO), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for n in names:
        if not re.search(r"/bbe_\d{4}-\d{2}-\d{2}\.jsonl$", n):
            continue
        p.stdin.write(f"origin/bbe-history:{n}\n".encode()); p.stdin.flush()
        header = p.stdout.readline().split()
        if len(header) < 3:
            continue
        size = int(header[2])
        body = p.stdout.read(size); p.stdout.read(1)
        for line in body.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "game_date" not in r or "batter_id" not in r or "pitcher_id" not in r:
                continue
            rows.append((r["game_date"], r["batter_id"], r["pitcher_id"], r.get("pitch_type") or "", int(bool(r.get("is_hr"))), int(bool(r.get("is_hard_hit"))), r.get("launch_speed")))
    p.stdin.close(); p.wait()
    pickle.dump(rows, open(out, "wb"))
    print(f"bbe: {len(rows)} batted balls from {len(names)} files")


def probables(cache):
    out = Path(cache) / "probables.json"
    if out.exists():
        return
    games = {}
    d0 = date(2025, 3, 15)
    days = [(d0 + timedelta(i)).isoformat() for i in range((date(2026, 10, 23) - d0).days)]

    def one(day):
        j = curl_json(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}&hydrate=probablePitcher")
        res = {}
        for dd in j.get("dates", []):
            for g in dd["games"]:
                if g.get("gameType") not in ("R", "F", "D", "L", "W"):
                    continue
                t = g["teams"]
                res[g["gamePk"]] = {"date": day, "home_team": t["home"]["team"]["id"], "away_team": t["away"]["team"]["id"],
                                    "home_pitcher": (t["home"].get("probablePitcher") or {}).get("id"), "away_pitcher": (t["away"].get("probablePitcher") or {}).get("id")}
        return res
    with ThreadPoolExecutor(12) as ex:
        for r in ex.map(one, days):
            games.update(r)
    out.write_text(json.dumps(games))
    print(f"probables: {len(games)} games, {sum(1 for g in games.values() if g['home_pitcher'] and g['away_pitcher'])} with both starters")


def glogs(cache):
    cache = Path(cache)
    for season in (2025, 2026):
        pf = cache / f"players_{season}.json"
        if not pf.exists():
            pf.write_text(json.dumps(curl_json(f"https://statsapi.mlb.com/api/v1/sports/1/players?season={season}")["people"]))
        ids = [p["id"] for p in json.load(open(pf)) if p.get("primaryPosition", {}).get("abbreviation") not in ("P", None)]

        def one(pid):
            f = cache / f"glog2_{season}_{pid}.json"
            if f.exists():
                return
            try:
                sp = curl_json(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season={season}")["stats"][0]["splits"]
            except Exception:
                sp = []
            f.write_text(json.dumps([{"date": x["date"], "pk": (x.get("game") or {}).get("gamePk"), "home": bool(x.get("isHome")), "ab": x["stat"].get("atBats", 0),
                                      "hr": x["stat"].get("homeRuns", 0), "h": x["stat"].get("hits", 0)} for x in sp]))
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(one, ids))
        print(f"glogs {season}: {len(ids)} hitters")


if __name__ == "__main__":
    c = sys.argv[1]
    Path(c).mkdir(parents=True, exist_ok=True)
    bbe(c); probables(c); glogs(c)
    print("done")
