"""
LAMP v1 -- NHL anytime-goal scorer.   model_version: nhl_goal_v1
Output: public/data/current/nhl_scorer_latest.json

    FINAL = SCORER (who is dangerous right now) x MATCHUP (is tonight a good spot)

SCORER, 0-100 -- last 10 games, recency weighted:
  35  shots on goal
  25  power-play time on ice          (PP scoring is its own bucket, not averaged away)
  20  even-strength goals per 60      (regressed toward the league rate: 10 games is small)
  15  shooting quality                (season shooting%, regressed -- see GAPS)
   5  role stability                  (steady ice time)
MATCHUP, 0.5-1.5:
  goalie save% (recent starts, regressed)  and  team shots against per game.
  Starter unknown -> 1.0 and flagged. Never held back for it.

GAPS, stated once: high-danger-zone shot share and line numbers are not in the
public stats API, so "shot quality" is shooting% and "stability" is TOI
steadiness. Goalie save% is overall, not by the zones this shooter uses.
"""
import logging
from typing import Dict, List, Optional

try:
    from bots.dash_shared import normalize, recency_weight, final_score
except ImportError:
    from dash_shared import normalize, recency_weight, final_score

log = logging.getLogger("nhl_scorer")
MODEL_VERSION = "nhl_goal_v1"

LG_SHOOT_PCT = 0.10
LG_ES_GOALS_PER60 = 0.55
LG_SAVE_PCT = 0.905


def scorer_score(games: List[Dict]) -> Dict:
    """games: this player's earlier games, oldest first. Each:
    {shots, goals, pp_goals, toi_min, pp_toi_min}. Nothing from the game being scored."""
    g = games[-10:]
    if not g:
        return {"scorer_score": 0.0}
    sog = recency_weight([x["shots"] for x in g])
    pp = recency_weight([x["pp_toi_min"] for x in g])
    es_goals = sum(x["goals"] - x["pp_goals"] for x in g)
    es_hours = sum(max(0.0, x["toi_min"] - x["pp_toi_min"]) for x in g) / 60.0
    k = 3.0                                             # hours of prior: 3h at the league rate
    es60 = (es_goals + LG_ES_GOALS_PER60 * k) / (es_hours + k)
    goals_s = sum(x.get("season_goals", 0) for x in g[-1:])        # season-to-date, carried on the last row
    shots_s = sum(x.get("season_shots", 0) for x in g[-1:])
    shoot = (goals_s + LG_SHOOT_PCT * 50) / (shots_s + 50)
    toi = [x["toi_min"] for x in g]
    mean = sum(toi) / len(toi)
    cv = (sum((t - mean) ** 2 for t in toi) / len(toi)) ** 0.5 / mean if mean else 1.0

    pts = {
        "sog_pts": 35 * normalize(sog, 0.5, 4.5),
        "pp_pts": 25 * normalize(pp, 0.0, 4.0),
        "es_pts": 20 * normalize(es60, 0.2, 1.2),
        "quality_pts": 15 * normalize(shoot, 0.06, 0.16),
        "stability_pts": 5 * (1.0 - min(1.0, cv / 0.25)),
    }
    return {"scorer_score": round(min(100.0, sum(pts.values())), 2), **{k_: round(v, 2) for k_, v in pts.items()}}


def matchup_multiplier(goalie_games: Optional[List[Dict]], team_sa: Optional[List[float]]) -> Dict:
    """goalie_games: the opposing goalie's earlier starts {saves, shots_against}.
    team_sa: the opposing team's shots against in its earlier games."""
    if not goalie_games:
        return {"matchup_multiplier": 1.0, "goalie_known": False}
    saves = sum(x["saves"] for x in goalie_games[-10:])
    sa = sum(x["shots_against"] for x in goalie_games[-10:])
    sv = (saves + LG_SAVE_PCT * 200) / (sa + 200)                   # 200 shots of prior
    goalie_bad = 1.0 - normalize(sv, 0.885, 0.925)                  # 1.0 = leaky goalie
    sa_norm = normalize(sum(team_sa[-10:]) / len(team_sa[-10:]), 24.0, 36.0) if team_sa else 0.5
    raw = 0.6 * goalie_bad + 0.4 * sa_norm
    return {"matchup_multiplier": round(max(0.5, min(1.5, 0.5 + raw)), 3), "goalie_known": True,
            "goalie_sv_pct": round(sv, 4)}


def score_player(games: List[Dict], goalie_games: Optional[List[Dict]], team_sa: Optional[List[float]]) -> Dict:
    sc = scorer_score(games)
    mt = matchup_multiplier(goalie_games, team_sa)
    return {"final_score": final_score(sc["scorer_score"], mt["matchup_multiplier"]),
            "model_version": MODEL_VERSION, **sc, **mt}
