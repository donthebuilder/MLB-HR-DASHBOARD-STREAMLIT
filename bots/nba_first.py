#!/usr/bin/env python3
"""
nba_first.py -- FIRST BASKET and early usage, from ESPN play-by-play.

    python bots/nba_first.py pull  [--cache DIR] [--seasons 2024 2025]     (once; ~300 KB per game, cached compact)
    python bots/nba_first.py study [--cache DIR]

pull   per game: who the pre-game STARTERS were (ESPN box `starter` flag), who scored the first basket (and whether it was a three), and every shot
       attempt in the first 4:00 of the game by player (the "first possessions" usage). Compact files: first_<gameId>.json.
study  the first-basket market through the same fit-early / grade-late test. For each starter (only starters are eligible, as on the site):
         season FGA share            his shots as a share of his team's, trailing           (what the site's buckets-first-v1 reads)
         EARLY usage                 his share of his team's shots in the first 4:00 of games, trailing   <- the idea: who gets the first possessions
         first-basket rate           how often he has scored his team's first basket / the game's first basket, trailing
         ppg, minutes, home, the opposing team's first-4-minute points allowed, and the book spread (a big favourite gets the first look more often)
       Graded on the later season: how often the model's top starter per game / top 3 per game scored the game's first basket, vs his plain FGA share.
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"


def clock_secs(c):
    try:
        m, s = str(c).split(":")
        return int(m) * 60 + float(s)
    except ValueError:
        return None


def one_game(gid, cache):
    f = Path(cache) / f"first_{gid}.json"
    if f.exists():
        return json.load(open(f))
    for _ in range(3):
        raw = subprocess.run(["curl", "-s", "-m", "60", ESPN.format(gid)], capture_output=True, text=True).stdout
        if raw.startswith("{"):
            d = json.loads(raw)
            break
    else:
        return None
    starters = {}
    for t in (d.get("boxscore") or {}).get("players", []):
        ab = t["team"]["abbreviation"]
        starters[ab] = [a["athlete"]["id"] for a in t["statistics"][0]["athletes"] if a.get("starter")]
    first = None
    early = defaultdict(int)
    team_first = {}
    for p in d.get("plays", []):
        if not p.get("shootingPlay"):
            continue
        per = (p.get("period") or {}).get("number")
        secs = clock_secs((p.get("clock") or {}).get("displayValue"))
        who = (p.get("participants") or [{}])[0].get("athlete", {}).get("id")
        tm = (p.get("team") or {}).get("id")
        if per == 1 and secs is not None and secs >= 8 * 60 and who:
            early[who] += 1
        if p.get("scoringPlay") and who:
            if first is None:
                first = {"pid": who, "three": p.get("scoreValue") == 3, "team_id": tm}
            team_first.setdefault(tm, who)
    teams = {c["team"]["abbreviation"]: c["team"]["id"] for c in (d.get("header", {}).get("competitions") or [{}])[0].get("competitors", [])}
    out = {"id": gid, "starters": starters, "first": first, "early": dict(early), "team_first": team_first, "team_ids": teams}
    f.write_text(json.dumps(out))
    return out


def pull(cache, seasons):
    ids = [(g["id"]) for s in seasons for g in bb.load(s, cache)]
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda i: one_game(i, cache), ids))
    print(f"{sum(1 for r in res if r)} of {len(ids)} games, {sum(1 for r in res if r and r['first'])} with a first basket")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "study"])
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    ap.add_argument("--seasons", nargs="+", type=int, default=[2024, 2025])
    a = ap.parse_args()
    if a.cmd == "pull":
        pull(a.cache, a.seasons)
    else:
        print("study: written after the pull (see nba_first_study.py)")
