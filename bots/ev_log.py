#!/usr/bin/env python3
"""
ev_log.py -- run the EV-dingers scan on a schedule (GitHub Actions, not your computer), keep every result, grade it afterwards.

    python bots/ev_log.py snapshot nba|nhl|nfl|mlb OUT_DIR [--limit 10] [--hours 8]
    python bots/ev_log.py grade    OUT_DIR YYYY-MM-DD          (ET game date)
    python bots/ev_log.py report   OUT_DIR

snapshot   scan_rows() from ev_process.py: SGO book prices vs Kalshi's order book. Saves EVERY checked prop (not just the green ones) with its
           verdict, so "does a GREEN flag earn more than a PASS?" can be answered. Month budget guard: SGO bills one object per event, so
           this stops itself at EV_MONTH_CAP objects (default 900 of the 2,500 a month the key allows; the site uses the rest).
grade      settles each saved prop against that night's real box scores/stat lines at the BOOK price, flat $1. A player with no stat line is void.
report     return by verdict, by EV band and by market, with the standard error. A few hundred bets before any of it means anything.
Nothing here places a bet or touches a book. It records a price you could have had and what happened.
"""
import json
import os
import re
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devig              # noqa: E402
import ev_process as EP   # noqa: E402
import injury_status as I  # noqa: E402


def now_et():
    return datetime.now(timezone.utc) - timedelta(hours=5)


def curl(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True).stdout)


def budget(out, add=0, cap=None):
    cap = cap or int(os.environ.get("EV_MONTH_CAP", "900"))
    f = Path(out) / "_budget.json"
    month = now_et().strftime("%Y-%m")
    b = json.load(open(f)) if f.exists() else {}
    if b.get("month") != month:
        b = {"month": month, "used": 0}
    if b["used"] + add > cap:
        return False, b
    b["used"] += add
    f.write_text(json.dumps(b))
    return True, b


def snapshot(sport, out, limit, hours):
    Path(out).mkdir(parents=True, exist_ok=True)
    ok, b = budget(out, add=limit)
    if not ok:
        print(f"{sport}: skipped, SGO month budget reached ({b['used']} objects this month)")
        return 0
    a = Namespace(sport=sport, limit=limit, hours=hours, min_ev=0.03, depth_checks=40, env=str(Path.home() / "Desktop/moonshot-push/.env.local"))
    results, n_props, n_cands, skipped = EP.scan_rows(a)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    path = Path(out) / f"ev_scan_{sport}_{ts}.json"
    path.write_text(json.dumps({"sport": sport, "fetched_utc": ts, "date_et": now_et().date().isoformat(), "props": n_props, "matched": n_cands, "rows": results}, indent=1))
    from collections import Counter
    print(f"{sport}: {n_props} props, {n_cands} matched, {len(results)} order books checked {dict(Counter(r['verdict'] for r in results))}; budget used {b['used']}/{os.environ.get('EV_MONTH_CAP', '900')}")
    return 0


