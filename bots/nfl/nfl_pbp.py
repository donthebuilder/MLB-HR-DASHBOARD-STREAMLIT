#!/usr/bin/env python3
"""nfl_pbp.py — same-day drive state from nflverse, free, no new dependency.

Donovan asked (2026-08-28) for a live-play-by-play source that doesn't
depend on ESPN. There isn't a truly real-time free one: nflreadpy's own docs
say `load_pbp()` "updates nightly after each game day, and additionally at
specific points on game days," with raw data usually posted within ~15
minutes of a game ending and the fully-corrected version landing the
following Thursday. `nflreadpy` was ALREADY a dependency here (nfl_field.py
uses `load_pbp()` for the field charts) — this is the same free source,
just read for drive state instead of shot charts.

So this is NOT a replacement for nfl_espn.fetch()'s live `situation` parse
(SHIP-B7-DATA.sh) — that one updates on ESPN's own clock and is the only
candidate for true down-to-down live state. This is a SECOND, independent
source that:
  - confirms/corrects the ESPN guess once nflreadpy catches up during or
    after the game (nflreadpy's schema here — down/ydstogo/posteam/qtr/
    game_seconds_remaining/yardline_100 — is well-established and already
    trusted elsewhere in this codebase, unlike ESPN's undocumented
    `situation` block, which nfl_espn.py itself flags as an unverified guess)
  - covers the gap when ESPN's live block is absent, wrong-shaped, or the
    game has gone final and ESPN stops updating `situation` but the site's
    Games page still wants to show "how did that drive end"

Regular season only — nflverse carries no preseason play-by-play at all
(same limitation nfl_espn.py's own docstring documents for the schedule
side).
"""
from __future__ import annotations
import functools
from typing import Any

import nflreadpy as nfl
import polars as pl


@functools.lru_cache(maxsize=4)
def _pbp(season: int) -> pl.DataFrame:
    return nfl.load_pbp(seasons=[season]).filter(pl.col("season_type") == "REG")


def last_drive_state(season: int, week: int | None = None) -> dict[str, dict[str, Any]]:
    """Most recent down/distance/possession per nflverse game_id.

    Keyed by nflverse's own game_id ("{season}_{week:02d}_{away}_{home}"),
    not ESPN's numeric id — attach_pbp_state() below does that translation,
    since nfl_espn.py's rows already carry nflverse-compatible team codes
    (see its own FIX dict / module docstring).

    Returns {} on any failure (network, schema drift, no data yet for a
    week) — never raises. A quiet {} is exactly what a game that hasn't
    kicked off yet, or a season nflreadpy hasn't ingested yet, looks like,
    and callers already treat a missing game_id as "nothing to show".
    """
    try:
        df = _pbp(season)
        if week:
            df = df.filter(pl.col("week") == week)
        d = df.filter(pl.col("down").is_not_null(), pl.col("game_id").is_not_null())
        if d.height == 0:
            return {}
        last = (d.sort("game_seconds_remaining", descending=False)
                  .group_by("game_id").agg(pl.all().first()))
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for r in last.iter_rows(named=True):
        gsr = r.get("game_seconds_remaining")
        down = r.get("down")
        dist = r.get("ydstogo")
        yl = r.get("yardline_100")
        out[str(r["game_id"])] = {
            "quarter": int(r["qtr"]) if r.get("qtr") is not None else None,
            "down": int(down) if down is not None else None,
            "distance": int(dist) if dist is not None else None,
            "yardline_100": int(yl) if yl is not None else None,
            "possession": r.get("posteam"),
            "game_seconds_remaining": int(gsr) if gsr is not None else None,
            "desc": r.get("desc"),
        }
    return out


def attach_pbp_state(games: list[dict[str, Any]], season: int, week: int | None = None) -> list[dict[str, Any]]:
    """Merge nflverse's last-known drive state onto ESPN game rows.

    Matches by team codes + week rather than any ESPN id (nflverse doesn't
    know ESPN's ids and vice versa) — safe because nfl_espn.py already
    normalizes home/away to nflverse's own abbreviations for exactly this
    kind of cross-reference. Returns NEW dicts; never mutates `games`.
    Fields land as pbp_* so they're clearly a second, slower-but-verified
    source and never silently overwrite the live ESPN situation fields.
    """
    state = last_drive_state(season, week)
    out = []
    for g in games:
        row = dict(g)
        wk = g.get("week") or week
        home, away = g.get("home"), g.get("away")
        gid = f"{season}_{int(wk):02d}_{away}_{home}" if wk and home and away else None
        s = state.get(gid) if gid else None
        row["pbp_quarter"] = s.get("quarter") if s else None
        row["pbp_down"] = s.get("down") if s else None
        row["pbp_distance"] = s.get("distance") if s else None
        row["pbp_yardline_100"] = s.get("yardline_100") if s else None
        row["pbp_possession"] = s.get("possession") if s else None
        row["pbp_desc"] = s.get("desc") if s else None
        row["pbp_source"] = "nflverse" if s else None
        out.append(row)
    return out


if __name__ == "__main__":
    import sys
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    wk = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    state = last_drive_state(yr, wk)
    print(f"{len(state)} games with drive state, {yr} week {wk}")
    for gid, s in list(state.items())[:5]:
        print(f"  {gid}: {s}")
