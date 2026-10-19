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
        `hot` (Home's HOTTEST STREAK row, nothing else): for the bars' FIRST market
        (TD), each player whose newest game of the newest season reached the bar,
        with his last HOT_WINDOW games (all seasons, as the site's volume test reads) as [s, w, g_td, g_rec, g_car]. That
        is every player the site's streakBoard could rank with a streak > 0, with
        the columns its volume test (streaks.js USAGE) reads, so Home needs no
        other log.
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


HOT_WINDOW = 30            # streaks.js WINDOW for the Home row
HOT_COLS = ("g_td", "g_rec", "g_car")   # the market's field + streaks.js USAGE.TD cols


def hot_streaks(logs: dict, bars: dict) -> dict:
    """The `hot` block for the first market in `bars` (TD). {} when the bars name
    none or the logs hold no season."""
    if not bars or not logs:
        return {}
    market, spec = next(iter(bars.items()))
    if not isinstance(spec, (list, tuple)) or len(spec) < 2:
        return {}
    field, bar = spec[0], spec[1]
    if field not in HOT_COLS:
        return {}      # the cols above are the TD market's; another first market needs its own
    seasons = [e["s"] for b in logs.values() for e in (b or {}).get("log") or []
               if isinstance(e, dict) and isinstance(e.get("s"), (int, float))]
    if not seasons:
        return {}
    season = max(seasons)
    players = {}
    for pid, b in logs.items():
        # ALL seasons, newest last: the site's volume test (streaks.js usageFor) reads
        # the last 30 rows of the whole log, last season's included, while the streak
        # itself (seriesFor) is this season's rows only.
        rows = sorted((e for e in (b or {}).get("log") or []
                       if isinstance(e, dict) and isinstance(e.get("s"), (int, float))
                       and isinstance(e.get("w"), (int, float))),
                      key=lambda e: (e["s"], e["w"]))
        if not rows or rows[-1]["s"] != season or _num(rows[-1].get(field)) < bar:
            continue
        players[str(pid)] = [[e["s"], e["w"], *[_num(e.get(c)) for c in HOT_COLS]] for e in rows[-HOT_WINDOW:]]
    return {"market": market, "field": field, "bar": bar, "cols": list(HOT_COLS), "season": season,
            "players": players}


def write(out: Path, prefix: str, bars: dict, logs: dict, season: int | None = None) -> dict:
    """Write the index and the per-player files. The per-player directory is
    replaced wholesale, so a player who left the slate does not keep a stale file."""
    out = Path(out)
    rows = team_games(logs)
    (out / f"{prefix}logs_index.json").write_text(json.dumps(
        {"bars": bars, "season": season, "team_games": rows, "players": len(logs),
         "hot": hot_streaks(logs, bars)},
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
