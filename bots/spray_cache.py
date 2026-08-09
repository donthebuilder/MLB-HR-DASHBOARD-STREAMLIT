"""
Spray Chart Cache Bot

Builds a lightweight spray chart cache from the current MLB slate.
Also pulls Statcast zone profiles for each batter and their opposing pitcher
so HotZoneMap.js can render batter hot zones, pitcher damage zones, and
pitcher location tendency (kill zone overlay).

Inputs accepted:
  - A plain list of player rows
  - A dict with players / all_players / player_pool / slate_players / rows / picks
  - A dict with games containing players, away_players, or home_players

Outputs written to BOTH:
  - bots/outputs/spray_cache_YYYY-MM-DD.json
  - bots/outputs/spray_cache_latest.json
  - public/data/spray_cache_YYYY-MM-DD.json
  - public/data/spray_cache_latest.json
  - public/data/pitch/batter_XXXXX.json  (one per player — what SprayChart.js reads)
    └─ now includes: zone_profile (batter), pitcher_zone_profile (pitcher damage + tendency)

ZONE PROFILE CACHING:
  Zone profiles use a 120-day rolling Statcast lookback that barely changes
  day to day. Instead of re-pulling every batter/pitcher on every run, each
  profile is cached to public/data/zone_cache/{batter|pitcher}_{id}.json with
  a "generated" timestamp. Profiles younger than ZONE_CACHE_TTL_HOURS are
  reused as-is; only missing/stale profiles trigger a fresh Statcast call.
  This is what keeps the GitHub Actions job under the 15-minute ceiling.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import pandas as pd
    from pybaseball import statcast_batter, statcast_pitcher
    HAS_STATCAST = True
except ImportError:
    HAS_STATCAST = False
    print("WARNING: pybaseball not installed — zone profiles will be skipped", file=sys.stderr)

# ---------------------------------------------------------------------------
# Zone profile constants
# ---------------------------------------------------------------------------

# Statcast zone IDs:
#   1-9  = standard 3x3 strike zone (used for HR cell, 9-zone map)
#   11-14 = shadow zones just outside the plate edges
#   (zones 10, 15-17 exist but are rarely populated; we ignore them)
STRIKE_ZONES  = [1, 2, 3, 4, 5, 6, 7, 8, 9]
SHADOW_ZONES  = [11, 12, 13, 14]
ALL_ZONES     = STRIKE_ZONES + SHADOW_ZONES

# Minimum PA sample before we flag a cell as low-confidence
MIN_SAMPLE    = 8
ZONE_LOOKBACK = 120  # days — matches main bot lookback

# How long a cached zone profile is considered fresh before we rebuild it.
# 120-day rolling window barely moves day to day, so 24h is generous safety
# margin, not a stretch — most days nothing would even change month-over-month.
ZONE_CACHE_TTL_HOURS = 24

# How many missing/stale profiles this run is allowed to fetch from Statcast
# before stopping. This is the real timeout guard: a chunked workflow can run
# this script multiple times (one per matrix job) and each invocation only
# eats into its own slice of the budget, so no single job can run long enough
# to hit GitHub's 15-minute ceiling even on a fully-cold cache day.
MAX_FETCHES_PER_RUN = 60


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
OUTPUTS_DIR = REPO_ROOT / "bots" / "outputs"
PUBLIC_DATA_DIR = REPO_ROOT / "public" / "data"
PITCH_DIR = PUBLIC_DATA_DIR / "pitch"
ZONE_CACHE_DIR = PUBLIC_DATA_DIR / "zone_cache"

OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
PITCH_DIR.mkdir(parents=True, exist_ok=True)
ZONE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


BBE_FIELDS = (
    "date", "game_date", "event", "result", "bb_type",
    "ev", "launch_speed", "la", "launch_angle",
    "distance", "hit_distance_sc", "hc_x", "hc_y",
    "lane", "spray_side", "is_hr", "is_xbh", "is_barrel",
    "is_hard_hit", "is_350_plus", "is_375_plus", "is_400_plus",
    "is_pull_air", "pitch_type", "pitch_name", "pitch_velocity",
    "release_speed", "pitcher", "pitcher_name", "arm", "p_throws",
)

STAT_FIELDS = (
    "sample_bbe", "avg_ev", "max_ev", "avg_la", "avg_distance",
    "max_distance", "hard_hit_rate", "barrel_rate", "pull_rate",
    "fb_rate", "gb_rate", "ld_rate", "popup_rate", "dist_350_plus",
    "dist_375_plus", "dist_400_plus", "hr", "xbh", "best_lane",
    "lane_counts", "lane_damage_counts",
)

PLAYER_KEYS = (
    "players", "all_players", "player_pool", "slate_players", "rows", "picks",
    "top_picks", "hr_picks", "hit_picks", "hrr_picks", "contact_picks",
    "top_board", "hr_board", "alt_looks",
)


def clean_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return default
    text = str(value).strip()
    return text if text else default


def first_present(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def looks_like_player(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    has_name = any(row.get(k) for k in ("name", "player", "player_name"))
    has_id = any(row.get(k) for k in ("player_id", "id", "mlb_id"))
    has_stats = any(k in row for k in ("hr_score", "hit_score", "hrr_score", "spray_chart", "bbe_profile", "team"))
    return (has_name or has_id) and has_stats


def add_player(players: list[dict[str, Any]], row: Any, extra: dict[str, Any] | None = None) -> None:
    if not isinstance(row, dict):
        return
    if not looks_like_player(row):
        return
    merged = dict(extra or {})
    merged.update(row)
    players.append(merged)


def extract_players(raw: Any) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []

    if isinstance(raw, list):
        for row in raw:
            add_player(players, row)
        return dedupe_players(players)

    if not isinstance(raw, dict):
        return []

    for key in PLAYER_KEYS:
        for row in raw.get(key) or []:
            add_player(players, row)

    for game in raw.get("games") or []:
        if not isinstance(game, dict):
            continue
        game_extra = {
            "game_pk": game.get("game_pk") or game.get("game_id"),
            "game_time": game.get("game_time") or game.get("start_time"),
            "venue_name": game.get("venue_name") or game.get("venue"),
            "away": game.get("away"),
            "home": game.get("home"),
        }
        for row in game.get("players") or []:
            add_player(players, row, game_extra)
        for row in game.get("away_players") or []:
            extra = dict(game_extra)
            extra["team"] = row.get("team") or game.get("away")
            extra["opponent"] = row.get("opponent") or game.get("home")
            add_player(players, row, extra)
        for row in game.get("home_players") or []:
            extra = dict(game_extra)
            extra["team"] = row.get("team") or game.get("home")
            extra["opponent"] = row.get("opponent") or game.get("away")
            add_player(players, row, extra)

    if not players:
        def walk(node: Any, depth: int = 0, extra: dict[str, Any] | None = None) -> None:
            if depth > 4 or node is None:
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1, extra)
                return
            if not isinstance(node, dict):
                return
            local = dict(extra or {})
            for key in ("game_pk", "game_id", "game_time", "start_time", "away", "home", "venue_name", "venue"):
                if node.get(key) is not None:
                    local[key] = node.get(key)
            add_player(players, node, local)
            for key, value in node.items():
                if key in ("weather", "metadata"):
                    continue
                walk(value, depth + 1, local)
        walk(raw)

    return dedupe_players(players)


def dedupe_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in players:
        pid = clean_str(first_present(row, "player_id", "id", "mlb_id", default=""))
        name = clean_str(first_present(row, "name", "player_name", "player", default=""))
        game = clean_str(first_present(row, "game_pk", "game_id", "game_time", "team", default=""))
        key = f"{pid or name}|{game}"
        if not key.strip("|"):
            continue
        prev = by_key.get(key)
        prev_score = float(first_present(prev or {}, "hr_score", default=0) or 0)
        score = float(first_present(row, "hr_score", default=0) or 0)
        if prev is None or score >= prev_score:
            by_key[key] = row
    return list(by_key.values())


def find_latest_slate() -> Path | None:
    patterns = [
        "public/data/today_slate.json",
        "public/data/today.json",
        "public/data/slate_today.json",
        "bots/outputs/today_slate.json",
        "bots/outputs/today.json",
        "bots/outputs/*-today.json",
        "bots/outputs/*today*.json",
        "outputs/*-today.json",
        "outputs/*today*.json",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(Path(p) for p in glob.glob(str(REPO_ROOT / pattern)))
    candidates = [p for p in candidates if p.exists() and p.stat().st_size > 0]
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0] if candidates else None


def extract_date(path: Path, raw: Any) -> str:
    for source in (path.name, clean_str((raw or {}).get("date") if isinstance(raw, dict) else ""), clean_str((raw or {}).get("slate_date") if isinstance(raw, dict) else "")):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", source or "")
        if match:
            return match.group(1)
    return dt.date.today().isoformat()


def normalize_bbe(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    row = {key: event.get(key) for key in BBE_FIELDS if key in event}
    if "ev" not in row and "launch_speed" in row:
        row["ev"] = row.get("launch_speed")
    if "la" not in row and "launch_angle" in row:
        row["la"] = row.get("launch_angle")
    if "distance" not in row and "hit_distance_sc" in row:
        row["distance"] = row.get("hit_distance_sc")
    if "pitch_velocity" not in row and "release_speed" in row:
        row["pitch_velocity"] = row.get("release_speed")
    if "arm" not in row and "p_throws" in row:
        row["arm"] = row.get("p_throws")
    # stand = batter handedness on this PA (populated in player_output after normalize)
    return row


def extract_stats(player: dict[str, Any]) -> dict[str, Any]:
    profile = player.get("bbe_profile") if isinstance(player.get("bbe_profile"), dict) else {}
    out = {key: profile.get(key) for key in STAT_FIELDS if key in profile}
    aliases = {
        "avg_ev": ("recent_ev", "avg_ev"),
        "max_ev": ("max_ev",),
        "avg_la": ("recent_la", "avg_la"),
        "hard_hit_rate": ("recent_hard_hit_rate", "hard_hit_rate"),
        "barrel_rate": ("recent_barrel_rate", "barrel_rate"),
        "pull_rate": ("recent_pull_rate", "pull_rate"),
        "fb_rate": ("recent_fb_rate", "fb_rate"),
        "dist_350_plus": ("recent_350_num", "l20pa_350_num", "hits_350_plus"),
        "dist_375_plus": ("recent_375_num", "l20pa_375_num", "hits_375_plus"),
        "dist_400_plus": ("recent_400_num", "l20pa_400_num", "hits_400_plus"),
        "hr": ("last5_hr", "season_hr"),
        "xbh": ("last5_xbh", "season_xbh"),
    }
    for target, keys in aliases.items():
        if out.get(target) is None:
            out[target] = first_present(player, *keys, default=None)
    sample = first_present(player, "bbe_count", "l20pa_bbe", default=None)
    if out.get("sample_bbe") is None and sample is not None:
        out["sample_bbe"] = sample
    return out


def player_output(player: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    pid = first_present(player, "player_id", "id", "mlb_id", default=None)
    name = clean_str(first_present(player, "name", "player_name", "player", default=""))
    if pid is None and not name:
        return None

    key = str(pid) if pid is not None else name.lower().replace(" ", "_")
    spray = player.get("spray_chart") or player.get("batted_ball_log") or player.get("contact_log") or []
    if not isinstance(spray, list):
        spray = []

    bats = clean_str(first_present(player, "bats", "stand", "bat_side", default="?"), "?")
    raw_bbe = [normalize_bbe(event) for event in spray if isinstance(event, dict)]
    # stamp batter stand onto every hit so SprayChart.js hand-filter works
    for hit in raw_bbe:
        if not hit.get("stand"):
            hit["stand"] = bats
    bbe = raw_bbe

    out = {
        "player_id": pid,
        "name": name,
        "team": clean_str(first_present(player, "team", "team_abbr", "batting_team", default="")),
        "opponent": clean_str(first_present(player, "opponent", "opp", "pitcher_team", default="")),
        "bats": bats,
        "lineup_spot": first_present(player, "lineup_spot", "batting_order", default=None),
        "lineup_confirmed": bool(first_present(player, "lineup_confirmed", default=False)),
        "pitcher": clean_str(first_present(player, "pitcher_name", "pitcher", "opposing_pitcher", default="")),
        "pitcher_id": first_present(player, "pitcher_id", "opposing_pitcher_id", default=None),
        "pitcher_hand": clean_str(first_present(player, "pitcher_throws", "p_throws", "throws", default="")),
        "venue": clean_str(first_present(player, "venue_name", "venue", default="")),
        "game_time": clean_str(first_present(player, "game_time", "start_time", default="")),
        "stats": extract_stats(player),
        "bbe": bbe,
        # These fields are read directly by SprayChart.js
        "spray_chart": bbe,
        "batted_ball_log": bbe,
        "contact_log": bbe,
        # Zone profiles — populated later in build_spray_cache
        "zone_profile": None,
        "pitcher_zone_profile": None,
    }
    return key, out


def build_spray_cache(slate_path: Path) -> dict[str, Any]:
    print(f"Loading slate: {slate_path}", file=sys.stderr)
    with slate_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    players = extract_players(raw)
    slate_date = extract_date(slate_path, raw)
    print(f"Slate date: {slate_date}", file=sys.stderr)
    print(f"Players found: {len(players)}", file=sys.stderr)

    players_out: dict[str, dict[str, Any]] = {}
    total_bbe = 0
    with_spray = 0

    for player in players:
        built = player_output(player)
        if not built:
            continue
        key, out = built
        total_bbe += len(out["bbe"])
        if out["bbe"]:
            with_spray += 1
        players_out[key] = out

    print(f"Players with spray: {with_spray}/{len(players_out)}", file=sys.stderr)
    print(f"Total BBE events: {total_bbe}", file=sys.stderr)

    # --- Zone profile pass (Statcast pull per player, cache-aware) ---
    if HAS_STATCAST:
        print("Building zone profiles via Statcast (cache-aware)...", file=sys.stderr)
        zone_hits = 0
        cache_hits = 0
        fresh_fetches = 0
        skipped_budget = 0

        for key, out in players_out.items():
            batter_zone, pitcher_zone, stats = build_zone_profiles(out, fresh_fetches < MAX_FETCHES_PER_RUN)
            cache_hits += stats["cache_hits"]
            fresh_fetches += stats["fresh_fetches"]
            skipped_budget += stats["skipped_budget"]

            if batter_zone:
                out["zone_profile"] = batter_zone
                zone_hits += 1
            if pitcher_zone:
                out["pitcher_zone_profile"] = pitcher_zone

        print(
            f"Zone profiles: {zone_hits}/{len(players_out)} populated "
            f"({cache_hits} from cache, {fresh_fetches} freshly fetched, "
            f"{skipped_budget} skipped — over MAX_FETCHES_PER_RUN budget)",
            file=sys.stderr,
        )
        if skipped_budget > 0:
            print(
                f"NOTE: {skipped_budget} profiles were skipped this run because "
                f"MAX_FETCHES_PER_RUN={MAX_FETCHES_PER_RUN} was hit. Run again "
                f"(or run the next chunk) to pick up the remainder — they'll "
                f"still be missing/stale next run so nothing is lost.",
                file=sys.stderr,
            )
    else:
        print("Skipping zone profiles (pybaseball not available)", file=sys.stderr)

    return {
        "schema_version": "spray_v1",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "slate_date": slate_date,
        "source_slate": str(slate_path),
        "player_count": len(players_out),
        "players_with_spray": with_spray,
        "total_bbe": total_bbe,
        "players": players_out,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    size_kb = path.stat().st_size // 1024
    print(f"Written: {path} ({size_kb} KB)", file=sys.stderr)


def write_individual_batter_files(players_out: dict[str, dict[str, Any]]) -> int:
    """Write one batter_XXXXX.json per player into public/data/pitch/.

    This is what SprayChart.js fetches at /data/pitch/batter_{player_id}.json.
    """
    written = 0
    for key, player in players_out.items():
        pid = player.get("player_id")
        if not pid:
            continue
        # Shape the file to match what SprayChart.js expects:
        # { spray_chart: [...], batted_ball_log: [...], contact_log: [...] }
        bbe_list = player.get("bbe", [])
        batter_payload = {
            "player_id": pid,
            "player_name": player.get("name", ""),
            "type": "batter",
            "team": player.get("team", ""),
            "bats": player.get("bats", "?"),
            "pitcher": player.get("pitcher", ""),
            "pitcher_id": player.get("pitcher_id"),
            "pitcher_hand": player.get("pitcher_hand", ""),
            "venue": player.get("venue", ""),
            "lineup_spot": player.get("lineup_spot"),
            # these three keys are what SprayChart.js reads
            "spray_chart": bbe_list,
            "batted_ball_log": bbe_list,
            "contact_log": bbe_list,
            "stats": player.get("stats", {}),
            # pass through pitch profile if present in slate row
            "batter_pitch_type_profile": player.get("batter_pitch_type_profile", {}),
            "pitcher_arsenal": player.get("pitcher_arsenal", {}),
            "pitcher_pitch_usage": player.get("pitcher_pitch_usage", {}),
            "pitcher_pitch_mix": player.get("pitcher_pitch_mix", {}),
            "pitcher_name": player.get("pitcher", ""),
            "pitcher_throws": player.get("pitcher_hand", ""),
            "pitcher_era": player.get("pitcher_era", ""),
            "wind_mph": player.get("weather_wind_mph") or player.get("wind_mph"),
            "wind_deg": player.get("weather_wind_deg") or player.get("wind_deg"),
            "wind_direction_label": player.get("wind_direction_label", ""),
            "venue_name": player.get("venue", ""),
            # Zone profiles for HotZoneMap.js
            "zone_profile":         player.get("zone_profile"),
            "pitcher_zone_profile": player.get("pitcher_zone_profile"),
        }
        write_json(PITCH_DIR / f"batter_{pid}.json", batter_payload)
        written += 1
    return written



ZONES_DIR = PUBLIC_DATA_DIR / "current" / "zones" / "today"


def write_zone_files(players_out: dict[str, dict[str, Any]]) -> int:
    """One small file per hitter holding only the zone profiles.

    Deliberately NOT merged into current/detail/<slate>/batter_<id>.json. This
    job runs on a fresh checkout where public/data is gitignored and therefore
    empty, so anything written into detail/ here would be a stub with no
    spray_chart -- and publish_data.sh copies detail/ as a whole directory, so
    publishing those stubs would replace the real detail files and blank every
    spray chart, pitch profile and EV log on the site. A directory this job
    alone owns has no such failure mode.
    """
    written = 0
    for player in players_out.values():
        pid = player.get("player_id")
        if not pid:
            continue
        zp = player.get("zone_profile")
        pzp = player.get("pitcher_zone_profile")
        if not zp and not pzp:
            continue
        payload = {"player_id": pid, "name": player.get("name", "")}
        if zp:
            payload["zone_profile"] = zp
        if pzp:
            payload["pitcher_zone_profile"] = pzp
        write_json(ZONES_DIR / f"batter_{pid}.json", payload)
        written += 1
    return written

def build_fence_board(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """🧱🚀 FENCE BOARD (2026-08-08, Donovan: "people who pull in the
    direction and have hit it out or on the fence line in the last 5–15
    games"). Pure Statcast landing data — measured distances and Savant's
    own pull-air flag, no invented numbers. Per slate hitter, over his last
    15 GAME DATES of tracked batted balls:
      over_ct       375+ ft — over the fence comfortably, any direction
      fence_ct      pull-air 320–374 ft — the wall-scraper zone, where a
                    short pull porch turns outs into homers
      deep_pull_ct  pull-air 350+ — the sharpest single shape for HR
      hr_ct         actual homers in the window
    The site joins this with the league's real wall dimensions (fieldInfo)
    to say: his fence-line balls vs TONIGHT's pull wall."""
    rows = []
    for pid, player in (payload.get("players") or {}).items():
        bbe = player.get("bbe") or []
        if not bbe:
            continue
        dates = sorted({str(h.get("date") or "")[:10] for h in bbe if h.get("date")}, reverse=True)[:15]
        if not dates:
            continue
        window = [h for h in bbe if str(h.get("date") or "")[:10] in set(dates)]
        def dist(h):
            try:
                return float(h.get("distance") or h.get("hit_distance_sc") or 0)
            except (TypeError, ValueError):
                return 0.0
        over = [h for h in window if dist(h) >= 375]
        fence = [h for h in window if h.get("is_pull_air") and 320 <= dist(h) < 375]
        deep_pull = [h for h in window if h.get("is_pull_air") and dist(h) >= 350]
        hrs = [h for h in window if h.get("is_hr")]
        pull_air_ct = sum(1 for h in window if h.get("is_pull_air"))
        if not (over or fence or deep_pull):
            continue
        rows.append({
            "player_id": pid, "name": player.get("name", ""),
            "team": player.get("team", ""), "venue": player.get("venue", ""),
            "bats": player.get("bats", "?"),
            "games": len(dates), "bbe": len(window),
            "over_ct": len(over), "fence_ct": len(fence),
            "deep_pull_ct": len(deep_pull), "hr_ct": len(hrs),
            "pull_air_ct": pull_air_ct,
            "longest": max((dist(h) for h in window), default=0),
        })
    rows.sort(key=lambda r: (r["deep_pull_ct"] * 3 + r["fence_ct"] * 1.5 + r["over_ct"]), reverse=True)
    return rows


def write_outputs(payload: dict[str, Any]) -> None:
    date_str = payload["slate_date"]

    # fence board — compact, one file for the whole slate
    fence = {"slate_date": date_str,
             "generated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
             "rows": build_fence_board(payload)}
    for base in (OUTPUTS_DIR, PUBLIC_DATA_DIR / "current"):
        base.mkdir(parents=True, exist_ok=True)
        write_json(base / "fence_board.json", fence)
    print(f"Written: fence_board.json ({len(fence['rows'])} hitters with fence-line contact)", file=sys.stderr)

    # Existing combined cache files
    for base in (OUTPUTS_DIR, PUBLIC_DATA_DIR):
        write_json(base / f"spray_cache_{date_str}.json", payload)
        write_json(base / "spray_cache_latest.json", payload)

    # Individual batter files for SprayChart.js
    written = write_individual_batter_files(payload.get("players", {}))
    print(f"Written: {written} individual batter files to public/data/pitch/", file=sys.stderr)
    zoned = write_zone_files(payload.get("players", {}))
    print(f"Written: {zoned} zone files to public/data/current/zones/today/", file=sys.stderr)



# ---------------------------------------------------------------------------
# Zone profile builders (Statcast) — now cache-aware
# ---------------------------------------------------------------------------

def _start_date(lookback_days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")


def _end_date() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _zone_cell(df: "pd.DataFrame", zone_id: int) -> dict[str, Any]:
    """Aggregate one Statcast zone cell from a pitcher or batter DataFrame."""
    zdf = df[df["zone"] == zone_id]
    pa = len(zdf)

    if pa == 0:
        return {"zone": zone_id, "pa": 0, "bbe": 0, "hr": 0, "hr_rate": None,
                "ba": None, "xwoba": None, "xslg": None, "low_sample": True}

    # BA: hits / at-bats (exclude walks, HBP, sac flies from denominator)
    ab_events = {"single", "double", "triple", "home_run", "field_out", "grounded_into_double_play",
                 "double_play", "triple_play", "strikeout", "strikeout_double_play",
                 "fielders_choice", "fielders_choice_out", "force_out", "other_out"}
    hit_events = {"single", "double", "triple", "home_run"}

    bbe_mask = zdf["type"] == "X"  # batted ball events only
    bbe_count = int(bbe_mask.sum())

    ab_mask = zdf["events"].isin(ab_events)
    ab_count = int(ab_mask.sum())
    hit_count = int(zdf["events"].isin(hit_events).sum())
    hr_count = int((zdf["events"] == "home_run").sum())

    ba = round(hit_count / ab_count, 3) if ab_count > 0 else None
    hr_rate = round(hr_count / pa, 3) if pa > 0 else None

    xwoba_col = "estimated_woba_using_speedangle" if "estimated_woba_using_speedangle" in zdf.columns else None
    xslg_col  = "estimated_slg_using_speedangle"  if "estimated_slg_using_speedangle"  in zdf.columns else None

    xwoba = round(float(zdf[xwoba_col].dropna().mean()), 3) if xwoba_col and not zdf[xwoba_col].dropna().empty else None
    xslg  = round(float(zdf[xslg_col].dropna().mean()),  3) if xslg_col  and not zdf[xslg_col].dropna().empty  else None

    # Batted-ball shape per zone (audit #11, 2026-08-08): the site's zone-match
    # readout wants gb/fly alongside ba. Rates are over BBE, not PA — a zone a
    # hitter whiffs in isn't a ground-ball zone, it's a whiff zone. bb_type is
    # Statcast's own label; guard because older pulls may not carry the column.
    gb_rate = fb_rate = None
    if "bb_type" in zdf.columns and bbe_count > 0:
        bb = zdf.loc[bbe_mask, "bb_type"].dropna()
        if len(bb):
            gb_rate = round(int((bb == "ground_ball").sum()) / bbe_count, 3)
            fb_rate = round(int(bb.isin(["fly_ball", "popup"]).sum()) / bbe_count, 3)

    return {
        "zone":       zone_id,
        "pa":         pa,
        "bbe":        bbe_count,
        "hr":         hr_count,
        "hr_rate":    hr_rate,
        "ba":         ba,
        "xwoba":      xwoba,
        "xslg":       xslg,
        "gb_rate":    gb_rate,
        "fb_rate":    fb_rate,
        "low_sample": pa < MIN_SAMPLE,
    }


def _cache_path(kind: str, player_id: int) -> Path:
    return ZONE_CACHE_DIR / f"{kind}_{player_id}.json"


def _read_cache(kind: str, player_id: int) -> dict[str, Any] | None:
    """Return a cached zone profile if it exists and is younger than ZONE_CACHE_TTL_HOURS."""
    path = _cache_path(kind, player_id)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    generated_str = cached.get("generated")
    if not generated_str:
        return None
    try:
        generated = dt.datetime.fromisoformat(generated_str)
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=dt.UTC)

    age_hours = (dt.datetime.now(dt.UTC) - generated).total_seconds() / 3600
    if age_hours > ZONE_CACHE_TTL_HOURS:
        return None
    # Schema bump (audit #11): profiles cached before gb_rate/fb_rate existed
    # would otherwise be served until TTL expiry and the site would show a
    # zone grid with no batted-ball shape for days. A stale schema is a miss.
    cells = cached.get("zones_9") or cached.get("damage") or []
    if cells and "gb_rate" not in cells[0]:
        return None
    return cached


def _write_cache(kind: str, player_id: int, profile: dict[str, Any]) -> None:
    write_json(_cache_path(kind, player_id), profile)


def build_batter_zone_profile(player_id: int, lookback: int = ZONE_LOOKBACK) -> dict[str, Any] | None:
    """Return per-zone hitting stats for a batter over the last `lookback` days.

    Returns a dict with:
      zones_9  — 9-cell strike zone (HR focus)
      zones_13 — 13-cell grid (9 + 4 shadow, BA / xwOBA / xSLG focus)
    """
    if not HAS_STATCAST:
        return None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = statcast_batter(_start_date(lookback), _end_date(), player_id=player_id)
        if df is None or df.empty or "zone" not in df.columns:
            return None

        df["zone"] = pd.to_numeric(df["zone"], errors="coerce")
        df = df.dropna(subset=["zone"])
        df["zone"] = df["zone"].astype(int)

        nine   = [_zone_cell(df, z) for z in STRIKE_ZONES]
        shadow = [_zone_cell(df, z) for z in SHADOW_ZONES]

        return {
            "player_id":  player_id,
            "lookback":   lookback,
            "total_pa":   len(df),
            "zones_9":    nine,
            "zones_13":   nine + shadow,
            "generated":  dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    except Exception as exc:
        exc_str = str(exc)
        if "tokeniz" in exc_str.lower() or "field" in exc_str.lower() or "saw" in exc_str.lower():
            print(f"  zone profile batter {player_id}: bad CSV from Statcast (skipping)", file=sys.stderr)
        else:
            print(f"  zone profile batter {player_id}: {exc}", file=sys.stderr)
        return None


def build_pitcher_zone_profile(pitcher_id: int, lookback: int = ZONE_LOOKBACK) -> dict[str, Any] | None:
    """Return per-zone damage stats AND location tendency for a pitcher.

    damage  — what the pitcher *allows* per zone (HR rate, xwOBA, xSLG)
    tendency — share of pitches thrown to each zone (location heat)
    kill_zones — zones where both damage rate AND tendency are high
    """
    if not HAS_STATCAST:
        return None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = statcast_pitcher(_start_date(lookback), _end_date(), player_id=pitcher_id)
        if df is None or df.empty or "zone" not in df.columns:
            return None

        df["zone"] = pd.to_numeric(df["zone"], errors="coerce")
        df = df.dropna(subset=["zone"])
        df["zone"] = df["zone"].astype(int)

        total_pitches = len(df)

        damage   = {}
        tendency = {}

        for z in ALL_ZONES:
            cell = _zone_cell(df, z)
            damage[z] = cell

            zone_pitches = int((df["zone"] == z).sum())
            tendency[z] = {
                "zone":    z,
                "pitches": zone_pitches,
                "pct":     round(zone_pitches / total_pitches, 3) if total_pitches > 0 else None,
            }

        # Kill zones: top-3 damage (xwOBA) that also land in top-5 tendency pitches
        damage_ranked   = sorted(
            [z for z in ALL_ZONES if damage[z]["xwoba"] is not None],
            key=lambda z: damage[z]["xwoba"], reverse=True
        )
        tendency_ranked = sorted(
            ALL_ZONES, key=lambda z: tendency[z]["pitches"], reverse=True
        )
        top_damage   = set(damage_ranked[:5])
        top_tendency = set(tendency_ranked[:5])
        kill_zones   = sorted(top_damage & top_tendency)

        return {
            "pitcher_id":    pitcher_id,
            "lookback":      lookback,
            "total_pitches": total_pitches,
            "damage":        [damage[z]   for z in ALL_ZONES],
            "tendency":      [tendency[z] for z in ALL_ZONES],
            "kill_zones":    kill_zones,
            "generated":     dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    except Exception as exc:
        exc_str = str(exc)
        if "tokeniz" in exc_str.lower() or "field" in exc_str.lower() or "saw" in exc_str.lower():
            print(f"  zone profile pitcher {pitcher_id}: bad CSV from Statcast (skipping)", file=sys.stderr)
        else:
            print(f"  zone profile pitcher {pitcher_id}: {exc}", file=sys.stderr)
        return None


def build_zone_profiles(
    player: dict[str, Any], budget_remaining: bool
) -> tuple[dict | None, dict | None, dict[str, int]]:
    """Pull batter + pitcher zone profiles for a player row, using the cache first.

    Returns (batter_profile, pitcher_profile, stats) where stats tracks
    cache_hits / fresh_fetches / skipped_budget for run-level logging.
    """
    pid = first_present(player, "player_id", "id", "mlb_id", default=None)
    pitcher_id = first_present(player, "pitcher_id", "opposing_pitcher_id", default=None)

    stats = {"cache_hits": 0, "fresh_fetches": 0, "skipped_budget": 0}
    batter_profile = None
    pitcher_profile = None

    if pid:
        pid_int = int(pid)
        cached = _read_cache("batter", pid_int)
        if cached:
            batter_profile = cached
            stats["cache_hits"] += 1
        elif budget_remaining:
            batter_profile = build_batter_zone_profile(pid_int)
            stats["fresh_fetches"] += 1
            if batter_profile:
                _write_cache("batter", pid_int, batter_profile)
            time.sleep(1.2)  # Statcast rate limit safety
        else:
            stats["skipped_budget"] += 1

    if pitcher_id:
        pitcher_id_int = int(pitcher_id)
        cached = _read_cache("pitcher", pitcher_id_int)
        if cached:
            pitcher_profile = cached
            stats["cache_hits"] += 1
        elif budget_remaining:
            pitcher_profile = build_pitcher_zone_profile(pitcher_id_int)
            stats["fresh_fetches"] += 1
            if pitcher_profile:
                _write_cache("pitcher", pitcher_id_int, pitcher_profile)
            time.sleep(1.2)  # Statcast rate limit safety
        else:
            stats["skipped_budget"] += 1

    return batter_profile, pitcher_profile, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Build spray chart cache from today's slate")
    parser.add_argument("--slate", type=str, default=None, help="Path to today slate JSON")
    args = parser.parse_args()

    slate_path = Path(args.slate).resolve() if args.slate else find_latest_slate()
    if not slate_path or not slate_path.exists():
        print("ERROR: No slate file found. Pass --slate path/to/today_slate.json", file=sys.stderr)
        return 1

    payload = build_spray_cache(slate_path)
    if payload["player_count"] == 0:
        print("ERROR: Slate loaded, but no player rows were extracted.", file=sys.stderr)
        return 1

    write_outputs(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
