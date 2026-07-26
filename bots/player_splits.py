#!/usr/bin/env python3
"""
player_splits.py — day/night, home/away, day-of-week and win/loss splits.

Why this exists
---------------
The scoring bot only carries vs-RHP / vs-LHP splits. It has nothing for
day vs night, home vs away, day of week, or how a hitter does in wins vs
losses, so the dashboard couldn't show any of it.

MLB's gameLog endpoint returns one row per game with `date`, `isHome`,
`isWin` and `game.dayNight` attached to that game's batting line. A single
request per hitter therefore yields all four split families at once --
cheaper and more flexible than statSplits, which needs separate sitCodes
and can't do day-of-week at all.

Output: public/data/current/splits/<slate>/<player_id>.json

Usage:
    python bots/player_splits.py --slate public/data/today_slate.json
    python bots/player_splits.py --slate ... --label tomorrow
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import requests

MLB_BASE = "https://statsapi.mlb.com/api/v1"
REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_DIR = REPO_ROOT / "public" / "data" / "current"
TIMEOUT = 30
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def num(v: Any, d: float = 0.0) -> float:
    try:
        if v in (None, "", ".---", "-.--"):
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def blank() -> Dict[str, float]:
    return {k: 0.0 for k in
            ("g", "pa", "ab", "h", "hr", "2b", "3b", "bb", "k", "rbi", "r", "tb")}


def add(acc: Dict[str, float], stat: Dict[str, Any]) -> None:
    acc["g"] += 1
    acc["pa"] += num(stat.get("plateAppearances"))
    acc["ab"] += num(stat.get("atBats"))
    acc["h"] += num(stat.get("hits"))
    acc["hr"] += num(stat.get("homeRuns"))
    acc["2b"] += num(stat.get("doubles"))
    acc["3b"] += num(stat.get("triples"))
    acc["bb"] += num(stat.get("baseOnBalls"))
    acc["k"] += num(stat.get("strikeOuts"))
    acc["rbi"] += num(stat.get("rbi"))
    acc["r"] += num(stat.get("runs"))
    acc["tb"] += num(stat.get("totalBases"))


def finish(acc: Dict[str, float]) -> Dict[str, Any]:
    """Rate stats computed from the totals, not averaged from per-game rates --
    averaging rates would weight a 1-AB game the same as a 5-AB game."""
    ab, pa, h, tb = acc["ab"], acc["pa"], acc["h"], acc["tb"]
    xbh = acc["2b"] + acc["3b"] + acc["hr"]
    obp_den = ab + acc["bb"]
    return {
        "G": int(acc["g"]), "PA": int(pa), "AB": int(ab), "H": int(h),
        "HR": int(acc["hr"]), "XBH": int(xbh), "BB": int(acc["bb"]),
        "K": int(acc["k"]), "RBI": int(acc["rbi"]), "R": int(acc["r"]),
        "AVG": round(h / ab, 3) if ab else 0.0,
        "OBP": round((h + acc["bb"]) / obp_den, 3) if obp_den else 0.0,
        "SLG": round(tb / ab, 3) if ab else 0.0,
        "OPS": round((h / ab if ab else 0) + (tb / ab if ab else 0), 3) if ab else 0.0,
        "ISO": round((tb - h) / ab, 3) if ab else 0.0,
        "HR/PA": round(acc["hr"] / pa, 4) if pa else 0.0,
        "K%": round(acc["k"] / pa, 3) if pa else 0.0,
    }


def build_splits(games: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[str, Dict[str, Dict[str, float]]] = {
        "day_night": defaultdict(blank),
        "home_away": defaultdict(blank),
        "day_of_week": defaultdict(blank),
        "win_loss": defaultdict(blank),
    }
    for g in games:
        stat = g.get("stat") or {}
        if not stat:
            continue
        game = g.get("game") or {}

        dn = str(game.get("dayNight") or "").lower()
        if dn in ("day", "night"):
            add(buckets["day_night"]["Day" if dn == "day" else "Night"], stat)

        if g.get("isHome") is not None:
            add(buckets["home_away"]["Home" if g["isHome"] else "Away"], stat)

        if g.get("isWin") is not None:
            add(buckets["win_loss"]["Win" if g["isWin"] else "Loss"], stat)

        try:
            d = dt.date.fromisoformat(str(g.get("date")))
            add(buckets["day_of_week"][DOW[d.weekday()]], stat)
        except Exception:
            pass

    out: Dict[str, Any] = {}
    for family, groups in buckets.items():
        out[family] = {k: finish(v) for k, v in groups.items()}
    # Keep weekdays in calendar order rather than whatever order they appeared.
    if out.get("day_of_week"):
        out["day_of_week"] = {d: out["day_of_week"][d] for d in DOW
                              if d in out["day_of_week"]}
    return out


def fetch_game_log(session: requests.Session, player_id: Any, season: int) -> List[Dict[str, Any]]:
    try:
        r = session.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": "hitting", "season": season},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        stats = (r.json() or {}).get("stats") or []
        return stats[0].get("splits", []) if stats else []
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Build per-hitter situational splits.")
    ap.add_argument("--slate", required=True, help="Path to the slate JSON")
    ap.add_argument("--label", default="today", choices=["today", "tomorrow"])
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    args = ap.parse_args()

    src = Path(args.slate)
    if not src.exists():
        print(f"ERROR: slate not found: {src}", file=sys.stderr)
        return 1
    rows = json.loads(src.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("players") or rows.get("rows") or []

    ids: Dict[Any, str] = {}
    for r in rows:
        pid = r.get("player_id")
        if pid not in (None, "") and pid not in ids:
            ids[pid] = r.get("name") or str(pid)

    out_dir = CURRENT_DIR / "splits" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    written = empty = 0
    for i, (pid, name) in enumerate(ids.items(), start=1):
        games = fetch_game_log(session, pid, args.season)
        if not games:
            # Fall back one season: early in a season a hitter may have no log
            # yet, and last year's splits beat showing nothing at all.
            games = fetch_game_log(session, pid, args.season - 1)
        if not games:
            empty += 1
            continue

        payload = build_splits(games)
        payload["player_id"] = pid
        payload["name"] = name
        payload["games_logged"] = len(games)
        payload["season"] = args.season
        (out_dir / f"{pid}.json").write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        written += 1

        if i % 25 == 0:
            print(f"  {i}/{len(ids)} hitters…", file=sys.stderr)
        time.sleep(0.08)  # be polite to the API

    print(f"splits: wrote {written} files ({empty} with no game log) -> {out_dir}",
          file=sys.stderr)
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
