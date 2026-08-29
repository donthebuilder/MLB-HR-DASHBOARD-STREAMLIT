#!/usr/bin/env python3
"""spray_archive.py — batter detail files for players NOT on tonight's slate.

Donovan (2026-08-28): "i need to be able to see the spray chart even if
they player isnt on. the bot." Today the spray chart, EV log and pitch
tabs on a player only work for the ~250-270 hitters make_slim.py wrote a
current/detail/{today|tomorrow}/batter_{pid}.json for -- which only ever
covers names on THAT NIGHT's slate. QuickSearch.js already resolves ANY
active MLB hitter by name straight off MLB's live people-search endpoint,
but PlayerModal.js never even tried to fetch a detail file for one (see its
own `if (player?.api_only) { setDetail(null); ... }` short-circuit), and
there was nowhere for it to succeed even if it had.

This script is the SAME statcast_batter() pull mlb_dashboard.py already
does for every slate hitter (see its spray_points construction — this
mirrors that field-for-field so SprayField.js needs no changes at all),
pointed at every hitter on an ACTIVE MLB ROSTER league-wide instead of just
tonight's slate, published to a slate-INDEPENDENT path:

    public/data/current/detail/archive/batter_{pid}.json

Deliberately its own file, not a change to mlb_dashboard.py or
spray_cache.py: those two are the real-time, must-not-break slate
pipeline, and this is a nice-to-have, best-effort, off-slate convenience
that has no business risking either one. If this script's whole run fails,
tonight's board is untouched.

NOT real-time, NOT exhaustive on day one, and it says so honestly:
  - ~750-900 hitters sit on active 26-man rosters league-wide at any time
    (30 teams), roughly 3x a single night's slate. Pulling that many
    Statcast games in one CI run doesn't fit a reasonable timeout at the
    same polite ~1/sec pace spray_cache.py's own zone profiles already use
    to avoid hammering Baseball Savant.
  - So this uses the exact cache + budget pattern spray_cache.py's zone
    profiles established: MAX_FETCHES_PER_RUN caps how many hitters get a
    FRESH Statcast pull in one run, everyone else this run skips is picked
    up on the NEXT scheduled run, and a per-player file already fresher
    than ARCHIVE_TTL_HOURS is left alone. Run on a schedule (see
    .github/workflows/spray-archive.yml), coverage fills in gradually
    across days rather than in one shot -- a hitter who was searched
    before he's been archived simply shows "not archived yet", which is
    the honest state, not a silent wrong chart.
  - The GitHub Actions cache (see the workflow) is what makes "gradually"
    actually true: a fresh CI checkout has no public/data at all (it's
    gitignored on main, same reason spray_cache.py's zone_cache/pitch dirs
    are restored from actions/cache rather than git), so without a
    restored cache every run would start from zero and only ever cover
    MAX_FETCHES_PER_RUN players, forever. Restoring the SAME directory
    this script publishes into from run to run is what lets coverage
    accumulate instead of resetting nightly.

One field IS knowingly dropped versus a slate player's real spray_chart:
`hr_class` (the "no-doubter / likely / maybe / cheap" xHR badge).
xhr_hr_class() reads a season-long, whole-slate-calibrated probability
table that only exists inside a completed mlb_dashboard.py run -- pulling
that table in here would mean importing (and trusting the import side
effects of) the entire 9,700-line production bot for one cosmetic label.
Every archived hit carries hr_class="" instead; SprayField.js already
treats an empty class as "no badge", not an error.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    from pybaseball import statcast_batter
    HAS_STATCAST = True
except ImportError:
    HAS_STATCAST = False
    print("WARNING: pybaseball not installed — spray_archive has nothing to do", file=sys.stderr)

MLB_BASE = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 30

TODAY = dt.date.today()
SEASON = TODAY.year
SEASON_START = dt.date(SEASON, 3, 1)

# How many hitters this run is allowed to pull FRESH from Statcast before it
# stops. This is the real timeout guard -- a scheduled run that's cold on
# every player still finishes in a few minutes; whatever it doesn't get to
# is exactly as "not archived yet" next run as it was this run, nothing is
# lost. Same idea as spray_cache.py's MAX_FETCHES_PER_RUN, tuned down: a
# batter's full spray chart is a heavier pull than a zone profile alone.
MAX_FETCHES_PER_RUN = 90

# A hitter's batted-ball history doesn't meaningfully change hour to hour --
# unlike the day's slate, nobody is checking an archived player's chart for
# what happened ten minutes ago. Generous TTL keeps this run's budget spent
# on players who've never been archived at all, not re-fetching yesterday's.
ARCHIVE_TTL_HOURS = 48

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
ARCHIVE_DIR = REPO_ROOT / "public" / "data" / "current" / "detail" / "archive"


def _get_json(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def active_team_ids() -> list[int]:
    """Every current MLB club, fetched live rather than hardcoded -- a
    relocation/rename (there was one in 2026) shouldn't need a code change
    here. Returns [] on any failure; the caller then simply archives
    nobody this run rather than guessing at a stale team list."""
    try:
        data = _get_json(f"{MLB_BASE}/teams", params={"sportId": 1, "activeStatus": "Y"})
        return [t["id"] for t in data.get("teams", []) if t.get("id")]
    except Exception as exc:
        print(f"active_team_ids: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def active_hitters(team_ids: list[int]) -> dict[int, dict[str, str]]:
    """{player_id: {name, team, bats}} for every active-roster position
    player league-wide. Pitchers (position code "1") are excluded -- this
    is a spray/EV chart archive, not a full-roster archive, and a pure
    pitcher has no batted-ball history worth a chart. A two-way player
    (position "Y", e.g. Ohtani) is kept; he bats too."""
    out: dict[int, dict[str, str]] = {}
    for team_id in team_ids:
        try:
            data = _get_json(f"{MLB_BASE}/teams/{team_id}/roster",
                              params={"rosterType": "active", "season": SEASON})
        except Exception as exc:
            print(f"  roster fetch failed for team {team_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        team_abbr = ""
        for entry in data.get("roster", []):
            person = entry.get("person") or {}
            pid = person.get("id")
            pos = (entry.get("position") or {}).get("code")
            if not pid or pos == "1":
                continue
            out[int(pid)] = {
                "name": person.get("fullName", ""),
                "team": team_abbr,
                "bats": "",
            }
    return out


def _clean_num(value: Any, digits: int = 2) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        v = float(value)
        return None if pd.isna(v) else round(v, digits)
    except Exception:
        return None


# Identical to mlb_dashboard.py's own spray_lane_from_hcx / spray_side_for_hand
# on purpose -- SprayField.js reads `lane`/`spray_side` from a slate player's
# real detail file today, so an archived player's file has to classify the
# same coordinate the same way or his chart would read differently than a
# slate teammate's for no reason a viewer could see.
def spray_lane_from_hcx(hc_x: Any) -> str:
    try:
        x = float(hc_x)
    except Exception:
        return ""
    if x < 90:
        return "LF"
    if x < 120:
        return "LCF"
    if x < 155:
        return "CF"
    if x < 185:
        return "RCF"
    return "RF"


def spray_side_for_hand(lane: str, stand: str) -> str:
    lane = str(lane or "").upper()
    stand = str(stand or "").upper()
    if not lane or stand not in {"L", "R"}:
        return "unknown"
    if stand == "R":
        if lane == "LF": return "pull"
        if lane == "LCF": return "pull_center"
        if lane == "CF": return "center"
        return "oppo"
    if lane == "RF": return "pull"
    if lane == "RCF": return "pull_center"
    if lane == "CF": return "center"
    return "oppo"


def dedupe_statcast_bbe(frame: "pd.DataFrame") -> "pd.DataFrame":
    if frame is None or len(frame) == 0:
        return frame
    cols = [c for c in ["game_pk", "at_bat_number", "pitch_number", "pitcher", "batter"] if c in frame.columns]
    if len(cols) >= 3:
        return frame.drop_duplicates(subset=cols, keep="last")
    cols = [c for c in ["game_date", "at_bat_number", "pitch_number", "pitcher", "batter"] if c in frame.columns]
    if len(cols) >= 3:
        return frame.drop_duplicates(subset=cols, keep="last")
    return frame.drop_duplicates(keep="last")


_NAME_CACHE: dict[int, str] = {}


def resolve_person_name(pid: Any, default: str = "—") -> str:
    """Best-effort opposing-pitcher name for a spray point, cached for the
    life of this run. A miss degrades to `default`, same as the slate
    pipeline's own resolve_mlb_person_name()."""
    try:
        pid_int = int(pid)
    except Exception:
        return default
    if not pid_int:
        return default
    if pid_int in _NAME_CACHE:
        return _NAME_CACHE[pid_int]
    try:
        data = _get_json(f"{MLB_BASE}/people/{pid_int}")
        name = (data.get("people") or [{}])[0].get("fullName", default)
    except Exception:
        name = default
    _NAME_CACHE[pid_int] = name
    return name