# ---------------------------------------------------------------- grading: real stat lines by player name
def stats_nba(day):
    out = {}
    for e in curl(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={day.replace('-', '')}").get("events", []):
        if not e["status"]["type"].get("completed"):
            continue
        d = curl(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={e['id']}")
        for t in (d.get("boxscore") or {}).get("players", []):
            L = t["statistics"][0]["labels"]
            for a in t["statistics"][0]["athletes"]:
                s = dict(zip(L, a["stats"]))
                if not a["stats"] or s.get("MIN") in (None, "", "--"):
                    continue
                pts, reb, ast = (int(s.get(k) or 0) for k in ("PTS", "REB", "AST"))
                out[I.key(a["athlete"]["displayName"])] = {"Points": pts, "Rebounds": reb, "Assists": ast, "PRA": pts + reb + ast}
    return out


def stats_nhl(day):
    q = f'gameDate="{day}" and gameTypeId=2'
    from urllib.parse import quote
    rows = curl(f"https://api.nhle.com/stats/rest/en/skater/summary?isAggregate=false&isGame=true&limit=-1&cayenneExp={quote(q)}").get("data", [])
    return {I.key(r["skaterFullName"]): {"Anytime goal": r["goals"], "Any assist": r["assists"], "Any point (G+A)": r["points"], "Shots on goal": r["shots"]} for r in rows}


def stats_mlb(day):
    out = {}
    for d in curl(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}").get("dates", []):
        for g in d["games"]:
            if g["status"]["abstractGameState"] != "Final":
                continue
            box = curl(f"https://statsapi.mlb.com/api/v1/game/{g['gamePk']}/boxscore")
            for side in ("away", "home"):
                for pl in box["teams"][side]["players"].values():
                    b = (pl.get("stats") or {}).get("batting") or {}
                    if b:
                        out[I.key(pl["person"]["fullName"])] = {"Home run": int(b.get("homeRuns") or 0)}
    return out


def stats_nfl(day):
    import nflreadpy as nfl
    import polars as pl
    yr = int(day[:4]) if int(day[5:7]) > 6 else int(day[:4]) - 1
    sch = nfl.load_schedules(seasons=[yr]).filter(pl.col("gameday") == day)
    if sch.height == 0:
        return {}
    wk = int(sch["week"][0])
    teams = set(sch["home_team"].to_list()) | set(sch["away_team"].to_list())
    st = nfl.load_player_stats(seasons=[yr]).filter((pl.col("week") == wk) & pl.col("team").is_in(list(teams)))
    return {I.key(r["player_display_name"]): {"Anytime TD": (r["rushing_tds"] or 0) + (r["receiving_tds"] or 0)} for r in st.iter_rows(named=True)}


STATS = {"nba": stats_nba, "nhl": stats_nhl, "mlb": stats_mlb, "nfl": stats_nfl}


def grade(out, day):
    out = Path(out)
    cache = {}
    n_files = 0
    for f in sorted(out.glob("ev_scan_*.json")):
        doc = json.load(open(f))
        if doc.get("date_et") != day:
            continue
        gf = out / f.name.replace("ev_scan_", "ev_graded_")
        if gf.exists():
            continue
        sp = doc["sport"]
        if sp not in cache:
            cache[sp] = STATS[sp](day)
        graded = []
        for r in doc["rows"]:
            st = cache[sp].get(I.key(r["player"]))
            v = (st or {}).get(r["market"])
            if v is None:
                res, profit = "void", 0.0
            else:
                line = float(r["line"]) if r["line"] is not None else 0.5
                win = v > line
                res, profit = ("win", devig.ev(1.0, r["price"])) if win else ("loss", -1.0)       # ev(1, price) = the payout per $1
            graded.append({**r, "actual": v, "result": res, "profit": round(profit, 4)})
        gf.write_text(json.dumps({**{k: doc[k] for k in ("sport", "fetched_utc", "date_et")}, "rows": graded}, indent=1))
        n_files += 1
        print(f"graded {gf.name}: {sum(g['result'] == 'win' for g in graded)} won, {sum(g['result'] == 'loss' for g in graded)} lost, {sum(g['result'] == 'void' for g in graded)} void")
    if not n_files:
        print(f"nothing new to grade for {day}")
    return 0


def report(out):
    rows = []
    for f in sorted(Path(out).glob("ev_graded_*.json")):
        rows += [r for r in json.load(open(f))["rows"] if r["result"] != "void"]
    if not rows:
        print("nothing graded yet")
        return 0

    def line(label, rs):
        if not rs:
            return
        n = len(rs)
        mean = sum(r["profit"] for r in rs) / n
        se = (sum((r["profit"] - mean) ** 2 for r in rs) / max(1, n - 1)) ** 0.5 / n ** 0.5
        print(f"   {label:<22}{n:>6}{100 * sum(r['result'] == 'win' for r in rs) / n:>8.1f}%{100 * mean:>+9.1f}%{100 * se:>8.1f}%")
    print(f"{len(rows)} graded props across {len({r['fetched_utc'][:10] for r in rows})} days\n   {'':<22}{'bets':>6}{'won':>9}{'ROI':>10}{'(se)':>8}")
    for v in ("STRONG", "GOOD", "WEAK", "PASS"):
        line(f"verdict {v}", [r for r in rows if r["verdict"] == v])
    for lo, hi in ((-99, 0), (0, 3), (3, 6), (6, 12), (12, 99)):
        line(f"top EV {lo}..{hi}%", [r for r in rows if lo <= r["top_ev_pct"] < hi])
    for sp in sorted({r["sport"] for r in rows}):
        line(f"all {sp}", [r for r in rows if r["sport"] == sp])
    print("\n   ROI within about +/- 2 standard errors of zero is not a finding. Judge after hundreds of bets, not tens.")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot":
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10
        hrs = int(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 8
        sys.exit(snapshot(sys.argv[2], sys.argv[3], lim, hrs))
    elif cmd == "grade":
        sys.exit(grade(sys.argv[2], sys.argv[3]))
    elif cmd == "report":
        sys.exit(report(sys.argv[2]))
    else:
        print(__doc__)
