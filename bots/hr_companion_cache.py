"""
HR Companion Cache Bot

For every player in today's slate, finds the last date they hit a home run,
then returns every OTHER player who also homered that same day.

This is the "who else went yard the last time this guy went yard" lookup.

How it works:
  1. Reads the most recent today.json (slate file) from the outputs folder
  2. Uses spray_chart to find each player's last HR date
  3. Builds a league-wide HR log from all available slate files
  4. For each player on today's slate, finds companions (other HR hitters on same date)
  5. Writes hr_companion_cache.json to the outputs folder

Output shape (per player entry):
  {
    "player_id": 677594,
    "name": "Julio Rodríguez",
    "team": "SEA",
    "last_hr_date": "2026-05-27",
    "days_since_hr": 2,
    "companions": [
      {
        "player_id": 678554,
        "name": "Curtis Mead",
        "team": "WSH",
        "hr_count": 1        # how many HRs that day
      },
      ...
    ],
    "companion_count": 18
  }

The site can read this alongside the dashboard and show "18 others also homered
the last time Julio went yard (May 27)" as a companion signal.

Run: python hr_companion_cache.py [--slate path/to/today.json]
"""

import argparse
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


# ─── paths ────────────────────────────────────────────────────────────────────

ROOT    = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(parents=True, exist_ok=True)

OUT_PATH    = OUTPUTS / "hr_companion_cache.json"
OUT_LATEST  = OUTPUTS / "hr_companion_latest.json"   # always the same alias


# ─── helpers ──────────────────────────────────────────────────────────────────

def find_latest_slate() -> Path | None:
    """Find the most recent *-today.json or slate_*.json in outputs or root."""
    candidates = sorted(
        list(OUTPUTS.glob("*-today.json"))
        + list(OUTPUTS.glob("slate_*.json"))
        + list(ROOT.glob("*-today.json"))
        + list(ROOT.glob("slate_*.json")),
        reverse=True,
    )
    return candidates[0] if candidates else None


def extract_date_from_path(path: Path) -> str | None:
    import re
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return m.group(1) if m else None