def spray_points_for(df: "pd.DataFrame") -> list[dict[str, Any]]:
    """Field-for-field the same shape as mlb_dashboard.py's own spray_points
    (its HitterRecord build, "Full BBE profile + spray chart" section) --
    see that file for the canonical version this mirrors. `hr_class` is the
    one field always left "" here; see the module docstring for why."""
    bbe = df[df.get("type") == "X"].copy() if "type" in df.columns else df.iloc[0:0].copy()
    bbe = dedupe_statcast_bbe(bbe)
    if not len(bbe):
        return []
    sort_cols = [c for c in ["game_date", "at_bat_number", "pitch_number"] if c in bbe.columns]
    bbe_sorted = bbe.sort_values(sort_cols, ascending=False, na_position="last") if sort_cols else bbe
    points: list[dict[str, Any]] = []
    for _, bp in bbe_sorted.head(120).iterrows():
        ev2 = _clean_num(bp.get("launch_speed"), 1)
        la2 = _clean_num(bp.get("launch_angle"), 1)
        dist2 = _clean_num(bp.get("hit_distance_sc"), 1)
        hc_x = _clean_num(bp.get("hc_x"), 2)
        hc_y = _clean_num(bp.get("hc_y"), 2)
        pitch_code = str(bp.get("pitch_type", "") or "")
        event = str(bp.get("events", "") or "")
        traj = str(bp.get("bb_type", "") or "")
        stand = str(bp.get("stand", "") or "")
        lane = spray_lane_from_hcx(hc_x)
        spray_side = spray_side_for_hand(lane, stand)
        pull_air = hc_x is not None and traj in {"fly_ball", "line_drive", "popup"} and \
            spray_side in {"pull", "pull_center"}
        points.append({
            "date": str(bp.get("game_date", ""))[:10],
            "pitch_type": pitch_code, "pitch_name": pitch_code,
            "event": event, "result": event,
            "bb_type": traj, "trajectory": traj,
            "ev": ev2, "launch_angle": la2, "la": la2,
            "distance": dist2, "hc_x": hc_x, "hc_y": hc_y,
            "lane": lane, "spray_side": spray_side, "stand": stand,
            "pitcher": resolve_person_name(bp.get("pitcher")),
            "pitcher_id": int(bp.get("pitcher")) if str(bp.get("pitcher") or "").isdigit() else 0,
            "arm": str(bp.get("p_throws", "?") or "?"),
            "pitch_velocity": _clean_num(bp.get("release_speed"), 1),
            "is_hr": event == "home_run",
            "hr_class": "",  # see module docstring — no whole-slate xHR table to classify against here
            "is_xbh": event in {"double", "triple", "home_run"},
            "is_barrel": bool(ev2 is not None and la2 is not None and ev2 >= 98 and 24 <= la2 <= 32),
            "is_hard_hit": bool(ev2 is not None and ev2 >= 95),
            "is_350_plus": bool(dist2 is not None and dist2 >= 350),
            "is_375_plus": bool(dist2 is not None and dist2 >= 375),
            "is_400_plus": bool(dist2 is not None and dist2 >= 400),
            "is_pull_air": bool(pull_air),
        })
    return points


