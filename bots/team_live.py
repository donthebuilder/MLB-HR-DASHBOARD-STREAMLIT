#!/usr/bin/env python3
"""
team_live.py -- today's MONEYLINE / SPREAD board from team_models.py, logged before the games and graded after.

    python bots/team_live.py board    nba|nhl|nfl DAY [--out FILE]
    python bots/team_live.py snapshot nba|nhl|nfl DAY OUT_DIR       (games not yet started only; rows frozen once saved)
    python bots/team_live.py grade    nba|nhl|nfl DAY OUT_DIR
    python bots/team_live.py report   OUT_DIR

Per game: the model's expected margin (as a spread) and home win probability, the book's spread / moneyline / total (ESPN scoreboard), and the gap. The history
behind each number runs across seasons, as in the backtest. The model is NOT expected to beat the closing line (team_models.py measures how close it gets); the log
exists so that is checked going forward, game by game.
"""
import datetime as dt, json, math, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team_models as tm            # noqa: E402
import lineups                      # noqa: E402
from nba_live import parse_spread    # noqa: E402

HERE = Path(__file__).resolve().parent
ESPN_TO_NHL = {"NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}
import os
CACHE = {"nba": ".cache/dash_nba", "nhl": ".cache/dash_nhl", "nfl": ".cache/dash_nfl"}


def cache_dir(sport):
    return os.environ.get(f"DASH_CACHE_{sport.upper()}", str(Path.home() / CACHE[sport]))


def slate(sport, day):
    """[{home, away, start, book_spread_home (negative = home favoured), total}] for the day."""
    if sport == "nfl":
        import nflreadpy as nfl
        out = []
        for r in nfl.load_schedules([int(day[:4]) if int(day[5:7]) >= 3 else int(day[:4]) - 1]).iter_rows(named=True):
            if r["game_type"] == "REG" and str(r["gameday"]) == day and r["home_score"] is None:
                out.append({"espn_id": str(r.get("espn") or ""), "home": r["home_team"], "away": r["away_team"], "start": f"{r['gameday']} {r['gametime']}", "book_spread_home": -float(r["spread_line"]) if r["spread_line"] is not None else None,
                            "ml_home": r["home_moneyline"], "ml_away": r["away_moneyline"], "total": r["total_line"], "status": "Scheduled"})
        return out
    out = []
    for g in lineups.espn(sport, day):
        h, a = g["sides"]["home"]["team"], g["sides"]["away"]["team"]
        if sport == "nhl":
            h, a = ESPN_TO_NHL.get(h, h), ESPN_TO_NHL.get(a, a)
        out.append({"espn_id": str(g["game_id"]), "home": h, "away": a, "start": g["start_utc"], "book_spread_home": None if sport == "nhl" else parse_spread(g.get("spread"), g["sides"]["home"]["team"], g["sides"]["away"]["team"]),
                    "book_moneyline_fav": g.get("spread") if sport == "nhl" else None, "total": g.get("total"),
                    "status": g["status"]})
    return out


def board(sport, day):
    cache = cache_dir(sport)
    games = {"nba": tm.games_nba, "nhl": tm.games_nhl, "nfl": tm.games_nfl}[sport](cache)
    games = [g for g in games if g["date"] < day]
    todays = slate(sport, day)
    season = int(day[:4]) if int(day[5:7]) >= 8 else int(day[:4]) - 1
    model = json.load(open(HERE / "models" / f"{sport}_team_v1.json"))
    for i, t in enumerate(todays):
        games.append({"season": season if sport == "nfl" else season + 1 if False else (games[-1]["season"] if games else season), "date": day, "id": f"LIVE{i}", "home": t["home"], "away": t["away"], "hs": 0, "as": 0, "line": None})
    rows, _ = tm.build(games, sport, model["elo"]["k"], model["elo"]["hfa"])
    byid = {r["id"]: r for r in rows}
    mu, sd, w = np.array(model["mu"]), np.array(model["sd"]), np.array(model["weights"])
    out = []
    for i, t in enumerate(todays):
        r = byid.get(f"LIVE{i}")
        if not r:
            continue
        x = np.array([r[c] for c in model["features"]], float)
        em = float(w[0] + ((x - mu) / sd) @ w[1:])
        pw = float(tm.Phi(np.array(em / model["resid_sd"])))
        bs = t.get("book_spread_home")
        out.append({"espn_id": t.get("espn_id"), "home": t["home"], "away": t["away"], "start": t["start"], "status": t.get("status"), "model_spread_home": round(-em, 1), "model_p_home": round(pw, 4),
                    "book_spread_home": bs, "book_total": t.get("total"), "gap_pts": round((-em) - bs, 1) if bs is not None and sport != "nhl" else None,
                    "history_games": int(r["ngames"]), "model_version": model["model_version"]})
    return out


def snapshot(sport, day, out_dir):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / f"shadow_team_{sport}_{day}.json"
    have = json.load(open(path)) if path.exists() else {"date": day, "sport": sport, "games": {}}
    new = 0
    for g in board(sport, day):
        key = f"{g['away']}@{g['home']}"
        if key in have["games"] or g["status"] not in ("Scheduled", None):
            continue
        have["games"][key] = g; new += 1
    path.write_text(json.dumps(have))
    print(f"{day} team {sport}: {new} game(s) saved, {len(have['games'])} in file")


def grade(sport, day, out_dir):
    path = Path(out_dir) / f"shadow_team_{sport}_{day}.json"
    d = json.load(open(path))
    cache = cache_dir(sport)
    res = {f"{g['away']}@{g['home']}": g for g in {"nba": tm.games_nba, "nhl": tm.games_nhl, "nfl": tm.games_nfl}[sport](cache) if g["date"] == day}
    import game_lines as GL
    n = 0
    for k, g in d["games"].items():
        r = res.get(k)
        if r:
            g["home_margin"] = r["hs"] - r["as"]; n += 1
            if g.get("espn_id") and "close_spread_home" not in g:
                oc = GL.open_close_line(sport, g["espn_id"], Path(cache).parent / "lineoc")
                if oc:
                    g["close_spread_home"] = oc.get("close_spread_home"); g["close_ml_home"] = oc.get("close_ml_home"); g["close_ml_away"] = oc.get("close_ml_away")
    path.write_text(json.dumps(d))
    print(f"{day} team {sport}: graded {n} of {len(d['games'])}")


def grade_all(out_dir):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for f in sorted(Path(out_dir).glob("shadow_team_*.json")):
        d = json.load(open(f))
        if d["date"] < today and any(g.get("home_margin") is None for g in d["games"].values()):
            grade(d["sport"], d["date"], out_dir)


def report(out_dir):
    for sport in ("nba", "nhl", "nfl"):
        gs = []
        for f in sorted(Path(out_dir).glob(f"shadow_team_{sport}_*.json")):
            gs += [g for g in json.load(open(f))["games"].values() if g.get("home_margin") is not None]
        if not gs:
            continue
        y = np.array([1.0 if g["home_margin"] > 0 else 0.0 for g in gs]); p = np.array([g["model_p_home"] for g in gs])
        line = f"  {sport.upper()}: {len(gs)} graded games, win log loss {tm.ll(p, y):.4f}, accuracy {100 * np.mean((p > .5) == (y == 1)):.1f}%"
        if sport != "nhl":
            wl = [g for g in gs if g.get("book_spread_home") is not None]
            if wl:
                m = np.array([g["home_margin"] for g in wl]); ms = np.array([-g["model_spread_home"] for g in wl]); bs = np.array([-g["book_spread_home"] for g in wl])
                line += f"; spread error model {np.mean(np.abs(ms - m)):.2f} vs book {np.mean(np.abs(bs - m)):.2f}"
            cl = [g for g in gs if g.get("book_spread_home") is not None and g.get("close_spread_home") is not None and abs(g["model_spread_home"] - g["book_spread_home"]) >= 1.0]
            if cl:
                clv = [(g["book_spread_home"] - g["close_spread_home"]) if g["model_spread_home"] < g["book_spread_home"] else (g["close_spread_home"] - g["book_spread_home"]) for g in cl]
                line += f"; CLV (our line >= 1 pt from the book's when saved): {len(cl)} games, line moved our way {100 * np.mean(np.array(clv) > 0):.0f}% / against {100 * np.mean(np.array(clv) < 0):.0f}%, average {np.mean(clv):+.2f} pts"
        print(line)


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "board":
        rows = board(sys.argv[2], sys.argv[3])
        out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else HERE / "outputs" / f"team_board_{sys.argv[2]}_latest.json"
        out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps({"date": sys.argv[3], "games": rows}, indent=1))
        for r in rows:
            print(f"{r['away']:>4} @ {r['home']:<4} model {r['model_spread_home']:+5.1f} (home win {100 * r['model_p_home']:.0f}%)   book {r['book_spread_home']}   gap {r['gap_pts']}")
    elif c == "snapshot":
        snapshot(sys.argv[2], sys.argv[3], sys.argv[4])
    elif c == "grade":
        grade(sys.argv[2], sys.argv[3], sys.argv[4])
    elif c == "grade-all":
        grade_all(sys.argv[2])
    else:
        report(sys.argv[2])
