#!/usr/bin/env python3
"""nfl_game_calls.py -- one TD call per team, every game (BATCH GAME CALLS, G1+G2).

THE RULE (.claude-notes/BATCH-GAME-CALLS-PLAN.md on the site side):

  GAME   every scheduled game on the slate gets an entry.
  TEAM   in each game, the top player on EACH team by the TD score this build
         already carries (nfl_week.json players[].scores.TD). The higher of the
         two is TOP, the game's call; the other is the second call, role "TD".
  FLOOR  the second call exists only when he is ON THE BOARD: rank <=
         ceil(n/3) of the slate's TD board -- the same cut the site's
         lib/callStatus.js tdBoardCut() uses. The board is the site's tdPool:
         RB/WR/TE (the TD market's own `positions`), not on a bye, a finite TD
         score. A side whose best misses the floor gets a no_call row naming
         him and his rank. TOP is never floored.
  TIES   TD score, then xTD per game (stats.xTD), then each TD component in the
         market's weight order, then player_id -- deterministic, and logged on
         the call (tie_break) whenever a tie was actually broken.
  LOCK   the game's kickoff. Every build appends one line per game to
         nfl_game_calls_log_<season>_w<ww>.jsonl, ONLY for games whose kickoff
         is still in the future (and whose ESPN state is still "pre"). Nothing
         pregame is ever written at or after kickoff. Rows are never rewritten.
         The grader reads the last row with built_at < kickoff.
  GRADE  hit = nfl_results lines[pid].TD >= 1, the TD market's own grade; no
         line = VOID (not a miss). A graded game is never regraded.

NO MODEL CHANGE. The score is the TD score of record; nothing is reweighted.
The probability td_game_probability = 1 - exp(-xTD/gm) is LOGGED ONLY, labelled
PROBABILITY_SOURCE. It is not a calibrated model probability and must not be
shown on a page until the calibration gate in the plan is met.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
from pathlib import Path

MARKET = "TD"
BOARD_SHARE = 1 / 3                 # lib/callStatus.js TD_BOARD_SHARE
RULE = "game-calls-v1"              # the selection rule's version (not a model)
DEFAULT_POSITIONS = ["RB", "WR", "TE"]
PROBABILITY_SOURCE = "Poisson on xTD per game; not a calibrated model probability"
PROBABILITY_NOTE = ("td_game_probability is logged only -- not shown on any page "
                    "until the calibration gate (100+ graded calls, every band within "
                    "5 points of its hit rate)")
SECOND_ROLE = "TD"
BANDS = [(1, 10), (11, 25), (26, 50), (51, 100), (101, None)]

# The workflow sets this to "0" when the data branch HAS a game-calls log /
# graded file that did not restore onto disk. Appending to (or grading from) a
# partial copy would publish it over the real one, so those writes stand down.
RESTORE_ENV = "NFL_GAME_CALLS_RESTORED"


def _restored_ok() -> bool:
    return os.environ.get(RESTORE_ENV, "1") != "0"


# ── time ─────────────────────────────────────────────────────────────────────

def parse_ts(s) -> "dt.datetime | None":
    """ISO time -> aware UTC datetime. ESPN's '2026-10-02T00:15Z' included."""
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


# ── the board ────────────────────────────────────────────────────────────────

