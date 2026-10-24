#!/usr/bin/env python3
"""
nba_live.py -- tonight's NBA board: every rotation player on tonight's teams, scored, long shots included.

    python bots/nba_live.py [YYYY-MM-DD] [--out FILE]

Same code path as the backtest: this and last season's finished games are streamed through buckets_backtest.run_games, with
tonight's games appended as zero-stat rows. Teams carry over from last season so the board is not empty in October (the
backtest started each season from zero; a player needs 8 prior games, from either season).

The tuned model (models/nba_pts_v2.json) gives P(25+ points). Then:
  - injuries (ESPN): anyone under 50% to play is taken OUT of tonight's box, so the teammate-out effect (the third-strongest
    factor in the fit) fires for the men who pick up his shots; everyone else keeps his chance of playing.
  - the book's spread and total (ESPN) go in as features, and a BLOWOUT WATCH flag is raised at the site's rule (a spread of
    10+, game_lines.BLOWOUT_SPREAD): a star on the big underdog reached 25+ about 23% of the time against 42% on the favourite.
Tier = thirds of tonight's probability: TOP / MIDDLE / LONG SHOT. Nobody is dropped for being a long shot.
"""
import argparse
import datetime as dt
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buckets_backtest as bb   # noqa: E402
import game_lines as G          # noqa: E402
import injury_status            # noqa: E402
import lineups                  # noqa: E402


def parse_spread(details, home, away):
    m = re.match(r"\s*([A-Z]{2,4})\s+([-+]?\d+(?:\.\d+)?)", str(details or ""))
    if not m:
        return 0.0 if str(details or "").upper() in ("EVEN", "PK", "PICK") else None
    fav, val = m.group(1), float(m.group(2))
    return val if fav == home else -val if fav == away else None


