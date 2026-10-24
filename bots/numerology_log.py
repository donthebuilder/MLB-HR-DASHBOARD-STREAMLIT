#!/usr/bin/env python3
"""
numerology_log.py -- the numerology test that cannot be fooled: written BEFORE the game, graded AFTER, kept forever.

    python bots/numerology_log.py snapshot DIR [--date YYYY-MM-DD] [--allow-final]
    python bots/numerology_log.py grade    DIR  YYYY-MM-DD
    python bots/numerology_log.py report   DIR

Why: the MLB backtest (numerology_tail.py) found a small, steady lean -- hitters with 2+ of their numbers equal to the
game date's root homer about 0.7 points more often than hitters of the same quality (t 3.1 over six seasons) -- and
nothing in NHL, NFL or NBA. A pattern found by looking at old games is a hypothesis. This makes it a test:

  snapshot   every hitter in a POSTED lineup, with his five numbers and how many equal today's root, saved before first
             pitch (games already started are skipped). First snapshot of a player wins; nothing is ever rewritten.
  grade      the next day, from the boxscores: did he homer, did he get a hit, did he play.
  report     home-run rate by number of alignments, against hitters with none -- accumulating night after night.

Hypothesis, frozen on 2026-10-24: P(HR | 2+ aligned) > P(HR | 0 aligned), same direction in 2027. Not tuned on later data.
"""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lineups                    # noqa: E402
import numerology_lab as nl       # noqa: E402


def get(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "60", url], capture_output=True, text=True).stdout)


def et_today():
    return (datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat()


def snapshot(out_dir, day, allow_final=False):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"numerology_pregame_{day}.json"
    have = json.load(open(path)) if path.exists() else {}
    season = int(day[:4])
    people = {p["id"]: p for p in get(f"https://statsapi.mlb.com/api/v1/sports/1/players?season={season}")["people"]}
    hrs = {x["player"]["id"]: int(x["stat"].get("homeRuns") or 0) for x in
           get(f"https://statsapi.mlb.com/api/v1/stats?stats=season&group=hitting&sportIds=1&season={season}&limit=2000&playerPool=All")["stats"][0]["splits"]}
    added = 0
    for g in lineups.mlb(day):
        if not allow_final and g["status"] not in ("Pre-Game", "Scheduled", "Warmup"):
            continue                                  # started or finished: not pregame any more
        for side in g["sides"].values():
            if not side["posted"]:
                continue
            for pid_s, spot in side["order"].items():
                pid = int(pid_s)
                if str(pid) in have:
                    continue
                p = people.get(pid, {})
                f = nl.flag(p.get("primaryNumber"), p.get("birthDate"), hrs.get(pid, 0) + 1, p.get("fullName"), day)
                dr = nl.digits_root(day)
                have[str(pid)] = {"player_id": pid, "name": p.get("fullName"), "team": side["team"], "game_pk": g["game_id"], "spot": spot,
                                  "aligned": sum(1 for v in f["axes"].values() if v and v == dr), "axes": f["axes"], "date_root": dr,
                                  "snapshot_utc": datetime.now(timezone.utc).isoformat()}
                added += 1
    path.write_text(json.dumps(have, indent=1))
    print(f"{day}: +{added} hitters ({len(have)} saved) -> {path}")


def grade(out_dir, day):
    out = Path(out_dir)
    pre = out / f"numerology_pregame_{day}.json"
    if not pre.exists():
        print("no pregame file for", day)
        return
    have = json.load(open(pre))
    res = {}
    for pk in sorted({v["game_pk"] for v in have.values()}):
        box = get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
        for side in ("away", "home"):
            for key, pl in box["teams"][side]["players"].items():
                b = (pl.get("stats") or {}).get("batting") or {}
                if b:
                    res[pl["person"]["id"]] = {"hr": int(b.get("homeRuns") or 0), "h": int(b.get("hits") or 0), "ab": int(b.get("atBats") or 0), "bb": int(b.get("baseOnBalls") or 0)}
    graded = []
    for pid_s, v in have.items():
        r = res.get(int(pid_s))
        graded.append({**v, "played": bool(r and (r["ab"] or r["bb"])), **(r or {"hr": 0, "h": 0, "ab": 0, "bb": 0})})
    (out / f"numerology_graded_{day}.json").write_text(json.dumps(graded, indent=1))
    print(f"{day}: graded {len(graded)} hitters, {sum(g['played'] for g in graded)} played, {sum(g['hr'] for g in graded)} HR")


def report(out_dir):
    rows = []
    for f in sorted(Path(out_dir).glob("numerology_graded_*.json")):
        rows += [r for r in json.load(open(f)) if r["played"]]
    if not rows:
        print("nothing graded yet")
        return
    print(f"{len(rows)} graded hitter-games, {len({r['player_id'] for r in rows})} hitters")
    print(f"   {'aligned':<9}{'n':>8}{'HR rate':>10}{'2+ HR':>9}")
    for k in range(4):
        rs = [r for r in rows if (r["aligned"] >= 3 if k == 3 else r["aligned"] == k)]
        if rs:
            print(f"   {('3+' if k == 3 else k):<9}{len(rs):>8}{100 * sum(r['hr'] >= 1 for r in rs) / len(rs):>9.1f}%{100 * sum(r['hr'] >= 2 for r in rs) / len(rs):>8.2f}%")
    hi = [r for r in rows if r["aligned"] >= 2]; lo = [r for r in rows if r["aligned"] == 0]
    if hi and lo:
        print(f"   2+ aligned vs none: {100 * sum(r['hr'] >= 1 for r in hi) / len(hi):.1f}% vs {100 * sum(r['hr'] >= 1 for r in lo) / len(lo):.1f}%"
              f"   (a few hundred nights are needed before a gap of this size means anything)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot":
        d = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else et_today()
        snapshot(sys.argv[2], d, "--allow-final" in sys.argv)
    elif cmd == "grade":
        grade(sys.argv[2], sys.argv[3])
    elif cmd == "report":
        report(sys.argv[2])
    else:
        print(__doc__)
