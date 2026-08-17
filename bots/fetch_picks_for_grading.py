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
import random
import sys
import time
from pathlib import Path

import requests


# ── A 429 IS NOT A MISSING FILE (2026-08-17) ────────────────────────────────
#
# Run 86867016481 failed with:
#
#     No published slate at .../current/today_slim.json (HTTP 429).
#     ##[error]Process completed with exit code 1.
#
# The file was published. It was fine. I fetched that exact URL by hand while
# reading the log. 429 is raw.githubusercontent.com rate-limiting the runner —
# transient, and the standard response is to wait and ask again.
#
# Two separate faults, and the second is the expensive one:
#
#   1. `if resp.status_code != 200` treated every non-200 identically, so a
#      rate limit exited 1 and killed the job.
#   2. The message ASSERTED A FALSEHOOD. "No published slate" points whoever
#      reads it at the publishing pipeline — a bot that didn't run, a branch
#      that didn't update, a path that changed — none of which was happening.
#      A wrong error message costs more than no error message, because it buys
#      confident investigation of the wrong thing.
#
# So: retry the transient statuses, and never describe a status as absence
# unless it actually is one.
#
#   404 / 410 → genuinely not published. Report as such, do not retry.
#   429       → rate limited. Honour Retry-After when the server sends it.
#   5xx       → server-side wobble. Retry.
#   timeouts / connection resets → retry.
#
# Jittered backoff, because several jobs in this repo hit the same host in the
# same second and a fixed sleep just re-collides them.

TRANSIENT_STATUS = {429, 500, 502, 503, 504, 522, 524}
PERMANENT_ABSENT = {404, 410}


def get_with_retry(url, *, timeout=60, tries=5, base=2.0, label=""):
    """GET a URL, retrying only what is worth retrying.

    Returns the final Response, or None when every attempt raised. The caller
    still decides what a given status means — this only handles *waiting*.
    """
    what = label or url
    last = None
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            last = resp
            if resp.status_code not in TRANSIENT_STATUS:
                return resp
            # Retry-After may be seconds or an HTTP date; only the numeric form
            # is worth trusting here, and it is capped so a hostile or mistaken
            # header cannot park CI for an hour.
            wait = None
            hdr = resp.headers.get("Retry-After", "")
            if hdr.strip().isdigit():
                wait = min(60.0, float(hdr.strip()))
            if wait is None:
                wait = min(60.0, base ** attempt) + random.uniform(0, 1.5)
            if attempt == tries:
                print(f"::warning::{what}: HTTP {resp.status_code} on all {tries} attempts "
                      f"— this is a transient status (rate limit or server error), "
                      f"not a missing file.", file=sys.stderr)
                return resp
            print(f"{what}: HTTP {resp.status_code} (transient) — attempt {attempt}/{tries}, "
                  f"waiting {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
        except Exception as exc:
            last = None
            if attempt == tries:
                print(f"::warning::{what}: {type(exc).__name__} on all {tries} attempts: {exc}",
                      file=sys.stderr)
                return None
            wait = min(60.0, base ** attempt) + random.uniform(0, 1.5)
            print(f"{what}: {type(exc).__name__} ({exc}) — attempt {attempt}/{tries}, "
                  f"waiting {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
    return last


def describe_status(code):
    """Say what a status MEANS, so the log stops accusing the wrong component."""
    if code in PERMANENT_ABSENT:
        return "not published yet"
    if code == 429:
        return "rate limited by raw.githubusercontent.com (the file is fine)"
    if 500 <= code < 600:
        return "GitHub raw is erroring server-side"
    if code in (401, 403):
        return "refused — permissions or a private branch, not absence"
    return f"unexpected HTTP {code}"

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
# CREATE IT HERE, AT IMPORT (2026-08-14). This mkdir used to live at the bottom
# of main(), AFTER the pair_builder write and behind the `slate_cached` early
# return -- so on a fresh CI checkout it had not run yet when the pair-builder
# write fired. bots/outputs/ is gitignored, so the directory genuinely does not
# exist on a runner: pb_dest.write_text() raised FileNotFoundError, the bare
# `except Exception` below swallowed it as "pair_builder fetch skipped", and
# live_results_tracker fell through to its OWN internal pool builder.
#
# That is the "pairs cashed on one side, different pairs on the other" report:
# the Pairs tab renders the published pair_builder_latest.json while the
# Results tab was rendering a completely different set of tickets the grader
# invented from the same top hitters -- same people, different pairings.
OUT_DIR.mkdir(parents=True, exist_ok=True)


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
        # RETRIED TOO (2026-08-17). This is best-effort, but "best-effort" was
        # letting a 429 fall through to the grader inventing its own pools —
        # exactly the divergence the 08-07 note above exists to prevent. A rate
        # limit silently swapping the tickets under Results is worse than a
        # slow job.
        pb = get_with_retry(f"{RAW_BASE}/public/data/current/pair_builder_latest.json",
                            label="pair_builder_latest.json")
        if pb is not None and pb.status_code == 200 and pb.text.strip():
            pb_dest.write_text(pb.text, encoding="utf-8")
            print(f"Fetched pair_builder_latest.json -> {pb_dest}")
        elif pb is None:
            print("::warning::pair_builder_latest.json unreachable after retries; "
                  "grader will use internal pools and Results may disagree with Pairs.",
                  file=sys.stderr)
        else:
            print(f"pair_builder_latest.json: {describe_status(pb.status_code)}; "
                  f"grader will use internal pools.")
    except Exception as exc:
        # NOT silent. If this ever fails again the grader will quietly grade
        # tickets nobody was shown, so it needs to be findable in the log.
        print(f"::warning::pair_builder fetch FAILED ({type(exc).__name__}: {exc}) "
              f"— grader will invent its own pools and the Results tab will "
              f"disagree with the Pairs tab.", file=sys.stderr)

    if slate_cached:
        return 0

    url = f"{RAW_BASE}/public/data/current/{args.label}_slim.json"
    resp = get_with_retry(url, label=f"{args.label}_slim.json")
    if resp is None:
        print(f"Could not reach {url} after retries.", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        # SAY WHAT THE STATUS MEANS, not what it would mean if it were a 404.
        # This line used to read "No published slate at <url> (HTTP 429)" — a
        # confident claim that the file was missing, printed about a file that
        # was sitting there perfectly. See the block at the top of this module.
        print(f"Slate fetch failed — {describe_status(resp.status_code)} "
              f"(HTTP {resp.status_code}) at {url}.", file=sys.stderr)
        if resp.status_code in TRANSIENT_STATUS:
            print("::warning::This is a transient failure. The slate is very likely "
                  "published and fine — re-run the job.", file=sys.stderr)
        return 1

    try:
        rows = resp.json()
    except Exception as exc:
        print(f"Published slate is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(rows, list) or not rows:
        print("Published slate is empty; nothing to grade.", file=sys.stderr)
        return 1

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