def load_slate(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d if isinstance(d, list) else []


def get_last_hr_date_from_spray(player: dict) -> str | None:
    """
    Use spray_chart to find the most recent HR date.
    spray_chart entries have: {date, event, is_hr, ...}
    """
    spray = player.get("spray_chart") or []
    hr_dates = [
        e["date"] for e in spray
        if isinstance(e, dict) and e.get("is_hr") and e.get("date")
    ]
    return max(hr_dates) if hr_dates else None


def days_between(date_str: str, today: dt.date) -> int:
    try:
        d = dt.date.fromisoformat(date_str)
        return (today - d).days
    except Exception:
        return 999


# ─── build league-wide HR log from all slates ─────────────────────────────────

def build_hr_log(exclude_path: Path | None = None) -> dict[str, list[dict]]:
    """
    Returns: { "2026-05-27": [{player_id, name, team, hr_count}, ...], ... }
    Aggregated from all available slate files.
    """
    hr_log: dict[str, dict[int, dict]] = defaultdict(dict)

    slate_paths = sorted(
        list(OUTPUTS.glob("*-today.json"))
        + list(OUTPUTS.glob("slate_*.json"))
        + list(ROOT.glob("*-today.json"))
        + list(ROOT.glob("slate_*.json"))
    )

    for path in slate_paths:
        players = load_slate(path)
        for p in players:
            spray = p.get("spray_chart") or []
            pid   = p.get("player_id")
            name  = p.get("name", "")
            team  = p.get("team", "")
            if not pid:
                continue
            for e in spray:
                if not (isinstance(e, dict) and e.get("is_hr") and e.get("date")):
                    continue
                date = e["date"]
                if pid not in hr_log[date]:
                    hr_log[date][pid] = {"player_id": pid, "name": name, "team": team, "hr_count": 0}
                hr_log[date][pid]["hr_count"] += 1

    # Convert to lists sorted by player_id for stable output
    result: dict[str, list[dict]] = {}
    for date, pid_map in hr_log.items():
        result[date] = sorted(pid_map.values(), key=lambda x: x["player_id"])

    return result


# ─── main build ───────────────────────────────────────────────────────────────

def build_companion_cache(slate_path: Path, today: dt.date) -> dict:
    print(f"Loading slate: {slate_path.name}", file=sys.stderr)
    players = load_slate(slate_path)
    if not players:
        print("ERROR: slate is empty or invalid", file=sys.stderr)
        return {}

    slate_date_str = extract_date_from_path(slate_path) or today.isoformat()
    print(f"Slate date: {slate_date_str} | Players: {len(players)}", file=sys.stderr)

    print("Building league HR log from all slate files...", file=sys.stderr)
    hr_log = build_hr_log()
    all_dates = sorted(hr_log.keys(), reverse=True)
    print(f"  HR log covers {len(all_dates)} dates, "
          f"{sum(len(v) for v in hr_log.values())} total HR events", file=sys.stderr)

    entries: list[dict] = []
    found_hr   = 0
    no_hr_data = 0

    for p in players:
        pid  = p.get("player_id")
        name = p.get("name", "Unknown")
        team = p.get("team", "")

        if not pid:
            continue

        last_hr_date = get_last_hr_date_from_spray(p)

        if not last_hr_date:
            # Fallback: check hr_log directly for this player across all dates
            for date in all_dates:
                if any(e["player_id"] == pid for e in hr_log.get(date, [])):
                    last_hr_date = date
                    break

        if not last_hr_date:
            no_hr_data += 1
            entries.append({
                "player_id":      pid,
                "name":           name,
                "team":           team,
                "last_hr_date":   None,
                "days_since_hr":  None,
                "companions":     [],
                "companion_count": 0,
            })
            continue

        found_hr += 1
        days_ago  = days_between(last_hr_date, today)

        # Find companions = everyone else who also homered on that same date
        same_day_hrs = hr_log.get(last_hr_date, [])
        companions   = [
            e for e in same_day_hrs
            if e["player_id"] != pid
        ]
        # Sort by name for stable output
        companions.sort(key=lambda x: x["name"])

        entries.append({
            "player_id":      pid,
            "name":           name,
            "team":           team,
            "last_hr_date":   last_hr_date,
            "days_since_hr":  days_ago,
            "companions":     companions,
            "companion_count": len(companions),
        })

    # Sort: players with HR data first, then by name
    entries.sort(key=lambda x: (x["last_hr_date"] is None, x["name"]))

    print(f"  Players with HR data:    {found_hr}", file=sys.stderr)
    print(f"  Players without HR data: {no_hr_data}", file=sys.stderr)

    # Summary stats
    companion_counts = [e["companion_count"] for e in entries if e["last_hr_date"]]
    avg_companions   = sum(companion_counts) / len(companion_counts) if companion_counts else 0
    max_companions   = max(companion_counts) if companion_counts else 0

    top_companion_days: dict[str, int] = defaultdict(int)
    for e in entries:
        if e["last_hr_date"]:
            top_companion_days[e["last_hr_date"]] += 1

    payload = {
        "schema_version":  "companion_v1",
        "generated_at":    dt.datetime.now().isoformat(timespec="seconds"),
        "slate_date":      slate_date_str,
        "slate_file":      slate_path.name,
        "total_players":   len(entries),
        "players_with_hr": found_hr,
        "avg_companions":  round(avg_companions, 1),
        "max_companions":  max_companions,
        "players":         entries,
    }

    return payload


# ─── write output ─────────────────────────────────────────────────────────────

def write_output(payload: dict, today: dt.date) -> None:
    blob = json.dumps(payload, indent=2, default=str)

    # Dated file
    dated_path = OUTPUTS / f"hr_companion_cache_{today.isoformat()}.json"
    dated_path.write_text(blob, encoding="utf-8")

    # Always-current alias
    OUT_LATEST.write_text(blob, encoding="utf-8")
    OUT_PATH.write_text(blob, encoding="utf-8")

    print(f"Written: {dated_path.name}", file=sys.stderr)
    print(f"Alias:   {OUT_LATEST.name}", file=sys.stderr)

    # Print a quick summary to stdout
    top5 = sorted(
        [p for p in payload["players"] if p["last_hr_date"]],
        key=lambda x: -x["companion_count"]
    )[:5]
    print(f"\nTop companion counts for {payload['slate_date']}:")
    for p in top5:
        print(f"  {p['name']:<28} last HR={p['last_hr_date']}  "
              f"companions={p['companion_count']}  ({p['days_since_hr']}d ago)")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Build HR companion cache for today's slate")
    parser.add_argument("--slate", type=str, default=None,
                        help="Path to today.json slate file (auto-detected if omitted)")
    parser.add_argument("--date", type=str, default=None,
                        help="Override today's date (YYYY-MM-DD)")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    if args.slate:
        slate_path = Path(args.slate)
    else:
        slate_path = find_latest_slate()

    if not slate_path or not slate_path.exists():
        print("ERROR: No slate file found. Pass --slate path/to/today.json", file=sys.stderr)
        return 1

    payload = build_companion_cache(slate_path, today)
    if not payload:
        return 1

    write_output(payload, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
