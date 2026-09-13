#!/usr/bin/env python3
"""nfl_charting.py — which season the PARTICIPATION-backed tables come from.

THE BUG THIS EXISTS TO FIX (found 2026-09-13, two weeks before it would have
fired silently):

`stats_season_for()` flips the context season from 2025 to 2026 as soon as
three weeks of 2026 have been played -- roughly Sept 28. That is correct for
every table built off play-by-play or player stats, which nflverse publishes
nightly all season. It is WRONG for the four tables built off
`load_participation()`, because participation is not an in-season dataset:

    nflverse publishes pbp_participation ONCE A YEAR, after the postseason.
    The 2025 file landed 2026-02-10. There is no pbp_participation_2026 and
    there will not be one until roughly February 2027.

Verified live 2026-09-13, both ends of it:
    load_participation(seasons=[2026]) -> ValueError: Season must be
                                          between 2016 and 2025
    https://github.com/nflverse/nflverse-data/releases/download/
        pbp_participation/pbp_participation_2026.csv -> HTTP 404

So on the week the flip happens, these four go from "last season's charting,
which is real" to empty, and nfl_bot's extras loop catches the exception and
writes {} without anything downstream knowing the difference:

    coverage_team      man/zone + coverage-shell mix
    coverage_player    receiver's line vs man and vs zone
    disruption_team    formation mix + down-to-down pressure rate
    route_value        per-target value by route type

The fix is not a try/except -- there already is one, and it is exactly what
made this silent. The fix is to stop asking participation for a season it
structurally cannot have. These tables are charting-season tables, and the
charting season is its own clock.

FTN charting (`load_ftn_charting`) is the in-season alternative in principle,
but ftn_charting_2026 was also 404 as of 2026-09-13 (release last touched
2026-08-31), and it carries a different column set -- no coverage shell, no
man/zone, no route. Swapping to it is a real piece of work, not a fallback,
and it is deliberately not attempted here.
"""
from __future__ import annotations
import functools

import nflreadpy as nfl

# How far back to walk before giving up. Two is enough for the real case (this
# season has no file, last season does) plus one year of slack if nflverse is
# ever late publishing. Walking further would silently serve charting old
# enough to be about a different league.
MAX_LOOKBACK = 2


@functools.lru_cache(maxsize=8)
def charting_season(season: int) -> int:
    """The newest season <= `season` that participation data actually exists
    for. Returns `season` itself when it does.

    Probes by loading. That sounds expensive and is not: nflreadpy range-checks
    the season before it downloads anything, so the miss costs nothing, and the
    hit is the same cached load the caller was going to make anyway.
    """
    for candidate in range(season, season - MAX_LOOKBACK - 1, -1):
        try:
            df = nfl.load_participation(seasons=[candidate])
        except Exception:
            continue
        if df.height:
            return candidate
    # Nothing usable. Hand back the season asked for and let the caller's own
    # error path report it, rather than inventing a year.
    return season


def is_current(season: int) -> bool:
    """True when the charting tables are this season's own, not last season's.
    The site labels the difference; it should never have to guess it."""
    return charting_season(season) == season