def _calibrate(model, p):
    """raw model chance -> chance as graded on the holdout (isotonic, models/*.json 'calibration')."""
    c = model.get("calibration")
    if not c:
        return p
    xs, ys = c["x"], c["y"]
    if p <= xs[0]:
        return ys[0] * p / xs[0] if xs[0] else p
    if p >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if p <= xs[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (p - xs[i - 1]) / (xs[i] - xs[i - 1]) if xs[i] > xs[i - 1] else ys[i]
    return p


def athlete(pid, cache):
    f = Path(cache) / f"ath_{pid}.json"
    if f.exists():
        return json.load(open(f))
    try:
        a = json.loads(__import__("subprocess").run(["curl", "-s", "-m", "30", f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}"],
                                                     capture_output=True, text=True).stdout)["athlete"]
        v = {"name": a.get("displayName"), "jersey": a.get("jersey"), "birth": None}
    except Exception:
        v = {"name": None, "jersey": None, "birth": None}
    f.write_text(json.dumps(v))
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--out", default=None)
    ap.add_argument("--cache", default=str(Path.home() / ".cache/dash_nba"))
    a = ap.parse_args()
    day = a.day
    tonight = [g for g in lineups.espn("nba", day) if g["status"] not in ("Final",)]
    if not tonight:
        print(f"no NBA games left on {day}")
        return 0
    stream = []
    for s in (2025, 2026):
        stream += [g for g in bb.load(s, a.cache) if g["date"] < day]
    stream.sort(key=lambda g: (g["date"], g["id"]))
    inj = injury_status.fetch("nba")
    inj_by_key = {r["key"]: r for r in inj}

    recent = defaultdict(lambda: defaultdict(list))      # team -> pid -> [minutes in the team's last 10 games]
    last10 = defaultdict(list)
    for g in stream:
        for t in g["teams"]:
            last10[t["abbr"]].append(t)
    for team, ts in last10.items():
        for t in ts[-10:]:
            for p in t["players"]:
                if p["min"] > 0:
                    recent[team][p["pid"]].append(p["min"])
    meta, lines, synth_ids, skipped = {}, {}, set(), []
    for n, g in enumerate(tonight):
        gid = str(9_000_000_000 + n)
        synth_ids.add(gid)
        away, home = g["sides"]["away"]["team"], g["sides"]["home"]["team"]
        sp = parse_spread(g.get("spread"), home, away)
        lines[gid] = {"spread_home": sp, "total": g.get("total")} if sp is not None else None
        meta[gid] = {"away": away, "home": home, "spread_home": sp, "total": g.get("total"), "start": g.get("start_utc")}
        teams = []
        for abbr, is_home in ((away, False), (home, True)):
            players = []
            for pid, mins in recent[abbr].items():
                if len(mins) < 3 or sum(mins) / len(mins) < 10:
                    continue
                name = athlete(pid, a.cache)["name"]
                hit = inj_by_key.get(injury_status.key(name or ""))
                if hit and hit["p_play"] < 0.5:
                    skipped.append((name, abbr, hit["status"], hit["p_play"]))
                    continue
                players.append({"pid": pid, "min": 30.0, "pts": 0, "fga": 0, "fta": 0, "to": 0, "oreb": 0})
            teams.append({"abbr": abbr, "home": is_home, "players": players})
        stream.append({"id": gid, "date": day, "teams": teams})

    rows = bb.run_games(stream, 2026, lambda gid: lines.get(gid) if gid in lines else None, only_ids=synth_ids)
    model = json.load(open(Path(__file__).resolve().parent / "models" / "nba_pts_v2.json"))
    out_rows = []
    for r in rows:
        z = model["intercept"] + sum(w * (r["f"][n] - m) / s for n, w, m, s in zip(model["features"], model["weights"], model["mu"], model["sd"]))
        p_raw = 1 / (1 + math.exp(-z))
        p = _calibrate(model, p_raw)
        mt = meta[r["game"]]
        info = athlete(r["pid"], a.cache)
        spread_team = (-mt["spread_home"] if r["home"] else mt["spread_home"]) if mt["spread_home"] is not None else None   # + = this club favoured
        watch = G.blowout_watch("nba", mt["spread_home"])
        out_rows.append({"name": info["name"], "team": r["team"], "opponent": r["opp"], "home": r["home"], "model_version": model["model_version"],
                         "p_25": round(p, 4), "p_25_raw": round(p_raw, 4), "ppg": round(r["f"]["ppg"], 1), "l10_ppg": round(r["f"]["l10"], 1), "minutes_pts": r["f"]["minutes_pts"],
                         "attempts_pts": r["f"]["attempts_pts"], "teammates_out_ppg": round(r["f"]["teammates_out_ppg"], 1), "games_missed": r["f"]["games_missed"],
                         "back_to_back": bool(r["f"]["b2b"]), "book_spread_for_his_club": spread_team, "book_total": mt["total"],
                         "blowout_watch": bool(watch and watch["watch"]),
                         "blowout_risk": ("his club is the big UNDERDOG" if watch and watch["watch"] and spread_team is not None and spread_team < 0
                                          else "his club is the big favourite" if watch and watch["watch"] else None),
                         "start_utc": mt["start"]})
    injury_status.apply(out_rows, inj)
    for r in out_rows:
        r["p_25_adj"] = round(r["p_25"] * r["p_play"], 4)
    order = sorted(r["p_25"] for r in out_rows)
    lo, hi = order[len(order) // 3], order[2 * len(order) // 3]
    for r in out_rows:
        r["tier"] = "TOP" if r["p_25"] >= hi else "MIDDLE" if r["p_25"] >= lo else "LONG SHOT"
    out_rows.sort(key=lambda r: (r["p_25_adj"], r["p_25_raw"]), reverse=True)   # the calibrated top block has ties; raw breaks them
    out = Path(a.out) if a.out else Path(__file__).resolve().parent / "outputs" / "nba_board_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"date": day, "model_version": model["model_version"], "players": out_rows,
                               "left_out_injured": [{"name": n, "team": t, "status": s, "p_play": p} for n, t, s, p in skipped]}, indent=1))
    print(f"{len(tonight)} games, {len(out_rows)} players scored, {len(skipped)} taken out as injured -> {out}")
    print(f"{'player':<22}{'team':<5}{'vs':<5}{'P(25+)':>8}{'plays':>7}{'ppg':>6}{'l10':>6}  spread / flag")
    for r in out_rows[:12]:
        print(f"{(r['name'] or '?')[:21]:<22}{r['team']:<5}{r['opponent']:<5}{100 * r['p_25']:>7.1f}%{100 * r['p_play']:>6.0f}%{r['ppg']:>6.1f}{r['l10_ppg']:>6.1f}  {r['book_spread_for_his_club']}  {r['blowout_risk'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
