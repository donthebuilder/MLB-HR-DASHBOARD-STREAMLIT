#!/usr/bin/env python3
"""nfl_logs_split.py -- per-player game-log files + a slim team-games index.

Why (bot task 3, 2026-10-11): TUDDY parsed nfl_logs.json (1.37 MB) on every tab
because the header's expected-touchdowns tile and the "Best game" tile are fit
from team touchdown totals that the site summed out of every player's log. The
sums are team facts, so they ship once, small; a player's own log ships on its
own and is fetched when his card or page opens.

ADDITIVE. nfl_logs.json is still written, byte for byte as before; nothing here
rewrites a published row. Three outputs, all in `out`:

  <prefix>logs_index.json    {"bars", "season", "team_games": [...], "players": n}
        team_games rows are EXACTLY what lib/nfl/teamTdModel.js teamGames()
        builds from the full log -- {s, w, tm, opp, td, d, h}, one per club per
        game, td = the sum of that club's tracked players' g_td, oldest first --
        so fitTeams()/slateTotals() run unchanged on them and give the same
        numbers.
  <prefix>logs/<gsis_id>.json   logs[gsis_id] as-is: {"log": [...], "rates": {...}}
        (same field names as the entry in nfl_logs.json).

The index is built from the SAME on-slate-filtered logs nfl_logs.json carries,
so the totals are identical to what the site fits today.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def team_games(logs: dict) -> list[dict]:
    """teamGames() of lib/nfl/teamTdModel.js, in Python."""
    rows: dict[tuple, dict] = {}
    for blk in (logs or {}).values():
        for e in (blk or {}).get("log") or []:
            if not isinstance(e, dict) or not e.get("tm") or not e.get("opp"):
                continue
            s, w = e.get("s"), e.get("w")
            if not isinstance(s, (int, float)) or not isinstance(w, (int, float)):
                continue
            key = (s, w, e["tm"])
            r = rows.get(key)
            if r is None:
                h = e.get("h")
                r = rows[key] = {"s": s, "w": w, "tm": e["tm"], "opp": e["opp"], "td": 0.0,
                                 "d": e.get("d") or None,
                                 "h": 1 if h == 1 else 0 if h == 0 else None}
            r["td"] += _num(e.get("g_td"))
    return sorted(rows.values(), key=lambda r: (r["s"] * 100 + r["w"]))


def write(out: Path, prefix: str, bars: dict, logs: dict, season: int | None = None) -> dict:
    """Write the index and the per-player files. The per-player directory is
    replaced wholesale, so a player who left the slate does not keep a stale file."""
    out = Path(out)
    rows = team_games(logs)
    (out / f"{prefix}logs_index.json").write_text(json.dumps(
        {"bars": bars, "season": season, "team_games": rows, "players": len(logs)},
        separators=(",", ":")))
    d = out / f"{prefix}logs"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    n = 0
    for pid, blk in (logs or {}).items():
        pid = str(pid)
        if not pid or "/" in pid or pid.startswith("."):
            continue
        (d / f"{pid}.json").write_text(json.dumps(blk, separators=(",", ":")))
        n += 1
    return {"team_games": len(rows), "players": n}
