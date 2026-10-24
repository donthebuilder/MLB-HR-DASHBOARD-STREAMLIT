#!/usr/bin/env python3
"""
numerology_lab.py -- does numerology alignment predict anything? Tested the same way as every other angle.

    python bots/numerology_lab.py nhl|nfl [--cache DIR]

The arithmetic is the site's (lib/numerology/core.js), carried over exactly: a number reduces by adding its
digits until one is left (17 -> 8). Five axes per player, each reduced to a root 1-9:
    JERSEY   his shirt number                      DAY    his birth day of the month
    PATH     life path: every digit of his birthdate   NEXT   his next goal / TD  (season total so far + 1)
    NAME     his full name, A=1..Z=26, summed
and the game date's own root (every digit of YYYY-MM-DD).
TAGS (all known before the game):
    braid 2+ / 3+        two or three of his own axes land on the same root
    date-hit 1+ / 2+ / 3+  that many of his axes equal TODAY's date root
    jersey = date root   next = date root   name = date root   path = date root   (single axes)
    birthday today       his birthday is the game date
    all axes agree       every axis he has is on one root
Each tag is compared with players without it, using outcome MINUS HIS OWN usual rate -- so a star is not credited
for being a star -- and run through angle_engine (false-discovery control across every tag, replication in every
season, minimum effect). Reported for two groups: EVERYONE, and the LONG SHOTS (bottom third by his own trailing
scoring rate), which is where "low chance but hella aligned" lives.
"""
import argparse
import datetime as dt
import re
import subprocess
import sys
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "nfl"))
import angle_engine as eng   # noqa: E402


def root(v):
    return 1 + ((int(v) - 1) % 9) if v and int(v) > 0 else None


def digits_root(s):
    d = re.sub(r"\D", "", str(s or ""))
    return root(sum(int(c) for c in d)) if d else None


def life_path(bd):
    d = re.sub(r"\D", "", str(bd or ""))
    return root(sum(int(c) for c in d)) if len(d) >= 8 else None


def name_root(name):
    s = sum(ord(c) - 96 for c in re.sub(r"[^a-z]", "", (name or "").lower()))
    return root(s)


def axes_for(jersey, birth, nxt, name):
    return {"JERSEY": root(jersey) if jersey else None,
            "DAY": root(int(str(birth)[8:10])) if birth and str(birth)[8:10].isdigit() and int(str(birth)[8:10]) > 0 else None,
            "PATH": life_path(birth), "NEXT": root(nxt), "NAME": name_root(name)}


def tags_for(ax, birth, game_date):
    dr = digits_root(game_date)
    vals = [v for v in ax.values() if v]
    counts = defaultdict(int)
    for v in vals:
        counts[v] += 1
    best = max(counts.values()) if counts else 0
    hits = sum(1 for v in vals if v == dr)
    t = {"braid: 2+ of his own numbers agree": best >= 2, "braid: 3+ of his own numbers agree": best >= 3,
         "date-hit 1+ (an axis = today's root)": hits >= 1, "date-hit 2+": hits >= 2, "date-hit 3+": hits >= 3,
         "jersey = today's root": ax["JERSEY"] == dr, "next = today's root": ax["NEXT"] == dr,
         "name = today's root": ax["NAME"] == dr, "life path = today's root": ax["PATH"] == dr,
         "birthday today": bool(birth) and str(birth)[5:10] == str(game_date)[5:10],
         "all his numbers agree (3+ axes)": len(vals) >= 3 and best == len(vals)}
    return t


def flag(jersey, birth, next_count, name, game_date):
    """One row's numerology read, for a board: {'axes', 'strength', 'flagged', 'tags'}. A flag is a LANE, not a score:
    nothing here multiplies into final_score, and the lab below is where its measured record lives."""
    ax = axes_for(jersey, birth, next_count, name)
    t = tags_for(ax, birth, game_date)
    strength = (3 if t["braid: 3+ of his own numbers agree"] else 2 if t["braid: 2+ of his own numbers agree"] else 0)
    strength += sum(1 for k in ("date-hit 2+", "date-hit 3+", "birthday today") if t[k])
    return {"axes": ax, "strength": strength, "flagged": t["braid: 3+ of his own numbers agree"] or t["date-hit 2+"] or t["birthday today"],
            "tags": [k for k, v in t.items() if v]}


def curl_json(url):
    raw = subprocess.run(["curl", "-s", "-m", "40", "--retry", "2", url], capture_output=True, text=True).stdout
    return json.loads(raw)


