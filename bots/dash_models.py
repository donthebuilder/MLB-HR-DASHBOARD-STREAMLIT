"""
DASH models -- the one place every model in the bot repo is listed and run.

    python bots/dash_models.py --list

Player scorers all follow the same shape (bots/dash_shared.py):
    FINAL = SCORER x MATCHUP, capped 100, model_version on every row.

MOONSHOT's adapter lives here: it turns the dashboard's per-player rows into
the inputs moonshot_v4_scorer.py takes, writes public/data/current/
moonshot_v4_latest.json, and records every input it had to estimate.
"""

import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from bots import injury_status
    from bots import lineups
    from bots import moonshot_v4_scorer as moonshot
    from bots import tuddy_v2_scorer as tuddy
    from bots import mlb_data_fixes
except ImportError:  # run from inside bots/
    import injury_status
    import lineups
    import moonshot_v4_scorer as moonshot
    import tuddy_v2_scorer as tuddy
    import mlb_data_fixes

log = logging.getLogger("dash_models")

# ---------------------------------------------------------------------------
# REGISTRY -- every model, one row each. status: live | shadow | planned
# ---------------------------------------------------------------------------
REGISTRY: List[Dict[str, str]] = [
    {"sport": "MLB", "name": "MOONSHOT hr_score",   "version": "mlb_hr_v4",     "status": "live",    "where": "bots/mlb_dashboard.py (MODEL_WEIGHTS hr_blend)",           "out": "today.json rows"},
    {"sport": "MLB", "name": "MOONSHOT batter x matchup", "version": "mlb_hr_v5", "status": "live",    "where": "bots/moonshot_v4_scorer.py via dash_models.run_moonshot", "out": "public/data/current/moonshot_v4_latest.json"},
    {"sport": "MLB", "name": "Moneyline",           "version": "(unversioned)", "status": "live",    "where": "bots/moneyline_bot.py (playoff_odds.win_probability vs market)", "out": "moneyline bot output"},
    {"sport": "MLB", "name": "Spread / run line",   "version": "-",             "status": "planned", "where": "no model exists; odds_fetch.py only carries the lines",  "out": "-"},
    {"sport": "NFL", "name": "TUDDY TD",            "version": "nfl_td_v2",     "status": "live",    "where": "bots/nfl/nfl_scoring.py MODELS['TD']",                    "out": "nfl_bot.py output"},
    {"sport": "NFL", "name": "TUDDY TD shadow",     "version": "nfl_td_v3",     "status": "shadow",  "where": "bots/nfl/nfl_scoring.py SHADOW_MODELS['TD']",             "out": "nfl_bot.py output"},
    {"sport": "NFL", "name": "TUDDY usage x lane",  "version": "nfl_td_v4",     "status": "live",    "where": "bots/tuddy_v2_scorer.py",                                  "out": "public/data/current/tuddy_v2_latest.json"},
    {"sport": "ALL", "name": "Availability (p_play)", "version": "-",             "status": "live",    "where": "bots/injury_status.py (ESPN injury feeds)",                "out": "p_play / expected_score on rows"},
    {"sport": "ALL", "name": "Lineups & lines",     "version": "-",             "status": "live",    "where": "bots/lineups.py (statsapi, ESPN)",                         "out": "lineup_status / p_start; spread, total"},
    {"sport": "NHL", "name": "LAMP tuned board (every skater)", "version": "nhl_goal_v2", "status": "live", "where": "bots/nhl_live.py + models/nhl_goal_v2.json",       "out": "bots/outputs/nhl_board_latest.json"},
    {"sport": "NHL", "name": "LAMP shooter x goalie", "version": "nhl_goal_v1", "status": "planned", "where": "bots/nhl_scorer.py",                                       "out": "public/data/current/nhl_scorer_latest.json"},
    {"sport": "NBA", "name": "BUCKETS",             "version": "nba_pts_v1",    "status": "planned", "where": "bots/buckets_scorer.py",                                   "out": "public/data/current/buckets_scorer_latest.json"},
]