def _cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    generated_str = cached.get("generated") if isinstance(cached, dict) else None
    if not generated_str:
        return False
    try:
        generated = dt.datetime.fromisoformat(generated_str)
    except ValueError:
        return False
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=dt.UTC)
    age_hours = (dt.datetime.now(dt.UTC) - generated).total_seconds() / 3600
    return age_hours <= ARCHIVE_TTL_HOURS


def build_one(pid: int, meta: dict[str, str]) -> dict[str, Any] | None:
    """One archive payload, or None on any failure (network, empty
    response, bad schema) -- the caller simply doesn't write a file for
    him this run, which is indistinguishable from "not archived yet"."""
    try:
        df = statcast_batter(SEASON_START.isoformat(), TODAY.isoformat(), pid)
    except Exception as exc:
        print(f"  statcast_batter {pid} ({meta.get('name','?')}): {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if df is None or len(df) == 0:
        return None
    bats = ""
    if "stand" in df.columns:
        modes = df["stand"].dropna()
        bats = str(modes.iloc[-1]) if len(modes) else ""
    points = spray_points_for(df)
    return {
        "player_id": pid,
        "player_name": meta.get("name", ""),
        "name": meta.get("name", ""),
        "type": "batter",
        "team": meta.get("team", ""),
        "bats": bats or meta.get("bats", "?") or "?",
        "source": "archive",
        "generated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        # Same three keys a slate detail file carries — SprayField.js and the
        # EV log read `spray_chart`; contact_log/batted_ball_log are aliases
        # kept for anything still reading the older names.
        "spray_chart": points,
        "batted_ball_log": points,
        "contact_log": points,
    }


def build_archive(limit: int | None = None, max_fetches: int = MAX_FETCHES_PER_RUN) -> dict[str, int]:
    stats = {"candidates": 0, "already_fresh": 0, "fetched": 0, "written": 0, "skipped_budget": 0, "empty": 0}
    if not HAS_STATCAST:
        return stats

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    team_ids = active_team_ids()
    hitters = active_hitters(team_ids)
    stats["candidates"] = len(hitters)
    if limit:
        hitters = dict(list(hitters.items())[:limit])

    fetched = 0
    for pid, meta in hitters.items():
        out_path = ARCHIVE_DIR / f"batter_{pid}.json"
        if _cache_fresh(out_path):
            stats["already_fresh"] += 1
            continue
        if fetched >= max_fetches:
            stats["skipped_budget"] += 1
            continue
        payload = build_one(pid, meta)
        fetched += 1
        stats["fetched"] += 1
        time.sleep(1.2)  # Statcast rate-limit safety, same pace as spray_cache.py
        if payload is None or not payload["spray_chart"]:
            stats["empty"] += 1
            continue
        out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        stats["written"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Archive batter spray/EV data for off-slate hitters")
    ap.add_argument("--limit", type=int, default=None, help="Only consider the first N candidate hitters (testing)")
    ap.add_argument("--max-fetches", type=int, default=MAX_FETCHES_PER_RUN)
    args = ap.parse_args()

    if not HAS_STATCAST:
        print("pybaseball not available — nothing to do", file=sys.stderr)
        return 0

    stats = build_archive(limit=args.limit, max_fetches=args.max_fetches)
    print(
        f"spray_archive: {stats['candidates']} active hitters league-wide | "
        f"{stats['already_fresh']} already fresh | {stats['fetched']} fetched this run "
        f"({stats['written']} written, {stats['empty']} came back with no batted balls) | "
        f"{stats['skipped_budget']} left for next run (budget)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