# ───────────────────────────────── NHL ─────────────────────────────────
def collect_nhl(cache):
    import nhl_backtest as nb
    cache = Path(cache)
    rows, birth, jersey = [], {}, {}
    for season in (2023, 2024, 2025):
        sid = f"{season}{season + 1}"
        bp = cache / f"bios_{sid}.json"
        if not bp.exists():
            bp.write_text(json.dumps(curl_json(f"https://api.nhle.com/stats/rest/en/skater/bios?isAggregate=false&isGame=false&limit=-1&cayenneExp=seasonId%3D{sid}%20and%20gameTypeId%3D2")["data"]))
        for r in json.load(open(bp)):
            birth[r["playerId"]] = r.get("birthDate")
    def jer(pid):
        p = cache / f"jersey_{pid}.json"
        if p.exists():
            return pid, json.load(open(p))
        try:
            v = curl_json(f"https://api-web.nhle.com/v1/player/{pid}/landing").get("sweaterNumber")
        except Exception:
            v = None
        p.write_text(json.dumps(v))
        return pid, v
    for season in (2023, 2024, 2025):
        data = nb.load_season(season, cache)
        per = defaultdict(list)
        for r in data["skater/summary"]:
            if r["positionCode"] == "G" or not r.get("timeOnIcePerGame"):
                continue
            per[r["playerId"]].append(r)
        need = [p for p in per if p not in jersey]
        with ThreadPoolExecutor(12) as ex:
            for pid, v in ex.map(jer, need):
                jersey[pid] = v
        for pid, rs in per.items():
            rs.sort(key=lambda x: x["gameDate"])
            goals = 0
            for i, x in enumerate(rs):
                if i >= 8:
                    rate = (goals + 0.15 * 10) / (i + 10)
                    toi = sum(y["timeOnIcePerGame"] for y in rs[max(0, i - 10):i]) / min(10, i) / 60.0
                    if toi >= 8:
                        ax = axes_for(jersey.get(pid), birth.get(pid), goals + 1, x["skaterFullName"])
                        rows.append({"season": season, "rate": rate, "y": int(x["goals"] >= 1), "d": 100 * (int(x["goals"] >= 1) - rate),
                                     "tags": tags_for(ax, birth.get(pid), x["gameDate"]), "ax": ax})
                goals += x["goals"]
    return rows


# ───────────────────────────────── NFL ─────────────────────────────────
def collect_nfl(cache):
    import nflreadpy as nfl
    import nfl_features as F
    import polars as pl
    rows = []
    for season in (2023, 2024, 2025):
        tbl = F.build(season).filter(pl.col("season_type") == "REG", pl.col("position").is_in(["RB", "WR", "TE"]))
        ros = nfl.load_rosters(seasons=[season]).select("gsis_id", "jersey_number", "birth_date").unique(subset=["gsis_id"], keep="last")
        info = {r["gsis_id"]: (r["jersey_number"], str(r["birth_date"]) if r["birth_date"] else None) for r in ros.iter_rows(named=True)}
        by = defaultdict(list)
        for r in tbl.select("player_id", "player_display_name", "week", "rushing_tds", "receiving_tds", "game_id").iter_rows(named=True):
            by[r["player_id"]].append(r)
        game_dates = {}
        sched = nfl.load_schedules(seasons=[season]).select("game_id", "gameday")
        for r in sched.iter_rows(named=True):
            game_dates[r["game_id"]] = str(r["gameday"])
        for pid, rs in by.items():
            rs.sort(key=lambda x: x["week"])
            tds = 0
            for i, x in enumerate(rs):
                td = (x["rushing_tds"] or 0) + (x["receiving_tds"] or 0)
                if i >= 3:
                    rate = (tds + 0.3 * 5) / (i + 5)
                    jn, bd = info.get(pid, (None, None))
                    ax = axes_for(jn, bd, tds + 1, x["player_display_name"])
                    gd = game_dates.get(x["game_id"])
                    if gd:
                        rows.append({"season": season, "rate": rate, "y": int(td >= 1), "d": 100 * (int(td >= 1) - rate),
                                     "tags": tags_for(ax, bd, gd), "ax": ax})
                tds += td
    return rows


def report(rows, label, unit):
    names = list(rows[0]["tags"])
    print(f"\n{label}: {len(rows)} player-games, base rate {100 * sum(r['y'] for r in rows) / len(rows):.1f}%")
    res = eng.evaluate(rows, names, "d", min_effect=1.0, min_n=60)
    eng.print_table(res, unit)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=["nhl", "nfl"])
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()
    cache = a.cache or str(Path.home() / f".cache/dash_{a.sport}")
    rows = collect_nhl(cache) if a.sport == "nhl" else collect_nfl(cache)
    print(f"{a.sport.upper()}: effect = percentage points of scoring probability over his own usual rate (1 point minimum to act)")
    report(rows, "EVERYONE", "pp")
    order = sorted(r["rate"] for r in rows)
    cut = order[len(order) // 3]
    long = [r for r in rows if r["rate"] <= cut]
    report(long, "LONG SHOTS (bottom third by his own usual rate)", "pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
