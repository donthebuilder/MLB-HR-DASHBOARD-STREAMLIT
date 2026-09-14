#!/usr/bin/env python3
"""Silent-skip alarm: is the published board older than its own cadence?

WHY THIS EXISTS (2026-09-14). GitHub Actions drops scheduled runs silently
under load -- no failure, no email, nothing red. It has happened to nfl.yml
and to today.yml both. It stopped being cosmetic the day the prediction of
record became the last run written before first pitch
(claude/record-is-leaked-2026-09-14.md): a skipped pregame slot is now a
stale record, graded against a lineup that is two hours out of date. On
2026-09-13 today.yml's 16:00-18:00 UTC half-hourly slots were skipped and
the record for those games fell back 39-169 minutes.

WHAT IT CHECKS -- the OUTPUT, not GitHub's run history. The run history is
exactly the thing that lies when a slot is skipped, so this reads the
generated_at stamped on the published meta files on the data branch and asks
one question: within the hours this board is supposed to be refreshing, is
the freshest file older than the gap a skipped slot would open?

  MLB  today_run_meta.json  generated_at   active 12:00-07:59 UTC   > 120 min
  NFL  nfl_meta.json        run_meta.generated_at  game days only   > 240 min

Both thresholds and windows are env-overridable so they can be tuned without
a code change. One Discord line per stale board; silent when everything is
fresh or the board is out of window (nothing scheduled = nothing to miss).

HONEST LIMIT, stated rather than hidden: this workflow is itself a GitHub
cron and can itself be skipped. Two independent schedules being dropped in
the same window is far less likely than one, so this catches the common
case; it is not a watchdog that can watch itself. A truly external heartbeat
(an uptime pinger hitting a route) is the next rung if this proves not
enough -- noted, not built.

Exit code is always 0: a monitor that fails its own run and goes red is just
one more red X to learn to ignore. It speaks on Discord or says nothing.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(v):
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def in_window(now: dt.datetime, start_h: int, end_h: int) -> bool:
    """Is `now` (UTC) within [start_h, end_h) hours, wrap-around aware?"""
    h = now.hour
    if start_h <= end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h   # e.g. 12 -> 8 next day


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def newest_prediction_log(current: Path, prefix: str) -> dt.datetime | None:
    """Fallback freshness for MLB/NFL: the newest generated_at across the
    prediction_log headers on disk, when the meta file is missing."""
    best = None
    for p in current.glob(f"{prefix}prediction_log_*.jsonl"):
        try:
            with p.open(encoding="utf-8") as fh:
                first = fh.readline().strip()
            hdr = json.loads(first) if first else {}
        except Exception:
            continue
        g = parse_ts(hdr.get("generated_at"))
        if g and (best is None or g > best):
            best = g
    return best


def mlb_age(current: Path):
    meta = read_json(current / "today_run_meta.json")
    g = parse_ts(meta.get("generated_at")) if isinstance(meta, dict) else None
    if g is None:
        g = newest_prediction_log(current, "")   # prediction_log_*, not nfl_
    return g


def nfl_age(current: Path):
    meta = read_json(current / "nfl_meta.json")
    if isinstance(meta, dict):
        rm = meta.get("run_meta") or {}
        g = parse_ts(rm.get("generated_at")) or parse_ts(meta.get("built_at"))
        if g:
            return g
    return newest_prediction_log(current, "nfl_")


def post_discord(lines: list[str]) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK", "")
    if not hook or not lines:
        return
    body = json.dumps({"content": "\n".join(lines)[:1900]}).encode()
    for url in [u.strip() for u in hook.split(",") if u.strip()]:
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"  ! discord post failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="public/data/current",
                    help="the data-branch current/ directory to read timestamps from")
    ap.add_argument("--post", action="store_true", help="post to Discord (else print only)")
    args = ap.parse_args()
    current = Path(args.dir)
    now = now_utc()

    # (name, age_fn, window_start_h, window_end_h, threshold_min, game_days_only)
    checks = [
        ("MLB board (today.yml)", mlb_age,
         env_int("MLB_WINDOW_START", 12), env_int("MLB_WINDOW_END", 8),
         env_int("MLB_STALE_MIN", 120), None),
        ("NFL board (nfl.yml)", nfl_age,
         env_int("NFL_WINDOW_START", 13), env_int("NFL_WINDOW_END", 6),
         env_int("NFL_STALE_MIN", 240), {3, 6, 0}),  # Thu, Sun, Mon (Mon=0)
    ]

    alerts, report = [], []
    for name, age_fn, ws, we, thresh, game_days in checks:
        if game_days is not None and now.weekday() not in game_days:
            report.append(f"  {name}: off day, not checked")
            continue
        if not in_window(now, ws, we):
            report.append(f"  {name}: out of window ({ws:02d}-{we:02d} UTC), not checked")
            continue
        g = age_fn(current)
        if g is None:
            alerts.append(f"⚠️ {name}: no freshness timestamp found at all — the meta file is missing.")
            report.append(f"  {name}: NO TIMESTAMP")
            continue
        age = (now - g).total_seconds() / 60
        line = f"  {name}: {age:.0f} min old (threshold {thresh})"
        report.append(line)
        if age > thresh:
            alerts.append(
                f"⚠️ {name} is **{age:.0f} min old** (should refresh inside {thresh}). "
                f"A GitHub cron slot was likely skipped — the record for any game that "
                f"locks now is stale. Last build: {g.isoformat(timespec='minutes')}."
            )

    print(f"staleness_watch @ {now.isoformat(timespec='minutes')}")
    print("\n".join(report) or "  (nothing in window)")
    if alerts:
        print("\nALERTS:")
        print("\n".join("  " + a for a in alerts))
        if args.post:
            post_discord(["🚨 **Board staleness**"] + alerts)
    else:
        print("\nall fresh / out of window — no alert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
