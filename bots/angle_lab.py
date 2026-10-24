#!/usr/bin/env python3
"""
angle_lab.py -- NBA: which situations actually move a scorer, measured against his own usual.

    python bots/angle_lab.py [--cache DIR]

Rotation players (last-10 average 24+ minutes) over two seasons. Every number is
"points (or minutes) minus HIS OWN last-10 average", so a star isn't credited for being a star.

TABLE 1 -- PREGAME ANGLES: things known before tip. Each tag is compared with the rotation
          players who did NOT have it. Reported: n, extra points, extra minutes, 25+ rate gap,
          and a t-score (|t| < 2 means indistinguishable from noise -- don't move a ranking on it).
TABLE 2 -- GAME SCRIPTS: what happened, scored for the stars (top two scorers on each team) only.
          Blowout, comeback win, blown lead, close, overtime. Not known pregame -- these are for the
          live board and for weighting scenarios -- but they show what the script costs or pays.
"""
import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402


def collect(cache):
    rows = []
    for season in (2023, 2024):
        _first = len(rows)
        games = sorted(bb.load(season, cache), key=lambda g: (g["date"], g["id"]))
        hist = defaultdict(list)             # pid -> [(min, pts, fga)]
        team_dates = defaultdict(list)       # team -> dates of its games
        team_res = defaultdict(list)         # team -> [(pf, pa, pace)]
        for g in games:
            t = g["teams"]
            pts = [sum(p["pts"] for p in x["players"]) for x in t]
            poss = [sum(p["fga"] + 0.44 * p["fta"] - p["oreb"] + p["to"] for p in x["players"]) for x in t]
            day = dt.date.fromisoformat(g["date"])

            def net(a):
                h = team_res[a][-10:]
                return sum(f - c for f, c, _ in h) / len(h) if h else None
            def pace(a):
                h = team_res[a][-10:]
                return sum(x[2] for x in h) / len(h) if h else None
            def allowed(a):
                h = team_res[a][-10:]
                return sum(x[1] for x in h) / len(h) if h else None
            for i, x in enumerate(t):
                o = t[1 - i]
                if net(x["abbr"]) is None or net(o["abbr"]) is None or len(team_dates[x["abbr"]]) < 5:
                    continue
                gap = net(x["abbr"]) - net(o["abbr"]) + (3.0 if x["home"] else -3.0)
                d_prev = [(day - dt.date.fromisoformat(d)).days for d in team_dates[x["abbr"]][-3:]]
                tonight = {p["pid"] for p in x["players"] if p["min"] > 0}
                roster = []
                for p in x["players"]:
                    h = hist[p["pid"]][-10:]
                    if len(h) >= 6 and sum(m for m, _, _ in h) / len(h) >= 24:
                        roster.append((p, sum(m for m, _, _ in h) / len(h), sum(q for _, q, _ in h) / len(h), h))
                top2 = {id(r[0]) for r in sorted(roster, key=lambda r: r[2], reverse=True)[:2]}
                # a top-3 scorer from the last 10 who is not in tonight's box
                tg_pts = defaultdict(list)
                for q, hh in ((q, hist[q][-10:]) for q in list(hist)):
                    pass
                for p, m10, q10, h in roster:
                    last3 = [q for _, q, _ in h[-3:]]
                    tag = {
                        "back-to-back (played yesterday)": d_prev[-1] <= 1 if d_prev else False,
                        "3+ days rest": d_prev[-1] >= 3 if d_prev else False,
                        "3rd game in 4 nights": len(d_prev) >= 2 and d_prev[-1] + d_prev[-2] <= 3,
                        "home": x["home"],
                        "big favourite (gap +6)": gap >= 6,
                        "big underdog (gap -6)": gap <= -6,
                        "opponent plays fast (top third)": (pace(o["abbr"]) or 0) >= 101.0,
                        "opponent plays slow (bottom third)": (pace(o["abbr"]) or 0) <= 97.5,
                        "opponent defence leaky (allows 117+)": (allowed(o["abbr"]) or 0) >= 117.0,
                        "opponent defence stingy (allows <=110)": (allowed(o["abbr"]) or 0) <= 110.0,
                        "hot (last 3 avg 4+ over his last 10)": sum(last3) / 3 >= q10 + 4,
                        "cold (last 3 avg 4+ under his last 10)": sum(last3) / 3 <= q10 - 4,
                    }
                    rows.append({"pid": p["pid"], "star": id(p) in top2, "min": p["min"], "pts": p["pts"],
                                 "dmin": p["min"] - m10, "dpts": p["pts"] - q10, "y25": int(p["pts"] >= 25),
                                 "tags": tag, "margin": pts[i] - pts[1 - i], "lines": (x.get("lines"), o.get("lines")),
                                 "min10": m10, "pts10": q10})
            for r_ in rows[_first:]:
                r_.setdefault("season", season)
            _first = len(rows)
            for i, x in enumerate(t):
                team_dates[x["abbr"]].append(g["date"])
                team_res[x["abbr"]].append((pts[i], pts[1 - i], poss[i]))
                for p in x["players"]:
                    if p["min"] > 0:
                        hist[p["pid"]].append((p["min"], p["pts"], p["fga"]))
    return rows


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def var(xs):
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)


