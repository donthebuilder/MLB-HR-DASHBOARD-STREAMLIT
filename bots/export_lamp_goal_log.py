#!/usr/bin/env python3
"""export_lamp_goal_log.py -- copy the graded NHL goal board into a file the harness can read.

The NHL board's prediction of record is lamp_goal_log in Supabase (one row per game, player and model_version; the last pregame
write is the lock; graded once after the final). The table is the PUBLIC record -- the site reads it with the anon key -- so this
needs only the project URL and that anon key, nothing private:

    SUPABASE_URL=https://xxxx.supabase.co SUPABASE_ANON_KEY=... python3 bots/export_lamp_goal_log.py --out public/data/current

Writes lamp_goal_log_export.json: a list of {game_id, player_id, model_version, game_date, score, pct, legs, dressed, goals, hit}
for graded rows only. Read-only: nothing in Supabase is touched. bots/model_harness.py (nhl_goal_rows) is the only reader.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

COLS = "game_id,player_id,model_version,game_date,game_type,score,pct,legs,dressed,goals,hit"
PAGE = 1000


def fetch(url: str, key: str, since: str | None = None) -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        q = {"select": COLS, "hit": "not.is.null", "game_type": "eq.2", "order": "game_date.asc,game_id.asc,player_id.asc"}
        if since:
            q["game_date"] = f"gte.{since}"
        req = urllib.request.Request(
            f"{url.rstrip('/')}/rest/v1/lamp_goal_log?{urllib.parse.urlencode(q)}",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Range-Unit": "items", "Range": f"{start}-{start + PAGE - 1}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.loads(r.read().decode("utf-8"))
        out.extend(page)
        if len(page) < PAGE:
            return out
        start += PAGE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="public/data/current")
    ap.add_argument("--since", default=None, help="only game dates on or after this (YYYY-MM-DD)")
    a = ap.parse_args()
    url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_ANON_KEY not set -- nothing exported (the NHL board stays 'no rows yet')")
        return 0
    rows = fetch(url, key, a.since)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lamp_goal_log_export.json").write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    print(f"exported {len(rows)} graded regular-season goal-board rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
