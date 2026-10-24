#!/usr/bin/env python3
"""
buckets_backtest.py -- BUCKETS v1 against naive baselines on every NBA game in two seasons.

    python bots/buckets_backtest.py [--cache DIR] [--seasons 2023 2024]

Box scores come from ESPN's public site API (stats.nba.com refuses this machine),
pulled once and cached. A player is scored on his earlier games THIS season
only (8+), using nothing from the game itself.

Reported for "scores 25+" and "scores 30+": the best candidate per game, the top 3
per game, and the top 10 per night, against season points per game, last-10 points
per game, and a coin. There are no historical NBA prices here.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_scorer as bk   # noqa: E402

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
BOX_VERSION = "v4"          # v2 stored home/away; v3 adds quarter scores; v4 adds reb/ast/3PM/3PA/stl/blk (the other lanes)
RANGE = {2023: ("2023-10-24", "2024-04-14"), 2024: ("2024-10-22", "2025-04-13"), 2025: ("2025-10-21", "2026-04-13"),
         2026: ("2026-10-20", (dt.date.today() - dt.timedelta(days=1)).isoformat())}
MIN_PRIOR = 8


def get(url, retries=3):
    for _ in range(retries):
        r = subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True)
        if r.stdout.startswith("{"):
            try:
                return json.loads(r.stdout)
            except ValueError:
                pass
    return None


def day_events(day, cache):
    p = Path(cache) / f"day_{day}.json"
    if p.exists():
        return json.load(open(p))
    d = get(f"{ESPN}/scoreboard?dates={day.replace('-', '')}")
    ids = [e["id"] for e in (d or {}).get("events", []) if e.get("status", {}).get("type", {}).get("completed")]
    json.dump(ids, open(p, "w"))
    return ids


def box(gid, day, cache):
    p = Path(cache) / f"box{BOX_VERSION}_{gid}.json"
    if p.exists():
        return json.load(open(p))
    d = get(f"{ESPN}/summary?event={gid}")
    out = {"id": gid, "date": day, "teams": []}
    for t in ((d or {}).get("boxscore") or {}).get("players", []):
        st = t["statistics"][0]
        L = st["labels"]
        rows = []
        for a in st["athletes"]:
            s = dict(zip(L, a["stats"]))
            if not a["stats"] or s.get("MIN") in (None, "", "--"):
                continue
            def mk(x):
                try:
                    m, n = x.split("-"); return int(m), int(n)
                except Exception:
                    return 0, 0
            fg, ft = mk(s.get("FG", "0-0")), mk(s.get("FT", "0-0"))
            tp = mk(s.get("3PT", "0-0"))
            rows.append({"pid": a["athlete"]["id"], "min": float(s["MIN"] or 0), "pts": int(s.get("PTS") or 0),
                         "fga": fg[1], "fta": ft[1], "to": int(s.get("TO") or 0), "oreb": int(s.get("OREB") or 0),
                         "reb": int(s.get("REB") or 0), "ast": int(s.get("AST") or 0), "tpm": tp[0], "tpa": tp[1],
                         "stl": int(s.get("STL") or 0), "blk": int(s.get("BLK") or 0)})
        out["teams"].append({"abbr": t["team"]["abbreviation"], "players": rows})
    home = {m["team"]["abbreviation"]: m.get("homeAway") == "home"
            for m in ((d or {}).get("boxscore") or {}).get("teams", [])}
    lines = {}
    for c in (((d or {}).get("header") or {}).get("competitions") or [{}])[0].get("competitors", []):
        try:
            lines[c["team"]["abbreviation"]] = [int(l["displayValue"]) for l in c.get("linescores", [])]
        except (KeyError, ValueError):
            pass
    for tm in out["teams"]:
        tm["home"] = home.get(tm["abbr"], False)
        tm["lines"] = lines.get(tm["abbr"], [])
    json.dump(out, open(p, "w"))
    return out


def load(season, cache):
    a, b = (dt.date.fromisoformat(x) for x in RANGE[season])
    days = [(a + dt.timedelta(n)).isoformat() for n in range((b - a).days + 1)]
    Path(cache).mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(12) as ex:
        ids = list(ex.map(lambda d: day_events(d, cache), days))
    jobs = [(g, d) for d, gs in zip(days, ids) for g in gs]
    with ThreadPoolExecutor(12) as ex:
        return [x for x in ex.map(lambda j: box(j[0], j[1], cache), jobs) if len(x["teams"]) == 2]


def run(season, cache):
    import game_lines as G
    lines_dir = Path(cache).parent / "nba_lines"            # closing lines, when pulled (game_lines.fetch_closing)
    games = sorted(load(season, cache), key=lambda g: (g["date"], g["id"]))
    return run_games(games, season, lambda gid: G.close_line("nba", gid, lines_dir) if lines_dir.exists() else None)


def run_games(games, season, line_for, only_ids=None):
    """Features and scores for every rotation player-game in `games` (oldest first). nba_live.py passes a stream of
    finished games plus tonight's as zero-stat rows, so the live board and the backtest share one code path.
    only_ids: if given, return rows for those game ids only."""
    team_hist = defaultdict(list)                  # team -> [(pace, pts_allowed, pts_for)]
    team_last = {}                                 # team -> date of its last game
    team_games = defaultdict(list)                 # team -> [{pid: (min, pts)}], oldest first
    last_idx = {}                                  # (team, pid) -> index of the team game he last played
    ph = defaultdict(list)                         # player -> games
    out = []
    for g in games:
        t = g["teams"]
        poss, pts_for, reb_for, ast_for, tpm_for = [], [], [], [], []
        for team in t:
            P = team["players"]
            poss.append(sum(p["fga"] + 0.44 * p["fta"] - p["oreb"] + p["to"] for p in P))
            pts_for.append(sum(p["pts"] for p in P))
            reb_for.append(sum(p.get("reb", 0) for p in P)); ast_for.append(sum(p.get("ast", 0) for p in P)); tpm_for.append(sum(p.get("tpm", 0) for p in P))
        for i, team in enumerate(t):
            opp = t[1 - i]["abbr"]
            tp = [x[0] for x in team_hist[team["abbr"]]]
            op = [x[0] for x in team_hist[opp]]
            tpace = sum(tp[-10:]) / len(tp[-10:]) if tp else bk.LG_PACE
            opace = sum(op[-10:]) / len(op[-10:]) if op else bk.LG_PACE
            allowed = [x[1] for x in team_hist[opp]]

            def net(tm):
                h = team_hist[tm][-10:]
                return sum(x[2] - x[1] for x in h) / len(h) if h else 0.0
            tg = team_games[team["abbr"]]
            tonight = {p["pid"] for p in team["players"] if p["min"] > 0}
            cnt, mins, tpts = defaultdict(int), defaultdict(float), defaultdict(float)
            for gm in tg[-10:]:
                for pid_, (m_, pt_) in gm.items():
                    cnt[pid_] += 1; mins[pid_] += m_; tpts[pid_] += pt_
            # regulars (5+ of the last 10, 18+ min) who are not in tonight's box: the "teammate out" angle.
            # Absence is read from the box score; a pregame injury report would say the same for nearly all of them.
            out_ppg = sum(tpts[q] / cnt[q] for q in cnt if cnt[q] >= 5 and mins[q] / cnt[q] >= 18 and q not in tonight)
            gap_days = (dt.date.fromisoformat(g["date"]) - dt.date.fromisoformat(team_last[team["abbr"]])).days \
                if team["abbr"] in team_last else 5
            ln = line_for(g["id"])
            has_ln = bool(ln and ln.get("spread_home") is not None)
            book_fav = (-ln["spread_home"] if team["home"] else ln["spread_home"]) if has_ln else 0.0
            for p in team["players"]:
                hist = ph[p["pid"]]
                if len(hist) >= MIN_PRIOR and p["min"] >= 10:
                    s = bk.score_player(hist, tpace, opace, allowed)
                    f = {k: s[k] for k in ("scoring_pts", "usage_pts", "attempts_pts", "minutes_pts", "pace_pts")}
                    f.update({"opp_allowed": s.get("opp_pts_allowed", 113.0), "pace": (tpace + opace) / 2,
                              "strength_gap": net(team["abbr"]) - net(opp) + (3.0 if team["home"] else -3.0),
                              "is_home": float(team["home"]), "b2b": float(gap_days <= 1),
                              "book_fav": float(book_fav), "book_blowout_loss": float(max(0.0, -book_fav)),
                              "book_total": float(ln.get("total") or 0.0) if has_ln else 0.0,
                              "teammates_out_ppg": out_ppg,
                              "games_missed": float(min(10, len(tg) - 1 - last_idx[(team["abbr"], p["pid"])]))
                              if (team["abbr"], p["pid"]) in last_idx else 0.0,
                              "ppg": sum(x["pts"] for x in hist) / len(hist),
                              "l10": sum(x["pts"] for x in hist[-10:]) / len(hist[-10:]),
                              "form3": sum(x["pts"] for x in hist[-3:]) / len(hist[-3:]) - sum(x["pts"] for x in hist[-10:]) / len(hist[-10:]),
                              "fav_gap": max(0.0, net(team["abbr"]) - net(opp) + (3.0 if team["home"] else -3.0)),
                              "dog_gap": max(0.0, -(net(team["abbr"]) - net(opp) + (3.0 if team["home"] else -3.0)))})
                    if only_ids is not None and g["id"] not in only_ids:
                        pass
                    else:
                      out.append({"night": g["date"], "game": g["id"], "y25": int(p["pts"] >= 25), "y30": int(p["pts"] >= 30),
                                "pid": p["pid"], "team": team["abbr"], "opp": opp, "home": bool(team["home"]),
                                "bk": s["final_score"], "ppg": sum(x["pts"] for x in hist) / len(hist),
                                "l10": sum(x["pts"] for x in hist[-10:]) / len(hist[-10:]),
                                "rand": ((int(p["pid"]) * 2654435761 + int(g["id"])) % 1000) / 1000, "f": f, "season": season,
                                "lanes": lane_rows(hist, p, team_hist[opp], f)})
        for i, team in enumerate(t):
            team_hist[team["abbr"]].append((poss[i], pts_for[1 - i], pts_for[i], reb_for[1 - i], ast_for[1 - i], tpm_for[1 - i]))   # [3:] = what the OTHER side got against him
            team_last[team["abbr"]] = g["date"]
            team_games[team["abbr"]].append({q["pid"]: (q["min"], q["pts"]) for q in team["players"] if q["min"] > 0})
            for q in team["players"]:
                if q["min"] > 0:
                    last_idx[(team["abbr"], q["pid"])] = len(team_games[team["abbr"]]) - 1
            for p in team["players"]:
                m = max(p["min"], 1.0)
                ph[p["pid"]].append({"pts": p["pts"], "fga": p["fga"], "minutes": p["min"],
                                     "usage": (p["fga"] + 0.44 * p["fta"] + p["to"]) / m,
                                     "reb": p.get("reb", 0), "ast": p.get("ast", 0), "tpm": p.get("tpm", 0), "tpa": p.get("tpa", 0)})
    return out


LANE_BARS = {"reb": 10, "ast": 8, "3pm": 4, "pra": 35}      # the site's bars: REB 10+, AST 8+, 3PM 4+, PRA 35+
LANE_STAT = {"reb": "reb", "ast": "ast", "3pm": "tpm"}


def lane_rows(hist, p, opp_hist, f):
    """Features and the outcome for each non-points lane, from games BEFORE this one. Empty until the v4 box cache (reb/ast/3PM) exists."""
    if "reb" not in p:
        return {}
    h10 = hist[-10:]
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    common = {"min10": mean([x["minutes"] for x in h10]), "fga10": mean([x["fga"] for x in h10]), "pace": f["pace"], "is_home": f["is_home"],
              "b2b": f["b2b"], "teammates_out_ppg": f["teammates_out_ppg"], "games_missed": f["games_missed"], "book_fav": f["book_fav"],
              "book_total": f["book_total"]}
    o10 = opp_hist[-10:]
    out = {}
    for mk, stat in LANE_STAT.items():
        sea = mean([x[stat] for x in hist]); l10 = mean([x[stat] for x in h10])
        per_min = sum(x[stat] for x in h10) / max(1.0, sum(x["minutes"] for x in h10)) * 30.0
        feats = {**common, "season_avg": sea, "l10_avg": l10, "l10_per30": per_min, "form3": mean([x[stat] for x in hist[-3:]]) - l10}
        if mk == "reb":
            feats["opp_allowed"] = mean([x[3] for x in o10]) if o10 else 44.0
        elif mk == "ast":
            feats["opp_allowed"] = mean([x[4] for x in o10]) if o10 else 25.0
            feats["usage10"] = mean([x["usage"] for x in h10])
        else:
            made, att = sum(x["tpm"] for x in hist), sum(x["tpa"] for x in hist)
            feats["tpa10"] = mean([x["tpa"] for x in h10]); feats["tp_pct"] = (made + 0.36 * 50) / (att + 50)
            feats["opp_allowed"] = mean([x[5] for x in o10]) if o10 else 12.5
        out[mk] = {"y": int(p[stat if mk != "3pm" else "tpm"] >= LANE_BARS[mk]), "f": feats, "avg": sea}
    pra_h = [x["pts"] + x["reb"] + x["ast"] for x in hist]
    out["pra"] = {"y": int(p["pts"] + p["reb"] + p["ast"] >= LANE_BARS["pra"]),
                  "f": {**common, "season_avg": mean(pra_h), "l10_avg": mean(pra_h[-10:]), "ppg": f["ppg"], "opp_allowed": f["opp_allowed"],
                        "form3": mean(pra_h[-3:]) - mean(pra_h[-10:])}, "avg": mean(pra_h)}
    return out


def topk(rows, key, y, k, group):
    g = defaultdict(list)
    for r in rows:
        g[r[group]].append(r)
    hit = n = 0
    for v in g.values():
        top = sorted(v, key=lambda r: r[key], reverse=True)[:k]
        hit += sum(r[y] for r in top); n += len(top)
    return 100 * hit / n if n else float("nan")


def report(rows, title):
    base25 = 100 * sum(r["y25"] for r in rows) / len(rows); base30 = 100 * sum(r["y30"] for r in rows) / len(rows)
    print(f"\n{title}: {len(rows)} player-games, base 25+ {base25:.1f}%, 30+ {base30:.1f}%")
    for label, key in (("BUCKETS v1", "bk"), ("season ppg", "ppg"), ("last-10 ppg", "l10"), ("coin", "rand")):
        print(f"  {label:<14} 25+: top1/game {topk(rows, key, 'y25', 1, 'game'):5.1f}%  top3/game {topk(rows, key, 'y25', 3, 'game'):5.1f}%"
              f"  top10/night {topk(rows, key, 'y25', 10, 'night'):5.1f}%   |  30+: top1/game {topk(rows, key, 'y30', 1, 'game'):5.1f}%"
              f"  top10/night {topk(rows, key, 'y30', 10, 'night'):5.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    ap.add_argument("--seasons", nargs="+", type=int, default=[2023, 2024])
    a = ap.parse_args()
    allrows = []
    for s in a.seasons:
        rows = run(s, a.cache)
        report(rows, f"{s}-{str(s + 1)[2:]}")
        allrows += rows
    report(allrows, "POOLED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
