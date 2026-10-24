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


def pythagorean_date(day):
    """True when month^2 + day^2 = (two-digit year)^2: 2026-10-24 -> 10^2 + 24^2 = 26^2 (100 + 576 = 676). Returns the equation text or None."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", str(day or ""))
    if not m:
        return None
    y, mo, d = int(m[1]) % 100, int(m[2]), int(m[3])
    return f"{mo}\u00b2 + {d}\u00b2 = {y}\u00b2" if y and mo * mo + d * d == y * y else None


def next_pythagorean_dates(after, n=3):
    """The next n Pythagorean dates after `after` (YYYY-MM-DD), looking through year 2199."""
    out = []
    d = dt.date.fromisoformat(after) + dt.timedelta(days=1)
    end = dt.date(2199, 12, 31)
    while d <= end and len(out) < n:
        if pythagorean_date(d.isoformat()):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def date_facts(day):
    """Calendar facts for a game date. They describe the DATE, not any player; they are for the flavour line, never a score."""
    facts = []
    eq = pythagorean_date(day)
    if eq:
        facts.append(f"Pythagorean date: {eq}")
    r = digits_root(day)
    if r:
        facts.append(f"date root {r}")
    return facts


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
                                     "tags": tags_for(ax, birth.get(pid), x["gameDate"]), "ax": ax, "date": x["gameDate"], "x": int(x["goals"] >= 2)})
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
                                     "tags": tags_for(ax, bd, gd), "ax": ax, "date": gd, "x": int(td >= 2)})
                tds += td
    return rows


def report(rows, label, unit, y="y", d="d"):
    names = list(rows[0]["tags"])
    print(f"\n{label}: {len(rows)} player-games, base rate {100 * sum(r[y] for r in rows) / len(rows):.1f}%")
    res = eng.evaluate(rows, names, d, min_effect=1.0, min_n=60)
    eng.print_table(res, unit)
    return res


# ───────────────────────────────── MLB ─────────────────────────────────
def collect_mlb(cache, seasons=(2024, 2025, 2026)):
    """Every hitter-game of 2024-26 from statsapi game logs. HR and 1+ hit, each minus HIS OWN usual rate."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    rows = []
    for season in seasons:
        pf = cache / f"players_{season}.json"
        if not pf.exists():
            pf.write_text(json.dumps(curl_json(f"https://statsapi.mlb.com/api/v1/sports/1/players?season={season}")["people"]))
        people = [p for p in json.load(open(pf)) if p.get("primaryPosition", {}).get("abbreviation") not in ("P", None)]
        info = {p["id"]: p for p in people}

        def glog(pid):
            f = cache / f"glog_{season}_{pid}.json"
            if f.exists():
                return pid, json.load(open(f))
            try:
                sp = curl_json(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season={season}")["stats"][0]["splits"]
            except Exception:
                sp = []
            out = [{"date": x["date"], "hr": x["stat"].get("homeRuns", 0), "h": x["stat"].get("hits", 0), "ab": x["stat"].get("atBats", 0)} for x in sp]
            f.write_text(json.dumps(out))
            return pid, out
        with ThreadPoolExecutor(12) as ex:
            logs = dict(ex.map(glog, list(info)))
        for pid, lg in logs.items():
            lg = sorted((g for g in lg if g["ab"] > 0), key=lambda g: g["date"])
            p = info[pid]
            hrs = 0
            for i, g in enumerate(lg):
                if i >= 20:
                    hr_rate = (hrs + 0.04 * 30) / (i + 30)
                    h_rate = (sum(1 for q in lg[:i] if q["h"] > 0) + 0.65 * 10) / (i + 10)
                    ax = axes_for(p.get("primaryNumber"), p.get("birthDate"), hrs + 1, p.get("fullName"))
                    rows.append({"season": season, "rate": hr_rate, "hrate": h_rate, "y": int(g["hr"] >= 1), "yh": int(g["h"] >= 1),
                                 "d": 100 * (int(g["hr"] >= 1) - hr_rate), "dh": 100 * (int(g["h"] >= 1) - h_rate),
                                 "tags": tags_for(ax, p.get("birthDate"), g["date"]), "ax": ax, "date": g["date"], "x": int(g["hr"] >= 2)})
                hrs += g["hr"]
    return rows


# ───────────────────────────────── NBA ─────────────────────────────────
def collect_nba(cache):
    """Every NBA rotation player-game of 2023-25. Outcome: 25+ points, minus HIS OWN usual 25+ rate."""
    import buckets_backtest as bb
    cache = Path(cache)
    rows = []
    boxes = {s: sorted(bb.load(s, str(cache)), key=lambda g: (g["date"], g["id"])) for s in (2023, 2024)}
    pids = sorted({p["pid"] for gs in boxes.values() for g in gs for t in g["teams"] for p in t["players"]})
    info = {}

    def ath(pid):
        f = cache / f"ath_{pid}.json"
        if f.exists():
            return pid, json.load(open(f))
        try:
            a = curl_json(f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}")["athlete"]
            m = re.match(r"(\d+)/(\d+)/(\d{4})", a.get("displayDOB") or "")
            v = {"name": a.get("displayName"), "jersey": a.get("jersey"), "birth": f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}" if m else None}
        except Exception:
            v = {"name": None, "jersey": None, "birth": None}
        f.write_text(json.dumps(v))
        return pid, v
    with ThreadPoolExecutor(12) as ex:
        info = dict(ex.map(ath, pids))
    for season, games in boxes.items():
        hist = defaultdict(list)
        for g in games:
            for t in g["teams"]:
                for p in t["players"]:
                    if p["min"] <= 0:
                        continue
                    h = hist[p["pid"]]
                    if len(h) >= 8 and sum(m for m, _ in h[-10:]) / len(h[-10:]) >= 15:
                        n25 = sum(1 for _, q in h if q >= 25)
                        rate = (n25 + 0.1 * 10) / (len(h) + 10)
                        inf = info.get(p["pid"], {})
                        ax = axes_for(inf.get("jersey"), inf.get("birth"), n25 + 1, inf.get("name"))
                        rows.append({"season": season, "rate": rate, "y": int(p["pts"] >= 25), "d": 100 * (int(p["pts"] >= 25) - rate),
                                     "tags": tags_for(ax, inf.get("birth"), g["date"]), "ax": ax, "date": g["date"], "x": int(p["pts"] >= 35)})
                    h.append((p["min"], p["pts"]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=["nhl", "nfl", "mlb", "nba"])
    ap.add_argument("--cache", default=None)
    ap.add_argument("--seasons", nargs="+", type=int, default=None, help="mlb only: seasons to pull")
    ap.add_argument("--confirm", action="store_true", help="mlb only: out-of-sample check of the one pre-registered hypothesis")
    a = ap.parse_args()
    cache = a.cache or str(Path.home() / f".cache/dash_{a.sport}")
    if a.sport == "mlb" and a.confirm:
        # PRE-REGISTERED (written before these seasons were pulled): MLB 2024-26 showed hitters with 3+ of their numbers equal to
        # the game date's root homering about 3 points more often than their own usual rate (t 3.2, same sign in all 3 seasons).
        # One hypothesis, tested once, on seasons that played no part in finding it.
        rows = collect_mlb(cache, tuple(a.seasons or (2021, 2022, 2023)))
        print(f"OUT-OF-SAMPLE, MLB seasons {a.seasons or (2021, 2022, 2023)}: {len(rows)} player-games")
        for tag in ("date-hit 3+", "date-hit 2+"):
            w = [r for r in rows if r["tags"][tag]]; wo = [r for r in rows if not r["tags"][tag]]
            d, t = eng.welch([r["d"] for r in w], [r["d"] for r in wo])
            per = ", ".join(f"{sn}: {eng.welch([r['d'] for r in w if r['season'] == sn], [r['d'] for r in wo if r['season'] == sn])[0]:+.2f}" for sn in sorted({r['season'] for r in rows}))
            print(f"   {tag:<14} n={len(w):6}  effect {d:+.2f} pp  t {t:+.1f}   by season  {per}   (found in-sample: +3.00 / +0.88)")
        return 0
    rows = {"nhl": collect_nhl, "nfl": collect_nfl, "mlb": collect_mlb, "nba": collect_nba}[a.sport](cache)
    print(f"{a.sport.upper()}: effect = percentage points of probability over his own usual rate (1 point minimum to act)")
    report(rows, "EVERYONE" + (" -- home run" if a.sport == "mlb" else ""), "pp")
    cut = sorted(r["rate"] for r in rows)[len(rows) // 3]
    report([r for r in rows if r["rate"] <= cut], "LONG SHOTS (bottom third by his own usual rate)", "pp")
    if a.sport == "mlb":
        report(rows, "EVERYONE -- 1+ hit", "pp", y="yh", d="dh")
        cut = sorted(r["hrate"] for r in rows)[len(rows) // 3]
        report([r for r in rows if r["hrate"] <= cut], "LONG SHOTS for a hit (bottom third by his own hit rate)", "pp", y="yh", d="dh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
