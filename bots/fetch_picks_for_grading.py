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
import os
import sys
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    PHOENIX = ZoneInfo("America/Phoenix")
except Exception:  # pragma: no cover
    PHOENIX = None

# Derive the repo from the Actions environment rather than hardcoding it.
# GITHUB_REPOSITORY is always set by GitHub Actions ("owner/name"), so this
# follows the repo it's actually running in. The previous hardcoded value
# still pointed at the OLD repo after the migration, so every fetch 404'd
# and took the spray-cache and companion jobs down with it.
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip() or "donthebuilder/MLB-HR-DASHBOARD-STREAMLIT"
DATA_BRANCH = os.environ.get("DATA_BRANCH", "data").strip() or "data"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{DATA_BRANCH}"

REPO_ROOT = Path(__file__).resolve().parent.parent
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
    slate_cached = dest.exists() and dest.stat().st_size > 0
    if slate_cached:
        print(f"Picks already present: {dest}")

    # ALIGNMENT FIX (2026-08-07): the grader used to invent its own pair/pool
    # tickets because this fetch never gave it the published ones. Pull
    # pair_builder_latest.json too (best-effort — the grader falls back to its
    # internal builder if this file is missing or for a different date), so
    # live grading runs against the SAME tickets the site displays.
    pb_dest = OUT_DIR / "pair_builder_latest.json"
    try:
        pb = requests.get(f"{RAW_BASE}/public/data/current/pair_builder_latest.json", timeout=60)
        if pb.status_code == 200 and pb.text.strip():
            pb_dest.write_text(pb.text, encoding="utf-8")
            print(f"Fetched pair_builder_latest.json -> {pb_dest}")
        else:
            print(f"No pair_builder_latest published (HTTP {pb.status_code}); grader will use internal pools.")
    except Exception as exc:
        print(f"pair_builder fetch skipped: {exc}")

    if slate_cached:
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
    blob = json.dumps(rows)
    dest.write_text(blob, encoding="utf-8")
    print(f"Wrote {len(rows)} picks to {dest}")

    # Also write the stable path the other slate-consuming bots look for.
    # spray_cache.find_latest_slate() checks public/data/today_slate.json
    # first, and hr_companion_cache.py takes --slate pointing at it. Without
    # this, both died in CI with "No slate file found" because a fresh
    # checkout has no slate anywhere on disk.
    stable = REPO_ROOT / "public" / "data" / "today_slate.json"
    stable.parent.mkdir(parents=True, exist_ok=True)
    stable.write_text(blob, encoding="utf-8")
    print(f"Wrote stable slate copy to {stable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