def _finite(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _num(v, default=float("-inf")) -> float:
    return float(v) if _finite(v) else default


def td_market(payload: dict) -> dict:
    return next((m for m in payload.get("markets") or [] if m.get("key") == MARKET), {}) or {}


def weight_order(market: dict) -> list[str]:
    """TD component keys, heaviest weight first (ties by key, for determinism)."""
    w = market.get("weights") or {}
    return [k for k, _ in sorted(w.items(), key=lambda kv: (-float(kv[1] or 0), kv[0]))]


def sort_key(p: dict, legs: list[str]):
    comps = (p.get("components") or {}).get(MARKET) or {}
    return (
        -_num((p.get("scores") or {}).get(MARKET)),
        -_num((p.get("stats") or {}).get("xTD")),
        *[-_num(comps.get(k)) for k in legs],
        str(p.get("player_id") or ""),
    )


def td_board(payload: dict) -> list[dict]:
    """The site's tdPool(): eligible, not on bye, finite TD score -- in the
    tie-broken order this module ranks by."""
    m = td_market(payload)
    elig = set(m.get("positions") or DEFAULT_POSITIONS)
    legs = weight_order(m)
    rows = [p for p in payload.get("players") or []
            if isinstance(p, dict) and not p.get("on_bye")
            and p.get("position") in elig
            and _finite((p.get("scores") or {}).get(MARKET))]
    return sorted(rows, key=lambda p: sort_key(p, legs))


def board_cut(n: int) -> "int | None":
    """lib/callStatus.js tdBoardCut(): the last rank inside the top third."""
    return math.ceil(n * BOARD_SHARE) if n > 0 else None


def _tie_note(best: dict, runner: "dict | None", legs: list[str]) -> "str | None":
    """Which leg separated two players tied on TD score, or None if no tie."""
    if runner is None:
        return None
    a, b = sort_key(best, legs), sort_key(runner, legs)
    if a[0] != b[0]:
        return None
    names = ["TD score", "xTD/gm", *legs, "player_id"]
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return (f"tied with {runner.get('name')} ({runner.get('player_id')}) on TD score "
                    f"{(best.get('scores') or {}).get(MARKET)}; broken by {names[i]}")
    return None


def _call(p: dict, role: str, rank: int, n: int, tie: "str | None") -> dict:
    xtd = (p.get("stats") or {}).get("xTD")
    xtd = round(float(xtd), 3) if _finite(xtd) else None
    out = {
        "role": role,
        "player_id": p.get("player_id"),
        "name": p.get("name"),
        "team": p.get("team"),
        "position": p.get("position"),
        "score": (p.get("scores") or {}).get(MARKET),
        "slate_rank": rank,
        "of": n,
        "xtd_pg": xtd,
        # LOGGED ONLY -- see PROBABILITY_NOTE. Never printed on a page.
        "td_game_probability": round(1 - math.exp(-xtd), 4) if xtd is not None and xtd >= 0 else None,
        "probability_source": PROBABILITY_SOURCE,
        "questionable": bool(p.get("questionable")),
    }
    if tie:
        out["tie_break"] = tie
    return out


def select_game(game: dict, board: list[dict], legs: list[str]) -> dict:
    """The calls for one game, from the ranked board. Pure."""
    n = len(board)
    cut = board_cut(n)
    rank_of = {id(p): i + 1 for i, p in enumerate(board)}
    sides = []
    for team in (game.get("away"), game.get("home")):
        mine = [p for p in board if p.get("team") == team]
        sides.append((team, mine[0] if mine else None, mine[1] if len(mine) > 1 else None))
    calls, no_call = [], []
    present = [s for s in sides if s[1] is not None]
    present.sort(key=lambda s: sort_key(s[1], legs))
    for i, (team, best, runner) in enumerate(present):
        rank = rank_of[id(best)]
        tie = _tie_note(best, runner, legs)
        if i == 0:
            calls.append(_call(best, "TOP", rank, n, tie))
        elif cut is not None and rank <= cut:
            calls.append(_call(best, SECOND_ROLE, rank, n, tie))
        else:
            no_call.append({
                "team": team, "best_player_id": best.get("player_id"),
                "best_name": best.get("name"), "best_rank": rank, "of": n,
                "reason": "below_floor",
                "text": f"no call this side -- best is {best.get('name')}, #{rank} of {n}",
            })
    for team, best, _ in sides:
        if best is None:
            no_call.append({
                "team": team, "best_player_id": None, "best_name": None, "best_rank": None,
                "of": n, "reason": "no_scored_player",
                "text": "no call this side -- no TD-scored RB/WR/TE on this team in this build",
            })
    return {
        "game_id": game.get("game_id"), "kickoff": game.get("kickoff"),
        "away": game.get("away"), "home": game.get("home"), "state": game.get("state"),
        "calls": calls, "no_call": no_call,
    }


def build_calls(payload: dict) -> dict:
    """Every game's calls for this build, ignoring the lock. Pure."""
    board = td_board(payload)
    legs = weight_order(td_market(payload))
    n = len(board)
    games = [select_game(g, board, legs) for g in payload.get("games") or []
             if isinstance(g, dict) and g.get("game_id")]
    return {"season": payload.get("season"), "week": payload.get("week"),
            "built_at": payload.get("built_at"), "board_n": n, "top_third": board_cut(n),
            "tie_order": ["TD score", "xTD/gm", *legs, "player_id"], "games": games}


# ── the lock log ─────────────────────────────────────────────────────────────

def log_path(out_dir: Path, season, week, prefix: str = "nfl_") -> Path:
    return Path(out_dir) / f"{prefix}game_calls_log_{season}_w{int(week):02d}.jsonl"


def read_log(path: Path) -> list[dict]:
    rows = []
    if not Path(path).exists():
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def pregame(game: dict, built_at: dt.datetime, now: dt.datetime) -> bool:
    """True only while a game may still take a pregame row: kickoff strictly
    after BOTH the build time and the write time, and ESPN still says pre."""
    ko = parse_ts(game.get("kickoff"))
    if ko is None:
        return False
    if game.get("state") not in (None, "pre"):
        return False
    return built_at < ko and now < ko


def locked_rows(rows: list[dict]) -> dict[str, dict]:
    """game_id -> the locked row: the last row with built_at < that game's
    kickoff. The kickoff is the latest one logged for the game (a moved game
    locks at its new time). A row at/after kickoff is never read."""
    latest_ko: dict[str, dt.datetime] = {}
    for r in rows:
        gid, ko, b = str(r.get("game_id")), parse_ts(r.get("kickoff")), parse_ts(r.get("built_at"))
        if ko is None or b is None:
            continue
        if gid not in latest_ko or b >= latest_ko[gid][1]:
            latest_ko[gid] = (ko, b)
    out: dict[str, tuple[dt.datetime, dict]] = {}
    for r in rows:
        gid, b = str(r.get("game_id")), parse_ts(r.get("built_at"))
        if gid not in latest_ko or b is None:
            continue
        ko = latest_ko[gid][0]
        if b >= ko:
            continue
        if gid not in out or b >= out[gid][0]:
            out[gid] = (b, r)
    return {g: r for g, (_, r) in out.items()}


def write(payload: dict, out_dir: Path, prefix: str = "nfl_", run_meta: dict | None = None,
          now: dt.datetime | None = None) -> dict:
    """Write {prefix}game_calls.json and, for the current week only, append
    this build's pregame rows to the lock log. Returns the published doc.

    prefix ending in "next_" is the look-ahead board: mode 'next', no log,
    never graded."""
    out_dir = Path(out_dir)
    now = now or dt.datetime.now(dt.timezone.utc)
    run_meta = run_meta or {}
    is_next = prefix.endswith("next_")
    doc = build_calls(payload)
    doc.update({
        "mode": "next" if is_next else "week",
        "rule": RULE,
        "market": MARKET,
        "model_version": (run_meta.get("model_versions") or {}).get(MARKET),
        "run_id": run_meta.get("run_id"),
        "probability_note": PROBABILITY_NOTE,
    })
    built_at = parse_ts(payload.get("built_at")) or now
    season, week = payload.get("season"), payload.get("week")

    lp = None
    locks: dict[str, dict] = {}
    if not is_next and payload.get("mode") == "week" and isinstance(week, int):
        lp = log_path(out_dir, season, week, prefix)
        locks = locked_rows(read_log(lp))

    # A game that has kicked off shows its LOCKED calls, never this build's
    # re-sort. One with no pregame row says so.
    fresh = []
    for g in doc["games"]:
        if pregame(g, built_at, now):
            g["locked"] = False
            fresh.append(g)
            continue
        if is_next:
            g["locked"] = False
            continue
        lk = locks.get(str(g["game_id"]))
        if lk:
            g.update({"calls": lk.get("calls", []), "no_call": lk.get("no_call", []),
                      "locked": True, "locked_built_at": lk.get("built_at")})
        else:
            g.update({"calls": [], "no_call": [], "locked": True, "locked_built_at": None,
                      "no_lock_reason": "no pregame build was logged before kickoff"})

    if lp is not None and fresh:
        if not _restored_ok():
            print(f"  game calls: {RESTORE_ENV}=0 -- the branch's log did not restore; "
                  f"not appending (would publish a partial log)")
        else:
            head = {"built_at": payload.get("built_at"), "logged_at": now.isoformat(),
                    "run_id": run_meta.get("run_id"), "season": season, "week": week,
                    "rule": RULE, "model_version": doc["model_version"],
                    "board_n": doc["board_n"], "top_third": doc["top_third"]}
            with open(lp, "a", encoding="utf-8") as fh:
                for g in fresh:
                    row = {**head, **{k: v for k, v in g.items() if k != "locked"}}
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            print(f"  game calls: logged {len(fresh)} pregame game(s) to {lp.name}")

    (out_dir / f"{prefix}game_calls.json").write_text(json.dumps(doc, separators=(",", ":")))
    return doc


# ── grading (G2) ─────────────────────────────────────────────────────────────

def graded_path(out_dir: Path, season, week, prefix: str = "nfl_") -> Path:
    return Path(out_dir) / f"{prefix}game_calls_graded_{season}_w{int(week):02d}.json"


def totals_path(out_dir: Path, season, prefix: str = "nfl_") -> Path:
    return Path(out_dir) / f"{prefix}game_calls_totals_{season}.json"


def _void_reason(pid: str, raw_ids: set[str], call: dict) -> str:
    if pid in raw_ids:
        return "recorded a line, but not at a TD-eligible position (RB/WR/TE) in the week's stats"
    why = "no stat line in the week's nflverse stats: did not play (inactive, scratched or DNP)"
    if call.get("questionable"):
        why += "; he was listed questionable at lock"
    return why + "; the source does not say which"


def grade_game(row: dict, lines: dict, raw_ids: set[str]) -> dict:
    """One locked row -> graded game. lines = nfl_results eligible lines."""
    calls, seen = [], set()
    for c in row.get("calls") or []:
        pid = str(c.get("player_id"))
        if pid in seen:        # one player, graded once per game
            continue
        seen.add(pid)
        td = (lines.get(pid) or {}).get(MARKET)
        if td is None:
            calls.append({**c, "result": "void", "td": None,
                          "void_reason": _void_reason(pid, raw_ids, c)})
        else:
            calls.append({**c, "result": "hit" if float(td) >= 1 else "miss", "td": td})
    return {"game_id": row.get("game_id"), "kickoff": row.get("kickoff"),
            "away": row.get("away"), "home": row.get("home"), "status": "graded",
            "locked_built_at": row.get("built_at"), "calls": calls,
            "no_call": row.get("no_call") or []}


def grade_week(log_rows: list[dict], lines: dict, teams_with_lines: set[str],
               raw_ids: set[str], existing: dict | None = None,
               now: dt.datetime | None = None) -> dict:
    """Graded games for one week. A game is gradable once the week's stats
    hold a line for either of its teams; before that it is 'pending'. A game
    already graded in `existing` is kept as it was -- never regraded."""
    now = now or dt.datetime.now(dt.timezone.utc)
    prior = {str(g.get("game_id")): g for g in (existing or {}).get("games") or []
             if g.get("status") == "graded"}
    games = []
    for gid, row in sorted(locked_rows(log_rows).items(), key=lambda kv: (str(kv[1].get("kickoff")), kv[0])):
        if gid in prior:
            games.append(prior[gid])
            continue
        ko = parse_ts(row.get("kickoff"))
        played = bool({row.get("away"), row.get("home")} & teams_with_lines)
        if ko is None or ko > now or not played:
            games.append({"game_id": row.get("game_id"), "kickoff": row.get("kickoff"),
                          "away": row.get("away"), "home": row.get("home"),
                          "status": "pending", "locked_built_at": row.get("built_at"),
                          "calls": row.get("calls") or [], "no_call": row.get("no_call") or []})
            continue
        games.append(grade_game(row, lines, raw_ids))
    return {"games": games, "summary": summarize(games)}


def _band(rank) -> str:
    for lo, hi in BANDS:
        if rank is not None and rank >= lo and (hi is None or rank <= hi):
            return f"{lo}-{hi}" if hi else f"{lo}+"
    return "unranked"


def summarize(games: list[dict]) -> dict:
    s = {"games": 0, "calls": 0, "hits": 0, "voids": 0,
         "top": {"n": 0, "hit": 0, "void": 0}, "second": {"n": 0, "hit": 0, "void": 0},
         "bands": {}}
    for g in games:
        if g.get("status") != "graded":
            continue
        s["games"] += 1
        for c in g.get("calls") or []:
            side = s["top"] if c.get("role") == "TOP" else s["second"]
            band = s["bands"].setdefault(_band(c.get("slate_rank")), {"n": 0, "hit": 0, "void": 0})
            if c.get("result") == "void":
                side["void"] += 1; band["void"] += 1; s["voids"] += 1
                continue
            s["calls"] += 1; side["n"] += 1; band["n"] += 1
            if c.get("result") == "hit":
                s["hits"] += 1; side["hit"] += 1; band["hit"] += 1
    return s


def _merge(a: dict, b: dict) -> dict:
    out = {k: a.get(k, 0) + b.get(k, 0) for k in ("games", "calls", "hits", "voids")}
    for side in ("top", "second"):
        out[side] = {k: a[side][k] + b[side][k] for k in ("n", "hit", "void")}
    out["bands"] = {}
    for src in (a["bands"], b["bands"]):
        for k, v in src.items():
            t = out["bands"].setdefault(k, {"n": 0, "hit": 0, "void": 0})
            for f in ("n", "hit", "void"):
                t[f] += v.get(f, 0)
    return out


def season_totals(out_dir: Path, season, prefix: str = "nfl_", now=None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    tot = summarize([])
    weeks = []
    for f in sorted(glob.glob(str(Path(out_dir) / f"{prefix}game_calls_graded_{season}_w*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        s = d.get("summary") or summarize(d.get("games") or [])
        weeks.append({"week": d.get("week"), **{k: s[k] for k in ("games", "calls", "hits", "voids", "top", "second")}})
        tot = _merge(tot, s)
    return {"season": season, "updated_at": now.isoformat(), "rule": RULE, "market": MARKET,
            "note": "n excludes voids; second = the TD call on the other side, top-third floor",
            **tot, "weeks": weeks}


def run_grading(out_dir: Path, season: int, week: int, lines: dict,
                teams_with_lines: set[str], raw_ids: set[str], prefix: str = "nfl_",
                now: dt.datetime | None = None) -> "Path | None":
    """Called from nfl_results.main(). Writes the week's graded file and the
    season totals. Nothing is written when the week has no lock log, or when
    the branch's copies did not restore (RESTORE_ENV=0)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not _restored_ok():
        print(f"  game calls: {RESTORE_ENV}=0 -- not grading over a partial restore")
        return None
    rows = read_log(log_path(out_dir, season, week, prefix))
    if not rows:
        print(f"  game calls: no lock log for week {week} -- nothing to grade")
        return None
    gp = graded_path(out_dir, season, week, prefix)
    existing = None
    if gp.exists():
        try:
            existing = json.loads(gp.read_text())
        except Exception:
            existing = None
    body = grade_week(rows, lines, teams_with_lines, raw_ids, existing, now)
    doc = {"season": season, "week": week, "rule": RULE, "market": MARKET,
           "graded_at": now.isoformat(),
           "grade": "hit = an anytime TD (lines[pid].TD >= 1); no line = void",
           "lock": "last log row with built_at < kickoff; never regraded", **body}
    gp.write_text(json.dumps(doc, separators=(",", ":")))
    s = body["summary"]
    print(f"  game calls w{int(week):02d}: {s['games']} graded game(s) · {s['calls']} call(s) · "
          f"{s['hits']} hit · TOP {s['top']['hit']}/{s['top']['n']} · "
          f"second {s['second']['hit']}/{s['second']['n']} · {s['voids']} void")
    totals_path(out_dir, season, prefix).write_text(
        json.dumps(season_totals(out_dir, season, prefix, now), separators=(",", ":")))
    return gp


# ── dry run ──────────────────────────────────────────────────────────────────

def _print(doc: dict) -> None:
    print(f"week {doc['week']} · board {doc['board_n']} · top third {doc['top_third']}")
    for g in doc["games"]:
        parts = [f"{c['role']:3} {c['name']} {c['team']} {c['score']} #{c['slate_rank']}"
                 for c in g["calls"]]
        parts += [f"--  {n['text']} ({n['team']})" for n in g["no_call"]]
        print(f"  {g['away']}@{g['home']:4} " + " | ".join(parts))


if __name__ == "__main__":
    import sys
    p = json.loads(Path(sys.argv[1]).read_text())
    _print(build_calls(p))
