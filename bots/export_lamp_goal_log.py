#!/usr/bin/env python3
"""export_lamp_goal_log.py -- copy the graded NHL goal board into a file the harness can read.

The NHL board's prediction of record is lamp_goal_log in Supabase (one row per game, player and model_version; the last pregame
write is the lock; graded once after the final). The table is the PUBLIC record. By default this asks the SITE for it (GET /api/record/lamp-goal-export, added 2026-10-16), so the bot
repo needs no database secret at all:

    python3 bots/export_lamp_goal_log.py --out public/data/current            # site = https://dashnetwork.vercel.app, or $SITE_URL

If the site is unreachable and SUPABASE_URL + SUPABASE_ANON_KEY are set, it falls back to reading the table directly.

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


def fetch_site(site: str) -> list[dict]:
    req = urllib.request.Request(f"{site.rstrip('/')}/api/record/lamp-goal-export", headers={"User-Agent": "dash-model-harness"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"unexpected answer: {str(data)[:120]}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="public/data/current")
    ap.add_argument("--since", default=None, help="only game dates on or after this (YYYY-MM-DD)")
    a = ap.parse_args()
    site = os.environ.get("SITE_URL", "").strip() or "https://dashnetwork.vercel.app"
    rows = None
    try:
        rows = fetch_site(site)
        print(f"asked {site}: {len(rows)} graded rows")
    except Exception as e:
        print(f"the site export failed ({type(e).__name__}: {e})")
    if rows is None:
        url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_ANON_KEY", "").strip()
        if not url or not key:
            print("no site export and no SUPABASE_URL / SUPABASE_ANON_KEY -- nothing exported (the NHL board stays 'no rows yet')")
            return 0
        rows = fetch(url, key, a.since)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lamp_goal_log_export.json").write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    print(f"exported {len(rows)} graded regular-season goal-board rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
