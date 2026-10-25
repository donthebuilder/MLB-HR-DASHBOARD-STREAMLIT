#!/usr/bin/env python3
"""
nfl_shadow.py -- prospective record of the NFL TD lanes, graded after the games.

    python bots/nfl/nfl_shadow.py snapshot SEASON WEEK OUT_DIR [--cache DIR]
    python bots/nfl/nfl_shadow.py grade    SEASON WEEK OUT_DIR
    python bots/nfl/nfl_shadow.py report   OUT_DIR

Per player: the fitted anytime-TD probability (nfl_td_fit_v1), the first-TD probability for RB/WR/TE (nfl_first_td_v1, rescaled per game), and the v2
score, saved BEFORE kickoff. A team's rows are refreshed on every run until its kickoff and frozen after it (so a late injury or line move is picked up,
and nothing is ever written after the game starts). Grading: anytime TD from the week's player stats, first TD from play-by-play.
Runs on its own schedule (Thursday, Sunday, Monday), not with the NHL/NBA jobs.
"""
import datetime as dt, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))


def kickoffs(season, week):
    from nfl_regrade import kickoffs as k
    return k(season, week)


def snapshot(season, week, out_dir, cache):
    import nfl_features as F, nfl_first as NF, td_fit, lane_fit
    from nfl_td_v3_backtest import VARIANTS
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / f"shadow_nfl_{season}_w{int(week):02d}.json"
    have = json.load(open(path)) if path.exists() else {"season": season, "week": week, "teams": {}}
    now = dt.datetime.now(dt.timezone.utc)
    ko = kickoffs(season, week)
    slate, league, bye = F.upcoming_rows(season, week)
    fit = td_fit.score(slate)
    lanes = lane_fit.score_all(slate)
    rows = NF.build_rows(cache, (season,), live={"season": season, "week": week, "slate": slate})
    live = [r for r in rows if r.get("live")]
    fm = json.load(open(HERE.parent / "models" / "nfl_first_td_v1.json"))
    X = np.array([[r[c] for c in fm["features"]] for r in live], float)
    z = (X - np.array(fm["mu"])) / np.array(fm["sd"]) @ np.array(fm["weights"]) + fm["intercept"]
    z = fm["platt"]["a"] + fm["platt"]["b"] * z
    p = 1 / (1 + np.exp(-z))
    by = defaultdict(list)
    for i, r in enumerate(live):
        by[r["game"]].append(i)
    pf = np.zeros(len(live))
    for ix in by.values():
        pf[ix] = p[ix] / p[ix].sum() * fm["listed_share"]
    tl = fm.get("tail")
    if tl:                                              # the top end over-predicted on the holdout: compress above t, strictly increasing
        pf = np.where(pf > tl["t"], tl["t"] + (pf - tl["t"]) * tl["slope"], pf)
    teams = defaultdict(list)
    names = {r["player_id"]: r.get("name") or r.get("player_display_name") for r in slate.iter_rows(named=True)}
    for r in slate.iter_rows(named=True):
        teams[r["team"]].append(r)
    fr = {live[i]["pid"]: float(pf[i]) for i in range(len(live))}
    written = frozen = 0
    for tm, rs in teams.items():
        k = ko.get(tm)
        if tm in have["teams"] and have["teams"][tm].get("locked"):
            continue
        if k is not None and now >= k and not __import__("os").environ.get("NFL_SHADOW_TEST_BACKFILL"):   # the env var is for testing the grader only
            if tm in have["teams"]:
                have["teams"][tm]["locked"] = True; frozen += 1
            continue
        have["teams"][tm] = {"locked": False, "kickoff": k.isoformat() if k else None, "saved_utc": now.isoformat(),
                             "rows": [{"pid": r["player_id"], "name": names.get(r["player_id"]), "pos": r.get("position"), "opp": r.get("opponent_team"),
                                       "p_td_fit": fit.get(str(r["player_id"])), "p_first_td": round(fr[r["player_id"]], 4) if r["player_id"] in fr else None,
                                       "p_lane": {m: v[str(r["player_id"])] for m, v in lanes.items() if str(r["player_id"]) in v}} for r in rs]}
        written += 1
    path.write_text(json.dumps(have))
    print(f"{season} w{week}: {written} team(s) written/refreshed, {frozen} frozen at kickoff, {len(have['teams'])} in file")


