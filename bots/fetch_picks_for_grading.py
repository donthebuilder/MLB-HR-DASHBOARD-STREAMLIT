#!/usr/bin/env python3
"""
fetch_picks_for_grading.py — give the grader its input file in CI.

Why this exists
---------------
live_results_tracker.load_rows() only looks in bots/outputs/ for a file named
mlb_breakdown_today_<DATE>.json. That works on your Mac, where the scoring bot
just wrote it. It does NOT work in GitHub Actions: the grading workflow is a
separate run with a fresh checkout, so bots/outputs/ is empty and the grader
died with FileNotFoundError.

This pulls the published slate off the `data` branch and drops it where the
grader expects it. The slim payload is used -- grading only reads scalar
fields (name, player_id, game_pk, pick_type, rank, scores), none of which the
slimming step removes.

Usage:
    python bots/fetch_picks_for_grading.py            # today (Phoenix)
    python bots/fetch_picks_for_grading.py --date yesterday
    python bots/fetch_picks_for_grading.py --date 2026-07-24
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    PHOENIX = ZoneInfo("America/Phoenix")
except Exception:  # pragma: no cover
    PHOENIX = None

GITHUB_REPO = "donthebuilder/MLB-HR-DASHBOARD"
DATA_BRANCH = "data"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{DATA_BRANCH}"

OUT_DIR = Path(__file__).resolve().parent / "outputs"


def phoenix_today() -> dt.date:
    if PHOENIX is not None:
        return dt.datetime.now(PHOENIX).date()
    return dt.date.today()


def resolve(arg: str) -> dt.date:
    arg = (arg or "today").strip().lower()
    if arg in {"today", "auto", "live"}:
        return phoenix_today()
    if arg == "yesterday":
        return phoenix_today() - dt.timedelta(days=1)
    return dt.date.fromisoformat(arg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="today")
    ap.add_argument("--label", default="today", choices=["today", "tomorrow"])
    args = ap.parse_args()

    date = resolve(args.date)
    dest = OUT_DIR / f"mlb_breakdown_today_{date.isoformat()}.json"
    if dest.exists() and dest.stat().st_size > 0:
        print(f"Picks already present: {dest}")
        return 0

    url = f"{RAW_BASE}/public/data/current/{args.label}_slim.json"
    try:
        resp = requests.get(url, timeout=60)
    except Exception as exc:
        print(f"Could not reach {url}: {exc}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"No published slate at {url} (HTTP {resp.status_code}).", file=sys.stderr)
        return 1

    try:
        rows = resp.json()
    except Exception as exc:
        print(f"Published slate is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(rows, list) or not rows:
        print("Published slate is empty; nothing to grade.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows), encoding="utf-8")
    print(f"Wrote {len(rows)} picks to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
