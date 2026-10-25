#!/usr/bin/env python3
"""
shadow_log.py -- prospective record of the NHL and NBA lane probabilities, graded the next morning.

    python bots/shadow_log.py snapshot nhl|nba DAY OUT_DIR     run the live board, keep every player's lane probabilities
    python bots/shadow_log.py grade    nhl|nba DAY OUT_DIR     fill in what happened (players who did not play are left out)
    python bots/shadow_log.py report   OUT_DIR                 calibration by band and best-pick-per-game, per lane

One file per sport per day: shadow_<sport>_<day>.json. Rows are written once before the games and never rewritten; grading adds
`actual`. This is the proof that the backtest numbers hold going forward, and the rows the scoreboard (model_harness.py) can read.
"""
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANES = {
    "nhl": [("p_goal", "goal", "Goal 1+"), ("p_points", "points", "Points 1+"), ("p_assist", "assists", "Assists 1+"), ("p_sog3", "shots3", "Shots 3+")],
    "nba": [("p_25", "pts25", "Points 25+"), ("reb", "reb10", "Rebounds 10+"), ("ast", "ast8", "Assists 8+"), ("3pm", "tpm4", "3PM 4+"), ("pra", "pra35", "PRA 35+"), ("dd", "dd", "Double-double")],
}


def curl(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "60", "--retry", "3", url], capture_output=True, text=True).stdout or "{}")


def snapshot(sport, day, out_dir):
    path = Path(out_dir) / f"shadow_{sport}_{day}.json"
    if path.exists():
        print(f"{path.name} already written; not rewritten")
        return
    tmp = tempfile.mktemp(suffix=".json")
    subprocess.run([sys.executable, str(HERE / f"{sport}_live.py"), day, "--out", tmp], check=True, capture_output=True)
    if not Path(tmp).exists():
        print(f"{day} {sport}: no games, nothing saved")
        return
    board = json.load(open(tmp))
    rows = []
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    for p in board["players"]:
        if p.get("start_utc") and str(p["start_utc"]).replace("Z", "+00:00") <= now.replace("+00:00", "+00:00") and str(p["start_utc"])[:16] <= now[:16]:
            continue                                      # the game has started: not a pregame record
        pr = {}
        for src, key, _ in LANES[sport]:
            v = p.get(src) if src in p else (p.get("lanes") or {}).get(src)
            if v is not None:
                pr[key] = v
        rows.append({"name": p["name"], "team": p["team"], "opp": p["opponent"], "p_play": p.get("p_play", 1.0), "p": pr})
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day, "sport": sport, "model_versions": board.get("model_version"), "rows": rows}))
    print(f"{day} {sport}: {len(rows)} players saved -> {path}")