# ---------------------------------------------------------------------------
# MOONSHOT adapter
# ---------------------------------------------------------------------------
class Gaps:
    """Counts inputs that were estimated or missing, so the run can report them."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.total = 0

    def note(self, field: str) -> None:
        self.counts[field] += 1

    def report(self) -> Dict[str, Any]:
        return {"players": self.total, "estimated_or_missing": dict(self.counts)}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return default if x != x else x
    except (TypeError, ValueError):
        return default


def clean_log(raw: Optional[list]) -> list:
    """Dashboard spray rows -> the shape the scorer reads.
    Rows use `date`, not `game_date`, and strikeout rows carry None for ev/la/distance."""
    out = []
    for b in raw or []:
        if b.get("is_k") or b.get("ev") is None:
            continue
        out.append({
            "game_date": str(b.get("game_date") or b.get("date") or "")[:10],
            "ev": _f(b.get("ev")),
            "launch_angle": _f(b.get("launch_angle")),
            "distance": _f(b.get("distance")),
            "bb_type": b.get("bb_type") or b.get("trajectory") or "",
            "is_hr": bool(b.get("is_hr")),
        })
    return [b for b in out if b["game_date"]]


def pitcher_hr_rate(row: Dict[str, Any], gaps: Gaps) -> float:
    """HR/9 against this batter's side, scaled so ~1.2 HR/9 (league) = 0.30."""
    bats, throws = str(row.get("bats") or "R"), str(row.get("pitcher_throws") or "R")
    side = ("L" if throws == "R" else "R") if bats == "S" else bats
    hr9 = _f(row.get("pitcher_hr9_vs_lhb" if side == "L" else "pitcher_hr9_vs_rhb"))
    if hr9 <= 0:
        hr9 = _f(row.get("pitcher_hr9"))
        gaps.note("pitcher_hr_rate_to_hand (used overall HR/9)")
    return min(1.0, hr9 / 4.0)


def moonshot_inputs(row: Dict[str, Any], season: int, gaps: Gaps) -> Tuple[Dict, Dict, int, float]:
    pid = int(_f(row.get("pitcher_id")))

    hardhit = _f(row.get("pitcher_hardhit_allowed"))
    if hardhit <= 0 and pid:
        hardhit = mlb_data_fixes.fetch_pitcher_hardhit_pct(pid, season)
        gaps.note("pitcher_hardhit_pct (fetched from Statcast)")
    ld_top2 = mlb_data_fixes.fetch_pitcher_ld_pct_top2(pid, season) if pid else mlb_data_fixes.LEAGUE_LD_TOP2

    iso = _f(row.get("season_iso"))
    gaps.note("iso_last10 (used season_iso)")
    fb = _f(row.get("recent_fb_rate"))
    gaps.note("season_fb_rate (used recent_fb_rate)")
    dc = _f(row.get("damage_conversion_score"))
    if dc <= 0:
        dc = 50.0
        gaps.note("dc_rating (damage_conversion_score missing, used 50)")

    season_ev = _f(row.get("season_avg_ev"))
    if season_ev <= 0:
        season_ev = _f(row.get("l25pa_avg_ev")) or _f(row.get("recent_ev"))
        gaps.note("season_ev (season_avg_ev missing, used recent EV)")

    pf = _f(row.get("park_factor"), 100.0)
    park = max(0.95, min(1.05, 1.0 + (pf - 100.0) / 100.0))

    games = max(1.0, _f(row.get("season_pa")) / 4.2)
    batter_inputs = {
        "batted_ball_log": clean_log(row.get("batted_ball_log")),
        "last5_ba": _f(row.get("last5_avg")),
        "season_ev": season_ev,
        "iso_last10": iso,
        "season_fb_rate": fb,
    }
    matchup_inputs = {
        "pitcher_hr_rate_to_hand": pitcher_hr_rate(row, gaps),
        "pitcher_fb_pct": _f(row.get("pitcher_statcast_fb_rate")) or _f(row.get("pitcher_fb_rate")),
        "pitcher_hardhit_pct": hardhit,
        "pitcher_ld_pct_top2": ld_top2,
        "dc_rating": dc,
        "park_factor": park,
    }
    return batter_inputs, matchup_inputs, int(_f(row.get("last10_hr"))), _f(row.get("season_hr")) / games


