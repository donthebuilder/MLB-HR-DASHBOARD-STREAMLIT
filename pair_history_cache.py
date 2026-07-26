#!/usr/bin/env python3
"""
MLB Pair History Cache Bot — 350 Pair Version

Builds same-day HR pair history for the current MLB season and writes dashboard-ready files:
  outputs/pair_history_cache.json
  outputs/pair_history_summary.json
  public/data/pair_history_cache.json
  public/data/pair_history_summary.json

What it tracks:
- Every date where 2+ players homered on the same day
- Every same-day HR pair from those dates
- Same-game HR pair counts
- Season count, career/total alias fields, recent dates, last hit date
- Top 350 pair rows by default

Designed for the Pair History Lab dashboard, which reads:
  /data/pair_history_summary.json
  /data/pair_history_cache.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

MLB_BASE = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 30

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

if ZoneInfo is not None:
    TODAY = dt.datetime.now(ZoneInfo("America/Phoenix")).date()
else:
    TODAY = dt.date.today()

ROOT_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT_DIR / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
# When this script lives in bots/, also mirror outputs to <repo_root>/outputs so the
# central site_data_sync.py finds them when running in CI. Safe no-op when same dir.
REPO_OUT_DIR = ROOT_DIR.parent / "outputs"
try:
    REPO_OUT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    REPO_OUT_DIR = OUT_DIR

DASHBOARD_REPO = Path(os.environ.get(
    "MLB_DASHBOARD_DIR",
    str(Path(__file__).resolve().parent.parent),
))

TEAM_FIXES = {"AZ": "ARI", "CHW": "CWS", "KCR": "KC", "SFG": "SF", "SDP": "SD", "TBR": "TB"}


def normalize_team(abbr: str) -> str:
    return TEAM_FIXES.get(str(abbr or "").upper(), str(abbr or "").upper())


def norm_name(name: str) -> str:
    return " ".join(str(name or "").replace(".", "").lower().split())


def pair_key(a: str, b: str) -> str:
    return "|".join(sorted([str(a or "").strip(), str(b or "").strip()], key=norm_name))


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


class MLB:
    def __init__(self, pause: float = 0.05) -> None:
        self.session = requests.Session()
        self.pause = pause
        self.cache: Dict[str, Any] = {}

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        key = url + "?" + json.dumps(params or {}, sort_keys=True)
        if key in self.cache:
            return self.cache[key]
        r = self.session.get(url, params=params or {}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        self.cache[key] = data
        if self.pause:
            time.sleep(self.pause)
        return data

    def schedule(self, day: dt.date) -> List[Dict[str, Any]]:
        data = self.get(f"{MLB_BASE}/schedule", params={"sportId": 1, "date": day.isoformat(), "hydrate": "team"})
        dates = data.get("dates") or []
        if not dates:
            return []
        return dates[0].get("games") or []

    def boxscore(self, game_pk: int) -> Dict[str, Any]:
        return self.get(f"{MLB_BASE}/game/{game_pk}/boxscore")


def player_identity(side_box: Dict[str, Any], player_key: str, stat: Dict[str, Any], team_abbr: str, game_pk: int, game_date: str) -> Dict[str, Any]:
    player = side_box.get("players", {}).get(player_key, {})
    person = player.get("person") or {}
    pid = safe_int(person.get("id") or str(player_key).replace("ID", ""), 0)
    name = person.get("fullName") or player.get("fullName") or stat.get("name") or f"Player {pid}"
    return {
        "player_id": pid,
        "name": name,
        "player_name": name,
        "team": normalize_team(team_abbr),
        "game_pk": game_pk,
        "date": game_date,
        "hr": safe_int(stat.get("homeRuns"), 0),
    }


def extract_game_hr_players(box: Dict[str, Any], game: Dict[str, Any], game_date: str) -> List[Dict[str, Any]]:
    teams = box.get("teams") or {}
    out: List[Dict[str, Any]] = []
    for side in ("away", "home"):
        side_box = teams.get(side) or {}
        team_info = (game.get("teams", {}).get(side, {}).get("team") or {})
        team_abbr = team_info.get("abbreviation") or team_info.get("teamCode") or ""
        players = side_box.get("players") or {}
        for player_key, player in players.items():
            batting = ((player.get("stats") or {}).get("batting") or {})
            hr = safe_int(batting.get("homeRuns"), 0)
            if hr <= 0:
                continue
            item = player_identity(side_box, player_key, batting, team_abbr, safe_int(game.get("gamePk"), 0), game_date)
            # A player with 2 HR still counts once for pair occurrence, but keep actual HR count.
            item["hr"] = hr
            out.append(item)
    return out


def build_pair_record(a: Dict[str, Any], b: Dict[str, Any], date_str: str) -> Dict[str, Any]:
    key = pair_key(a["name"], b["name"])
    same_game = a.get("game_pk") == b.get("game_pk")
    return {
        "pair_key": key,
        "players": [
            {"player_id": a.get("player_id"), "name": a.get("name"), "player_name": a.get("name"), "team": a.get("team")},
            {"player_id": b.get("player_id"), "name": b.get("name"), "player_name": b.get("name"), "team": b.get("team")},
        ],
        "dates": [date_str],
        "same_game_dates": [date_str] if same_game else [],
        "same_day_hr_count_season": 1,
        "same_day_hr_count_career": 1,
        "same_game_hr_count": 1 if same_game else 0,
        "last_same_day_hr": date_str,
        "history_boost": 0,
    }



DEFAULT_MANUAL_PAIR_HITS = [
    {
        "date": "2026-06-10",
        "note": "User-confirmed pair hit: Ian Happ + James Wood",
        "tag": "Manual Hit",
        "a": {"player_id": 664023, "name": "Ian Happ", "player_name": "Ian Happ", "team": "CHC", "game_pk": 824348, "hr": 1},
        "b": {"player_id": 695578, "name": "James Wood", "player_name": "James Wood", "team": "WSH", "game_pk": 823215, "hr": 1},
    }
]


def add_manual_pair_hit(existing: Dict[str, Any], hit: Dict[str, Any]) -> None:
    manual_dates = set(existing.get("manual_confirmed_dates") or [])
    date_str = str(hit.get("date") or "")[:10]
    if date_str:
        manual_dates.add(date_str)
    existing["manual_confirmed"] = True
    existing["manual_confirmed_dates"] = sorted(manual_dates)
    notes = list(existing.get("manual_notes") or [])
    note = str(hit.get("note") or "").strip()
    if note and note not in notes:
        notes.append(note)
    existing["manual_notes"] = notes[:5]
    tags = list(existing.get("tags") or [])
    tag = str(hit.get("tag") or "Manual Hit").strip()
    if tag and tag not in tags:
        tags.append(tag)
    existing["tags"] = tags[:8]


def apply_manual_pair_hits(pairs: Dict[str, Dict[str, Any]], start: dt.date, end: dt.date) -> int:
    """Guarantee user-confirmed pair hits are present even if a source feed misses them."""
    added_or_marked = 0
    for hit in DEFAULT_MANUAL_PAIR_HITS:
        date_str = str(hit.get("date") or "")[:10]
        try:
            hit_date = dt.date.fromisoformat(date_str)
        except Exception:
            continue
        if hit_date < start or hit_date > end:
            continue
        a = dict(hit.get("a") or {})
        b = dict(hit.get("b") or {})
        if not a.get("name") or not b.get("name"):
            continue
        key = pair_key(a["name"], b["name"])
        if key not in pairs:
            pairs[key] = build_pair_record(a, b, date_str)
        else:
            merge_pair(pairs[key], a, b, date_str)
        add_manual_pair_hit(pairs[key], hit)
        added_or_marked += 1
    return added_or_marked


def merge_pair(existing: Dict[str, Any], a: Dict[str, Any], b: Dict[str, Any], date_str: str) -> None:
    dates = set(existing.get("dates") or [])
    if date_str not in dates:
        dates.add(date_str)
    existing["dates"] = sorted(dates)
    existing["same_day_hr_count_season"] = len(existing["dates"])
    existing["same_day_hr_count_career"] = len(existing["dates"])
    if a.get("game_pk") == b.get("game_pk"):
        sg = set(existing.get("same_game_dates") or [])
        sg.add(date_str)
        existing["same_game_dates"] = sorted(sg)
        existing["same_game_hr_count"] = len(sg)
    existing["last_same_day_hr"] = max(existing["dates"])


def pair_score(item: Dict[str, Any], today: dt.date) -> float:
    season = safe_int(item.get("same_day_hr_count_season"), 0)
    total = safe_int(item.get("same_day_hr_count_career"), season)
    same_game = safe_int(item.get("same_game_hr_count"), 0)
    last = item.get("last_same_day_hr") or "1900-01-01"
    try:
        days_since = max(0, (today - dt.date.fromisoformat(str(last)[:10])).days)
    except Exception:
        days_since = 999
    recent = 12 if days_since <= 14 else 7 if days_since <= 30 else 3 if days_since <= 60 else 0
    return season * 18 + total * 3 + same_game * 5 + recent


def apply_boosts(pairs: Dict[str, Dict[str, Any]], today: dt.date) -> List[Dict[str, Any]]:
    rows = list(pairs.values())
    for item in rows:
        season = safe_int(item.get("same_day_hr_count_season"), 0)
        same_game = safe_int(item.get("same_game_hr_count"), 0)
        last = item.get("last_same_day_hr") or ""
        try:
            days_since = max(0, (today - dt.date.fromisoformat(str(last)[:10])).days)
        except Exception:
            days_since = 999
        boost = 0
        boost += min(24, season * 6)
        boost += min(8, same_game * 3)
        if days_since <= 14:
            boost += 8
        elif days_since <= 30:
            boost += 4
        if item.get("manual_confirmed"):
            boost += 6
        item["history_boost"] = int(boost)
        item["pair_score"] = round(pair_score(item, today) + (6 if item.get("manual_confirmed") else 0), 1)
        item["same_day_hr_count_career"] = safe_int(item.get("same_day_hr_count_career"), season)
        item["days_since_last_hit"] = days_since
        item["recent_pair_hit"] = days_since <= 14
        if days_since == 0:
            item["last_hit_bucket"] = "Today"
        elif days_since <= 7:
            item["last_hit_bucket"] = "Last 7"
        elif days_since <= 14:
            item["last_hit_bucket"] = "Last 14"
        elif days_since <= 30:
            item["last_hit_bucket"] = "Last 30"
        else:
            item["last_hit_bucket"] = "Older"
        item["repeat_count"] = season
        item["same_game_flag"] = same_game > 0
        # Helpful search aliases for the dashboard.
        players = item.get("players") or []
        if len(players) >= 2:
            item["player_1"] = players[0].get("name") or players[0].get("player_name")
            item["player_2"] = players[1].get("name") or players[1].get("player_name")
    rows.sort(key=lambda x: (safe_int(x.get("same_day_hr_count_season")), safe_int(x.get("history_boost")), x.get("last_same_day_hr") or ""), reverse=True)
    return rows


def copy_to_site(cache_path: Path, summary_path: Path, dashboard_repo: Path) -> None:
    data_dir = dashboard_repo / "public" / "data"
    # Accept either Next.js-style (app/) or any repo with public/ directory.
    has_public = (dashboard_repo / "public").exists()
    has_app = (dashboard_repo / "app").exists()
    if not has_public and not has_app:
        print(f"⚠️ Website repo not found at {dashboard_repo}. Files saved locally only.", file=sys.stderr)
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache_path, data_dir / "pair_history_cache.json")
    shutil.copy2(summary_path, data_dir / "pair_history_summary.json")
    print(f"📁 Copied pair history files to {data_dir}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build MLB same-day HR pair history cache with 350 top pairs.")
    parser.add_argument("--season", type=int, default=TODAY.year)
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD. Default: March 1 of season.")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD. Default: today.")
    parser.add_argument("--limit", type=int, default=350, help="How many top pairs to write into top_pairs. Default 350.")
    parser.add_argument("--dashboard-dir", default=str(DASHBOARD_REPO))
    parser.add_argument("--no-site-sync", action="store_true")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start_date) if args.start_date else dt.date(args.season, 3, 1)
    end = dt.date.fromisoformat(args.end_date) if args.end_date else min(TODAY, dt.date(args.season, 12, 31))
    limit = max(1, int(args.limit or 350))

    client = MLB()
    pairs: Dict[str, Dict[str, Any]] = {}
    hr_events: List[Dict[str, Any]] = []
    days_checked = 0
    games_checked = 0

    print(f"Running pair history cache: {start} → {end} | top_pairs limit {limit}", file=sys.stderr)
    for day in date_range(start, end):
        games = []
        try:
            games = client.schedule(day)
        except Exception as exc:
            print(f"⚠️ Schedule failed {day}: {exc}", file=sys.stderr)
            continue
        if not games:
            continue
        days_checked += 1
        day_hr_players: List[Dict[str, Any]] = []
        for game in games:
            game_pk = safe_int(game.get("gamePk"), 0)
            if not game_pk:
                continue
            status = str((game.get("status") or {}).get("detailedState") or "").lower()
            if not any(x in status for x in ["final", "completed", "game over"]):
                continue
            try:
                box = client.boxscore(game_pk)
                game_hrs = extract_game_hr_players(box, game, day.isoformat())
            except Exception as exc:
                print(f"⚠️ Boxscore failed {game_pk}: {exc}", file=sys.stderr)
                continue
            games_checked += 1
            day_hr_players.extend(game_hrs)
            hr_events.extend(game_hrs)
        # Unique player per day for pair logic. Multi-HR still one pair occurrence.
        by_pid: Dict[Any, Dict[str, Any]] = {}
        for p in day_hr_players:
            key = p.get("player_id") or p.get("name")
            old = by_pid.get(key)
            if old is None or safe_int(p.get("hr"), 0) > safe_int(old.get("hr"), 0):
                by_pid[key] = p
        unique = list(by_pid.values())
        if len(unique) < 2:
            continue
        for a, b in itertools.combinations(unique, 2):
            key = pair_key(a["name"], b["name"])
            if key not in pairs:
                pairs[key] = build_pair_record(a, b, day.isoformat())
            else:
                merge_pair(pairs[key], a, b, day.isoformat())

    manual_pair_hits_added = apply_manual_pair_hits(pairs, start, end)
    sorted_pairs = apply_boosts(pairs, end)
    top_pairs = sorted_pairs[:limit]

    cache_payload = {
        "schema": "mlb_pair_history_cache_v350",
        "season": args.season,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "top_pair_limit": limit,
        "pair_count": len(sorted_pairs),
        "hr_event_count": len(hr_events),
        "days_checked": days_checked,
        "games_checked": games_checked,
        "manual_pair_hits_added": manual_pair_hits_added,
        "pairs": {item["pair_key"]: item for item in sorted_pairs},
        "top_pairs": top_pairs,
    }
    summary_payload = {
        "schema": "mlb_pair_history_summary_v350",
        "season": args.season,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "generated_at": cache_payload["generated_at"],
        "top_pair_limit": limit,
        "pair_count": len(sorted_pairs),
        "hr_event_count": len(hr_events),
        "days_checked": days_checked,
        "games_checked": games_checked,
        "manual_pair_hits_added": manual_pair_hits_added,
        "top_pairs": top_pairs,
    }

    cache_path = OUT_DIR / "pair_history_cache.json"
    summary_path = OUT_DIR / "pair_history_summary.json"
    cache_path.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    # Root aliases help helper scripts find files even when called from scripts/.
    (ROOT_DIR / "pair_history_cache.json").write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
    (ROOT_DIR / "pair_history_summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    # Mirror into <repo_root>/outputs/ so the centralized site_data_sync.py picks them up in CI.
    if REPO_OUT_DIR != OUT_DIR:
        try:
            (REPO_OUT_DIR / "pair_history_cache.json").write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
            (REPO_OUT_DIR / "pair_history_summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
            print(f"📁 Mirrored pair history to {REPO_OUT_DIR}", file=sys.stderr)
        except Exception as exc:
            print(f"⚠️ Could not mirror to {REPO_OUT_DIR}: {exc}", file=sys.stderr)

    print(f"✅ Wrote {len(top_pairs)} top pairs / {len(sorted_pairs)} total pairs", file=sys.stderr)
    print(f"Saved: {cache_path}", file=sys.stderr)
    print(f"Saved: {summary_path}", file=sys.stderr)

    if not args.no_site_sync:
        copy_to_site(cache_path, summary_path, Path(args.dashboard_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
