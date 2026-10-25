#!/usr/bin/env python3
"""
model_harness.py -- ONE SCOREBOARD FOR EVERY SCORE, ON THE SAME GAMES.

Why this exists (2026-10-16, Donovan: "we need a harness for the model so we can best upgrade and
utilise it"): the repo had six one-off evaluators (eval_report, score_shootout, slate_eval,
precision_study, backtest_all, eval_shadow_picks), each answering its own question on its own
sample, and nothing that put a production score, its shadows and a plain baseline side by side. On the
locked MLB nights (09-09 to 10-15) the production HR score, three shadows and a one-number baseline
(season power) all landed within a couple of points of each other, and the baseline was not behind.
That is a finding a harness should print every night, not something found by hand.

WHAT IT DOES
  * takes ROWS -- one per (night, game, player) with the pregame scores that were locked before the
    game, a plain baseline stat, and the outcome (1/0) -- from a sport adapter;
  * for every score (production, shadows, overlays) and the baseline, on the SAME rows, measures
        - the top-N of the night hit rate
        - the best-in-each-game hit rate
        - against the board's own hit rate (every player)
  * compares each candidate to the PRODUCTION score and to the BASELINE with a paired test (the two
    picks sit on the same game, so only the games where they disagree carry information)
  * gives a plain verdict -- the promotion rule is Donovan's (2026-10-16): a new score replaces the one
    in use only when it beats it by PROMOTE_POINTS over at least MIN_GAMES games, same games, and the
    paired test agrees.

WHAT IT NEVER DOES
  * It never changes a score, a weight or a file the site reads. It writes model_scoreboard.json and
    prints. Production is frozen for 4 weeks (Donovan, 2026-10-16); ideas run as shadows beside it.
  * It never compares across versions on different games. A row's version rides along and the board is
    split by it; a comparison is only drawn between scores on the same rows.

Run:  python3 bots/model_harness.py --data public/data/current --out public/data/current
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
from pathlib import Path
from typing import Callable

TOP_N = 15            # the top N of a night: the board ranking is graded on its top 15 (Donovan, 2026-10-16)
MIN_GAMES = 150       # promotion needs at least this many games
PROMOTE_POINTS = 3.0  # ...and this many percentage points over the score in use
Z_NEEDED = 1.64       # one-sided 95% on the paired test


# ── small maths ──────────────────────────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a rate, as percentages. (0, 0) for n == 0."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1))


def paired(a_hits: list[int], b_hits: list[int]) -> dict:
    """Two picks on the same games. Only games where they differ carry information.
    z > 0 means A is ahead. Exact on counts, normal approximation on the difference."""
    n = len(a_hits)
    only_a = sum(1 for x, y in zip(a_hits, b_hits) if x == 1 and y == 0)
    only_b = sum(1 for x, y in zip(a_hits, b_hits) if x == 0 and y == 1)
    disc = only_a + only_b
    z = (only_a - only_b) / math.sqrt(disc) if disc else 0.0
    return {"games": n, "only_a": only_a, "only_b": only_b, "diff_pts": round(100 * (only_a - only_b) / n, 1) if n else 0.0, "z": round(z, 2)}


# ── the core: rows in, scoreboard out ────────────────────────────────────────
def _picks_by_game(rows: list[dict], key: str) -> dict[str, dict]:
    """The best-in-game row for a score (highest value; ties by player id so it is deterministic)."""
    best: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: str(r["id"])):     # a tie goes to the lower id, the same way every run
        v = r["s"].get(key)
        if not isinstance(v, (int, float)):
            continue
        cur = best.get(r["game"])
        if cur is None or v > cur["s"][key]:
            best[r["game"]] = r
    return best


def _top_by_night(rows: list[dict], key: str, n: int) -> list[dict]:
    by_night: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if isinstance(r["s"].get(key), (int, float)):
            by_night[r["night"]].append(r)
    out: list[dict] = []
    for night_rows in by_night.values():
        night_rows.sort(key=lambda r: -r["s"][key])
        out.extend(night_rows[:n])
    return out


def evaluate(rows: list[dict], scores: list[str], production: str, baseline: str,
             top_n: int = TOP_N, min_games: int = MIN_GAMES, promote_points: float = PROMOTE_POINTS) -> dict:
    """rows: [{night, game, id, y (0/1), s: {score_key: number}}]. Returns the scoreboard for one market."""
    rows = [r for r in rows if r.get("y") in (0, 1)]
    base_n = len(rows)
    base_k = sum(r["y"] for r in rows)
    games = sorted({r["game"] for r in rows})
    nights = sorted({r["night"] for r in rows})
    # the games every score can pick in (so every comparison is on identical games)
    common = set(games)
    picks = {k: _picks_by_game(rows, k) for k in scores + [baseline]}
    # a CANDIDATE with no rows at all yet (a shadow that has not run: e.g. the NHL composite before its first night) must not
    # empty the common set and blank every comparison: it is reported as 'no rows yet' and left out of the intersection
    empty = {k for k in picks if not picks[k] and k not in (production, baseline)}
    for k in picks:
        if k not in empty:
            common &= set(picks[k])
    games_common = sorted(common)

    table = []
    for k in scores + ([baseline] if baseline not in scores else []):
        if k in empty:
            table.append({"score": k, "role": "candidate", "best_in_game": {"hit": 0, "n": 0, "pct": None, "ci": (0.0, 0.0)},
                          "top_of_night": {"hit": 0, "n": 0, "pct": None, "ci": (0.0, 0.0)}, "verdict": "no rows yet"})
            continue
        pg = picks[k]
        pg_hits = [pg[g]["y"] for g in games_common]
        top = _top_by_night(rows, k, top_n)
        tk, tn = sum(r["y"] for r in top), len(top)
        row = {
            "score": k, "role": "production" if k == production else "baseline" if k == baseline else "candidate",
            "best_in_game": {"hit": sum(pg_hits), "n": len(pg_hits), "pct": round(100 * sum(pg_hits) / len(pg_hits), 1) if pg_hits else None, "ci": wilson(sum(pg_hits), len(pg_hits))},
            "top_of_night": {"hit": tk, "n": tn, "pct": round(100 * tk / tn, 1) if tn else None, "ci": wilson(tk, tn)},
        }
        if k != production and production in picks:
            row["vs_production"] = paired(pg_hits, [picks[production][g]["y"] for g in games_common])
        if k != baseline:
            row["vs_baseline"] = paired(pg_hits, [picks[baseline][g]["y"] for g in games_common])
        row["verdict"] = verdict(row, len(games_common), min_games, promote_points)
        table.append(row)
    return {
        "board": {"hit": base_k, "n": base_n, "pct": round(100 * base_k / base_n, 1) if base_n else None},
        "nights": len(nights), "from": nights[0] if nights else None, "to": nights[-1] if nights else None,
        "games_compared": len(games_common), "top_n": top_n,
        "rule": f"a score replaces the one in use only with +{promote_points:g} points over it, on at least {min_games} games, same games, paired z >= {Z_NEEDED}",
        "scores": table,
    }


def verdict(row: dict, games: int, min_games: int, promote_points: float) -> str:
    """One plain sentence per score. The promotion rule is applied to candidates vs the PRODUCTION score."""
    if row["role"] == "production":
        return "the score in use"
    if row["role"] == "baseline":
        return "plain baseline: the bar every score has to clear"
    vp, vb = row.get("vs_production") or {}, row.get("vs_baseline") or {}
    if games < min_games:
        return f"not enough games yet ({games} of {min_games})"
    beats = lambda v: v.get("diff_pts", 0) >= promote_points and v.get("z", 0) >= Z_NEEDED
    behind = lambda v: v.get("diff_pts", 0) <= -promote_points and v.get("z", 0) <= -Z_NEEDED
    # A score is only promoted when it beats the one in use AND is not left behind by the plain baseline:
    # replacing one thing that cannot beat chalk with another that cannot is not an upgrade.
    if beats(vp) and not behind(vb):
        return "BEATS production" + (" and the baseline: promote" if beats(vb) else ", but not clear of the baseline: hold")
    if behind(vp):
        return "behind production"
    if behind(vb):
        return "behind the plain baseline"
    return "no clear difference from production"


# ── MLB adapter: por_rows (locked pregame) + outcome_log, joined on game and player ───
MLB_HR = {
    "market": "mlb_hr", "label": "MLB home run (1+ HR)", "production": "hr", "baseline": "season_power",
    "scores": ["hr", "overall", "top_board", "hrw", "hr_shadow", "best_blend", "alt_hr", "season_power"],
}


def _score_of(row: dict, key: str):
    s, c, comp = row.get("scores") or {}, row.get("candidate") or {}, row.get("components") or {}
    return {
        "hr": s.get("hr"), "overall": s.get("overall"), "top_board": s.get("top_board"), "hrw": s.get("hrw"),
        "hr_shadow": c.get("hr_score_shadow"), "best_blend": c.get("best_blend_score"), "alt_hr": c.get("alt_hr_score"),
        "season_power": comp.get("season_power"),
    }.get(key)


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def mlb_hr_rows(data_dir: Path, market: dict = MLB_HR) -> list[dict]:
    """One row per locked hitter-game: only rows the lock kept (por_rows), graded from the FINAL outcome."""
    rows: list[dict] = []
    for por in sorted(data_dir.glob("por_rows_*.jsonl")):
        m = re.search(r"(\d{4}-\d\d-\d\d)", por.name)
        if not m:
            continue
        night = m.group(1)
        out: dict[tuple[str, str], dict] = {}
        for o in read_jsonl(data_dir / f"outcome_log_{night}.jsonl"):
            if o.get("void") or not o.get("is_final"):
                continue
            k = (str(o.get("game_pk")), str(o.get("player_id")))
            if k not in out or (o.get("revision") or 0) >= (out[k].get("revision") or 0):
                out[k] = o
        for r in read_jsonl(por):
            o = out.get((str(r.get("game_pk")), str(r.get("player_id"))))
            if not o:
                continue
            rows.append({
                "night": night, "game": str(r["game_pk"]), "id": str(r["player_id"]),
                "y": 1 if (o.get("home_runs") or 0) > 0 else 0,
                "version": (r.get("model_versions") or {}).get("hr"),
                "s": {k: _score_of(r, k) for k in market["scores"]},
            })
    return rows


# ── NFL adapter: the anytime-TD board ────────────────────────────────────────
# nfl_por_log_<season>_wNN.jsonl says, per game, WHICH pregame run was locked for it (and whether it locked late);
# that run's nfl_prediction_log_<run_id>.jsonl holds the score and its components for every player on the board; the graded
# week (nfl_results_<season>_wNN.json `lines`) holds what each player actually did. A game that locked late is not a
# pregame record and is left out. A player with no graded TD line (not eligible, did not play) is left out, not counted a miss.
NFL_TD = {
    "market": "nfl_td", "label": "NFL anytime TD (1+ TD)", "production": "td", "baseline": "f_touches",
    # td = the score in use; td_v3 = the shadow beside it; the rest are the board's own components, each a one-number
    # rule the harness grades like any score. f_touches (workload) is the plain baseline. implied_total is a TEAM number
    # (the same for every player in a game), so it cannot pick a player and is not graded here.
    "scores": ["td", "td_v3", "td_fit", "f_xtd", "f_touches", "f_snap_pct", "f_rz_opp", "f_gl_opp"],
}


def _nfl_score(row: dict, key: str):
    comp = row.get("components") or {}
    if key == "td":
        return row.get("score")
    if key == "td_v3":
        return ((row.get("shadow") or {}).get("nfl_td_v3") or {}).get("score")
    if key == "td_fit":              # the fitted, calibrated probability (nfl_td_fit_v1), attached to the shadow entry by nfl_bot.py
        for v in (row.get("shadow") or {}).values():
            if isinstance(v, dict) and v.get("p_td_fit") is not None:
                return v["p_td_fit"]
        return None
    return comp.get(key)


def nfl_td_rows(data_dir: Path, market: dict = NFL_TD) -> list[dict]:
    rows: list[dict] = []
    for por in sorted(data_dir.glob("nfl_por_log_*_w*.jsonl")):
        games = read_jsonl(por)
        if not games:
            continue
        season, week = games[0].get("season"), games[0].get("week")
        res_path = data_dir / f"nfl_results_{season}_w{int(week):02d}.json"
        if not res_path.exists():
            continue
        lines = (json.loads(res_path.read_text(encoding="utf-8")) or {}).get("lines") or {}
        night = f"{season}-w{int(week):02d}"
        td_rows_by_run: dict[str, list[dict]] = {}
        for g in games:
            if g.get("locked_late"):
                continue
            run = g.get("run_id")
            if run not in td_rows_by_run:
                td_rows_by_run[run] = [r for r in read_jsonl(data_dir / f"nfl_prediction_log_{run}.jsonl") if r.get("market") == "TD"]
            for r in td_rows_by_run[run]:
                if r.get("team") not in (g.get("home"), g.get("away")):
                    continue
                td = (lines.get(str(r.get("player_id"))) or {}).get("TD")
                if td is None:
                    continue
                rows.append({
                    "night": night, "game": str(g["game_id"]), "id": str(r["player_id"]),
                    "y": 1 if td >= 1 else 0,
                    "version": (g.get("model_versions") or {}).get("TD"),
                    "s": {k: _nfl_score(r, k) for k in market["scores"]},
                })
    return rows


# ── NHL adapter: the goal board (lamp_goal_log) ───────────────────────────────
# The NHL board is scored on the site and its prediction of record lives in Supabase (lamp_goal_log: one row per game,
# player, model_version, written until puck drop and never after; graded once). bots/export_lamp_goal_log.py copies the graded
# rows into lamp_goal_log_export.json next to the other data files; this adapter only reads that file. No file, no rows.
NHL_GOAL = {
    "market": "nhl_goal", "label": "NHL goal (1+ goal)", "production": "score", "baseline": "shots_pg",
    # score = the LAMP score in use; the three percentiles are its own legs, each graded alone; shots_pg (raw shots per
    # game) is the plain baseline every skater's score has to beat.
    "scores": ["score", "comp", "comp_v1", "pct_shots", "pct_goals", "pct_toi", "goals_pg", "shots_pg"],
}


def nhl_goal_rows(data_dir: Path, market: dict = NHL_GOAL) -> list[dict]:
    path = data_dir / "lamp_goal_log_export.json"
    if not path.exists():
        return []
    rows: list[dict] = []
    for r in json.loads(path.read_text(encoding="utf-8")) or []:
        if r.get("dressed") is not True or r.get("hit") is None:      # not graded, or he did not dress: not a miss
            continue
        legs, pct = r.get("legs") or {}, r.get("pct") or {}
        rows.append({
            "night": str(r["game_date"]), "game": str(r["game_id"]), "id": str(r["player_id"]),
            "y": 1 if r["hit"] else 0, "version": r.get("model_version"),
            "s": {"score": r.get("score"), "comp": r.get("comp"), "comp_v1": r.get("comp_v1"), "pct_shots": pct.get("shotsPg"), "pct_goals": pct.get("goalsPg"), "pct_toi": pct.get("toi"),
                  "goals_pg": legs.get("goalsPg"), "shots_pg": legs.get("shotsPg")},
        })
    return rows


# ── SHADOW-LOG adapters (bots/shadow_log.py, bots/nfl/nfl_shadow.py): the fitted lane probabilities, saved before the game and graded after ─────────────
# Files live under <data>/shadow_log/ (the bbe-history branch). There is no "production" score for these lanes, so the plain stat (his season rate) is both the
# production slot and the baseline: the fitted probability is a candidate that must beat the plain stat by PROMOTE_POINTS over MIN_GAMES, same games, to be
# called better. Players who did not play are not graded (a missing result is not a miss).
SHADOW_LANES = {
    "nhl_goal_fit": ("nhl", "goal", "NHL goal (fitted lane)"), "nhl_points": ("nhl", "points", "NHL points 1+ (fitted lane)"),
    "nhl_assists": ("nhl", "assists", "NHL assists 1+ (fitted lane)"), "nhl_shots3": ("nhl", "shots3", "NHL shots 3+ (fitted lane)"),
    "nba_pts25": ("nba", "pts25", "NBA points 25+ (fitted lane)"), "nba_reb10": ("nba", "reb10", "NBA rebounds 10+ (fitted lane)"),
    "nba_ast8": ("nba", "ast8", "NBA assists 8+ (fitted lane)"), "nba_tpm4": ("nba", "tpm4", "NBA 3PM 4+ (fitted lane)"),
    "nba_pra35": ("nba", "pra35", "NBA PRA 35+ (fitted lane)"), "nba_dd": ("nba", "dd", "NBA double-double (fitted lane)"),
    "mlb_hit": ("mlb", "hit", "MLB 1+ hit (fitted lane)"), "mlb_hrr": ("mlb", "hrr", "MLB 2+ H+R+RBI (fitted lane)"), "mlb_tb2": ("mlb", "tb2", "MLB 2+ total bases (fitted lane)"), "mlb_hr": ("mlb", "hr", "MLB 1+ HR (fitted lane)"),
}


def shadow_lane_rows(data_dir: Path, sport: str, key: str) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((data_dir / "shadow_log").glob(f"shadow_{sport}_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for r in d.get("rows", []):
            a = r.get("actual")
            if not a or key not in a or key not in (r.get("p") or {}):
                continue
            rows.append({"night": d["date"], "game": f"{d['date']}|" + "|".join(sorted([str(r["team"]), str(r["opp"])])), "id": str(r["name"]), "y": int(a[key]),
                         "version": "fitted_lane_v1", "s": {"fit": r["p"][key], "plain": (r.get("b") or {}).get(key)}})
    return rows


def nba_first_rows(data_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((data_dir / "shadow_log").glob("shadow_nbafirst_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for gid, g in d.get("games", {}).items():
            if not g.get("graded"):
                continue
            for r in g["rows"]:
                if "y" in r:
                    rows.append({"night": d["date"], "game": str(gid), "id": str(r["name"]), "y": int(r["y"]), "version": "nba_first_v1", "s": {"fit": r["p_first_basket"], "plain": r.get("ppg")}})
    return rows


ADAPTERS: dict[str, Callable[[Path], list[dict]]] = {"mlb_hr": mlb_hr_rows, "nfl_td": nfl_td_rows, "nhl_goal": nhl_goal_rows, "nba_first": nba_first_rows}
MARKETS = {"mlb_hr": MLB_HR, "nfl_td": NFL_TD, "nhl_goal": NHL_GOAL,
           "nba_first": {"market": "nba_first", "label": "NBA first basket (fitted lane, starters)", "production": "plain", "baseline": "plain", "scores": ["fit", "plain"]}}
for _k, (_sp, _key, _label) in SHADOW_LANES.items():
    ADAPTERS[_k] = (lambda d, _sp=_sp, _key=_key: shadow_lane_rows(d, _sp, _key))
    MARKETS[_k] = {"market": _k, "label": _label, "production": "plain", "baseline": "plain", "scores": ["fit", "plain"]}


def build(data_dir: Path) -> dict:
    boards = {}
    for key, market in MARKETS.items():
        rows = ADAPTERS[key](data_dir)
        if not rows:
            boards[key] = {"label": market["label"], "nights": 0, "games_compared": 0, "board": {"hit": 0, "n": 0, "pct": None}, "scores": [],
                           "note": "no locked, graded rows for this market yet"}
            continue
        by_ver = collections.Counter(r.get("version") for r in rows)
        board = evaluate(rows, market["scores"], market["production"], market["baseline"])
        board.update({"label": market["label"], "versions_in_sample": dict(by_ver)})
        boards[key] = board
    return {"generated_by": "bots/model_harness.py", "boards": boards}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="public/data/current")
    ap.add_argument("--out", default=None, help="write model_scoreboard.json here (default: print only)")
    a = ap.parse_args()
    result = build(Path(a.data))
    for key, b in result["boards"].items():
        if not b["scores"]:
            print(f"\n{b['label']}  ·  {b.get('note', 'no rows')}")
            continue
        print(f"\n{b['label']}  ·  {b['nights']} nights, {b['games_compared']} games  ·  every player {b['board']['pct']}%")
        print(f"  {'score':14}{'role':11}{'best in game':>16}{'top 15 of night':>20}   vs production        vs baseline")
        for r in b["scores"]:
            bg, tn = r["best_in_game"], r["top_of_night"]
            vp = r.get("vs_production"); vb = r.get("vs_baseline")
            fmt = lambda v: "-" if not v else "{:+.1f} z={:+.2f}".format(v["diff_pts"], v["z"])
            best = "{}% ({}/{})".format(bg["pct"], bg["hit"], bg["n"])
            top = "{}% ({}/{})".format(tn["pct"], tn["hit"], tn["n"])
            print("  {:14}{:11}{:>16}{:>20}   {:20} {:16} {}".format(r["score"], r["role"], best, top, fmt(vp), fmt(vb), r["verdict"]))
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model_scoreboard.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        print(f"\nwrote {out / 'model_scoreboard.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
