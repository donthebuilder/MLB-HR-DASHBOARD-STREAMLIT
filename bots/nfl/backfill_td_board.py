"""Backfill nfl_td_feed.td_board (and a missing gsis_id/position) from the
PREGAME prediction log -- one-off, 2026-09-24 audit.

Why: td_board was computed by buildTdEvent() and dropped before the insert
until 2026-09-20 (site commit that day), and the roster join missed a handful
of scorers outright. Every row from 09-13 .. 09-20 therefore carries
td_board=null, and /called?sport=nfl read that as "not on the board" -- the
public record said the model missed 97% of touchdowns.

Rule (same as nfl_signal_audit.py): a scorer is graded against the LAST
archived nfl_prediction_log run generated before HIS team's kickoff (nflverse
schedule, ET -> UTC). Rank = position among every player with a TD score in
that run, ties by score desc then name -- the same ordering
lib/nfl/tdFeed.js boardRankFor() uses live. Nothing is re-graded that already
has a value; only nulls are filled, and only from a run that predates kickoff.

Usage (from the bot repo root, data branch fetched):
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
    python bots/nfl/backfill_td_board.py --season 2026            # dry run
    python bots/nfl/backfill_td_board.py --season 2026 --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SUFFIX = re.compile(r"^(jr|sr|ii|iii|iv|v)$")


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s).lower()
    parts = s.split()
    while len(parts) > 1 and SUFFIX.match(parts[-1]):
        parts.pop()
    return " ".join(parts)


def kickoffs(season: int) -> dict[tuple[int, str], dt.datetime]:
    import nflreadpy as nfl
    import polars as pl
    s = nfl.load_schedules().filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))
    out = {}
    for r in s.select(["week", "gameday", "gametime", "home_team", "away_team"]).iter_rows(named=True):
        if not r["gameday"] or not r["gametime"]:
            continue
        local = dt.datetime.strptime(f"{r['gameday']} {r['gametime']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        ko = local.astimezone(dt.timezone.utc)
        out[(int(r["week"]), r["home_team"])] = ko
        out[(int(r["week"]), r["away_team"])] = ko
    return out


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def branch_logs(season: int) -> list[str]:
    names = []
    for line in git("ls-tree", "--name-only", "origin/data", "public/data/current/").splitlines():
        n = line.rsplit("/", 1)[-1]
        if n.startswith(f"nfl_prediction_log_{season}-wk") and n.endswith(".jsonl"):
            names.append(n)
    return sorted(names)


def log_meta(name: str) -> tuple[int, dt.datetime]:
    m = re.match(rf"nfl_prediction_log_\d+-wk(\d+)\.(\d{{6}})Z\.", name)
    week = int(m.group(1))
    head = git("show", f"origin/data:public/data/current/{name}").split("\n", 1)[0]
    gen = dt.datetime.fromisoformat(json.loads(head)["generated_at"].replace("Z", "+00:00"))
    return week, gen


def read_log(name: str) -> list[dict]:
    rows = []
    for i, line in enumerate(git("show", f"origin/data:public/data/current/{name}").splitlines()):
        if i == 0 or not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def rest(url: str, key: str, path: str, method: str = "GET", body=None):
    import urllib.request
    req = urllib.request.Request(f"{url}/rest/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing", file=sys.stderr)
        return 2

    ko = kickoffs(a.season)
    logs = branch_logs(a.season)
    meta = {n: log_meta(n) for n in logs}
    rows = rest(url, key, "nfl_td_feed?td_board=is.null&select=day,game_id,td_n,team,opponent,scorer_name,gsis_id,position,text,kind&order=day,game_id,td_n")
    print(f"{len(rows)} rows with td_board null; {len(logs)} archived runs")

    # Rows the site stored with NO scorer: ESPN writes rush TDs with a trailing
    # space and lib/nfl/tdFeed.js's `$`-anchored patterns refused them until
    # 2026-09-24. Same patterns, trimmed, so the row gets its scorer back.
    PATTERNS = [
        ("pass", re.compile(r"^(.+?) (\d+) Yd pass from (.+?)(?:\s*\((.+?)\))?$")),
        ("rush", re.compile(r"^(.+?) (\d+) Yd (?:Rush|Run)(?:\s*\((.+?)\))?$")),
        ("interception return", re.compile(r"^(.+?) (\d+) Yd Interception Return(?:\s*\((.+?)\))?$")),
        ("fumble return", re.compile(r"^(.+?) (\d+) Yd Fumble Return(?:\s*\((.+?)\))?$")),
        ("punt return", re.compile(r"^(.+?) (\d+) Yd Punt Return(?:\s*\((.+?)\))?$")),
        ("kickoff return", re.compile(r"^(.+?) (\d+) Yd Kickoff Return(?:\s*\((.+?)\))?$")),
        ("fumble return", re.compile(r"^(.+?) Fumble Recovery in End Zone(?:\s*\((.+?)\))?$")),
    ]
    KIND_WORD = {"pass": "PASS TD", "rush": "RUSH TD", "interception return": "PICK-6",
                 "fumble return": "FUMBLE RETURN TD", "punt return": "PUNT RETURN TD", "kickoff return": "KICK RETURN TD"}
    for r in rows:
        if r.get("scorer_name") or not r.get("text"):
            continue
        t = r["text"].strip()
        for kind, rx in PATTERNS:
            m = rx.match(t)
            if not m:
                continue
            r["scorer_name"], r["kind"] = m.group(1), kind
            r["_parsed"] = {"scorer_name": m.group(1), "kind": kind, "kind_word": KIND_WORD[kind]}
            if kind == "pass":
                r["_parsed"]["passer_name"] = m.group(3)
            if kind != "fumble return" or "Yd" in t:
                r["_parsed"]["yards"] = int(m.group(2))
            print(f"  parse {r['day']} {r['game_id']}#{r['td_n']}: '{t}' -> {kind} / {m.group(1)}")
            if a.apply:
                rest(url, key, f"nfl_td_feed?day=eq.{r['day']}&game_id=eq.{r['game_id']}&td_n=eq.{r['td_n']}", "PATCH", r["_parsed"])
            break

    cache: dict[str, tuple[list[dict], dict]] = {}
    filled = skipped = 0
    for r in rows:
        team, day = r.get("team"), r.get("day")
        if not team or not r.get("scorer_name"):
            skipped += 1
            print(f"  skip {day} {r['game_id']}#{r['td_n']}: no team/scorer ({r.get('scorer_name')})")
            continue
        # his team's game whose kickoff lands on this ET day (+1 for late games)
        game = None
        for (week, t), k in ko.items():
            if t != team:
                continue
            kd = k.astimezone(ET).date().isoformat()
            if kd == day or (dt.date.fromisoformat(day) - dt.date.fromisoformat(kd)).days == 1:
                game = (week, k)
        if not game:
            skipped += 1
            print(f"  skip {day} {team} {r['scorer_name']}: no scheduled game on that day")
            continue
        week, k = game
        cands = [n for n in logs if meta[n][0] == week and meta[n][1] < k]
        if not cands:
            skipped += 1
            print(f"  skip {day} {team} {r['scorer_name']}: no pregame run before {k.isoformat()}")
            continue
        run = cands[-1]
        if run not in cache:
            td = [x for x in read_log(run) if x.get("market") == "TD" and isinstance(x.get("score"), (int, float))]
            td.sort(key=lambda x: (-x["score"], x.get("player") or ""))
            by_id = {x["player_id"]: (i + 1, x) for i, x in enumerate(td)}
            cache[run] = (td, by_id)
        td, by_id = cache[run]
        hit = by_id.get(r.get("gsis_id")) if r.get("gsis_id") else None
        if not hit:
            nm = norm_name(r["scorer_name"])
            same = [(i + 1, x) for i, x in enumerate(td) if x.get("team") == team and norm_name(x.get("player", "")) == nm]
            hit = same[0] if len(same) == 1 else None
        if not hit:
            skipped += 1
            print(f"  off  {day} {team} {r['scorer_name']}: not in pregame run {run} (stays null = not on the board)")
            continue
        rank, x = hit
        patch = {"td_board": {"rank": rank, "of": len(td), "score": round(x["score"])}}
        if not r.get("gsis_id"):
            patch["gsis_id"] = x["player_id"]
        if not r.get("position") and x.get("position"):
            patch["position"] = x["position"]
        print(f"  fill {day} {team} {r['scorer_name']}: #{rank} of {len(td)} ({x['score']}) from {run}{' +gsis' if 'gsis_id' in patch else ''}")
        filled += 1
        if a.apply:
            rest(url, key, f"nfl_td_feed?day=eq.{day}&game_id=eq.{r['game_id']}&td_n=eq.{r['td_n']}", "PATCH", patch)
    print(f"{'APPLIED' if a.apply else 'DRY RUN'}: {filled} filled, {skipped} left null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