def run_moonshot(rows: List[Dict[str, Any]], slate_date: date, out_dir: Path,
                 season: Optional[int] = None) -> Optional[str]:
    """Score every hitter row and write moonshot_v4_latest.json. Never raises:
    a bad row loses itself, a bad run loses only this file."""
    season = season or slate_date.year
    gaps, scored = Gaps(), []
    for row in rows:
        try:
            if not row.get("pitcher_id"):
                continue
            gaps.total += 1
            b_in, m_in, hr10, hr_pg = moonshot_inputs(row, season, gaps)
            scored.append(moonshot.build_output_row(
                player_id=int(_f(row.get("player_id"))), name=str(row.get("name") or ""),
                team=str(row.get("team") or ""), bats=str(row.get("bats") or ""),
                opponent=str(row.get("opponent") or ""), pitcher_id=int(_f(row.get("pitcher_id"))),
                pitcher_name=str(row.get("pitcher_name") or ""), pitcher_hand=str(row.get("pitcher_throws") or ""),
                park_name=str(row.get("venue_name") or ""), game_pk=int(_f(row.get("game_pk"))),
                batter_inputs=b_in, matchup_inputs=m_in, recent_hr_last10=hr10,
                season_hr_per_game=hr_pg, game_date=slate_date.isoformat(),
            ))
        except Exception as e:
            log.warning(f"moonshot v5 skipped {row.get('name')}: {e}")
    if not scored:
        return None
    try:       # chance of playing: a score is only worth something if he plays
        inj = injury_status.fetch("mlb")
        injury_status.snapshot(inj, "mlb", Path(__file__).resolve().parent / "outputs" / "injury_snapshots")
        log.info(f"moonshot v5 injury report: {injury_status.apply(scored, inj)} of {len(scored)} players on it")
    except Exception as e:
        log.warning(f"injury report skipped: {e}")
    try:       # is he in the posted lineup? (unposted = 76% to play, measured)
        log.info(f"moonshot v5 lineups: {lineups.apply_mlb(scored, lineups.mlb(slate_date.isoformat()))}")
    except Exception as e:
        log.warning(f"lineup check skipped: {e}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = moonshot.write_output(scored, str(out_dir / moonshot.LIVE_OUTPUT_FILE), shadow=False)
    log.info(f"moonshot v5 gaps: {gaps.report()}")
    return path


# ---------------------------------------------------------------------------
# TUDDY adapter -- history in, scorer inputs out. Pure: the live run and
# nfl/nfl_tuddy_backtest.py both call it, so the backtest grades shipped code.
# ---------------------------------------------------------------------------
def tuddy_inputs(prior: List[Dict[str, Any]], opp_prior: List[Dict[str, Any]],
                 position: str, gaps: Gaps) -> Tuple[Dict, Dict, bool]:
    """
    prior      this player's earlier weeks, oldest first, each
               {rz_opp, gl_opp, td_actual, snap_pct}. NOTHING from the game being scored.
    opp_prior  the opponent's earlier weeks against this position, oldest first,
               each {td, rz_opp} (the position group's totals that week).
    """
    last5 = prior[-5:]
    rz = [_f(w.get("rz_opp")) for w in last5]
    gl = [_f(w.get("gl_opp")) for w in last5]
    snaps = [_f(w.get("snap_pct")) for w in last5 if w.get("snap_pct") is not None]
    rz_total = sum(rz)
    rz_td_pct = min(0.60, sum(_f(w.get("td_actual")) for w in last5) / rz_total) if rz_total else 0.0

    gaps.note("target_shares (unused by the scorer)")
    gaps.note("lane (needs route/target-location data)")
    gaps.note("explosive-play rates (not computed; overlay stays 1.0)")
    gaps.note("rz_td_pct (all TDs / red-zone touches, not RZ-only TDs)")
    gaps.note("role_weeks_consistent (proxy: recent weeks near his average snap share)")

    mean_snap = sum(snaps) / len(snaps) if snaps else 0.0
    role_weeks = sum(1 for x in snaps[-3:] if abs(x - mean_snap) <= 0.15) if snaps else 0

    win = opp_prior[-10:]
    n = len(win)
    td_allowed = sum(_f(w.get("td")) for w in win)
    rz_allowed = sum(_f(w.get("rz_opp")) for w in win)
    scorer_inputs = {
        "rz_touches_last5": rz, "gl_touches_last5": gl, "rz_td_pct": rz_td_pct,
        "snap_pcts": snaps, "target_shares": [], "role_weeks_consistent": role_weeks,
    }
    matchup_inputs = {
        "def_td_rate_vs_pos": td_allowed / n if n else 0.0,
        "def_rz_td_pct_vs_pos": min(1.0, td_allowed / rz_allowed) if rz_allowed else 0.0,
        "def_lane_td_rate": None,
    }
    gl_specialist = position in ("RB", "QB") and bool(gl) and sum(gl) / len(gl) >= 1.0
    return scorer_inputs, matchup_inputs, gl_specialist


def tuddy_score(scorer_inputs: Dict, matchup_inputs: Dict, position: str,
                game_total: float, gl_specialist: bool) -> float:
    sc = tuddy.scorer_score_tuddy(**scorer_inputs, position=position)
    mt = tuddy.position_matchup_multiplier(
        position=position, lane=None, is_goal_line_specialist=gl_specialist,
        game_total=game_total, **matchup_inputs)
    return tuddy.final_score_tuddy(sc, mt)["final_score"]


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if "--list" in sys.argv:
        for m in REGISTRY:
            print(f"{m['sport']:4} {m['status']:8} {m['version']:14} {m['name']:26} {m['where']}")
    else:
        print(__doc__)
