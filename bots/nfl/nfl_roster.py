"""Every NFL roster -- active, practice squad, IR -- for the current week.

2026-09-24, Donovan: "I should be able to search every active player", both
sports. TUDDY's week file (nfl_week.json) is the ~560 men the model rates;
this is everyone else too (~2,500 rows: ACT active, RES reserve/IR, DEV
practice squad, plus the odd RET/CUT/EXE), with the gsis_id every other TUDDY
file is keyed on, so a man who is not in this week's pool still resolves in
search and in the live touchdown feed's name -> id join.

lib/nfl/dataSource.js has carried nflRosterPaths() for nfl_roster.json since
09-13 waiting for exactly this file. Shape matches what tdFeed.matchRoster()
reads: players[].{gsis_id, name, team}.

Source: nflverse weekly rosters via nflreadpy (same dependency the rest of
bots/nfl uses). One call.

    python nfl_roster.py --season 2026 --out ../../public/data/current --prefix nfl_
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

STATUS_WORD = {
    "ACT": "Active", "RES": "Injured reserve / reserve list", "DEV": "Practice squad",
    "RET": "Retired", "CUT": "Released", "EXE": "Exempt", "PUP": "PUP", "SUS": "Suspended",
    "NON": "Non-football injury", "UDF": "Unsigned draft pick", "TRC": "Practice squad (IR)",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "public" / "data" / "current"))
    ap.add_argument("--prefix", default="nfl_")
    a = ap.parse_args()

    import nflreadpy as nfl
    import polars as pl

    r = nfl.load_rosters_weekly([a.season])
    if r.is_empty():
        print("no weekly rosters published for this season yet", file=sys.stderr)
        return 1
    week = int(r["week"].max())
    cur = r.filter(pl.col("week") == week)
    cols = ["gsis_id", "full_name", "team", "position", "status", "jersey_number", "headshot_url",
            "years_exp", "height", "weight", "birth_date", "college", "espn_id"]
    have = [c for c in cols if c in cur.columns]
    players = []
    for row in cur.select(have).iter_rows(named=True):
        if not row.get("gsis_id") or not row.get("full_name"):
            continue
        status = row.get("status") or ""
        # retired / released / exempt are not "on a roster" in any sense the
        # site needs; they only add weight to the file.
        if status in ("RET", "CUT", "EXE"):
            continue
        players.append({
            "gsis_id": row["gsis_id"],
            "player_id": row["gsis_id"],
            "name": row["full_name"],
            "team": row.get("team"),
            "position": row.get("position"),
            "status": status,
            "status_word": STATUS_WORD.get(status, status),
            "jersey": row.get("jersey_number"),
            "headshot": row.get("headshot_url"),
            "years_exp": row.get("years_exp"),
            "espn_id": row.get("espn_id"),
        })
    players.sort(key=lambda p: (p["team"] or "", p["name"]))
    out = {
        "season": a.season, "week": week,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "nflverse rosters_weekly via nflreadpy",
        "n": len(players), "players": players,
    }
    dest = Path(a.out) / f"{a.prefix}roster.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    by = {}
    for p in players:
        by[p["status"]] = by.get(p["status"], 0) + 1
    print(f"{dest.name}: week {week}, {len(players)} players, statuses {by}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