def compare(with_, without, field):
    a, b = [r[field] for r in with_], [r[field] for r in without]
    diff = mean(a) - mean(b)
    se = math.sqrt(var(a) / len(a) + var(b) / len(b)) if len(a) > 1 and len(b) > 1 else float("nan")
    return diff, diff / se if se else float("nan")


def script(r):
    own, opp = r["lines"]
    if not own or not opp or len(own) < 4 or len(opp) < 4:
        return None
    m = r["margin"]
    after = [sum(own[:q]) - sum(opp[:q]) for q in (1, 2, 3)]
    out = []
    if abs(m) >= 20:
        out.append("blowout win (20+)" if m > 0 else "blowout loss (20+)")
    if m > 0 and min(after[1], after[2]) <= -10:
        out.append("comeback win (trailed by 10+ after Q2 or Q3)")
    if m < 0 and max(after[1], after[2]) >= 10:
        out.append("blown lead (led by 10+ after Q2 or Q3, lost)")
    if abs(m) <= 5 and len(own) == 4:
        out.append("close game (<=5, no OT)")
    if len(own) > 4:
        out.append("overtime")
    return out


def collect_nhl(cache):
    """Forwards averaging 13+ minutes, scored on 1+ goal and on shots, each minus HIS OWN trailing rate."""
    import nhl_backtest as nb
    rows = []
    for season in (2023, 2024, 2025):
        data = nb.load_season(season, cache)
        pp = {(r["playerId"], r["gameId"]): r for r in data["skater/powerplay"]}
        per = defaultdict(list)
        for r in data["skater/summary"]:
            if r["positionCode"] in ("G", "D") or not r.get("timeOnIcePerGame"):
                continue
            q = pp.get((r["playerId"], r["gameId"]), {})
            per[r["playerId"]].append({"date": r["gameDate"], "game": r["gameId"], "team": r["teamAbbrev"], "opp": r["opponentTeamAbbrev"],
                                       "shots": r["shots"], "goals": r["goals"], "toi": r["timeOnIcePerGame"] / 60.0,
                                       "pp": (q.get("ppTimeOnIce") or 0) / 60.0, "home": r.get("homeRoad") == "H"})
        gl = defaultdict(list)                                  # goalie -> starts
        starter = {}
        for r in sorted(data["goalie/summary"], key=lambda r: (r["gameDate"], r["gameId"])):
            if r.get("gamesStarted"):
                gl[r["playerId"]].append((r["gameDate"], r["saves"], r["shotsAgainst"]))
                starter[(r["gameId"], r["opponentTeamAbbrev"])] = r["playerId"]
        tgoals = defaultdict(lambda: defaultdict(int))          # game -> team -> goals (every skater, D included)
        tdates = defaultdict(dict)                              # team -> game -> date
        shots_against = defaultdict(lambda: defaultdict(float))  # team -> game -> shots it faced (every skater)
        for r in data["skater/summary"]:
            if r["positionCode"] == "G":
                continue
            tgoals[r["gameId"]][r["teamAbbrev"]] += r["goals"]
            tdates[r["teamAbbrev"]][r["gameId"]] = r["gameDate"]
            shots_against[r["opponentTeamAbbrev"]][r["gameId"]] += r["shots"]
        for pid, rs in per.items():
            rs.sort(key=lambda x: x["date"])
            for i, x in enumerate(rs):
                h = rs[:i]
                if len(h) < 8 or sum(y["toi"] for y in h[-10:]) / len(h[-10:]) < 13:
                    continue
                g_rate = (sum(y["goals"] for y in h) + 0.15 * 10) / (len(h) + 10)
                s_rate = sum(y["shots"] for y in h) / len(h)
                gap = (dt.date.fromisoformat(x["date"]) - dt.date.fromisoformat(h[-1]["date"])).days
                gid = starter.get((x["game"], x["team"]))
                gh = [t for t in gl.get(gid, []) if t[0] < x["date"]][-10:] if gid else []
                sv = (sum(t[1] for t in gh) + 0.905 * 200) / (sum(t[2] for t in gh) + 200) if gh else None
                opp_sa = [v for gm, v in shots_against[x["opp"]].items() if tdates[x["opp"]].get(gm, "9") < x["date"]]
                opp_games = sorted(d for d in tdates[x["opp"]].values() if d < x["date"])
                opp_gap = (dt.date.fromisoformat(x["date"]) - dt.date.fromisoformat(opp_games[-1])).days if opp_games else 5
                oppsa10 = sum(opp_sa[-10:]) / len(opp_sa[-10:]) if opp_sa else None
                last3 = sum(y["shots"] for y in h[-3:]) / 3
                tm = tgoals[x["game"]]
                margin = tm[x["team"]] - tm[x["opp"]]
                tags = {
                    "home": x["home"],
                    "back-to-back (he played yesterday)": gap <= 1,
                    "opponent on a back-to-back": opp_gap <= 1,
                    "3+ days rest": gap >= 3,
                    "goalie weak (recent save% <= .895)": sv is not None and sv <= 0.895,
                    "goalie strong (recent save% >= .915)": sv is not None and sv >= 0.915,
                    "goalie unknown / not yet a starter": sv is None,
                    "opponent allows many shots (31+)": oppsa10 is not None and oppsa10 >= 31,
                    "opponent allows few shots (<=27)": oppsa10 is not None and oppsa10 <= 27,
                    "power-play regular (2.5+ min a game)": sum(y["pp"] for y in h[-10:]) / len(h[-10:]) >= 2.5,
                    "hot shooter (last 3 avg 1.5+ over usual)": last3 >= s_rate + 1.5,
                    "cold shooter (last 3 avg 1.5+ under usual)": last3 <= s_rate - 1.5,
                }
                rows.append({"season": season, "tags": tags, "dgoal": int(x["goals"] >= 1) - g_rate, "dshots": x["shots"] - s_rate,
                             "margin": margin, "goal": int(x["goals"] >= 1), "shots": x["shots"], "toi": x["toi"]})
    return rows