def snapshot_first(day, out_dir):
    """NBA first basket: re-written every run for games that have NOT started (the starters firm up toward tip), never touched once a game starts."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / f"shadow_nbafirst_{day}.json"
    have = json.load(open(path)) if path.exists() else {"date": day, "sport": "nbafirst", "games": {}}
    tmp = tempfile.mktemp(suffix=".json")
    subprocess.run([sys.executable, str(HERE / "nba_first_live.py"), day, "--out", tmp], check=True, capture_output=True)
    if not Path(tmp).exists():
        print(f"{day} nbafirst: no games"); return
    board = json.load(open(tmp))
    by_game = defaultdict(list)
    for r in board["players"]:
        by_game[r["game_id"]].append(r)
    new = kept = 0
    for gid, rs in by_game.items():
        entry = have["games"].get(gid)
        if entry and entry.get("locked"):
            continue
        if rs[0]["game_status"] != "Scheduled":              # started: no longer pregame
            if entry:
                entry["locked"] = True; kept += 1
            continue
        have["games"][gid] = {"locked": False, "status": rs[0]["game_status"], "starters_source": rs[0]["starters_source"], "saved_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                              "rows": [{"name": r["name"], "team": r["team"], "opp": r["opponent"], "p_first_basket": r["p_first_basket"], "top3": r["top3_in_game"], "no_history": r["no_history"]} for r in rs]}
        new += 1
    path.write_text(json.dumps(have))
    print(f"{day} nbafirst: {new} game(s) written/refreshed, {kept} locked at tip, {len(have['games'])} in file")


def grade_first(day, out_dir, cache):
    sys.path.insert(0, str(HERE))
    import nba_first as nf
    import injury_status
    from nba_live import athlete
    path = Path(out_dir) / f"shadow_nbafirst_{day}.json"
    d = json.load(open(path))
    n = 0
    for gid, g in d["games"].items():
        fd = nf.one_game(gid, cache)
        first = (fd or {}).get("first")
        if not first:
            continue
        who = athlete(first["pid"], cache)["name"]
        g["first_scorer"] = who
        for r in g["rows"]:
            r["y"] = int(injury_status.key(r["name"] or "") == injury_status.key(who or ""))
        g["first_by_listed_starter"] = any(r["y"] for r in g["rows"]); g["graded"] = True; n += 1
    path.write_text(json.dumps(d))
    print(f"{day} nbafirst: graded {n} of {len(d['games'])} games")


def report_first(out_dir):
    games = []
    for f in sorted(Path(out_dir).glob("shadow_nbafirst_*.json")):
        games += [g for g in json.load(open(f))["games"].values() if g.get("graded")]
    print(f"\nNBA FIRST BASKET: {len(games)} graded games")
    if not games:
        return
    t1 = sum(max(g["rows"], key=lambda r: r["p_first_basket"])["y"] for g in games)
    t3 = sum(sum(r["y"] for r in sorted(g["rows"], key=lambda r: -r["p_first_basket"])[:3]) for g in games)
    rows = [r for g in games for r in g["rows"]]
    listed = sum(g["first_by_listed_starter"] for g in games)
    print(f"  top pick {100 * t1 / len(games):.1f}% (backtest 14.8%)   top 3 {100 * t3 / len(games):.1f}% (backtest 40.0%)   first basket by a listed starter {100 * listed / len(games):.0f}% of games")
    print(f"  mean predicted {100 * sum(r['p_first_basket'] for r in rows) / len(rows):.1f}% vs actual {100 * sum(r['y'] for r in rows) / len(rows):.1f}% per starter")


def outcomes_nhl(day):
    url = ('https://api.nhle.com/stats/rest/en/skater/summary?isAggregate=false&isGame=true&limit=-1&cayenneExp='
           f'gameDate>="{day}" and gameDate<="{day}" and gameTypeId=2').replace(" ", "%20").replace('"', "%22")
    out = {}
    for r in curl(url).get("data", []):
        out[r["skaterFullName"]] = {"goal": int(r["goals"] >= 1), "points": int(r["points"] >= 1), "assists": int(r["assists"] >= 1), "shots3": int(r["shots"] >= 3)}
    return out


def outcomes_nba(day):
    esp = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
    out = {}
    for e in curl(f"{esp}/scoreboard?dates={day.replace('-', '')}").get("events", []):
        if not e.get("status", {}).get("type", {}).get("completed"):
            continue
        for t in (curl(f"{esp}/summary?event={e['id']}").get("boxscore") or {}).get("players", []):
            st = t["statistics"][0]
            for a in st["athletes"]:
                s = dict(zip(st["labels"], a["stats"]))
                if not a["stats"] or s.get("MIN") in (None, "", "--"):
                    continue
                g = lambda k: int(s.get(k) or 0)
                tpm = int((s.get("3PT") or "0-0").split("-")[0])
                out[a["athlete"]["displayName"]] = {"pts25": int(g("PTS") >= 25), "reb10": int(g("REB") >= 10), "ast8": int(g("AST") >= 8),
                                                    "tpm4": int(tpm >= 4), "pra35": int(g("PTS") + g("REB") + g("AST") >= 35),
                                                    "dd": int(sum(g(k) >= 10 for k in ("PTS", "REB", "AST")) >= 2)}
    return out


def grade(sport, day, out_dir):
    path = Path(out_dir) / f"shadow_{sport}_{day}.json"
    d = json.load(open(path))
    res = (outcomes_nhl if sport == "nhl" else outcomes_nba)(day)
    n = 0
    for r in d["rows"]:
        if r["name"] in res:
            r["actual"] = res[r["name"]]; n += 1
    d["graded"] = n
    path.write_text(json.dumps(d))
    print(f"{day} {sport}: graded {n} of {len(d['rows'])}")


def report(out_dir):
    for sport, lanes in LANES.items():
        files = sorted(Path(out_dir).glob(f"shadow_{sport}_*.json"))
        data = [json.load(open(f)) for f in files]
        rows = [(d["date"], r) for d in data for r in d["rows"] if r.get("actual")]
        print(f"\n{sport.upper()}: {len(data)} nights logged, {len(rows)} graded player-games")
        for _, key, label in lanes:
            pr = sorted(((r["p"][key], r["actual"][key]) for _, r in rows if key in r["p"]))
            if len(pr) < 50:
                print(f"  {label:<14} n={len(pr)} (too few to read)")
                continue
            q = len(pr) // 4
            bands = [pr[i * q:(i + 1) * q if i < 3 else len(pr)] for i in range(4)]
            print(f"  {label:<14} n={len(pr)}  " + "  ".join(f"{100 * sum(x for x, _ in b) / len(b):.0f}%->{100 * sum(y for _, y in b) / len(b):.0f}%" for b in bands) + "  (predicted->actual, quartiles)")


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "report":
        report(sys.argv[2]); report_first(sys.argv[2])
    elif sys.argv[2] == "nbafirst":
        import os
        cache = os.environ.get("DASH_NBA_CACHE", str(Path.home() / ".cache/dash_nba"))
        snapshot_first(sys.argv[3], sys.argv[4]) if c == "snapshot" else grade_first(sys.argv[3], sys.argv[4], cache)
    else:
        {"snapshot": snapshot, "grade": grade}[c](sys.argv[2], sys.argv[3], sys.argv[4])
