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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    a = ap.parse_args()
    rows = collect(a.cache)
    rot = rows
    print(f"{len(rot)} rotation player-games, {sum(r['star'] for r in rot)} of them stars\n")
    base25 = 100 * mean([r["y25"] for r in rot if r["star"]])
    print(f"1. PREGAME ANGLES -- rotation players, vs rotation players without the tag (his own last-10 is the yardstick)")
    print(f"   {'angle':<42}{'n':>6}{'+pts':>7}{'t':>6}{'+min':>7}{'t':>6}{'star 25+ gap':>14}")
    table = []
    for name in rot[0]["tags"]:
        w = [r for r in rot if r["tags"][name]]; wo = [r for r in rot if not r["tags"][name]]
        if len(w) < 100:
            continue
        dp, tp = compare(w, wo, "dpts"); dm, tm = compare(w, wo, "dmin")
        sw = [r for r in w if r["star"]]; swo = [r for r in wo if r["star"]]
        g25 = 100 * (mean([r["y25"] for r in sw]) - mean([r["y25"] for r in swo])) if sw and swo else float("nan")
        table.append((abs(tp), name, len(w), dp, tp, dm, tm, g25))
    for _, name, n, dp, tp, dm, tm, g25 in sorted(table, reverse=True):
        flag = "" if abs(tp) >= 2 else "   (noise)"
        print(f"   {name:<42}{n:>6}{dp:>+7.2f}{tp:>6.1f}{dm:>+7.2f}{tm:>6.1f}{g25:>+13.1f}%{flag}")

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
