#!/usr/bin/env python3
"""
make_slim.py — shrink the bot's slate JSON for the Streamlit app.

Why this exists
---------------
mlb_dashboard.py writes public/data/current/today.json at ~76 MB. About 50 MB
of that is per-player raw logs (spray_chart, contact_log, batted_ball_log,
batter_pitch_type_profile, full pitch-mix tables). The dashboard never reads
those at the top level -- they're detail payloads for the old player modal.

Committing 76 MB files every run is what pushed the repo past 20 GB and made
Streamlit Cloud time out cloning it. This script writes a companion
`*_slim.json` (~1 MB) with every scalar field intact and only the heavy
nested logs dropped. The Streamlit app loads the slim file; the full file
stays out of git entirely.

Usage:
    python bots/make_slim.py                     # slims today + tomorrow
    python bots/make_slim.py --in path/to.json --out path/to_slim.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "public" / "data"
CURRENT_DIR = DATA_DIR / "current"

# Keys dropped from every player row. Measured against a real 246-player
# slate: these 14 keys account for 49.2 MB of the 50 MB payload. Everything
# else -- all scores, reasons, splits, pitcher stats, weather, park -- is
# kept verbatim, so adding new scalar fields to the bot needs no change here.
DROP_KEYS = {
    "spray_chart",              # 10.1 MB
    "contact_log",              # 10.1 MB
    "batted_ball_log",          # 10.1 MB
    "batter_pitch_type_profile",  # 9.9 MB
    "pitch_mix_matchup",        # 1.7 MB
    "pitch_type_summary",       # 1.3 MB
    "pitcher_pitch_mix",        # 1.0 MB
    "pitcher_pitch_mix_vs_rhb",  # 1.0 MB
    "pitcher_pitch_mix_vs_lhb",  # 1.0 MB
    "pitcher_lineup_spot_damage",   # 0.9 MB
    "pitcher_pitch_arsenal_detail",  # 0.3 MB
    "pitcher_pitch_type_summary_vs_rhb",
    "pitcher_pitch_type_summary_vs_lhb",
    "pitcher_lineup_zone_damage",
    "game_log",
    "pitcher_pitch_mix_debug",
}

# NOTE: bbe_profile is deliberately NOT dropped. It's only 0.19 MB, and it's
# where max_ev and avg_la actually live -- the dashboard's Leaders board falls
# back to bbe_profile.max_ev / bbe_profile.avg_la for its Max EV and Launch
# Angle leaderboards, so dropping it silently emptied two whole boards.

# Written to a per-player detail file instead of being thrown away, so the
# player detail view (spray chart, contact log, pitch-type profile) still
# works without putting 50 MB into the payload every visitor downloads.
#
# spray_chart, contact_log and batted_ball_log are byte-identical to each
# other on every player (verified 246/246 on a real slate) -- the bot writes
# the same batted-ball list under three names for backwards compatibility.
# Only spray_chart is stored; the app reads the other two as aliases of it.
# That alone cuts the detail payload from 64 MB to ~21 MB.
BATTER_DETAIL_KEYS = [
    "spray_chart",
    "batter_pitch_type_profile",
    "pitch_mix_matchup",
    "pitch_type_summary",
]

# Pitcher-level payloads are identical for every batter in the lineup facing
# him, so storing them per batter wrote the same ~150 KB nine times over.
# Keyed by pitcher_id instead: ~30 files per slate rather than 246 copies.
PITCHER_DETAIL_KEYS = [
    "pitcher_pitch_mix",
    "pitcher_pitch_mix_vs_rhb",
    "pitcher_pitch_mix_vs_lhb",
    "pitcher_pitch_arsenal_detail",
    "pitcher_lineup_spot_damage",
    "pitcher_lineup_zone_damage",
]


def slim_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        out.append({k: v for k, v in row.items() if k not in DROP_KEYS})
    return out


def write_detail_files(rows: List[Dict[str, Any]], out_dir: Path) -> int:
    """One small JSON per player holding only the heavy per-player logs.

    The dashboard's player detail view (spray chart, contact log, pitch-type
    profile) needs this data, but it's ~50 MB across a slate -- far too much
    to ship in the payload every page load. Splitting it per player means the
    app fetches ~40 KB on demand only when someone actually opens a player.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    pitcher_acc: Dict[Any, Dict[str, Any]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        pid = row.get("player_id")
        if pid not in (None, ""):
            detail = {k: row[k] for k in BATTER_DETAIL_KEYS if row.get(k)}
            if detail:
                detail["player_id"] = pid
                detail["game_pk"] = row.get("game_pk")
                detail["name"] = row.get("name")
                (out_dir / f"batter_{pid}.json").write_text(
                    json.dumps(detail, separators=(",", ":")), encoding="utf-8"
                )
                written += 1

        # Pitcher payloads are merged across every batter facing him rather
        # than taken from the first row seen. Not all rows carry every field
        # (9 of 246 were missing lineup-spot damage on a real slate), so
        # first-row-wins silently dropped the spot/zone damage tables for
        # whole pitchers depending on batting order.
        pitcher_id = row.get("pitcher_id")
        if pitcher_id not in (None, ""):
            existing = pitcher_acc.setdefault(pitcher_id, {
                "pitcher_id": pitcher_id,
                "pitcher_name": row.get("pitcher_name"),
            })
            for k in PITCHER_DETAIL_KEYS:
                if k not in existing and row.get(k):
                    existing[k] = row[k]

    for pitcher_id, pdetail in pitcher_acc.items():
        if len(pdetail) > 2:  # more than just the id/name stubs
            (out_dir / f"pitcher_{pitcher_id}.json").write_text(
                json.dumps(pdetail, separators=(",", ":")), encoding="utf-8"
            )
            written += 1

    return written


def slim_file(src: Path, dest: Path) -> bool:
    if not src.exists():
        print(f"skip (missing): {src}", file=sys.stderr)
        return False
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"skip (unreadable): {src}: {exc}", file=sys.stderr)
        return False

    detail_written = 0
    if isinstance(payload, list):
        detail_written = write_detail_files(payload, CURRENT_DIR / "detail")
        slimmed: Any = slim_rows(payload)
    elif isinstance(payload, dict) and isinstance(payload.get("players"), list):
        slimmed = dict(payload)
        slimmed["players"] = slim_rows(payload["players"])
    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        slimmed = dict(payload)
        slimmed["rows"] = slim_rows(payload["rows"])
    else:
        slimmed = payload

    dest.parent.mkdir(parents=True, exist_ok=True)
    # separators drop the pretty-print whitespace: another ~15% off the wire.
    dest.write_text(json.dumps(slimmed, separators=(",", ":")), encoding="utf-8")

    before = src.stat().st_size / 1e6
    after = dest.stat().st_size / 1e6
    extra = f" (+{detail_written} detail files)" if detail_written else ""
    print(f"slim: {src.name} {before:.1f} MB -> {dest.name} {after:.1f} MB{extra}", file=sys.stderr)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Write slim copies of the slate JSON for the Streamlit app.")
    ap.add_argument("--in", dest="src", default=None, help="Single input JSON")
    ap.add_argument("--out", dest="dest", default=None, help="Single output JSON")
    args = ap.parse_args()

    if args.src:
        src = Path(args.src)
        dest = Path(args.dest) if args.dest else src.with_name(src.stem + "_slim.json")
        return 0 if slim_file(src, dest) else 1

    ok = False
    for label in ("today", "tomorrow"):
        for base in (CURRENT_DIR, DATA_DIR):
            src = base / f"{label}.json"
            if src.exists():
                ok = slim_file(src, CURRENT_DIR / f"{label}_slim.json") or ok
                break
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