def grade(season, week, out_dir):
    import nflreadpy as nfl, polars as pl, nfl_first as NF
    path = Path(out_dir) / f"shadow_nfl_{season}_w{int(week):02d}.json"
    d = json.load(open(path))
    st = nfl.load_player_stats(seasons=[season], summary_level="week").filter(pl.col("week") == week)
    td = {r["player_id"]: int(((r["rushing_tds"] or 0) + (r["receiving_tds"] or 0)) >= 1) for r in st.iter_rows(named=True)}
    import lane_fit
    stat_rows = {r["player_id"]: r for r in st.iter_rows(named=True)}
    first, _ = NF.pbp_facts(season)
    first_by_pair = {}
    for gid, v in first.items():
        parts = str(gid).split("_")
        if len(parts) >= 4 and int(parts[1]) == int(week):
            first_by_pair[frozenset(parts[2:4])] = v["pid"]
    n = 0
    for tm, g in d["teams"].items():
        for r in g["rows"]:
            if r["pid"] in td:
                r["td"] = td[r["pid"]]; n += 1
                r["lane_y"] = {m: int(lane_fit.OUTCOMES[m](stat_rows[r["pid"]])) for m in r.get("p_lane", {})}
            fp = first_by_pair.get(frozenset([tm, r["opp"]]))
            if fp is not None:
                r["first_td"] = int(fp == r["pid"])
    path.write_text(json.dumps(d))
    print(f"{season} w{week}: anytime TD graded for {n} players")


def report_lanes(out_dir):
    import lane_fit
    rows = []
    for f in sorted(Path(out_dir).glob("shadow_nfl_*.json")):
        d = json.load(open(f))
        rows += [r for g in d["teams"].values() for r in g["rows"] if r.get("lane_y")]
    for m in lane_fit.MARKETS:
        rs = [(r["p_lane"][m], r["lane_y"][m]) for r in rows if m in r.get("lane_y", {})]
        if len(rs) < 50:
            print(f"  {m:<9} n={len(rs)} (too few to read)"); continue
        rs.sort(); q = len(rs) // 4
        print(f"  {m:<9} n={len(rs)}  " + "  ".join(f"{100 * sum(x for x, _ in b) / len(b):.0f}%->{100 * sum(y for _, y in b) / len(b):.0f}%" for b in [rs[i * q:(i + 1) * q if i < 3 else len(rs)] for i in range(4)]))


def report(out_dir):
    rows = []
    for f in sorted(Path(out_dir).glob("shadow_nfl_*.json")):
        d = json.load(open(f))
        rows += [r for g in d["teams"].values() for r in g["rows"] if "td" in r]
    print(f"NFL: {len(rows)} graded player-weeks")
    for key, y, lab in (("p_td_fit", "td", "Anytime TD"), ("p_first_td", "first_td", "First TD")):
        rs = [r for r in rows if r.get(key) is not None and y in r]
        if len(rs) < 50:
            print(f"  {lab:<11} n={len(rs)} (too few to read)"); continue
        rs.sort(key=lambda r: r[key]); q = len(rs) // 4
        bands = [rs[i * q:(i + 1) * q if i < 3 else len(rs)] for i in range(4)]
        print(f"  {lab:<11} n={len(rs)}  " + "  ".join(f"{100 * sum(r[key] for r in b) / len(b):.0f}%->{100 * sum(r[y] for r in b) / len(b):.0f}%" for b in bands) + "  (predicted->actual, quartiles)")


def season_now():
    now = dt.datetime.now(dt.timezone.utc)
    return now.year if now.month >= 3 else now.year - 1


def auto_snapshot(out_dir, cache):
    import nfl_features as F
    season = season_now()
    price, _ = F.schedule_weeks(season)
    if price is None:
        print("no NFL week to price"); return
    snapshot(season, price, out_dir, cache)


def auto_grade(out_dir):
    """Grade every saved week that has ungraded rows and whose games are over (last kickoff + 5h)."""
    now = dt.datetime.now(dt.timezone.utc)
    for f in sorted(Path(out_dir).glob("shadow_nfl_*.json")):
        d = json.load(open(f))
        ks = [dt.datetime.fromisoformat(g["kickoff"]) for g in d["teams"].values() if g.get("kickoff")]
        pending = any("td" not in r for g in d["teams"].values() for r in g["rows"])
        if ks and pending and now > max(ks) + dt.timedelta(hours=5):
            grade(d["season"], d["week"], out_dir)


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "auto-snapshot":
        import os
        auto_snapshot(sys.argv[2], os.environ.get("DASH_NFL_CACHE", str(Path.home() / ".cache/dash_nfl")))
    elif c == "auto-grade":
        auto_grade(sys.argv[2])
    elif c == "report":
        report(sys.argv[2]); report_lanes(sys.argv[2])
    elif c == "snapshot":
        import os
        snapshot(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], os.environ.get("DASH_NFL_CACHE", str(Path.home() / ".cache/dash_nfl")))
    else:
        grade(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
