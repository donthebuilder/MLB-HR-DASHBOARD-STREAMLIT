#!/usr/bin/env python3
"""
BACKFILL REAL WEATHER onto the graded archive (2026-08-11).

WHY THIS EXISTS. weather_hr_effect_pct sits on 2,369 archived rows and is ZERO
on every one of them, while a live slate carries -2% to +8%. wind_boost is the
same. So no weather question has ever been answerable from the archive, and
the one Donovan actually asked -- is the model's moonshot skew weather-fragile
-- returned an empty table.

Unlike longest_hr_score, this IS recoverable. The rows carry venue_name and
game_time (5,017 of 5,766), which is everything a historical lookup needs.

WHAT IT WRITES, AND WHAT IT DELIBERATELY DOES NOT.

It writes RAW OBSERVED WEATHER under its own `wx_` prefix:

    wx_temp_f  wx_wind_mph  wx_wind_deg  wx_humidity  wx_precip  wx_source

It does NOT write weather_hr_effect_pct, and will never overwrite a bot field.
That number is the bot's own model of temperature, wind vector relative to the
park's orientation, humidity and air density combined; reproducing it here from
the outside would be a guess wearing the same field name, which is worse than
a gap. Raw observations are auditable and the analysis can build its own effect
on top of them.

SOURCES. Venue coordinates from the league's own /api/v1/venues (hydrated with
location), cached per venue. Weather from Open-Meteo's archive API, which is
free and needs no key. Both are looked up at the game's actual first-pitch
HOUR, in UTC, off game_time -- not the daily average, which would smear a 7pm
game into the afternoon.

SAFE BY CONSTRUCTION: additive, idempotent (rows with wx_source are skipped),
atomic writes via temp file + os.replace, and --dry-run reports without
writing. A venue it cannot geocode is left alone rather than guessed at.

USAGE
  python3 bots/backfill_weather.py --dry-run --limit 3
  python3 bots/backfill_weather.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
VENUES = "https://statsapi.mlb.com/api/v1/venues"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT = 30


def rows_of(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


_geo: dict[str, tuple[float, float] | None] = {}


def venue_coords(name: str, session: requests.Session) -> tuple[float, float] | None:
    """lat/lon for a venue NAME, via the league's own venue list. Cached."""
    if not _geo:
        try:
            r = session.get(VENUES, params={"hydrate": "location", "sportId": 1}, timeout=TIMEOUT)
            for v in (r.json().get("venues") or []):
                c = ((v.get("location") or {}).get("defaultCoordinates") or {})
                lat, lon = c.get("latitude"), c.get("longitude")
                if lat is not None and lon is not None:
                    _geo[str(v.get("name", "")).strip().lower()] = (float(lat), float(lon))
        except Exception as exc:
            print(f"venue lookup failed: {exc}", file=sys.stderr)
        if not _geo:
            _geo["__empty__"] = None
    return _geo.get(str(name or "").strip().lower())


_wx: dict[tuple, dict | None] = {}


def weather_at(lat: float, lon: float, iso_utc: str, session: requests.Session) -> dict | None:
    """Observed weather at the game's first-pitch HOUR."""
    day = iso_utc[:10]
    hour = iso_utc[11:13] or "18"
    key = (round(lat, 2), round(lon, 2), day, hour)
    if key in _wx:
        return _wx[key]
    try:
        r = session.get(ARCHIVE, params={
            "latitude": lat, "longitude": lon, "start_date": day, "end_date": day,
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "UTC",
        }, timeout=TIMEOUT)
        h = (r.json() or {}).get("hourly") or {}
        times = h.get("time") or []
        want = f"{day}T{hour}:00"
        i = times.index(want) if want in times else (len(times) // 2 if times else None)
        if i is None:
            _wx[key] = None
            return None
        def g(k):
            a = h.get(k) or []
            return a[i] if i < len(a) else None
        _wx[key] = {
            "wx_temp_f": g("temperature_2m"),
            "wx_wind_mph": g("wind_speed_10m"),
            "wx_wind_deg": g("wind_direction_10m"),
            "wx_humidity": g("relative_humidity_2m"),
            "wx_precip": g("precipitation"),
            "wx_source": "open-meteo/archive",
        }
    except Exception as exc:
        print(f"weather lookup failed {day} {hour}: {exc}", file=sys.stderr)
        _wx[key] = None
    return _wx[key]


def do_night(path: Path, session: requests.Session, dry: bool, pause: float) -> tuple[int, int, str]:
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        return 0, 0, f"unreadable ({exc.__class__.__name__})"
    rows = rows_of(payload)
    if not rows:
        return 0, 0, "no rows"
    want = [r for r in rows if not r.get("wx_source") and r.get("venue_name") and r.get("game_time")]
    if not want:
        have = sum(1 for r in rows if r.get("wx_source"))
        return 0, 0, (f"nothing to do ({have} already carry wx)" if have
                      else "no venue_name / game_time on these rows")
    filled = 0
    for r in want:
        c = venue_coords(r.get("venue_name"), session)
        if not c:
            continue
        w = weather_at(c[0], c[1], str(r.get("game_time")), session)
        if pause:
            time.sleep(pause)
        if not w:
            continue
        r.update(w)
        filled += 1
    if filled and not dry:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    miss = len(want) - filled
    return filled, len(want), f"{filled}/{len(want)} filled" + (f", {miss} unresolved" if miss else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--archive-dir", type=Path, default=Path.home() / "Desktop" / "results")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--pause", type=float, default=0.1)
    a = ap.parse_args()
    d = a.archive_dir.expanduser()
    if not d.is_dir():
        print(f"archive dir not found: {d}", file=sys.stderr)
        return 1
    files = sorted((p for p in d.glob("graded_results_*.json") if DATE_RE.search(p.name)), reverse=True)
    if a.limit:
        files = files[:a.limit]
    if not files:
        print(f"no graded_results_*.json in {d}", file=sys.stderr)
        return 1
    print(f"{len(files)} nights in {d}" + ("   [DRY RUN]" if a.dry_run else ""))
    tf = tw = 0
    with requests.Session() as s:
        for p in files:
            f, w, note = do_night(p, s, a.dry_run, a.pause)
            tf += f; tw += w
            print(f"  {'*' if f else ' '} {DATE_RE.search(p.name).group(1)}  {note}")
    print(f"\n{tf} of {tw} rows given real weather across {len(files)} nights."
          + ("  (dry run)" if a.dry_run else ""))
    print("Fields written: wx_temp_f, wx_wind_mph, wx_wind_deg, wx_humidity, wx_precip, wx_source.")
    print("The bot's own weather_hr_effect_pct is NOT touched — see the module docstring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
