"""
BUCKETS v1 -- NBA points scorer.   model_version: nba_pts_v1
Output: public/data/current/buckets_scorer_latest.json

    FINAL = SCORER (who is scoring right now) x MATCHUP (is tonight a good spot)

SCORER, 0-100 -- last 10 games, recency weighted (the anchor is 25+ points):
  30  scoring average                     the anchor
  20  usage, with its 5-game trend        rising = boost, falling = penalty
  20  shot attempts
  15  minutes                             the opportunity under everything else
  15  pace                                his team's pace x tonight's opponent's
MATCHUP, 0.5-1.5:
  opponent points allowed per game (recent), and tonight's pace.

GAPS, stated once: defender-level matchup (who guards him, their FG% allowed in his
zones) and shot zones need tracking data that is not in the public box score, so
the matchup here is team-level and "shot attempts" is total FGA. That 20-point
defender slice of the handoff spec is redistributed to minutes and pace.
"""
from typing import Dict, List, Optional

try:
    from bots.dash_shared import normalize, recency_weight, trend_slope, final_score
except ImportError:
    from dash_shared import normalize, recency_weight, trend_slope, final_score

MODEL_VERSION = "nba_pts_v1"
LG_PPG_ALLOWED = 113.0
LG_PACE = 99.0


def scorer_score(games: List[Dict], team_pace: float, opp_pace: float) -> Dict:
    """games: this player's earlier games, oldest first: {pts, fga, minutes, usage}."""
    g = games[-10:]
    if not g:
        return {"scorer_score": 0.0}
    pts = recency_weight([x["pts"] for x in g])
    fga = recency_weight([x["fga"] for x in g])
    mins = recency_weight([x["minutes"] for x in g])
    use = recency_weight([x["usage"] for x in g])
    tr = trend_slope([x["usage"] for x in g[-5:]])["slope"]
    usage_pts = 20 * normalize(use, 0.14, 0.34) + 4 * max(-1.0, min(1.0, tr / 0.02))
    pace = (team_pace + opp_pace) / 2.0
    parts = {
        "scoring_pts": 30 * normalize(pts, 8, 32),
        "usage_pts": max(0.0, min(20.0, usage_pts)),
        "attempts_pts": 20 * normalize(fga, 6, 22),
        "minutes_pts": 15 * normalize(mins, 18, 37),
        "pace_pts": 15 * normalize(pace, 95.0, 103.0),
    }
    return {"scorer_score": round(min(100.0, sum(parts.values())), 2), **{k: round(v, 2) for k, v in parts.items()}}


def matchup_multiplier(opp_pts_allowed: Optional[List[float]], pace: float) -> Dict:
    if not opp_pts_allowed:
        return {"matchup_multiplier": 1.0, "opponent_known": False}
    allowed = sum(opp_pts_allowed[-10:]) / len(opp_pts_allowed[-10:])
    raw = 0.65 * normalize(allowed, 106.0, 120.0) + 0.35 * normalize(pace, 95.0, 103.0)
    return {"matchup_multiplier": round(max(0.5, min(1.5, 0.5 + raw)), 3), "opponent_known": True,
            "opp_pts_allowed": round(allowed, 1)}


def score_player(games: List[Dict], team_pace: float, opp_pace: float,
                 opp_pts_allowed: Optional[List[float]]) -> Dict:
    sc = scorer_score(games, team_pace, opp_pace)
    mt = matchup_multiplier(opp_pts_allowed, (team_pace + opp_pace) / 2.0)
    return {"final_score": final_score(sc["scorer_score"], mt["matchup_multiplier"]),
            "model_version": MODEL_VERSION, **sc, **mt}
