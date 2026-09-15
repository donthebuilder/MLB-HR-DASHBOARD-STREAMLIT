#!/usr/bin/env python3
"""nfl_numerology.py — real jersey/birthdate/season-TD fields for the slate.

2026-09-15, B10(d) ("clone MLB to NFL"). MLB's Alignments/numerology feature
(lib/alignments.js on the site) reduces a hitter's jersey number, birth day,
life path and season/next home-run count to a single digit (1-9) and looks
for arithmetic clustering across the slate -- disclosed everywhere as
"pattern watching, not evidence" (MLB's own 08-28 sweep tested 18 axes
against 4,238 real player-nights and found zero significant axes). Donovan
asked to ship the NFL equivalent anyway, same disclosure line.

Two of MLB's seven axes have no honest NFL equivalent and are dropped rather
than faked: batting-order "spot" and fielding "position code" are both real,
already-1-9 numbers baseball publishes for every plate appearance; football
has no per-play equivalent to either, so this file does not invent one.

The other five axes need real data nfl_week.json doesn't carry yet --
jersey_number and birth_date live only in nflverse's roster file, and
season-to-date touchdowns aren't accumulated anywhere in this repo. All
three are real, already-public data (nflreadpy's own roster/stats
endpoints); this file's only job is fetching and shaping them, never
inventing a number.

SEASON TD USES THE SAME DEFINITION AS THE TD MARKET ITSELF
(nfl_scoring.py's `"TD": rushing_tds + receiving_tds`), summed over
COMPLETED weeks only (week < the slate's own week) -- a player "sitting on
N touchdowns" has to describe real games already played, not one still
ahead of him, the same trailing-only discipline nfl_features.py already
applies everywhere else.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl


@functools.lru_cache(maxsize=4)
def _roster(season: int) -> pl.DataFrame:
    return (nfl.load_rosters(seasons=[season])
            .select(["gsis_id", "jersey_number", "birth_date"])
            .filter(pl.col("gsis_id").is_not_null())
            .unique(subset=["gsis_id"], keep="last"))


@functools.lru_cache(maxsize=8)
def _season_td_through(season: int, week) -> pl.DataFrame:
    st = nfl.load_player_stats(seasons=[season]).filter(pl.col("season_type") == "REG")
    if week is not None:
        st = st.filter(pl.col("week") < week)
    return (st.with_columns(
                (pl.col("rushing_tds").fill_null(0) + pl.col("receiving_tds").fill_null(0)).alias("td"))
            .group_by("player_id")
            .agg(pl.col("td").sum().alias("season_td")))


def numerology_fields(season: int, week) -> dict:
    """player_id -> {jersey_number, birth_date, season_td}, real data only.

    Missing a row (no roster match, no stats yet) means the field is simply
    absent -- never zero standing in for unknown. The site skips a null axis
    rather than rendering it as a false root; that discipline starts here.
    """
    out: dict = {}
    try:
        ros = _roster(season)
        for r in ros.iter_rows(named=True):
            out[r["gsis_id"]] = {
                "jersey_number": r.get("jersey_number"),
                "birth_date": str(r["birth_date"]) if r.get("birth_date") else None,
            }
    except Exception as exc:  # noqa: BLE001
        print(f"numerology roster unavailable ({type(exc).__name__}: {exc})")

    try:
        td = _season_td_through(season, week)
        for r in td.iter_rows(named=True):
            pid = r["player_id"]
            out.setdefault(pid, {})["season_td"] = int(r["season_td"] or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"numerology season-TD unavailable ({type(exc).__name__}: {exc})")

    return out
