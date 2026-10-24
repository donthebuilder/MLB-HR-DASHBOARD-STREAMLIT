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


def in_week_windows(now: dt.datetime, windows: dict) -> bool:
    """windows: {UTC weekday (Mon=0): [(start_h, end_h), ...]}. True when `now`
    (UTC) falls inside one of that weekday's [start_h, end_h) hour ranges."""
    return any(a <= now.hour < b for a, b in windows.get(now.weekday(), ()))


# 2026-10-24. The hours the WORKFLOW itself runs on a 15-minute cadence, copied
# from nfl.yml's PREGAME CADENCE and nfl-fantasy-stats.yml. Outside them nothing
# is supposed to refresh, so a "stale" board there is not a skipped slot. The old
# whole-day sets alarmed every Friday afternoon (TNF is Fri 00-01Z) and on game
# nights after the last pregame build (the board is frozen at kickoff). UTC
# weekday, Mon=0. Sunday 0-6Z is Saturday night ET: nothing in the regular season.
NFL_BOARD_WINDOWS = {
    3: [(14, 24)],             # Thu: Thanksgiving + TNF run-up
    4: [(0, 1)],               # Thu night ET (TNF kickoff in EST)
    6: [(11, 24)],             # Sun: London, 1pm, 4pm, SNF run-up
    0: [(0, 1), (21, 24)],     # Sun night ET (SNF kickoff) + MNF run-up
    1: [(0, 1)],               # Mon night ET (MNF kickoff)
}
NFL_FANTASY_WINDOWS = {
    4: [(0, 6)],               # Thu night
    6: [(13, 24)],             # Sun (no Saturday games before December)
    0: [(0, 6)],               # Sun night
    1: [(0, 6)],               # Mon night
}


def mlb_has_games(now: dt.datetime) -> bool:
    """Does MLB play on the ET dates the watch window covers? FAILS OPEN: if the
    schedule cannot be read, say True so the board is checked as before. On a
    no-game day (World Series off days, 2026-10-20..22) the bot publishes nothing,
    and the board looks 'stale' without anything having been skipped."""
    import urllib.request
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    days = {now.astimezone(et).date(), (now - dt.timedelta(hours=8)).astimezone(et).date()}
    total = 0
    try:
        for d in days:
            url = ("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=%s&gameType=R,F,D,L,W" % d)
            with urllib.request.urlopen(url, timeout=10) as r:
                total += int(json.load(r).get("totalGames") or 0)
    except Exception:
        return True
    return total > 0


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


def nfl_fantasy_stats_age(current: Path):
    """2026-09-15. FRANCHISE's live scoring reads nfl_fantasy_stats.json, a
    SEPARATE publish from nfl.yml's own board (nfl_meta.json, checked by
    nfl_age above) -- nfl-fantasy-stats.yml is its own cron, on its own
    schedule, and can be silently skipped independently of nfl.yml. Nothing
    on file checked it. It went stale for 2+ hours during the 2026-09-15 MNF
    window (built_at stuck at 02:47Z while the game ran past 04:45Z) with no
    alert, because this function did not exist -- Week 1 stayed 'live',
    win/loss never tallied, and the Tue 13:00Z Week 2 roll ran the risk of
    firing against an unfinished week. Same read-the-OUTPUT discipline as
    nfl_age: this is generated_at off the published file, not GitHub's own
    run history (the thing that lies when a slot is skipped).
    """
    meta = read_json(current / "nfl_fantasy_stats.json")
    if isinstance(meta, dict):
        return parse_ts(meta.get("built_at"))
    return None


def post_discord(lines: list[str]) -> bool:
    """True when at least one webhook took the post. Ops alert: goes to
    DISCORD_OPS_WEBHOOK when set, else the shared DISCORD_WEBHOOK room until an
    ops channel exists. Sends a User-Agent (Discord 403s urllib's default) and
    reads the secret like the tracker (bots/discord_post.py)."""
    import discord_post
    if not lines:
        return True
    if not discord_post.webhook_urls(discord_post.ops_secret()):
        print("  ! DISCORD_OPS_WEBHOOK / DISCORD_WEBHOOK unset or holds no URL -- alert NOT delivered",
              file=sys.stderr)
        return False
    ok, bad, _ops = discord_post.post_ops({"content": "\n".join(lines)[:1900]})
    return ok > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="public/data/current",
                    help="the data-branch current/ directory to read timestamps from")
    ap.add_argument("--post", action="store_true", help="post to Discord (else print only)")
    args = ap.parse_args()
    current = Path(args.dir)
    now = now_utc()

    # (name, age_fn, window_start_h, window_end_h, threshold_min, week_windows)
    # week_windows None = the plain daily window; else the per-weekday table above.
    checks = [
        ("MLB board (today.yml)", mlb_age,
         env_int("MLB_WINDOW_START", 12), env_int("MLB_WINDOW_END", 8),
         env_int("MLB_STALE_MIN", 120), None),
        ("NFL board (nfl.yml)", nfl_age, 0, 24,
         env_int("NFL_STALE_MIN", 240), NFL_BOARD_WINDOWS),
        ("NFL fantasy stats (FRANCHISE live scoring)", nfl_fantasy_stats_age, 0, 24,
         env_int("NFLFS_STALE_MIN", 45), NFL_FANTASY_WINDOWS),
    ]

    alerts, report = [], []
    for name, age_fn, ws, we, thresh, wk in checks:
        if wk is not None:
            if not in_week_windows(now, wk):
                report.append(f"  {name}: not in its refresh hours, not checked")
                continue
        elif not in_window(now, ws, we):
            report.append(f"  {name}: out of window ({ws:02d}-{we:02d} UTC), not checked")
            continue
        elif name.startswith("MLB") and not mlb_has_games(now):
            report.append(f"  {name}: no MLB games today, not checked")
            continue
        g = age_fn(current)
        if g is None:
            alerts.append(f"⚠️ {name}: no freshness timestamp found at all — the status file is missing.")
            report.append(f"  {name}: NO TIMESTAMP")
            continue
        age = (now - g).total_seconds() / 60
        line = f"  {name}: {age:.0f} min old (threshold {thresh})"
        report.append(line)
        if age > thresh:
            alerts.append(
                f"⚠️ {name} is **{age:.0f} min old** (should refresh inside {thresh}). "
                f"A scheduled refresh was probably skipped, so any game that starts now "
                f"would be graded against old picks. Last build: {g.isoformat(timespec='minutes')}."
            )

    print(f"staleness_watch @ {now.isoformat(timespec='minutes')}")
    print("\n".join(report) or "  (nothing in window)")
    if alerts:
        print("\nALERTS:")
        print("\n".join("  " + a for a in alerts))
        if args.post and not post_discord(["🚨 **Board out of date**"] + alerts):
            print("ALERT NOT DELIVERED", file=sys.stderr)
            return 1
    else:
        print("\nall fresh / out of window — no alert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