def main_nhl(cache):
    import angle_engine as eng
    rows = collect_nhl(cache)
    print(f"{len(rows)} forward-games, base goal rate {100 * mean([r['goal'] for r in rows]):.1f}%\n")
    names = list(rows[0]["tags"])
    print("1. PREGAME ANGLES -- effect on goal probability (percentage points over his own usual rate)")
    for r in rows:
        r["dgoal_pp"] = 100 * r["dgoal"]
    eng.print_table(eng.evaluate(rows, names, "dgoal_pp", min_effect=1.0), "pp")
    print("\n   same angles, extra SHOTS on goal:")
    eng.print_table(eng.evaluate(rows, names, "dshots", min_effect=0.15), "sh")
    print("\n2. GAME SCRIPTS -- all forwards (post-game; for the live board)")
    for label, f in (("blowout win (3+ goals)", lambda m: m >= 3), ("one-goal win", lambda m: m == 1), ("one-goal loss", lambda m: m == -1),
                     ("blowout loss (3+ goals)", lambda m: m <= -3)):
        rs = [r for r in rows if f(r["margin"])]
        print(f"   {label:<26} n={len(rs):6}  goal {100 * mean([r['goal'] for r in rs]):5.1f}%  shots {mean([r['shots'] for r in rs]):4.2f}  minutes {mean([r['toi'] for r in rs]):5.1f}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", nargs="?", default="nba", choices=["nba", "nhl"])
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()
    if a.sport == "nhl":
        return main_nhl(a.cache or str(Path.home() / ".cache/dash_nhl"))
    a.cache = a.cache or str(Path.home() / ".cache/dash_nba")
    rows = collect(a.cache)
    rot = rows
    print(f"{len(rot)} rotation player-games, {sum(r['star'] for r in rot)} of them stars\n")
    base25 = 100 * mean([r["y25"] for r in rot if r["star"]])
    import angle_engine as eng
    print("1. PREGAME ANGLES -- effect = extra points vs his own last-10 average, tested by angle_engine")
    res = eng.evaluate(rot, list(rot[0]["tags"]), "dpts", min_effect=0.3)
    eng.print_table(res, "pts")
    print("\n   same angles, extra MINUTES:")
    eng.print_table(eng.evaluate(rot, list(rot[0]["tags"]), "dmin", min_effect=0.5), "min")

    print(f"\n2. GAME SCRIPTS -- the stars only (post-game: for the live board and for weighting scenarios)")
    stars = [r for r in rot if r["star"]]
    print(f"   {'script':<48}{'n':>6}{'share':>7}{'min':>7}{'pts':>7}{'vs usual':>9}{'25+':>7}")
    buckets = defaultdict(list)
    for r in stars:
        s = script(r)
        if s is None:
            continue
        for k in s or ["other"]:
            buckets[k].append(r)
    allk = [r for r in stars if script(r) is not None]
    print(f"   {'all games':<48}{len(allk):>6}{'':>7}{mean([r['min'] for r in allk]):>7.1f}{mean([r['pts'] for r in allk]):>7.1f}{mean([r['dpts'] for r in allk]):>+9.2f}{100 * mean([r['y25'] for r in allk]):>6.1f}%")
    for k, rs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"   {k:<48}{len(rs):>6}{100 * len(rs) / len(allk):>6.0f}%{mean([r['min'] for r in rs]):>7.1f}{mean([r['pts'] for r in rs]):>7.1f}{mean([r['dpts'] for r in rs]):>+9.2f}{100 * mean([r['y25'] for r in rs]):>6.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
