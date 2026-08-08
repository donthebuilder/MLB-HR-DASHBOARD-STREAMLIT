#!/usr/bin/env python3
"""
🚪 PEN DOOR WATCH (2026-08-08, Donovan: "i just want when the pitcher is
changed we get one of those discord notis").

Near-real-time pitching-change alerts. Runs every 10 minutes during game
hours; each run looks at ONE disjoint 10-minute clock window, so a change is
never posted twice and (as long as the run fires) never skipped. This
replaced the hourly digest inside live_results_tracker — one owner, no
double posts.

The live feed marks every change explicitly — eventType
'pitching_substitution', a written "X replaces Y" description, a UTC
startTime. VERIFIED on a real game feed (Mets/Pirates 2026-08-07) before
this file existed.

Deliberately tiny: schedule → live gamePks → fields-limited feed per live
game. Picks come from the published slate on the data branch (public); if
that fetch fails the alert still ships, just without the "our bats" line —
a missing garnish must never kill the meal.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request

API = "https://statsapi.mlb.com/api/v1"
FEED_FIELDS = ("gameData,teams,home,away,abbreviation,liveData,plays,allPlays,"
               "about,halfInning,inning,playEvents,details,eventType,description,startTime")
SLATE_URL = ("https://raw.githubusercontent.com/donthebuilder/MLB-HR-DASHBOARD-STREAMLIT/"
             "data/public/data/current/today_slim.json")
WINDOW_MIN = 10


def get_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "moonshot-pen-door/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"fetch failed {url[:80]}: {exc}", file=sys.stderr)
        return None


def discord_urls() -> list[str]:
    raw = os.environ.get("DISCORD_WEBHOOK", "")
    return [u.strip() for u in raw.replace(",", "\n").split() if u.strip().startswith("http")]


def post(msg: str) -> None:
    for url in discord_urls():
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"content": msg[:1900]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "moonshot-bot"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print(f"discord post failed: {exc}", file=sys.stderr)


def picks_by_game() -> dict[int, list[dict]]:
    """team+name per pick, keyed by game_pk — best-effort from the published slate."""
    data = get_json(SLATE_URL)
    out: dict[int, list[dict]] = {}
    if not data:
        return out
    rows = data if isinstance(data, list) else next(
        (data[k] for k in ("players", "all_players", "rows", "picks") if isinstance(data.get(k), list)), [])
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            pk = int(r.get("game_pk") or 0)
        except (TypeError, ValueError):
            continue
        if pk:
            out.setdefault(pk, []).append(
                {"name": str(r.get("name") or ""), "team": str(r.get("team") or "").upper()})
    return out


def main() -> int:
    now = dt.datetime.now(dt.UTC)
    minute_tick = (now.minute // WINDOW_MIN) * WINDOW_MIN
    tick = now.replace(minute=minute_tick, second=0, microsecond=0)
    w_start, w_end = tick - dt.timedelta(minutes=WINDOW_MIN), tick
    today = now.date().isoformat()
    yday = (now.date() - dt.timedelta(days=1)).isoformat()  # UTC evening = ET night games

    sched = get_json(f"{API}/schedule?sportId=1&startDate={yday}&endDate={today}"
                     "&fields=dates,games,gamePk,status,abstractGameState")
    live_pks = [g["gamePk"] for d in (sched or {}).get("dates", []) for g in d.get("games", [])
                if (g.get("status") or {}).get("abstractGameState") == "Live"]
    if not live_pks:
        print("no live games")
        return 0

    picks = picks_by_game()
    lines: list[str] = []
    for pk in live_pks:
        # the live feed lives on api/v1.1, unlike everything else on v1
        feed = get_json(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields={FEED_FIELDS}")
        if not feed:
            continue
        gteams = (feed.get("gameData", {}) or {}).get("teams", {}) or {}
        home_ab = (gteams.get("home") or {}).get("abbreviation", "")
        away_ab = (gteams.get("away") or {}).get("abbreviation", "")
        for play in ((feed.get("liveData", {}) or {}).get("plays", {}) or {}).get("allPlays", []) or []:
            about = play.get("about") or {}
            for ev in play.get("playEvents") or []:
                det = ev.get("details") or {}
                if det.get("eventType") != "pitching_substitution":
                    continue
                try:
                    t = dt.datetime.fromisoformat(str(ev.get("startTime") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not (w_start <= t < w_end):
                    continue
                half = str(about.get("halfInning") or "")
                pitching_ab = home_ab if half == "top" else away_ab
                batting_ab = away_ab if half == "top" else home_ab
                desc = str(det.get("description") or "").rstrip(".")
                our = [p["name"].split()[-1] for p in picks.get(pk, []) if p["team"] == batting_ab][:3]
                line = f"🚪 **{pitching_ab}** pen, {half} {about.get('inning')}: {desc}"
                if our:
                    line += f" — our bats attacking: {', '.join(our)}"
                lines.append(line)

    if lines:
        post("\n".join(lines[:10]))
        print(f"posted {len(lines)} pen-door change(s)")
    else:
        print("no changes in window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
