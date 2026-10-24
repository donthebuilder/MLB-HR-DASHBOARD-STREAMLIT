"""
Pitcher inputs the MOONSHOT v5 matchup multiplier needs and the dashboard
did not carry: hard-hit% allowed and line-drive% on the pitcher's top two
pitch types.

One Statcast pull per pitcher per season, held in memory for the run.
Failure returns the league average, never raises -- a pitcher with no data
must not cost a player their row.
"""

import logging
from typing import Dict, Optional, Tuple

log = logging.getLogger("mlb_data_fixes")

LEAGUE_HARDHIT = 0.35
LEAGUE_LD_TOP2 = 0.20

_PULLS: Dict[Tuple[int, int], Optional[object]] = {}


def _pull(pitcher_id: int, season: int):
    key = (pitcher_id, season)
    if key not in _PULLS:
        try:
            from pybaseball import statcast_pitcher
            _PULLS[key] = statcast_pitcher(f"{season}-03-01", f"{season}-10-31", pitcher_id)
        except Exception as e:
            log.warning(f"statcast pull failed for pitcher {pitcher_id}: {e}")
            _PULLS[key] = None
    return _PULLS[key]


def fetch_pitcher_hardhit_pct(pitcher_id: int, season: int) -> float:
    """Share of batted balls allowed at 95+ mph. League average if no data."""
    df = _pull(pitcher_id, season)
    if df is None or df.empty or "launch_speed" not in df.columns:
        return LEAGUE_HARDHIT
    bbe = df[df["launch_speed"].notna()]
    if len(bbe) == 0:
        return LEAGUE_HARDHIT
    return round(float((bbe["launch_speed"] >= 95).sum()) / len(bbe), 4)


def fetch_pitcher_ld_pct_top2(pitcher_id: int, season: int) -> float:
    """Line-drive share of batted balls on his two most-thrown pitch types."""
    df = _pull(pitcher_id, season)
    if df is None or df.empty or "pitch_type" not in df.columns or "bb_type" not in df.columns:
        return LEAGUE_LD_TOP2
    top2 = df["pitch_type"].value_counts().head(2).index.tolist()
    batted = df[df["pitch_type"].isin(top2) & df["bb_type"].notna()]
    if len(batted) == 0:
        return LEAGUE_LD_TOP2
    return round(float((batted["bb_type"] == "line_drive").sum()) / len(batted), 4)
