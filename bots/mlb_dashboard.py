#!/usr/bin/env python3
"""
MLB Daily Breakdown Bot — stable spec build

What this version tries to do:
- Pull today's MLB slate from MLB Stats API
- Use confirmed lineups when available; otherwise build projected lineups
- Pull season, last 10, last 5, split, weather, park and recent Statcast contact
- Score hitters for:
    * Top Pick (overall)
    * HR Pick
    * Hit Pick
    * HRR Pick
    * Base Pick
- Build a Top 15 HR board
- Build simple pairing sections:
    * Best HR Pairs
    * Hot + Due Pairs
    * Same-Date Homer Tag
    * Best Matchup Tag
    * Numerology Pair

Notes:
- This version intentionally avoids fragile CBS scraping and uses MLB Stats API + pybaseball + weather.
- If pybaseball is not installed, the bot still runs with fallbacks, but recent 350+ / FB / BABIP detail will be lighter.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import io
import json
import os
import platform
import math
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Pair History V2 helper: loaded best-effort so the bot still runs if the cache is missing.
try:
    from pair_history_helper import load_pair_history_cache, attach_pair_history_to_payload, player_hr_pa
except Exception:
    try:
        from pair_history_helper_v2 import load_pair_history_cache, attach_pair_history_to_payload, player_hr_pa
    except Exception:
        def load_pair_history_cache(*args, **kwargs):
            return {}
        def attach_pair_history_to_payload(payload, cache, slate_date=None):
            payload["pair_history_cache_loaded"] = False
            payload["pair_history_schema"] = "missing_helper"
            return payload
        def player_hr_pa(player):
            pa = float(player.get("season_pa") or player.get("pa") or 0) if isinstance(player, dict) else 0.0
            hr = float(player.get("season_hr") or player.get("hr") or 0) if isinstance(player, dict) else 0.0
            hrpa = (hr / pa) if pa > 0 else 0.0
            paphr = (pa / hr) if hr > 0 else None
            tier = "Elite" if hrpa >= .045 else "Strong" if hrpa >= .035 else "Playable" if hrpa >= .025 else "Low" if hrpa > 0 else "Unknown"
            return {"season_pa": int(pa), "season_hr": int(hr), "hr_per_pa": round(hrpa, 4), "pa_per_hr": round(paphr, 1) if paphr else None, "hr_pa_tier": tier}

import requests

# MODEL FOUNDATION (2026-08-21): the version registry (bots/model_registry.py,
# Task 1). Defensive import, matching the pair_history_helper pattern above --
# a broken/missing registry module must never take down the scoring bot. On
# failure, model_version/run_id stamping and the prediction log are skipped
# for the run (loudly, to stderr) rather than the whole job dying.
try:
    import model_registry as MODEL_REGISTRY
except Exception as _model_registry_exc:
    MODEL_REGISTRY = None
    print(f"model_registry import failed ({_model_registry_exc}); "
          f"model_version/run_id stamping and the prediction log are "
          f"disabled for this run.", file=sys.stderr)

# PROVENANCE (2026-08-21): deterministic scoring-configuration fingerprint
# (bots/config_fingerprint.py). Same defensive-import pattern as
# MODEL_REGISTRY just above -- a broken/missing module must never take down
# the scoring bot. On failure, config_hash is skipped for the run (loudly,
# to stderr); model_version/run_id stamping is unaffected either way.
try:
    import config_fingerprint as CONFIG_FINGERPRINT
except Exception as _config_fingerprint_exc:
    CONFIG_FINGERPRINT = None
    print(f"config_fingerprint import failed ({_config_fingerprint_exc}); "
          f"config_hash stamping is disabled for this run.", file=sys.stderr)

# BALL FLIGHT, SERVER-SIDE (2026-08-29): bots/trajectory.py -- mirrors
# lib/trajectory.js on the site so the nightly bot solves each batted ball's
# arc ONCE (apex_ft, hang_time_s, traj_poly) instead of every browser
# re-solving the same RK4 fit on every spray-chart render. Same defensive
# pattern as MODEL_REGISTRY/CONFIG_FINGERPRINT above: a broken/missing module
# must never take the scoring bot down. On failure, spray_chart rows simply
# don't carry the three trajectory fields, and the site falls back to its own
# client-side solveFlight() exactly as it did before this existed.
try:
    import trajectory as TRAJECTORY
except Exception as _trajectory_exc:
    TRAJECTORY = None
    print(f"trajectory import failed ({_trajectory_exc}); "
          f"spray_chart rows will not carry apex_ft/hang_time_s/traj_poly "
          f"for this run.", file=sys.stderr)

# SAVANT FEEDS (2026-08-23): the two league-wide tables StatsAPI does not carry
# — catcher throwing and team Outs Above Average (bots/savant_feeds.py). Same
# defensive-import pattern as the two above, and the reason is not theoretical:
# `from archive import rows_of` in three staged scripts, with archive.py never
# copied alongside them, is exactly why the accountability bot published
# nothing for two weeks and nobody noticed.
#
# ONE report at startup rather than a swallowed failure per game. A message
# printed thirty times is a message nobody reads, and a message printed zero
# times is how a feed goes dark for a fortnight.
try:
    import savant_feeds as SAVANT_FEEDS
except Exception as _savant_exc:
    SAVANT_FEEDS = None
    print(f"savant_feeds import failed ({_savant_exc}); opposing-catcher "
          f"throwing and team-defence fields will publish as 'missing' for "
          f"this run. Nothing else is affected.", file=sys.stderr)

try:
    import pandas as pd
except ImportError as exc:
    raise SystemExit("pandas is required. Install pandas first.") from exc

try:
    from pybaseball import statcast_batter, statcast_pitcher
except Exception:
    statcast_batter = None
    statcast_pitcher = None

MLB_BASE = "https://statsapi.mlb.com/api/v1"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OWM_BASE   = "https://api.openweathermap.org/data/2.5"
# WEATHER (2026-07-25): Open-Meteo is the only weather source that is
# actually required. It's free, needs no key, and is already the primary
# provider in fetch_weather() -- OpenWeatherMap was never more than a
# fallback for the rare case Open-Meteo returns nothing.
#
# The previously hardcoded key was removed outright. It had been committed to
# the repo and pasted in plaintext in chat, so it must be treated as burned;
# leaving it as a default meant every run kept using a compromised
# credential for no benefit. OWM is now strictly opt-in: set the OWM_API_KEY
# env var (GitHub Actions secret or local env) and the fallback re-enables
# itself. Leave it unset and the bot runs on Open-Meteo alone, which is the
# expected configuration.
OWM_API_KEY = os.environ.get("OWM_API_KEY", "").strip()
TIMEOUT = 30
if ZoneInfo is not None:
    TODAY = dt.datetime.now(ZoneInfo("America/Phoenix")).date()
else:
    TODAY = dt.date.today()
SEASON = TODAY.year
SEASON_START = dt.date(SEASON, 3, 1)

# ── CENTRALIZED SCORING WEIGHTS (Phase 1) ───────────────────────────────────
# Pulled out of the formulas below so the highest-impact tuning knobs live in
# one place instead of being scattered across a 9000+ line file. This is a
# mechanical extraction only -- every value here is copied verbatim from
# where it used to live inline, so scoring behavior is unchanged. Scope is
# deliberately limited to the three groups that get re-tuned most often (per
# the "per audit" backtest comments throughout the file): the main HR blend,
# the HR gate thresholds, and the recency hot/cold multiplier tiers.
# The hit/HRR/contact sub-formulas still have their weights inline for now --
# ask to extend this if you want those centralized too.
MODEL_WEIGHTS: Dict[str, Dict[str, float]] = {
    # hr_raw blend -- must sum to 1.00. This is the single most important
    # tuning surface in the whole model; see the "Recency-first HR blend"
    # comments in apply_model_v2_layers for the backtest history behind
    # these exact numbers.
    "hr_blend": {
        # RECALIBRATION (2026-07-25): backtest across 4,972 graded picks /
        # 64 days (4/27-7/24) measured each component's actual top-vs-bottom
        # decile HR-rate lift. batted_shape carried the single largest
        # weight in the blend (0.24) but ranked 30th of 43 signals measured
        # (+4.0pp lift) -- its own raw inputs (shape_max_ev +12.8pp, the
        # #1 signal in the whole dataset; shape_raw_pull_rate +8.6pp) far
        # outperform the composite it feeds. season_power, by contrast,
        # ranked #3 overall (+10.3pp) while carrying one of the smallest
        # weights. Trimmed batted_shape 0.24->0.17 (-7pp) and used that to
        # raise season_power 0.08->0.12 (+4pp) and fund a brand-new
        # pa_per_hr term (+3pp, see below). damage_conversion_score
        # (+9.8pp lift, already the 2nd-largest weight) measured as
        # well-calibrated and is left unchanged.
        # RE-WEIGHT (2026-07-26, by request): lift the two pitcher/contact-
        # quality terms Donovan weights most heavily in his own read of a
        # slate -- damage_conversion (DC) and pitcher_damage, whose single
        # largest input is pitcher_hr9. Funded from batted_shape, which the
        # 7/25 recalibration already flagged as the weakest large weight in
        # the blend (30th of 43 signals, +4.0pp lift), plus a point each off
        # pull_launch and pitch_match_term.
        #   batted_shape            0.17 -> 0.13  (-4)
        #   pull_launch             0.08 -> 0.07  (-1)
        #   pitch_match_term        0.08 -> 0.07  (-1)
        #   damage_conversion_score 0.13 -> 0.16  (+3)
        #   pitcher_damage          0.12 -> 0.15  (+3)
        # NOTE: this is a judgement call, not a backtested one. The 7/25
        # numbers measured DC as already well-calibrated at 0.13. Re-run
        # backtest_report.py after ~2 weeks and revert if HR_PICKS hit rate
        # drops -- git log has the old values.
        # SEASON_POWER 0.12 -> 0.24 (2026-08-09). Donovan chose to carry the
        # ISO signal inside the model rather than adjust it on the site, and
        # this is that change. The weight sweep over the graded archive:
        #
        #     weight   top-20 HR%   vs today (paired, 49 nights)
        #     0.00        19.5%     7W-7L
        #     0.12        19.7%     ← today
        #     0.18        20.5%     10W-3L  p=0.092
        #     0.24        21.1%     17W-7L  p=0.064   ← the knee
        #     0.30        21.0%     19W-10L
        #     0.50        21.0%     22W-18L
        #     0.70        21.5%     23W-19L p=0.644
        #     1.00        20.8%     19W-18L
        #
        # 0.24 is where the curve turns. Everything past it is flat and 1.00
        # comes back down, so doubling the weight buys the whole gain and
        # going further buys nothing while making the board more and more a
        # season-long power ranking. p=0.064 is not 0.05, and the honest
        # summary is "well-supported knee" rather than "proven".
        #
        # A CORRECTION WORTH KEEPING. On 2026-08-09 I reported this sweep as
        # climbing monotonically to 0.70 "without a single reversal" and
        # called that shape the finding. It was an artifact: season_power's
        # reconstruction needed season_slg, 26 of 58 graded nights don't
        # publish it, and the sweep was silently running on 32 nights while
        # its header said 58. SLG = AVG + ISO by definition, so those nights
        # are recoverable; with all 49 the monotone climb disappears and a
        # clean knee at 0.24 appears instead. Same conclusion, better reason,
        # and the earlier reasoning was wrong.
        #
        # Funded from the weights the 2026-07-25 recalibration already flagged
        # as weakest-per-point: batted_shape (30th of 43 signals, +4.0pp)
        # takes the largest cut, damage_conversion gives back most of its
        # 7/26 judgement-call raise. Sums to exactly 1.00 — asserted in test.
        # ─── TWO TERMS FOUND BY bots/missed_signals.py (2026-08-09) ─────────
        # Donovan: "there has to be a way to find missed signals, things that
        # we miss on HR hitters."
        #
        # There was, and it is not the scan we had been running. Every audit to
        # date asked "does this field separate homers?" — which mostly
        # rediscovers the model's OWN INPUTS and ranks them by how loudly they
        # shout. season_iso separates homers beautifully and hr_score already
        # knows about season_iso; finding it again is not actionable.
        #
        # The right question is conditional: among hitters the model scored the
        # SAME, does this field still separate them? Only a residual can be a
        # missed signal. Run across 4,995 graded picks in six hr_score bands,
        # with a within-band permutation test and a Benjamini-Hochberg
        # correction (170 fields means ~8 false positives at raw p<0.05):
        #
        #   pitcher_side_ops     20.3% vs 13.7%   +6.7   q=0.034
        #   pitcher_side_slug    19.6% vs 14.4%   +5.3   p=0.047
        #
        # Both are the opposing arm's production allowed TO THIS BATTER'S SIDE.
        # Both were computed, stored on the record, and then used by nothing —
        # they appear exactly twice in this file, at the dataclass default and
        # at the assignment. A field that survives a stratified test at +6.7
        # while carrying zero weight is the definition of a missed signal.
        "pitcher_side_prod": 0.04,
        # last5_hr measured +7.8 within band, the single largest residual on
        # the board. It reaches hr_raw only indirectly through batted_shape,
        # and recent_hr_form_score — which is 34% last5_hr and already computed
        # for other purposes — carried no blend weight at all. Recency is now
        # a first-class term rather than a by-product.
        "recent_form": 0.05,
        "batted_shape": 0.03,
        "pitch_fit": 0.06,
        # 0.15 → 0.10 (audit 2026-08-08): funds the aligned-stack raise; this
        # sub-score also just lost its double-counted pitch-match bonus.
        # → 0.08 (2026-08-09) funding season_power, → 0.06 funding
        # pitcher_side_prod, which is the same idea (this arm is hittable)
        # measured against the batter's actual side instead of pooled.
        "pitcher_damage": 0.06,
        "pull_launch": 0.06,
        "park_weather": 0.05,
        "lineup_opportunity": 0.01,
        "season_power": 0.24,
        "damage_conversion_score": 0.13,
        # 0.07 → 0.05 (2026-08-09), the other half of pitcher_side_prod's
        # funding. Both are matchup terms; the new one is the measured one.
        "pitch_match_term": 0.05,
        "weak_spot_interaction": 0.06,   # aligned stack (audit 2026-08-08: 27.4% at full stack)
        "bullpen_pitch_fit": 0.01,
        "yesterdays_hitters_score": 0.02,
        # NEW (2026-07-07): pitcher trend (worsening/improving vs his own
        # season baseline) and batter-vs-this-pitcher signal (only live with
        # a 10+ PA real sample). Funded by trimming batted_shape 0.27->0.24
        # and pitch_fit 0.12->0.11 (3pp total) to add these two.
        "pitcher_trend": 0.02,
        "bvp_signal": 0.02,
        # NEW (2026-07-25): h.hr_pa_score (season HR-per-PA rate, already
        # computed elsewhere as 100*minmax_norm(hr_per_pa, 0.015, 0.085) --
        # see hr_pa_score assignments) was never wired into hr_raw despite
        # its raw input, pa_per_hr, showing the single strongest inverse
        # separation of any signal measured: top decile (worst pa_per_hr)
        # 7.2% HR rate vs bottom decile (best pa_per_hr) 17.9%, a 10.6pp
        # spread, n=1,938. Started conservatively at 3pp (funded from
        # batted_shape) rather than sized to its full measured strength --
        # one recalibration pass isn't enough to bet big on a brand-new
        # blend component; revisit sizing after another month of data.
        "pa_per_hr": 0.03,
        # NEW (2026-07-31), both measured on 34 graded days / 3,265 player-days.
        #
        # k_rate (+0.04, taken from batted_shape): season strikeout rate
        # correlates with actually homering at r=+0.100 (t=5.6) and was not in
        # the blend at all. It reads backwards until you picture the swing --
        # hitters who strike out a lot swing hard and lift, and that is the
        # same swing that leaves the park. Strictly an HR-side signal; it is
        # deliberately NOT added to hit_score, where it points the other way.
        # Funded from batted_shape, which the 7/25 recalibration already
        # flagged as the weakest large weight (30th of 43 signals, +4.0pp).
        #
        # times_through (+0.02, taken from lineup_opportunity): a refinement
        # of the same idea lineup_opportunity was reaching for. Facing the
        # starter a third time is where hitters gain most, and whether a spot
        # gets that third look depends on the batting order AND how deep the
        # arm goes -- lineup_opportunity only knew the spot.
        "k_rate": 0.04,
        "times_through": 0.02,
    },
    # HR GATE (v2) thresholds -- how many of these 5 signals a player clears
    # decides the gate bonus/penalty applied to hr_raw. See "HR GATE (v2)"
    # in apply_model_v2_layers.
    "hr_gate_thresholds": {
        "iso": 0.180,
        "hrw": 70,
        "form_last5_hr": 1,
        "form_last10_hr": 3,
        "batted_shape": 80,
        "ideal_hr_contact": 0.15,
        "barrel_rate": 0.08,
    },
    # Recency-first multiplier tiers -- how much a hot/cold recent stretch
    # moves the final HR score. See "RECENCY-FIRST MULTIPLIER" in
    # apply_model_v2_layers.
    "recency_multiplier": {
        "hot_strong": 1.12,    # l5_hot and l7_hot and quality_rising
        "hot_medium": 1.08,    # l5_hot and (l7_hot or quality_rising)
        "hot_light": 1.04,     # l5_hot or l7_hot or l10_hot
        "cold_strong": 0.94,   # true_cold and quality_falling
        "cold_light": 0.96,    # true_cold only
        "neutral": 1.00,
        "soft_side_share": 0.65,  # hit/HRR/contact get this fraction of the HR-side swing
    },
}


# The blend is a weighted average -- if it stops summing to 1.00 every score
# silently shifts scale and nothing errors. The comment above has said "must
# sum to 1.00" since the weights were centralized; this makes it true.
def effective_side(bats: Any, throws: Any) -> str:
    """Which side of the plate this hitter is ACTUALLY batting from tonight.

    Switch hitters (bats == "S") bat opposite the arm -- left against a
    righty, right against a lefty -- so they hold the platoon edge in every
    matchup. Comparing bats directly against "L"/"R" silently dropped them
    out of every side-specific term: weak-side match, side HR/9, side WHIP,
    side slug/OPS and the handed pitch mix. They are ~11% of a slate.
    """
    b = (bats or "").upper()[:1]
    if b == "S":
        return "R" if (throws or "").upper()[:1] == "L" else "L"
    return b


def meatball_vs_hand(h: Any) -> "tuple":
    """(rate against this bat's side, rate against the other side, is_real).

    One resolver, because two places need the answer -- the decision engine's
    `pitcher_meatball_high` gate and the pitcher_damage blend -- and two copies
    of a platoon resolution is exactly how switch hitters fell out of every
    side-specific term for a season (see effective_side above).

    `is_real` is False whenever the side rate is just his overall rate wearing
    a split's clothes: no Statcast pull, a side under the 150-pitch floor, or a
    bat whose side could not be resolved. Callers that publish a gap must check
    it; callers that only want the best available rate need not.
    """
    overall = safe_float(getattr(h, "pitcher_meatball_pct", 0.070), 0.070)
    side = effective_side(getattr(h, "bats", ""), getattr(h, "pitcher_throws", ""))
    if side not in ("L", "R"):
        return overall, overall, False
    status = str(getattr(h, "pitcher_meatball_side_status", "missing"))
    real = (status == "ok") or (status == "one_side:%s" % side)
    lhb = safe_float(getattr(h, "pitcher_meatball_pct_vs_lhb", overall), overall)
    rhb = safe_float(getattr(h, "pitcher_meatball_pct_vs_rhb", overall), overall)
    this_side, other_side = (lhb, rhb) if side == "L" else (rhb, lhb)
    if not real:
        return overall, overall, False
    if status != "ok":
        # One real side. The rate is usable; the GAP is not, and returning the
        # other side's number here would let a caller that skipped the flag
        # compute a difference against a fabricated value.
        return this_side, this_side, False
    return this_side, other_side, True


_hr_blend_sum = round(sum(MODEL_WEIGHTS["hr_blend"].values()), 6)
if abs(_hr_blend_sum - 1.0) > 1e-6:
    raise ValueError(
        f'MODEL_WEIGHTS["hr_blend"] sums to {_hr_blend_sum}, expected 1.00. '
        "Re-balance the weights before running."
    )


def resolve_slate_date(date_arg: str, tomorrow: bool = False, days_ahead: int = 0) -> dt.date:
    """Resolve CLI slate date shortcuts without waiting for local midnight."""
    if tomorrow:
        return TODAY + dt.timedelta(days=1)
    if days_ahead:
        return TODAY + dt.timedelta(days=days_ahead)
    raw = str(date_arg or "today").strip().lower()
    if raw in {"today", "tod"}:
        return TODAY
    if raw in {"tomorrow", "tmrw", "next"}:
        return TODAY + dt.timedelta(days=1)
    return dt.date.fromisoformat(str(date_arg))


def statcast_data_end_date(slate_date: dt.date) -> dt.date:
    """For future slates, use available completed data through today."""
    return min(slate_date, TODAY)
ROOT_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT_DIR / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT_DIR / "mlb_hr_cache.sqlite"

def slate_output_label(slate_date: dt.date) -> str:
    """Return clean output label so every bot uses matching filenames."""
    return "tomorrow" if slate_date > TODAY else "today"


def write_json_and_aliases(main_path: Path, payload: Any, alias_paths: Iterable[Path]) -> None:
    """Write canonical JSON plus compatibility aliases for older dashboard/tracker code."""
    text = json.dumps(payload, indent=2)
    main_path.write_text(text, encoding="utf-8")
    for alias in alias_paths:
        if alias == main_path:
            continue
        alias.write_text(text, encoding="utf-8")


def write_text_and_aliases(main_path: Path, text: str, alias_paths: Iterable[Path]) -> None:
    """Write canonical TXT plus compatibility aliases for older dashboard/tracker code."""
    main_path.write_text(text, encoding="utf-8")
    for alias in alias_paths:
        if alias == main_path:
            continue
        alias.write_text(text, encoding="utf-8")


# ── WEBSITE REPO SYNC FIX V2 ─────────────────────────────────────────────────
# Real bot folder:
#   /Volumes/DONX/USERS/Kingdondondon/Downloads/mlb_hr_bot_starter
# Website repo:
#   /Volumes/DONX/USERS/Kingdondondon/Documents/GitHub/MLB HR MODEL
DASHBOARD_REPO = Path(os.environ.get(
    "MLB_DASHBOARD_DIR",
    str(Path(__file__).resolve().parent.parent)
))

def _sync_copy(src: Path, dest: Path, stream=None) -> bool:
    stream = stream or sys.stderr
    try:
        if not src.exists():
            print(f"⚠️ Website sync missing source: {src}", file=stream)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"📁 Website copy: {src.name} → {dest}", file=stream)
        return True
    except Exception as exc:
        print(f"⚠️ Website copy failed: {src} → {dest}: {exc}", file=stream)
        return False

def _sync_read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _sync_write_json(path: Path, payload, stream=None) -> None:
    stream = stream or sys.stderr
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"⚠️ Could not update {path}: {exc}", file=stream)

def _sync_git_best_effort(repo: Path, message: str, stream=None) -> None:
    stream = stream or sys.stderr
    if not (repo / ".git").exists():
        print(f"✅ Website files copied locally. Open GitHub Desktop for: {repo}", file=stream)
        return
    # Files are already copied. The GitHub Actions workflow commits and pushes.
    # Do not attempt git operations here — that causes rebase conflicts in CI.
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True, capture_output=True
    )
    if status.stdout.strip():
        print(f"✅ Website files staged in public/data — workflow will commit and push.", file=stream)
    else:
        print("✅ Website repo already clean after sync.", file=stream)

def _cleanup_legacy_public_data_files(data_dir: Path, stream=None) -> None:
    """Remove old duplicate slate files from public/data root only.

    Keeps stable files, history/, results/, pitch/, and debug files.
    """
    stream = stream or sys.stderr
    patterns = [
        "mlb_today_pitch_mix_version_v1_MAY_*.json",
        "mlb_today_pitch_mix_version_v1_MAY_*.txt",
        "mlb_tomorrow_pitch_mix_version_v1_MAY_*.json",
        "mlb_tomorrow_pitch_mix_version_v1_MAY_*.txt",
        "mlb_daily_breakdown_final_*.json",
        "mlb_daily_breakdown_final_*.txt",
        "mlb_tomorrow_early_breakdown_*.json",
        "mlb_tomorrow_early_breakdown_*.txt",
        "mlb_breakdown_today_*.json",
        "mlb_breakdown_today_*.txt",
        "mlb_breakdown_today_*.csv",
        "mlb_breakdown_tomorrow_*.json",
        "mlb_breakdown_tomorrow_*.txt",
        "mlb_breakdown_tomorrow_*.csv",
        "????-??-??.json",
        "????-??-??.txt",
    ]
    removed = 0
    try:
        for pat in patterns:
            for p in data_dir.glob(pat):
                if p.is_file():
                    p.unlink()
                    removed += 1
        if removed:
            print(f"🧹 V29 cleanup removed {removed} old duplicate public/data files.", file=stream)
    except Exception as exc:
        print(f"⚠️ V29 cleanup skipped: {exc}", file=stream)


def _is_dashboard_repo(p: Path) -> bool:
    """STREAMLIT MIGRATION (2026-07-25): the Next.js `app/` directory used to
    be the marker for "this is the dashboard repo". The site is Streamlit now
    and app/ is gone, so this check would have failed every run and the sync
    would have bailed with "Website repo not found". Accept either marker."""
    return (Path(p) / "streamlit_app.py").exists() or (Path(p) / "app").exists()


def sync_breakdown_to_website_repo_v2(slate_date: dt.date, slate_label: str, json_path: Path, txt_path: Path) -> None:
    """V29 clean cloud sync.

    Stable website files:
      public/data/today.json
      public/data/tomorrow.json
      public/data/history/YYYY-MM-DD-today.json
      public/data/history/YYYY-MM-DD-tomorrow.json
      public/data/index.json
      public/data/slates.json

    No legacy mlb_* aliases are copied into public/data root.
    """
    data_dir = DASHBOARD_REPO / "public" / "data"
    if not _is_dashboard_repo(DASHBOARD_REPO):
        print(f"⚠️ Website repo not found at {DASHBOARD_REPO}", file=sys.stderr)
        print("Set MLB_DASHBOARD_DIR if your website repo moves.", file=sys.stderr)
        return

    date_str = slate_date.isoformat()
    history_dir = data_dir / "history"
    current_dir = data_dir / "current"
    data_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    current_dir.mkdir(parents=True, exist_ok=True)

    # Root stable files the dashboard should read.
    stable_json = data_dir / f"{slate_label}.json"
    stable_txt = data_dir / f"{slate_label}.txt"
    current_json = current_dir / f"{slate_label}.json"
    current_txt = current_dir / f"{slate_label}.txt"

    # History lives in a folder so old slates do not clutter the root.
    hist_json = history_dir / f"{date_str}-{slate_label}.json"
    hist_txt = history_dir / f"{date_str}-{slate_label}.txt"

    copy_targets = [
        (json_path, stable_json),
        (txt_path, stable_txt),
        (json_path, current_json),
        (txt_path, current_txt),
        (json_path, hist_json),
        (txt_path, hist_txt),
    ]

    copied = 0
    for src, dest in copy_targets:
        copied += 1 if _sync_copy(src, dest, sys.stderr) else 0

    _cleanup_legacy_public_data_files(data_dir, sys.stderr)

    index_path = data_dir / "index.json"
    idx = _sync_read_json(index_path, {})
    if not isinstance(idx, dict):
        idx = {}

    current = idx.get("current", {})
    if not isinstance(current, dict):
        current = {}
    current[slate_label] = {
        "date": date_str,
        "label": slate_label.capitalize(),
        "path": f"/data/{slate_label}.json",
        "txt": f"/data/{slate_label}.txt",
        "history_path": f"/data/history/{date_str}-{slate_label}.json",
    }

    idx = idx if isinstance(idx, dict) else {}   # shape guard, see bots/check_shapes.py
    history = idx.get("history", [])
    if not isinstance(history, list):
        history = []
    history = [h for h in history if not (isinstance(h, dict) and h.get("date") == date_str and h.get("role") == slate_label)]
    history.insert(0, {
        "date": date_str,
        "role": slate_label,
        "label": f"{slate_label.capitalize()} Slate · {date_str}",
        "path": f"/data/history/{date_str}-{slate_label}.json",
        "txt": f"/data/history/{date_str}-{slate_label}.txt",
    })

    # Clean file list: stable files first, then history paths. No legacy root aliases.
    files = ["today.json", "tomorrow.json", "results_live.json", "pitch_usage_debug.json"]
    for h in history[:60]:
        if isinstance(h, dict) and h.get("path"):
            files.append(str(h["path"]).replace("/data/", ""))
    files = list(dict.fromkeys(files))[:90]

    idx["schema"] = "mlb_breakdown_data_index_v29"
    idx["current"] = current
    idx["files"] = files
    idx["history"] = history[:80]
    idx.setdefault("results", idx.get("results", []))
    idx["clean_output"] = True
    idx["latest_update"] = dt.datetime.now(dt.timezone.utc).isoformat()

    _sync_write_json(index_path, idx, sys.stderr)
    _sync_write_json(data_dir / "slates.json", idx, sys.stderr)

    print(f"✅ V29 clean sync {slate_label}: {copied} files copied. Stable path: public/data/{slate_label}.json", file=sys.stderr)
    _sync_git_best_effort(DASHBOARD_REPO, f"bot: clean {slate_label} slate {date_str}", sys.stderr)
# ─────────────────────────────────────────────────────────────────────────────

# ─── PARK FACTORS V2 ────────────────────────────────────────────────────────
# Per-stat factors sourced from Baseball Savant park-factor table (2025+ all hitters).
# Each entry: HR, HITS, 1B, 2B, 3B, K, BB, BARREL, HARDHIT, DIST, AVG_EV, AVG_DIST, ELEV, ROOF
# HR-side scoring uses HR factor; HIT-side uses HITS factor; CONTACT uses BARREL/HARDHIT.
# Values are league-relative (1.00 = league average). The legacy PARK_FACTORS dict is
# kept as a HR-factor *100 view so any older callsites still work.
PARK_FACTORS_V2: Dict[str, Dict[str, Any]] = {
    "COL": {"name": "Coors Field",                "hr": 1.14, "hits": 1.08, "b1": 1.12, "b2": 1.12, "b3": 1.36, "k": 0.86, "bb": 1.03, "barrel": 1.03, "hardhit": 1.03, "dist": 1.06, "avg_ev": 88.4, "avg_dist": 173.6, "elev": 5190, "roof": "Open"},
    "CIN": {"name": "Great American Ball Park",   "hr": 1.20, "hits": 1.02, "b1": 0.98, "b2": 1.04, "b3": 0.81, "k": 1.00, "bb": 1.03, "barrel": 1.04, "hardhit": 0.99, "dist": 1.00, "avg_ev": 88.3, "avg_dist": 173.8, "elev": 535,  "roof": "Open"},
    "ATH": {"name": "Sutter Health Park",         "hr": 1.02, "hits": 1.04, "b1": 1.01, "b2": 1.12, "b3": 0.86, "k": 0.95, "bb": 1.09, "barrel": 0.94, "hardhit": 0.99, "dist": 1.01, "avg_ev": 88.2, "avg_dist": 174.2, "elev": 24,   "roof": "Open"},
    "WSH": {"name": "Nationals Park",             "hr": 1.02, "hits": 1.03, "b1": 1.05, "b2": 0.98, "b3": 0.98, "k": 0.93, "bb": 1.00, "barrel": 1.07, "hardhit": 1.04, "dist": 1.00, "avg_ev": 88.5, "avg_dist": 162.9, "elev": 35,   "roof": "Open"},
    "CWS": {"name": "Rate Field",                 "hr": 1.05, "hits": 1.01, "b1": 1.02, "b2": 0.97, "b3": 0.85, "k": 0.98, "bb": 1.09, "barrel": 1.03, "hardhit": 1.01, "dist": 0.99, "avg_ev": 88.4, "avg_dist": 171.4, "elev": 595,  "roof": "Open"},
    "BAL": {"name": "Oriole Park at Camden Yards","hr": 1.05, "hits": 1.03, "b1": 1.03, "b2": 0.98, "b3": 1.08, "k": 0.99, "bb": 0.94, "barrel": 1.03, "hardhit": 1.02, "dist": 1.01, "avg_ev": 89.3, "avg_dist": 170.2, "elev": 33,   "roof": "Open"},
    "MIN": {"name": "Target Field",               "hr": 1.00, "hits": 1.03, "b1": 1.02, "b2": 1.07, "b3": 0.98, "k": 1.01, "bb": 0.97, "barrel": 1.04, "hardhit": 1.00, "dist": 1.01, "avg_ev": 88.5, "avg_dist": 174.3, "elev": 828,  "roof": "Open"},
    "LAA": {"name": "Angel Stadium",              "hr": 1.05, "hits": 1.02, "b1": 1.03, "b2": 0.95, "b3": 0.94, "k": 1.01, "bb": 1.04, "barrel": 1.00, "hardhit": 1.01, "dist": 1.01, "avg_ev": 88.8, "avg_dist": 168.9, "elev": 151,  "roof": "Open"},
    "MIA": {"name": "loanDepot park",             "hr": 0.96, "hits": 1.02, "b1": 1.00, "b2": 1.07, "b3": 1.14, "k": 0.98, "bb": 1.02, "barrel": 1.06, "hardhit": 1.04, "dist": 0.99, "avg_ev": 88.6, "avg_dist": 163.2, "elev": 10,   "roof": "Retractable"},
    "KC":  {"name": "Kauffman Stadium",           "hr": 0.89, "hits": 1.02, "b1": 1.01, "b2": 1.09, "b3": 1.35, "k": 0.94, "bb": 1.03, "barrel": 1.02, "hardhit": 1.02, "dist": 1.02, "avg_ev": 88.8, "avg_dist": 170.0, "elev": 856,  "roof": "Open"},
    "ARI": {"name": "Chase Field",                "hr": 0.90, "hits": 1.02, "b1": 1.00, "b2": 1.11, "b3": 1.40, "k": 0.96, "bb": 0.98, "barrel": 0.99, "hardhit": 1.03, "dist": 1.02, "avg_ev": 89.0, "avg_dist": 165.0, "elev": 1086, "roof": "Retractable"},
    "PHI": {"name": "Citizens Bank Park",         "hr": 1.07, "hits": 1.02, "b1": 1.02, "b2": 0.98, "b3": 1.02, "k": 1.04, "bb": 0.96, "barrel": 0.93, "hardhit": 0.99, "dist": 1.00, "avg_ev": 88.7, "avg_dist": 165.9, "elev": 20,   "roof": "Open"},
    "HOU": {"name": "Daikin Park",                "hr": 1.09, "hits": 1.01, "b1": 0.99, "b2": 0.98, "b3": 0.90, "k": 1.06, "bb": 1.02, "barrel": 0.98, "hardhit": 1.01, "dist": 0.99, "avg_ev": 88.3, "avg_dist": 166.7, "elev": 45,   "roof": "Retractable"},
    "TOR": {"name": "Rogers Centre",              "hr": 1.09, "hits": 1.00, "b1": 0.98, "b2": 1.01, "b3": 0.79, "k": 1.03, "bb": 0.98, "barrel": 1.05, "hardhit": 1.01, "dist": 1.00, "avg_ev": 88.5, "avg_dist": 168.6, "elev": 270,  "roof": "Retractable"},
    "ATL": {"name": "Truist Park",                "hr": 0.97, "hits": 1.02, "b1": 1.04, "b2": 1.00, "b3": 1.07, "k": 1.06, "bb": 0.98, "barrel": 0.98, "hardhit": 1.00, "dist": 1.02, "avg_ev": 89.3, "avg_dist": 167.1, "elev": 1001, "roof": "Open"},
    "LAD": {"name": "Dodger Stadium",             "hr": 1.12, "hits": 0.98, "b1": 0.95, "b2": 1.01, "b3": 0.71, "k": 1.03, "bb": 1.00, "barrel": 0.99, "hardhit": 1.00, "dist": 1.00, "avg_ev": 89.1, "avg_dist": 173.9, "elev": 515,  "roof": "Open"},
    "BOS": {"name": "Fenway Park",                "hr": 0.87, "hits": 1.02, "b1": 1.03, "b2": 1.07, "b3": 0.90, "k": 0.97, "bb": 0.98, "barrel": 0.95, "hardhit": 0.99, "dist": 0.99, "avg_ev": 88.7, "avg_dist": 159.3, "elev": 21,   "roof": "Open"},
    "TB":  {"name": "George M. Steinbrenner Field","hr": 1.04, "hits": 1.01, "b1": 1.04, "b2": 0.91, "b3": 0.76, "k": 1.01, "bb": 0.95, "barrel": 0.96, "hardhit": 1.05, "dist": 0.99, "avg_ev": 89.3, "avg_dist": 157.4, "elev": 34,   "roof": "Open"},
    "PIT": {"name": "PNC Park",                   "hr": 0.84, "hits": 1.01, "b1": 1.00, "b2": 1.14, "b3": 0.93, "k": 0.96, "bb": 1.00, "barrel": 0.98, "hardhit": 1.03, "dist": 1.00, "avg_ev": 88.8, "avg_dist": 163.7, "elev": 780,  "roof": "Open"},
    "NYY": {"name": "Yankee Stadium",             "hr": 1.04, "hits": 0.98, "b1": 0.97, "b2": 0.95, "b3": 0.91, "k": 1.01, "bb": 1.04, "barrel": 1.02, "hardhit": 1.03, "dist": 1.00, "avg_ev": 89.4, "avg_dist": 170.1, "elev": 55,   "roof": "Open"},
    "DET": {"name": "Comerica Park",              "hr": 0.98, "hits": 0.98, "b1": 0.99, "b2": 0.95, "b3": 1.20, "k": 0.98, "bb": 0.98, "barrel": 0.98, "hardhit": 1.00, "dist": 1.00, "avg_ev": 88.9, "avg_dist": 168.9, "elev": 600,  "roof": "Open"},
    "STL": {"name": "Busch Stadium",              "hr": 0.87, "hits": 1.00, "b1": 1.02, "b2": 1.03, "b3": 0.92, "k": 0.92, "bb": 0.95, "barrel": 1.01, "hardhit": 1.02, "dist": 1.00, "avg_ev": 88.9, "avg_dist": 161.5, "elev": 460,  "roof": "Open"},
    "NYM": {"name": "Citi Field",                 "hr": 0.98, "hits": 0.99, "b1": 1.01, "b2": 0.93, "b3": 0.87, "k": 1.03, "bb": 1.08, "barrel": 0.96, "hardhit": 0.99, "dist": 1.00, "avg_ev": 88.8, "avg_dist": 164.8, "elev": 10,   "roof": "Open"},
    "MIL": {"name": "American Family Field",      "hr": 1.07, "hits": 0.98, "b1": 0.98, "b2": 0.93, "b3": 0.90, "k": 1.08, "bb": 1.01, "barrel": 0.97, "hardhit": 0.96, "dist": 1.00, "avg_ev": 87.5, "avg_dist": 164.2, "elev": 597,  "roof": "Retractable"},
    "CLE": {"name": "Progressive Field",          "hr": 0.90, "hits": 0.99, "b1": 1.01, "b2": 1.03, "b3": 0.83, "k": 1.04, "bb": 1.03, "barrel": 0.97, "hardhit": 1.01, "dist": 0.99, "avg_ev": 88.0, "avg_dist": 165.4, "elev": 653,  "roof": "Open"},
    "SF":  {"name": "Oracle Park",                "hr": 0.82, "hits": 1.00, "b1": 1.03, "b2": 1.01, "b3": 1.18, "k": 0.96, "bb": 0.93, "barrel": 0.92, "hardhit": 1.00, "dist": 0.99, "avg_ev": 88.4, "avg_dist": 157.0, "elev": 0,    "roof": "Open"},
    "TEX": {"name": "Globe Life Field",           "hr": 0.97, "hits": 0.97, "b1": 0.98, "b2": 0.95, "b3": 0.90, "k": 1.01, "bb": 0.98, "barrel": 1.03, "hardhit": 1.03, "dist": 1.00, "avg_ev": 89.1, "avg_dist": 173.6, "elev": 545,  "roof": "Retractable"},
    "CHC": {"name": "Wrigley Field",              "hr": 1.01, "hits": 0.97, "b1": 0.99, "b2": 0.86, "b3": 1.06, "k": 1.02, "bb": 0.97, "barrel": 1.01, "hardhit": 1.03, "dist": 0.99, "avg_ev": 88.7, "avg_dist": 165.6, "elev": 595,  "roof": "Open"},
    "SD":  {"name": "Petco Park",                 "hr": 0.99, "hits": 0.96, "b1": 0.97, "b2": 0.93, "b3": 0.77, "k": 1.05, "bb": 1.02, "barrel": 1.00, "hardhit": 0.98, "dist": 0.99, "avg_ev": 88.2, "avg_dist": 165.2, "elev": 23,   "roof": "Open"},
    "SEA": {"name": "T-Mobile Park",              "hr": 0.96, "hits": 0.96, "b1": 0.96, "b2": 0.97, "b3": 0.67, "k": 1.10, "bb": 0.94, "barrel": 0.98, "hardhit": 1.00, "dist": 0.99, "avg_ev": 88.7, "avg_dist": 167.0, "elev": 10,   "roof": "Retractable"},
}

# Neutral fallback for unknown parks.
PARK_FACTORS_NEUTRAL: Dict[str, Any] = {
    "name": "League average", "hr": 1.00, "hits": 1.00, "b1": 1.00, "b2": 1.00, "b3": 1.00,
    "k": 1.00, "bb": 1.00, "barrel": 1.00, "hardhit": 1.00, "dist": 1.00,
    "avg_ev": 88.5, "avg_dist": 167.0, "elev": 500, "roof": "Open",
}


def get_park_factors(team_abbr: str) -> Dict[str, Any]:
    """Return the detailed park-factor dict for a home team abbreviation."""
    return dict(PARK_FACTORS_V2.get(team_abbr.upper(), PARK_FACTORS_NEUTRAL))


# Legacy single-number lookup. Kept for backward compatibility with any old callsites.
# The bot's primary scoring now uses PARK_FACTORS_V2; this view scales HR factor to a 95–113 range.
PARK_FACTORS: Dict[str, float] = {
    team: round(95.0 + (pf["hr"] - 0.82) * (113.0 - 95.0) / (1.20 - 0.82), 1)
    for team, pf in PARK_FACTORS_V2.items()
}
# Fill in any teams not in V2 with a neutral 100.
for _team in ["ARI", "ATL", "BAL", "BOS", "CHC", "CIN", "CLE", "COL", "CWS", "DET",
              "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "ATH",
              "PHI", "PIT", "SD", "SF", "SEA", "STL", "TB", "TEX", "TOR", "WSH"]:
    PARK_FACTORS.setdefault(_team, 100.0)

# Approximate MLB outfield wall distances used by Spray + Park Fit.
# These are intentionally simple LF/LCF/CF/RCF/RF lanes so the dashboard can compare
# recent batted-ball direction against today's park without needing a full CAD wall model.
PARK_DIMENSIONS: Dict[str, Dict[str, Any]] = {
    "Great American Ball Park": {"park_name":"Great American Ball Park","lf":328,"lcf":379,"cf":404,"rcf":370,"rf":325,"short_side":"RF","hr_friendly_side":"RF/LF"},
    "Yankee Stadium": {"park_name":"Yankee Stadium","lf":318,"lcf":399,"cf":408,"rcf":385,"rf":314,"short_side":"RF","hr_friendly_side":"RF"},
    "Fenway Park": {"park_name":"Fenway Park","lf":310,"lcf":379,"cf":390,"rcf":420,"rf":302,"short_side":"RF","hr_friendly_side":"LF/RF"},
    "Coors Field": {"park_name":"Coors Field","lf":347,"lcf":390,"cf":415,"rcf":375,"rf":350,"short_side":"RCF","hr_friendly_side":"LCF/RCF"},
    "Citizens Bank Park": {"park_name":"Citizens Bank Park","lf":329,"lcf":374,"cf":401,"rcf":369,"rf":330,"short_side":"LF/RF","hr_friendly_side":"LF/RF"},
    "Camden Yards": {"park_name":"Oriole Park at Camden Yards","lf":333,"lcf":384,"cf":400,"rcf":373,"rf":318,"short_side":"RF","hr_friendly_side":"RF"},
    "Oriole Park at Camden Yards": {"park_name":"Oriole Park at Camden Yards","lf":333,"lcf":384,"cf":400,"rcf":373,"rf":318,"short_side":"RF","hr_friendly_side":"RF"},
    "Dodger Stadium": {"park_name":"Dodger Stadium","lf":330,"lcf":385,"cf":395,"rcf":385,"rf":330,"short_side":"LF/RF","hr_friendly_side":"LF/RF"},
    "Wrigley Field": {"park_name":"Wrigley Field","lf":355,"lcf":368,"cf":400,"rcf":368,"rf":353,"short_side":"LCF/RCF","hr_friendly_side":"wind dependent"},
    "Comerica Park": {"park_name":"Comerica Park","lf":345,"lcf":370,"cf":412,"rcf":365,"rf":330,"short_side":"RF","hr_friendly_side":"RF","deep_risk_zone":"CF"},
    "Oracle Park": {"park_name":"Oracle Park","lf":339,"lcf":399,"cf":391,"rcf":421,"rf":309,"short_side":"RF","hr_friendly_side":"LF/RF line","deep_risk_zone":"RCF"},
    "T-Mobile Park": {"park_name":"T-Mobile Park","lf":331,"lcf":378,"cf":401,"rcf":381,"rf":326,"short_side":"RF","hr_friendly_side":"RF/LF"},
    "Petco Park": {"park_name":"Petco Park","lf":334,"lcf":390,"cf":396,"rcf":391,"rf":322,"short_side":"RF","hr_friendly_side":"RF"},
    "PNC Park": {"park_name":"PNC Park","lf":325,"lcf":389,"cf":399,"rcf":375,"rf":320,"short_side":"RF","hr_friendly_side":"RF/LF line"},
    "Kauffman Stadium": {"park_name":"Kauffman Stadium","lf":330,"lcf":387,"cf":410,"rcf":387,"rf":330,"short_side":"LF/RF","hr_friendly_side":"gaps with carry","deep_risk_zone":"CF"},
    "Target Field": {"park_name":"Target Field","lf":339,"lcf":377,"cf":404,"rcf":367,"rf":328,"short_side":"RF","hr_friendly_side":"RF/RCF"},
    "Minute Maid Park": {"park_name":"Minute Maid Park","lf":315,"lcf":362,"cf":409,"rcf":373,"rf":326,"short_side":"LF","hr_friendly_side":"LF"},
    "Globe Life Field": {"park_name":"Globe Life Field","lf":329,"lcf":372,"cf":407,"rcf":374,"rf":326,"short_side":"RF","hr_friendly_side":"LF/RF"},
    "American Family Field": {"park_name":"American Family Field","lf":344,"lcf":371,"cf":400,"rcf":374,"rf":345,"short_side":"LCF/RCF","hr_friendly_side":"gaps"},
    "Truist Park": {"park_name":"Truist Park","lf":335,"lcf":385,"cf":400,"rcf":375,"rf":325,"short_side":"RF","hr_friendly_side":"RF"},
    "Rogers Centre": {"park_name":"Rogers Centre","lf":328,"lcf":375,"cf":400,"rcf":375,"rf":328,"short_side":"LF/RF","hr_friendly_side":"LF/RF"},
    "Progressive Field": {"park_name":"Progressive Field","lf":325,"lcf":370,"cf":410,"rcf":375,"rf":325,"short_side":"LF/RF","hr_friendly_side":"LF/RF","deep_risk_zone":"CF"},
    "Busch Stadium": {"park_name":"Busch Stadium","lf":336,"lcf":375,"cf":400,"rcf":375,"rf":335,"short_side":"RF","hr_friendly_side":"LF/RF"},
    "loanDepot park": {"park_name":"loanDepot park","lf":344,"lcf":386,"cf":407,"rcf":392,"rf":335,"short_side":"RF","hr_friendly_side":"RF","deep_risk_zone":"CF/RCF"},
    "Citi Field": {"park_name":"Citi Field","lf":335,"lcf":358,"cf":408,"rcf":375,"rf":330,"short_side":"LCF/RF","hr_friendly_side":"LCF/RF"},
    "Nationals Park": {"park_name":"Nationals Park","lf":337,"lcf":377,"cf":402,"rcf":370,"rf":335,"short_side":"RF","hr_friendly_side":"RF/RCF"},
    "Angel Stadium": {"park_name":"Angel Stadium","lf":330,"lcf":387,"cf":396,"rcf":370,"rf":330,"short_side":"LF/RF","hr_friendly_side":"RCF"},
    "Chase Field": {"park_name":"Chase Field","lf":330,"lcf":374,"cf":407,"rcf":374,"rf":334,"short_side":"LF","hr_friendly_side":"LF/RCF"},
    "Sutter Health Park": {"park_name":"Sutter Health Park","lf":330,"lcf":380,"cf":403,"rcf":380,"rf":325,"short_side":"RF","hr_friendly_side":"RF"},
    # Venue-rename aliases (docket #16): the schedule now publishes these
    # names, and without the alias the lookup fell to DEFAULT dims.
    "Daikin Park": {"park_name":"Daikin Park","lf":315,"lcf":362,"cf":409,"rcf":373,"rf":326,"short_side":"LF","hr_friendly_side":"LF"},
    "Guaranteed Rate Field": {"park_name":"Rate Field","lf":330,"lcf":375,"cf":400,"rcf":375,"rf":335,"short_side":"LF","hr_friendly_side":"LF/RF"},
    "Rate Field": {"park_name":"Rate Field","lf":330,"lcf":375,"cf":400,"rcf":375,"rf":335,"short_side":"LF","hr_friendly_side":"LF/RF"},
}

DEFAULT_PARK_DIMENSIONS: Dict[str, Any] = {"park_name":"MLB Park","lf":330,"lcf":375,"cf":400,"rcf":375,"rf":330,"short_side":"LF/RF","hr_friendly_side":"neutral"}


def get_park_dimensions(venue_name: str) -> Dict[str, Any]:
    name = str(venue_name or "").strip()
    if name in PARK_DIMENSIONS:
        return dict(PARK_DIMENSIONS[name])
    low = name.lower()
    for key, dims in PARK_DIMENSIONS.items():
        if key.lower() in low or low in key.lower():
            return dict(dims)
    out = dict(DEFAULT_PARK_DIMENSIONS)
    out["park_name"] = name or out["park_name"]
    return out


def spray_lane_from_hcx(hc_x: Any) -> str:
    try:
        x = float(hc_x)
    except Exception:
        return ""
    if x < 90:
        return "LF"
    if x < 120:
        return "LCF"
    if x < 155:
        return "CF"
    if x < 185:
        return "RCF"
    return "RF"


def spray_side_for_hand(lane: str, stand: str) -> str:
    lane = str(lane or "").upper()
    stand = str(stand or "").upper()
    if not lane or stand not in {"L", "R"}:
        return "unknown"
    if stand == "R":
        if lane == "LF": return "pull"
        if lane == "LCF": return "pull_center"
        if lane == "CF": return "center"
        return "oppo"
    if stand == "L":
        if lane == "RF": return "pull"
        if lane == "RCF": return "pull_center"
        if lane == "CF": return "center"
        return "oppo"
    return "unknown"


def compute_spray_park_fit(venue_name: str, batter_hand: str, bbe_profile: Dict[str, Any], spray_points: List[Dict[str, Any]], weather: Optional[WeatherSummary] = None) -> Dict[str, Any]:
    dims = get_park_dimensions(venue_name)
    rows = [r for r in (spray_points or []) if isinstance(r, dict)]
    hand = str(batter_hand or "").upper()
    if hand not in {"L", "R"}:
        for r in rows:
            rh = str(r.get("stand") or "").upper()
            if rh in {"L", "R"}:
                hand = rh
                break
    lane_counts: Dict[str, int] = {"LF":0,"LCF":0,"CF":0,"RCF":0,"RF":0}
    damage_counts: Dict[str, int] = {"LF":0,"LCF":0,"CF":0,"RCF":0,"RF":0}
    pull_air = 0
    damage = 0
    for r in rows:
        lane = str(r.get("lane") or spray_lane_from_hcx(r.get("hc_x"))).upper()
        if lane in lane_counts:
            lane_counts[lane] += 1
            if r.get("is_hr") or r.get("is_xbh") or r.get("is_350_plus") or r.get("is_hard_hit") or r.get("is_barrel"):
                damage_counts[lane] += 1
        if r.get("is_pull_air"):
            pull_air += 1
        if r.get("is_hr") or r.get("is_xbh") or r.get("is_350_plus") or r.get("is_375_plus") or r.get("is_barrel"):
            damage += 1
    best_lane = max(damage_counts.items(), key=lambda kv: (kv[1], lane_counts.get(kv[0],0)))[0] if rows else ""
    pull_lane = "RF" if hand == "L" else "LF" if hand == "R" else ""
    pull_center_lane = "RCF" if hand == "L" else "LCF" if hand == "R" else ""
    lane_distance = safe_float(dims.get(best_lane.lower()), 400.0) if best_lane else 400.0
    pull_distance = safe_float(dims.get(pull_lane.lower()), 400.0) if pull_lane else 400.0
    n = max(1, len(rows))
    pull_air_rate = pull_air / n
    damage_rate = damage / n
    hard_rate = safe_float(bbe_profile.get("hard_hit_rate"), 0.0) if isinstance(bbe_profile, dict) else 0.0
    barrel_rate = safe_float(bbe_profile.get("barrel_rate"), 0.0) if isinstance(bbe_profile, dict) else 0.0
    d350 = safe_int(bbe_profile.get("dist_350_plus"), 0) if isinstance(bbe_profile, dict) else 0
    d375 = safe_int(bbe_profile.get("dist_375_plus"), 0) if isinstance(bbe_profile, dict) else 0
    d400 = safe_int(bbe_profile.get("dist_400_plus"), 0) if isinstance(bbe_profile, dict) else 0
    score = 40.0
    score += min(20, pull_air_rate * 42)
    score += min(18, d350 * 3.0 + d375 * 4.5 + d400 * 6.0)
    score += min(14, hard_rate * 18 + barrel_rate * 45)
    if pull_lane and pull_distance <= 325:
        score += 10
    elif pull_lane and pull_distance <= 335:
        score += 6
    if best_lane and lane_distance >= 405 and d400 == 0:
        score -= 10
    if safe_float(bbe_profile.get("gb_rate"), 0.0) >= 0.50 if isinstance(bbe_profile, dict) else False:
        score -= 6
    if weather is not None:
        score += safe_float(getattr(weather, "wind_boost", 0.0), 0.0) * 120
        score += safe_float(getattr(weather, "environment_boost", 0.0), 0.0) * 35
    score = max(0, min(100, round(score, 1)))
    if score >= 80:
        label = "Strong Park Fit"
    elif score >= 65:
        label = "Good Park Fit"
    elif score >= 50:
        label = "Neutral Park Fit"
    elif score >= 35:
        label = "Risky Park Fit"
    else:
        label = "Poor Park Fit"
    tags: List[str] = []
    if pull_lane and pull_distance <= 325 and pull_air_rate >= 0.18:
        tags.append("Short Porch Pull")
    if pull_air_rate >= 0.25:
        tags.append("Pull-Air Boost")
    if d375 >= 2:
        tags.append("375+ Carry")
    if d400 >= 1:
        tags.append("400+ Power")
    if best_lane in {"CF", "RCF", "LCF"} and lane_distance >= 400 and d400 == 0:
        tags.append("Deep Park Risk")
    if safe_float(bbe_profile.get("gb_rate"), 0.0) >= 0.50 if isinstance(bbe_profile, dict) else False:
        tags.append("Groundball Trap")
    if weather is not None and safe_float(getattr(weather, "wind_boost", 0.0), 0.0) > 0.02:
        tags.append("Wind Help")
    if not tags:
        tags.append("Neutral Fit")
    reason = f"{hand or '?'}HB best lane {best_lane or '—'} · pull lane {pull_lane or '—'} {int(pull_distance) if pull_lane else '—'} ft · damage {damage}/{n}"
    return {
        "score": score,
        "label": label,
        "best_lane": best_lane,
        "pull_lane": pull_lane,
        "pull_center_lane": pull_center_lane,
        "short_side": dims.get("short_side", ""),
        "short_side_distance": int(min([safe_float(dims.get(k), 999) for k in ["lf","lcf","cf","rcf","rf"]])),
        "best_lane_distance": int(lane_distance) if best_lane else None,
        "pull_lane_distance": int(pull_distance) if pull_lane else None,
        "pull_air_rate": round(pull_air_rate, 3),
        "damage_rate": round(damage_rate, 3),
        "tags": tags[:5],
        "reason": reason,
        "park_name": dims.get("park_name", venue_name),
        "dimensions": dims,
        "lane_counts": lane_counts,
        "damage_counts": damage_counts,
    }

TEAM_ABBR_FIXES = {"AZ": "ARI", "CHW": "CWS", "KCR": "KC", "SFG": "SF", "SDP": "SD", "TBR": "TB"}


@dataclasses.dataclass
class PitcherSummary:
    player_id: int
    name: str
    team_abbr: str
    throws: str = "?"
    era: float = 4.00
    whip: float = 1.30
    # Instrumentation (2026-08-13): era/whip/hr9 come straight from
    # client.person_stats(..., stat_type="season") a few lines below, which
    # -- unlike the hitter-side version of this exact call, and unlike this
    # class's own statcast_status/advanced_stats_status/extended_stats_status
    # -- had no try/except and no status tracking at all. A failed pull here
    # silently produces this class's own defaults (era 4.00, whip 1.30) with
    # nothing on the record distinguishing that from a real league-average
    # arm. Same "missing" until a real pull sets it otherwise as every
    # sibling status field on this class.
    season_stats_status: str = "missing"
    hr9: float = 1.10
    hr_allowed: int = 0
    k_rate: float = 0.0
    k9: float = 0.0
    babip: float = 0.300
    weak_side: str = ""
    fb_rate: float = 0.38
    statcast_bbe: int = 0
    statcast_games: int = 0
    statcast_base_bbe: int = 0
    statcast_base_games: int = 0
    statcast_status: str = "missing"
    ev_allowed: float = 88.5
    hardhit_allowed: float = 0.38
    barrel_allowed: float = 0.07
    statcast_fb_rate: float = 0.34
    dist375_allowed: int = 0
    dist400_allowed: int = 0
    # PITCHER BATTED-BALL PROFILE (2026-08-12): bb_type was already being read
    # off the same statcast pull to derive statcast_fb_rate -- these three add
    # nothing to the query, just three more groupby buckets on data already in
    # hand. Site caption on Pitchers.js has been saying gb/ld publish as 0
    # since this was never computed; now it is.
    gb_allowed: float = 0.42
    ld_allowed: float = 0.21
    popup_allowed: float = 0.05
    pitcher_attack_score: float = 0.0
    pitcher_attack_tag: str = ""
    hr9_vs_lhb: float = 1.05
    hr9_vs_rhb: float = 1.05
    whip_vs_lhb: float = 1.28
    whip_vs_rhb: float = 1.28
    hr_vs_lhb: int = 0
    hr_vs_rhb: int = 0
    xbh_vs_lhb: int = 0
    xbh_vs_rhb: int = 0
    weak_side_score_lhb: float = 0.0
    weak_side_score_rhb: float = 0.0
    weak_side_gap: float = 0.0
    l3_era: float = 4.20
    l3_whip: float = 1.30
    l3_hr9: float = 1.10
    l3_starts_found: int = 0
    fb_velo_delta: float = 0.0
    fb_velo_status: str = "missing"
    slug_vs_lhb: float = 0.400
    slug_vs_rhb: float = 0.400
    ops_vs_lhb: float = 0.720
    ops_vs_rhb: float = 0.720
    weak_spots: Tuple[int, ...] = ()
    lineup_spot_damage: Dict[str, Any] = dataclasses.field(default_factory=dict)
    lineup_zone_damage: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # DATA DEFECT #4 (2026-08-23): the five split axes the modal is waiting
    # on -- home/away, day/night, RISP, ahead/behind, by-month, by-DOW.
    # See parse_pitcher_situational_splits.
    situational_splits: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Advanced pitch-level stats from Statcast (decimals 0.0–1.0).
    # League-average fallbacks used when sample is too low or pybaseball missing.
    # ── THE RUNNING GAME (2026-08-23) ─────────────────────────────────────
    # Off StatsAPI's season pitching blob, which build_pitcher_profile already
    # fetches — wildPitches, pickoffs, balks, stolenBases and caughtStealing
    # are top-level keys on it, verified verbatim before this was written.
    # The rates are Optional because a rate over four attempts is not a rate,
    # and 0.0 would rank an unmeasured arm beside one that genuinely cannot
    # hold a runner.
    wild_pitches: int = 0
    pickoffs: int = 0
    balks: int = 0
    sb_against: int = 0
    cs_against: int = 0
    sb_attempts_against: int = 0
    cs_rate_against: Optional[float] = None   # the PAIR's number — the catcher throws it
    wp9: Optional[float] = None
    pickoff_rate: Optional[float] = None      # pickoffs per baserunner allowed
    running_game_status: str = "missing"
    meatball_pct: float = 0.070       # share of pitches in middle-middle "meatball" zone
    meatball_pct_vs_lhb: float = 0.070   # ...to LEFT-handed bats specifically
    meatball_pct_vs_rhb: float = 0.070   # ...to RIGHT-handed bats specifically
    meatball_pitches_vs_lhb: int = 0     # sample behind the LHB rate
    meatball_pitches_vs_rhb: int = 0     # sample behind the RHB rate
    meatball_side_status: str = "missing"  # ok | one_side:L | one_side:R | low_sample | missing
    putaway_pct: float = 0.180        # 2-strike finishing rate (K / 2-strike pitches)
    swstr_pct: float = 0.110          # swinging strikes / total pitches
    first_pitch_strike_pct: float = 0.600  # 1st-pitch strikes / total PA
    whiff_pct: float = 0.240          # whiffs / swings
    pullair_allowed_pct: float = 0.220   # share of BBE pulled AND in the air
    advanced_stats_sample: int = 0    # total pitches used for the rates above
    advanced_stats_status: str = "missing"
    # ── Extended season stats (per audit, 2026-07-07 request) ──────────────
    # These reuse data already fetched elsewhere (the official season `stat`
    # blob already pulled in build_pitcher_profile, plus the Statcast `psc`
    # profile already pulled for hardhit/barrel/EV) -- no new network calls.
    # FIP uses a fixed 3.10 constant since the exact seasonal MLB constant
    # isn't available here; treat it as an approximation, not the official
    # FanGraphs number.
    fip: float = 4.00
    avg_against: float = 0.250
    obp_against: float = 0.320
    slg_against: float = 0.400
    ops_against: float = 0.720
    iso_against: float = 0.150
    woba_against: float = 0.320
    tb_allowed: int = 0
    bb_allowed: int = 0
    bb_pct: float = 0.080
    # BB/9 (2026-08-12, Donovan: walks data for the site). Same extended-
    # stats block as bb_pct/bb_allowed just above -- ip is already in scope
    # in compute_pitcher_extended_stats for FIP, so this is one more line on
    # data already fetched, not a new call.
    bb9: float = 3.20
    barrels_allowed_count: int = 0
    hr_fb_pct: float = 0.100
    extended_stats_status: str = "missing"
    # Trend flag: is he getting hit harder / softer lately than his own
    # season baseline? Built from the same 5-game-trend-vs-8-game-baseline
    # split build_pitcher_statcast_profile already computes internally, just
    # exposed here as a standalone signal instead of only feeding the
    # blended rate. "worsening" = a real target; "improving" = fading away
    # from being one.
    trend_direction: str = "stable"   # "worsening" | "improving" | "stable" | "unknown"
    trend_reason: str = ""


@dataclasses.dataclass
class WeatherSummary:
    temp_f: Optional[float] = None
    wind_mph: Optional[float] = None
    wind_deg: Optional[float] = None
    roof: str = "open"
    environment_boost: float = 0.0
    humidity: Optional[float] = None          # % relative humidity
    feels_like_f: Optional[float] = None      # feels-like temp °F
    precip_chance: Optional[float] = None     # 0.0–1.0 probability of precip
    wind_direction_label: str = ""            # "out to CF", "in from CF", "crosswind", etc.
    wind_boost: float = 0.0                   # -0.06 to +0.06 based on direction vs park
    weather_source: str = "none"              # "owm", "open-meteo", or "none"


@dataclasses.dataclass
class HitterRecord:
    game_pk: int
    game_time: str
    team: str
    opponent: str
    venue_name: str
    lineup_confirmed: bool
    player_id: int
    name: str
    bats: str
    lineup_spot: int
    jersey_number: Optional[int]
    season_avg: float
    season_obp: float
    season_ops: float
    season_slg: float
    season_iso: float
    season_hr: int
    season_pa: int
    season_bb_rate: float
    season_k_rate: float
    last5_avg: float
    last5_hits: int
    last5_hr: int
    last5_xbh: int
    last5_runs: int
    last5_rbi: int
    last7_avg: float
    last7_hits: int
    last7_hr: int
    last7_xbh: int
    last7_runs: int
    last7_rbi: int
    last10_avg: float
    last10_hits: int
    last10_hr: int
    last10_xbh: int
    avg_vs_rhp: float
    avg_vs_lhp: float
    iso_vs_rhp: float
    iso_vs_lhp: float
    recent_350_num: int
    recent_350_den: int
    recent_distance_tracked: int
    recent_375_num: int
    recent_ev: float
    recent_hard_hit_rate: float
    recent_sweet_spot_rate: float
    recent_ideal_hr_contact: float
    recent_fb_rate: float
    recent_ld_rate: float
    recent_gb_rate: float
    recent_popup_rate: float
    recent_barrel_rate: float
    recent_xwoba: float
    recent_pull_rate: float
    l5_barrel_rate: float
    l10_barrel_rate: float
    l5_hard_hit_rate: float
    l10_hard_hit_rate: float
    l5_xwoba: float
    l10_xwoba: float
    l5_pull_rate: float
    l10_pull_rate: float
    l20pa_pa: int
    l20pa_bbe: int
    l20pa_hr: int
    l20pa_xbh: int
    l20pa_350_num: int
    l20pa_350_den: int
    l20pa_375_num: int
    l20pa_hard_hit_rate: float
    l20pa_ideal_hr_contact: float
    l20pa_fb_rate: float
    l20pa_barrel_rate: float
    l20pa_xwoba: float
    l20pa_pull_rate: float
    l25pa_pa: int
    l25pa_bbe: int
    l25pa_avg_ev: float
    l25pa_avg_la: float
    l25pa_hard_hit_rate: float
    l25pa_barrel_rate: float
    l25pa_sweet_spot_rate: float
    l25pa_ld_rate: float
    l25pa_gb_rate: float
    l25pa_fb_rate: float
    l25pa_popup_rate: float
    l25pa_air_rate: float
    l25pa_300_plus: int
    l25pa_375_plus: int
    l25pa_avg_bat_speed: Optional[float]
    l25pa_avg: float
    babip: float
    pitcher_id: int
    pitcher_name: str
    pitcher_team: str
    pitcher_throws: str
    pitcher_era: float
    pitcher_whip: float
    pitcher_hr9: float
    # BB/9 (2026-08-12) -- walks allowed per 9 innings, same shape as hr9.
    pitcher_bb9: float
    pitcher_hr_allowed: int
    pitcher_babip: float
    pitcher_fb_rate: float
    pitcher_statcast_bbe: int
    pitcher_statcast_games: int
    pitcher_statcast_base_bbe: int
    pitcher_statcast_base_games: int
    pitcher_statcast_status: str
    pitcher_ev_allowed: float
    pitcher_hardhit_allowed: float
    pitcher_barrel_allowed: float
    pitcher_statcast_fb_rate: float
    # PITCHER BATTED-BALL PROFILE (2026-08-12): the two the site page has been
    # captioning as "published as 0" -- ground_ball/line_drive rate allowed,
    # same bb_type column pitcher_statcast_fb_rate already reads.
    pitcher_gb_rate: float
    pitcher_ld_rate: float
    pitcher_popup_rate: float
    pitcher_375_allowed: int
    pitcher_400_allowed: int
    pitcher_attack_score: float
    pitcher_attack_tag: str
    pitcher_hr9_vs_lhb: float
    pitcher_hr9_vs_rhb: float
    pitcher_whip_vs_lhb: float
    pitcher_whip_vs_rhb: float
    pitcher_weak_side: str
    pitch_mix_score: float
    pitch_mix_note: str
    pitcher_primary_mix: str
    pitch_mix_sample: int
    weather_temp_f: Optional[float]
    weather_wind_mph: Optional[float]
    weather_wind_deg: Optional[float]
    roof: str
    park_factor: float
    # REMOVED per audit (2026-06-27): numerology_score (literal date/jersey-
    # number digit math, no statistical basis) confirmed dead -- only ever
    # fed numerology_pair_score, which was itself never called anywhere.
    weather_humidity: Optional[float] = None
    weather_feels_like_f: Optional[float] = None
    weather_precip_chance: Optional[float] = None
    weather_wind_direction_label: str = ""
    weather_wind_boost: float = 0.0
    weather_source: str = "none"
    weak_spot_flag: bool = False
    weak_spot_bonus: float = 0.0
    weak_spot_reason: str = ""
    multi_hit_score: float = 0.0
    multi_hit_flag: bool = False
    multi_hit_reason: str = ""
    hr_score: float = 0.0
    # Shadow A/B fields (2026-07-13): power-anchored score with NO recency
    # multiplier, plus its board rank. Exported via asdict for grading.
    hr_score_shadow: float = 0.0
    shadow_board_rank: int = 0
    # Longest-HR distance metric (2026-07-13): who hits the farthest ball,
    # from recent 400ft+/350ft+ rates, avg EV, and avg batted-ball distance.
    longest_hr_score: float = 0.0
    longest_hr_rank: int = 0
    hit_score: float = 0.0
    hrr_score: float = 0.0
    contact_score: float = 0.0
    self_check_hr_score: float = 0.0
    hr_confidence: float = 0.0
    # data_quality_score removed (audit 2026-06-29): was declared here but
    # never set or read anywhere in the file. Shipped as 0.0 in every player
    # record via dataclasses.asdict(). Deleted to stop polluting JSON output.
    lineup_pre_onbase: float = 0.0
    lineup_pre_babip: float = 0.0
    lineup_post_convert: float = 0.0
    lineup_post_babip: float = 0.0
    lineup_context_score: float = 0.0
    lineup_surrounding_recent: float = 0.0
    pitcher_weak_side_score: float = 0.0
    pitcher_weak_side_gap: float = 0.0
    pitcher_side_slug: float = 0.400
    pitcher_side_ops: float = 0.720
    weak_side_bonus: float = 0.0
    lineup_context_before_count: int = 0
    lineup_context_after_count: int = 0
    overall_score: float = 0.0
    hrw_score: float = 0.0
    hr_per_pa: float = 0.0
    hr_pa_score: float = 0.0
    # V2 model / dashboard-safe add-ons. Old score fields stay live for website compatibility.
    hr_score_legacy: float = 0.0
    hit_score_legacy: float = 0.0
    hrr_score_legacy: float = 0.0
    contact_score_legacy: float = 0.0
    overall_score_legacy: float = 0.0
    hr_score_v2: float = 0.0
    # The unblended model output, kept alongside the published hr_score so the
    # 2026-08-09 opportunity fold (0.70 hr + 0.20 hrr + 0.10 hit) can always be
    # measured against what it replaced. See the fold's comment block.
    hr_score_pure: float = 0.0
    hit_score_v2: float = 0.0
    hrr_score_v2: float = 0.0
    contact_score_v2: float = 0.0
    # RENAMED per audit (2026-06-27): was top_pick_score_v2, which computed a
    # real formula early then got immediately overwritten with a copy of
    # overall_score, and was never read by any frontend component (confirmed
    # via search -- present in the output JSON via dataclasses.asdict, but
    # nothing displayed it). Repurposed into a genuinely distinct second
    # opinion: rewards balance across hr/hrr/hit/contact scores (penalizes
    # lopsided one-trick profiles), discounts low-sample-size players, and
    # gives a small nudge for HR "dueness" (see hr_due_ratio).
    consistency_score: float = 0.0
    season_rbi: int = 0
    season_runs: int = 0
    season_rbi_per_pa: float = 0.0
    season_runs_per_pa: float = 0.0
    best_blend_score: float = 0.0
    alt_hr_score: float = 0.0
    recent_hr_form_score: float = 0.0
    batted_ball_power_score: float = 0.0
    matchup_power_score: float = 0.0
    pitch_mix_boost: float = 0.0
    bullpen_attack_score: float = 0.0
    # Bullpen pitch-mix fit (per audit, 2026-06-27): same calculate_pitch_mix_fit
    # logic already used for the starter, run against the bullpen's IP-weighted
    # team_mix instead. Reflects how well this batter matches up against the
    # pitches he's likely to see if the game goes to the bullpen.
    bullpen_pitch_fit: float = 50.0
    # Pitcher last-3-starts window (per audit, 2026-06-27) -- mirrors batter
    # last5/7/10 windows, which pitchers never had. Real game-to-game
    # variability in starting pitcher performance is documented and not
    # just noise around the season average.
    pitcher_l3_era: float = 4.20
    pitcher_l3_whip: float = 1.30
    pitcher_l3_hr9: float = 1.10
    pitcher_l3_starts_found: int = 0
    # Fastball velocity delta: most recent start vs season average. Negative
    # = velocity down (potential fatigue/decline warning).
    pitcher_fb_velo_delta: float = 0.0
    pitcher_fb_velo_status: str = "missing"
    batter_vs_bullpen_score: float = 0.0
    bullpen_era: float = 4.20
    bullpen_hr9: float = 1.10
    bullpen_whip: float = 1.30
    bullpen_quality: str = "average"
    bvp_pa: int = 0
    bvp_ab: int = 0
    bvp_hits: int = 0
    bvp_hr: int = 0
    bvp_xbh: int = 0
    bvp_avg: float = 0.0
    bvp_ops: float = 0.0
    bvp_note: str = "No BvP sample"
    hr_reason: str = ""
    hit_reason: str = ""
    hrr_reason: str = ""
    contact_reason: str = ""
    top_pick_reason: str = ""
    alt_reason: str = ""
    top_board_score_v2: float = 0.0
    top_board_rank_reason: str = ""
    top_board_bucket: str = ""
    top_board_tags: List[str] = dataclasses.field(default_factory=list)
    pmix_gate: str = "neutral"
    hrw_zone: str = "watch"
    lineup_spot_risk: str = "unknown"
    trap_risk_flag: bool = False
    high_confidence_hr_flag: bool = False
    high_confidence_hr_score: float = 0.0
    pitcher_spot_damage_score: float = 0.0
    pitcher_spot_damage_label: str = "Unknown"
    pitcher_spot_damage_reason: str = ""
    pitcher_zone_damage_score: float = 0.0
    pitcher_zone_damage_label: str = "Unknown"
    pitcher_zone_damage_reason: str = ""
    pitcher_lineup_spot_damage: Dict[str, Any] = dataclasses.field(default_factory=dict)
    pitcher_lineup_zone_damage: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # The five missing split axes, published for the modal's split control
    # (2026-08-23, data defect #4). Empty dict when the pull failed --
    # the site's rule: no data, no button.
    pitcher_situational_splits: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Matchup Attack Center fields for dashboard. Safe defaults keep older JSON compatible.
    pitcher_k_rate: float = 0.0
    pitcher_k9: float = 0.0
    matchup_score: float = 0.0
    matchup_tier: str = "Neutral"
    matchup_label: str = "Neutral"
    matchup_reason: str = ""
    pitcher_low_k_flag: bool = False
    weak_pitcher_flag: bool = False
    pitcher_safe_flag: bool = False
    # HR Score 2.0 beginner/dashboard fields. One displayed HR score, with old score kept for backend comparison.
    hr_score_old: float = 0.0
    hr_score_delta: float = 0.0
    hr_confidence_tier: str = "Weak"
    best_bet_type: str = "HRR / Hits"
    beginner_label: str = "Safer Production Play"
    simple_reason_1: str = ""
    simple_reason_2: str = ""
    simple_reason_3: str = ""
    risk_reason: str = ""
    advanced_reason: str = ""
    pitch_fit_summary: str = ""
    trap_flag: bool = False
    trap_reason: str = ""
    hidden_hr_value: bool = False
    hidden_value_reason: str = ""
    park_fit_summary: str = ""
    hr_shape_components: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # V31 Damage Conversion / Decision Engine fields.
    # These separate true HR plays from HRR/XBH and stop hard-fading power bats with a real mistake path.
    damage_conversion_score: float = 0.0
    damage_conversion_label: str = "Unknown"
    damage_conversion_reasons: List[str] = dataclasses.field(default_factory=list)
    best_damage_pitch_v31: str = ""
    pitcher_mistake_pitch_v31: str = ""
    pitcher_mistake_match: bool = False
    true_avoid_hr: bool = False
    power_watch_flag: bool = False
    hrr_xbh_flag: bool = False
    final_hr_role: str = "🧭 Contact / Monitor"
    best_use: str = "Hit / contact only"
    decision_reasons: List[str] = dataclasses.field(default_factory=list)
    avoid_hr_reasons: List[str] = dataclasses.field(default_factory=list)
    risk_flags_v31: List[str] = dataclasses.field(default_factory=list)
    missing_data_flags: List[str] = dataclasses.field(default_factory=list)
    confidence_penalty_reason: str = ""
    # Website/Pitch Lab embedded data blocks. These make today.json/tomorrow.json self-contained.
    bbe_profile: Dict[str, Any] = dataclasses.field(default_factory=dict)
    spray_chart: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    contact_log: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    batted_ball_log: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    # PERSONAL HR SHAPE (2026-08-14, Donovan: "each player needs to be
    # categorized by the homers they hit this season... maybe it will help
    # figure out when a certain batter is in their form -- not the overall
    # shape but their personal shape"). hr_shape_profile is his season homer
    # mix in the same five bands the site's lib/hrShape.js cuts (wall-scraper
    # / laser / standard / moonshot / no-doubter -- percentile slices of the
    # archive's homer distribution, not physics; see that file), plus his own
    # homer launch-angle window (la_lo/la_hi). The personal_shape_* fields
    # are the "is he in HIS form" read: of recent hard-hit balls, the share
    # landing inside HIS OWN homer launch-angle window, minus the same share
    # season-long -- positive means his recent contact is trending toward the
    # shape his homers actually take. DELIBERATELY NOT WIRED INTO ANY SCORE:
    # the generic version of this idea (recent_barrel_rate) does not predict
    # which night a hitter homers (p=0.58 on the graded archive), so this
    # ships as a computed + ARCHIVED field only -- a few weeks of graded
    # slates will say whether the PERSONAL version predicts anything before
    # it ever touches hr_score. Defaulted so old locked/saved rows still
    # reconstruct (same rule as every recent field addition here).
    hr_shape_profile: Dict[str, Any] = dataclasses.field(default_factory=dict)
    personal_shape_match: float = 0.0
    personal_shape_recent_rate: float = 0.0
    personal_shape_season_rate: float = 0.0
    personal_shape_status: str = "missing"
    pitcher_pitch_mix: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Full LHB/RHB pitch mix splits, kept separate from pitcher_pitch_mix (which
    # is selected to match THIS batter's own hand, for scoring). The frontend
    # toggle in PitchBreakdown.js needs BOTH splits regardless of batter hand,
    # so they're saved here unconditionally rather than discarded after the
    # per-batter selection above.
    pitcher_pitch_mix_vs_lhb: Dict[str, Any] = dataclasses.field(default_factory=dict)
    pitcher_pitch_mix_vs_rhb: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Flat convenience fields — these are what PitchBreakdown.js checks FIRST
    # (before falling back to pitcher_pitch_mix_vs_lhb/rhb above), so populate
    # both paths to avoid relying on the fallback chain.
    pitcher_pitch_type_summary_vs_lhb: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    pitcher_pitch_type_summary_vs_rhb: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    pitcher_primary_mix_vs_lhb: str = ""
    pitcher_primary_mix_vs_rhb: str = ""
    # Raw HR / XBH allowed counts by batter handedness (season-to-date,
    # not rates) — pulled from the same pitcher_split_stats call as
    # pitcher_hr9_vs_lhb/rhb above, just captured before being discarded.
    pitcher_hr_vs_lhb: int = 0
    pitcher_hr_vs_rhb: int = 0
    pitcher_xbh_vs_lhb: int = 0
    pitcher_xbh_vs_rhb: int = 0
    # New per audit (2026-06-27): HR dueness ratio (games_since_last_hr vs
    # expected cycle from hr_per_pa) and the stacked-bad-signal hard override
    # flag, both computed inside batted_shape.
    hr_due_ratio: float = 1.0
    hr_unreliable_shape_flag: bool = False
    # "Yesterdays Hitters" custom highlight match score, ported from the
    # user's external highlight tool (per request, 2026-06-29). Graded 0-100
    # (one of 9 criteria per ~11.1 points) plus an all-or-nothing flag for
    # the highlight tool's own "all must match" semantics.
    yesterdays_hitters_score: float = 0.0
    yesterdays_hitters_all_match: bool = False
    batter_pitch_type_profile: Dict[str, Any] = dataclasses.field(default_factory=dict)
    pitch_mix_matchup: Dict[str, Any] = dataclasses.field(default_factory=dict)
    pitch_type_summary: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    game_log: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    statcast_pull_status: str = "unknown"
    # Instrumentation (2026-08-13): two more "did the real pull work" flags,
    # same shape as statcast_pull_status just above and
    # pitcher_statcast_status/pitcher_fb_velo_status further up -- added
    # after last5_runs/last5_rbi/pitcher_whip/pitcher_era sat un-populating
    # in the graded archive for weeks with nothing on the record able to say
    # why. Defaulted (not required) so old locked/saved rows that predate
    # this field still reconstruct fine -- see build_hitter_records() for
    # where these get their real values.
    last5_status: str = "unknown"
    pitcher_season_stats_status: str = "unknown"
    # park_dimensions removed (audit 2026-06-29): was declared here but the
    # assignment (h.park_dimensions = park_dims) was never written. The local
    # variable park_dims at line ~6798 is only passed into compute_spray_park_fit()
    # as an argument; its output (park_fit, a different field) is what gets stored.
    # Shipped as {} in every player record via dataclasses.asdict(). Deleted.
    park_fit: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Advanced pitcher stats — sourced from Statcast/pybaseball when available.
    # Each is a decimal 0.0–1.0. Defaults are league averages so the model behaves
    # neutrally when the data is missing instead of NaN-ing out.
    # ── THE RUNNING GAME, ON THE HITTER'S ROW (2026-08-23) ────────────────
    # Donovan asked for wild pitches, pickoffs and pitcher stolen-bases-against
    # among "the other six" stats. Four of the six were never missing — they
    # were keys on a blob build_pitcher_profile already fetched every night.
    #
    # They land on the hitter's row like every other pitcher fact here,
    # because a slate row is one hitter facing one arm and the site reads it
    # that way. The rates are Optional: a caught-stealing rate over four
    # attempts is not a rate, and a 0.0 would put an unmeasured arm at the
    # bottom of the steal board beside one who genuinely cannot hold anybody.
    # ── WHO IS CATCHING, AND WHAT IS BEHIND HIM (2026-08-23) ──────────────
    # Donovan: "catcher CS%, team defense." Both are league-wide tables on
    # Baseball Savant (bots/savant_feeds.py); neither is on StatsAPI in a
    # usable shape. The catcher is resolved off the boxscore's posted order —
    # see find_catcher — and `catcher_source` travels with him because a
    # confirmed catcher and a likely one are not the same fact.
    #
    # opp_catcher_cs_rate is Optional and stays None under 10 attempts. A
    # backup at 1-of-2 is not a 50% thrower, and the steal board would put him
    # top.
    opp_catcher_id: int = 0
    opp_catcher_name: str = ""
    opp_catcher_source: str = ""            # lineup | roster | "" (not found)
    opp_catcher_cs_rate: Optional[float] = None
    opp_catcher_cs_rate_expected: Optional[float] = None
    opp_catcher_pop_time: Optional[float] = None
    opp_catcher_arm_strength: Optional[float] = None
    opp_catcher_sb_attempts: int = 0
    opp_catcher_status: str = "missing"
    # Team defence behind tonight's arm, split by batter hand — which is the
    # half that matters here, because every other pitcher term on this row is
    # already asking a platoon question.
    opp_def_oaa: Optional[int] = None
    opp_def_oaa_vs_hand: Optional[int] = None
    opp_def_success_rate: Optional[float] = None
    opp_def_status: str = "missing"
    pitcher_wild_pitches: int = 0
    pitcher_pickoffs: int = 0
    pitcher_balks: int = 0
    pitcher_sb_against: int = 0
    pitcher_cs_against: int = 0
    pitcher_sb_attempts_against: int = 0
    pitcher_cs_rate_against: Optional[float] = None
    pitcher_wp9: Optional[float] = None
    pitcher_pickoff_rate: Optional[float] = None
    pitcher_running_game_status: str = "missing"
    pitcher_meatball_pct: float = 0.070
    # The hand split of the same number (2026-08-23) -- see the long note in
    # build_pitcher_advanced_stats. `_vs_hand` is the one that applies to THIS
    # bat tonight, switch-hitter aware; `_edge_pp` is how many percentage
    # points MORE middle-middle this side sees than the other one.
    pitcher_meatball_pct_vs_lhb: float = 0.070
    pitcher_meatball_pct_vs_rhb: float = 0.070
    pitcher_meatball_pitches_vs_lhb: int = 0
    pitcher_meatball_pitches_vs_rhb: int = 0
    pitcher_meatball_side_status: str = "missing"
    meatball_pct_vs_hand: float = 0.070
    meatball_edge_pp: float = 0.0
    # THE GRADED COLUMN. Published and archived, deliberately NOT in hr_blend:
    # the standing rule is that no hr_blend weight moves before roadmap 9c
    # (~2026-09-22) because the graded archive still manufactures tuning
    # signals out of its own leak. So this ships as a column that is measured
    # for a few weeks and earns its way into the score afterwards -- the same
    # path personal_shape_match took. Status exists so a dead 0.0 on an arm
    # with no Statcast data is never mistaken for "this matchup is cold".
    # ── THE RUNNING GAME MODEL (2026-08-23) ───────────────────────────────
    # Donovan asked for a model built on the stats he listed. This is it, and
    # like meatball_fit_score it is a PUBLISHED, GRADED COLUMN worth zero
    # points in any blend — the standing rule is that no hr_blend weight moves
    # before 9c, and a steal model has no business inside a home-run score
    # anyway. It exists so the steal board can rank on something better than
    # raw volume, and so the question "do high-risk spots actually produce
    # steals" becomes answerable off graded nights.
    steal_risk_score: float = 0.0
    steal_risk_status: str = "missing"   # ok | thin | missing
    steal_risk_note: str = ""
    meatball_fit_score: float = 0.0
    meatball_fit_status: str = "missing"   # ok | no_side_split | missing
    meatball_fit_note: str = ""
    hr_pace_flag: bool = False             # honest EV-gap dueness x hot recent pitcher HR9
    hr_pace_gap: float = 0.0               # expected HRs (season rate) minus actual, over his recent PA
    hr_pace_note: str = ""
    pitcher_putaway_pct: float = 0.180
    pitcher_swstr_pct: float = 0.110
    pitcher_first_pitch_strike_pct: float = 0.600
    pitcher_whiff_pct: float = 0.240
    pitcher_pullair_allowed_pct: float = 0.220
    pitcher_advanced_stats_sample: int = 0
    pitcher_advanced_stats_status: str = "missing"
    # ── Extended pitcher stats, mirrors PitcherSummary (per audit, 2026-07-07
    # request) -- same reasoning as the block above: these reuse data already
    # fetched elsewhere, no new network calls.
    pitcher_fip: float = 4.00
    pitcher_avg_against: float = 0.250
    pitcher_obp_against: float = 0.320
    pitcher_slg_against: float = 0.400
    pitcher_ops_against: float = 0.720
    pitcher_iso_against: float = 0.150
    pitcher_woba_against: float = 0.320
    pitcher_tb_allowed: int = 0
    pitcher_bb_allowed: int = 0
    pitcher_bb_pct: float = 0.080
    pitcher_barrels_allowed_count: int = 0
    pitcher_hr_fb_pct: float = 0.100
    pitcher_extended_stats_status: str = "missing"
    pitcher_trend_direction: str = "stable"
    pitcher_trend_reason: str = ""
    # ── Richer batter-vs-this-pitcher (per audit, 2026-07-07 request) --
    # extends the existing bvp_* fields (pa/ab/hits/hr/xbh/avg/ops/note)
    # computed from the SAME Statcast slice build_batter_vs_pitcher_profile
    # already pulls, just aggregating more of it.
    bvp_babip: float = 0.300
    bvp_woba: float = 0.320
    bvp_iso: float = 0.150
    bvp_obp: float = 0.320
    bvp_k_pct: float = 0.220
    bvp_bb_pct: float = 0.080
    bvp_barrels: int = 0
    bvp_hard_hit: int = 0
    # Hitter ambush profile — currently a derived flag based on lineup spot + 1st-pitch
    # patterns from game logs. Future-ready field; defaults to league average.
    hitter_first_pitch_swing_pct: float = 0.280
    # Games since the hitter's last HR (capped at 60). Drives Due-state detection
    # on the frontend. Computed from MLB game log.
    games_since_last_hr: int = 60
    # ── The last game he BATTED in, and his own bounce-back record ──────────
    # From compute_blank_profile(), off the same game log last5/7/10 already
    # use — no extra request. The site's "After a blank" lens on Boards reads
    # these; every field defaults so a row locked before this shipped still
    # reconstructs, and the site treats blank_profile_status != "ok" as
    # "unknown", never as "he didn't blank."
    last_game_date: str = ""
    last_game_ab: int = 0
    last_game_pa: int = 0
    last_game_hits: int = 0
    last_game_hr: int = 0
    last_game_tb: int = 0
    last_game_rbi: int = 0
    last_game_runs: int = 0
    last_game_hrr: int = 0
    blank_streak: int = 0
    after_blank_n: int = 0
    after_blank_hit: int = 0
    after_blank_hrr1: int = 0
    after_blank_hrr2: int = 0
    after_blank_tb2: int = 0
    # The control cohorts (2026-08-16). overall_* is his normal rate across
    # every batted game -- context. after_hit_* is the clean complement of
    # after_blank_* and the only honest input to a two-proportion test. See
    # compute_blank_profile for why both exist and why they are not the same.
    overall_n: int = 0
    overall_hit: int = 0
    overall_hrr1: int = 0
    overall_hrr2: int = 0
    overall_tb2: int = 0
    after_hit_n: int = 0
    after_hit_hit: int = 0
    after_hit_hrr1: int = 0
    after_hit_hrr2: int = 0
    after_hit_tb2: int = 0
    blank_profile_status: str = "unknown"
    # Per-stat park factors. Set in build_hitter_records from PARK_FACTORS_V2.
    park_hr_factor: float = 1.00
    park_hits_factor: float = 1.00
    park_barrel_factor: float = 1.00
    park_hardhit_factor: float = 1.00
    park_k_factor: float = 1.00
    park_dist_factor: float = 1.00
    # Matchup tags driven by the new stats.
    ambush_setup_flag: bool = False         # Low 1stPS pitcher × early-swing hitter
    mistake_pitch_setup_flag: bool = False  # High meatball% × high pullair-allowed
    k_trap_flag: bool = False                # High putaway%/swstr% × hitter with K issues
    # Pitch-type match: hitter's crush pitch matches pitcher's HR-vulnerable pitch.
    # Surfaced as 🎯 PITCH-MATCH tag when true.
    pitch_type_match_flag: bool = False
    pitch_type_match_code: str = ""        # e.g. "SL"
    pitch_type_match_note: str = ""        # e.g. "Crushes SL (.450 EV 94.2) vs pitcher 28% SL HR-prone (4 HR allowed)"
    pitch_type_match_score: float = 0.0    # 0-120 (capped), used for bonus weighting
    # Full per-pitch arsenal with damage allowed (HR, EV, hard-hit% per pitch type).
    # List of dicts from pitch_type_summary — used for display + deeper analysis.
    pitcher_pitch_arsenal_detail: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    # Docket #4-7: exact season counting stats (were client-estimated).
    season_tb: int = 0
    # STOLEN BASES v1 (2026-08-23) — fetched-and-discarded until now; see
    # claude/moonshot-sb-research.md. No score reads these.
    season_sb: int = 0
    season_cs: int = 0
    season_sb_attempt_rate: float = 0.0
    season_ab: int = 0
    season_doubles: int = 0
    season_triples: int = 0
    season_babip: float = 0.0
    # Docket #19: true distance fields for the Longest board (site pre-wired).
    recent_400_num: int = 0
    recent_max_distance: float = 0.0
    recent_avg_distance: float = 0.0
    recent_avg_hr_distance: float = 0.0
    recent_pull_air_rate: float = 0.0
    recent_squared_up_rate: Optional[float] = None
    recent_squared_up_sample: int = 0
    recent_blast_rate: Optional[float] = None
    recent_bat_tracking_status: str = "missing"
    recent_bat_tracking_window: str = ""
    season_max_distance: float = 0.0
    # Docket #20: expected HRs from contact + luck (actual − expected).
    season_xhr: float = 0.0
    season_hr_luck: float = 0.0
    recent_xhr: float = 0.0
    xhr_bbe: int = 0
    pitcher_xhr_allowed: float = 0.0
    pitcher_hr_luck: float = 0.0
    pitcher_xhr_bbe: int = 0
    # MODEL FOUNDATION (2026-08-21): which scoring logic produced this row,
    # and which bot execution produced it. Both DEFAULTED on purpose -- see
    # load_locked_rows_by_game() above: a no-default field on HitterRecord
    # silently drops any in-flight locked/saved row that predates the field,
    # because that loader builds each row from a saved JSON dict inside a
    # bare try/except and only back-fills fields that have a dataclass
    # default. An empty string here means "written before this registry
    # existed" -- read as `pre_registry` at analysis time, never silently
    # relabeled as any specific version. model_version is the HR market's
    # version (bots/model_registry.py MODEL_VERSIONS["hr"]) since HR is the
    # flagship score every graded slot is compared against; the full
    # per-market version map for a run lives in that run's prediction-log
    # run_meta line, not repeated on every row. See docs/MODELS.md.
    model_version: str = ""
    run_id: str = ""
    # PROVENANCE (2026-08-21): deterministic fingerprint of the exact HR
    # scoring configuration in effect when this row was scored -- see
    # bots/config_fingerprint.py and docs/MODELS.md. Same defaulted-field
    # safety reasoning as model_version/run_id directly above: a no-default
    # field silently drops any in-flight locked/saved row that predates it
    # (see load_locked_rows_by_game()). Empty string means "written before
    # this field existed, or the fingerprint module failed to import" --
    # read as unknown provenance at analysis time, never backfilled with
    # today's live hash. model_version answers "what did we DECLARE we were
    # running"; config_hash answers "what was ACTUALLY in effect" -- the
    # machine-verifiable backstop for an unbumped version.
    config_hash: str = ""


class CacheDB:
    def __init__(self, path: Path) -> None:
        # This cache only ever holds re-fetchable API/Statcast responses --
        # nothing here is irreplaceable, so a corrupted file (crash mid-write,
        # disk issue, concurrent access, etc.) should degrade to "start fresh
        # and re-fetch," not crash the entire run. Confirmed real failure
        # mode: sqlite3.DatabaseError: database disk image is malformed,
        # raised straight out of CREATE TABLE before main() ever got a chance
        # to run.
        try:
            self.conn = sqlite3.connect(str(path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_items (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.conn.commit()
        except sqlite3.DatabaseError as exc:
            try:
                self.conn.close()
            except Exception:
                pass
            quarantine = path.with_name(f"{path.name}.corrupt-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}")
            try:
                if path.exists():
                    path.rename(quarantine)
                print(
                    f"⚠️ Cache DB was corrupted ({exc}); moved it to {quarantine.name} "
                    f"and starting a fresh cache. Nothing important is lost -- this "
                    f"file only holds re-fetchable API data.",
                    file=sys.stderr,
                )
            except Exception as rename_exc:
                print(f"⚠️ Cache DB was corrupted and could not be quarantined ({rename_exc}); overwriting.", file=sys.stderr)
            self.conn = sqlite3.connect(str(path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_items (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.conn.commit()

    def get(self, key: str, max_age_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT payload, updated_at FROM cache_items WHERE cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        if max_age_days is not None:
            try:
                updated = dt.datetime.fromisoformat(row["updated_at"])
                age = dt.datetime.now(dt.timezone.utc) - updated
                if age > dt.timedelta(days=max_age_days):
                    return None
            except Exception:
                return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return None

    def set(self, key: str, payload: Dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO cache_items(cache_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (key, json.dumps(payload), dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def build_recent_bat_tracking_lookup(db: CacheDB, end_date: dt.date) -> Dict[str, Dict[str, Any]]:
    """One official Savant bat-tracking pull shared by the entire slate.

    Squared-Up is not present in pybaseball's pitch-level CSV, so deriving it
    locally would require recreating Statcast's maximum-EV physics.  Savant's
    own bat-tracking leaderboard publishes the exact recent rate.  Pull the
    leaderboard once, cache it by window, and join by MLB player id.
    """
    start_date = end_date - dt.timedelta(days=14)
    window = f"{start_date.isoformat()}..{end_date.isoformat()}"
    key = f"bat_tracking_leaderboard_v1:{window}"
    cached = db.get(key, max_age_days=1)
    if isinstance(cached, dict) and isinstance(cached.get("players"), dict):
        return cached["players"]

    url = "https://baseballsavant.mlb.com/leaderboard/bat-tracking"
    params = {
        "seasonStart": end_date.year,
        "seasonEnd": end_date.year,
        "type": "batter",
        "minSwings": 1,
        "dateStart": start_date.isoformat(),
        "dateEnd": end_date.isoformat(),
        "csv": "true",
    }
    players: Dict[str, Dict[str, Any]] = {}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
        for row in reader:
            player_id = str(row.get("id") or "").strip()
            if not player_id:
                continue
            players[player_id] = {
                "squared_up_per_bat_contact": safe_float(row.get("squared_up_per_bat_contact"), 0.0),
                "blast_per_bat_contact": safe_float(row.get("blast_per_bat_contact"), 0.0),
                "contact": safe_int(row.get("contact"), 0),
                "window": window,
                "source": "baseball_savant_bat_tracking",
            }
        if players:
            db.set(key, {"players": players, "window": window, "source": "baseball_savant_bat_tracking"})
    except Exception as exc:
        print(f"⚠️ Recent Savant bat-tracking pull failed ({type(exc).__name__}); Squared-Up will be unavailable.", file=sys.stderr)
    return players


class MLBClient:
    def __init__(self, pause: float = 0.03) -> None:
        self.session = requests.Session()
        self.pause = pause
        self._cache: Dict[str, Any] = {}

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        key = url + "?" + json.dumps(params or {}, sort_keys=True, default=str)
        if key in self._cache:
            return self._cache[key]
        resp = self.session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        self._cache[key] = data
        if self.pause:
            time.sleep(self.pause)
        return data

    def schedule(self, date_: dt.date) -> List[Dict[str, Any]]:
        data = self.get_json(
            f"{MLB_BASE}/schedule",
            params={"sportId": 1, "date": date_.isoformat(), "hydrate": "probablePitcher,team,venue(timezone)"},
        )
        return data.get("dates", [{}])[0].get("games", [])

    def live_game(self, game_pk: int) -> Dict[str, Any]:
        return self.get_json(f"{MLB_BASE}.1/game/{game_pk}/feed/live")

    def person(self, player_id: int) -> Dict[str, Any]:
        return self.get_json(f"{MLB_BASE}/people/{player_id}")

    def person_stats(self, player_id: int, group: str, stat_type: str = "season", season: Optional[int] = None) -> Dict[str, Any]:
        params = {"stats": stat_type, "group": group, "season": season or SEASON}
        return self.get_json(f"{MLB_BASE}/people/{player_id}/stats", params=params)

    def person_game_log(self, player_id: int, season: Optional[int] = None, group: str = "hitting") -> Dict[str, Any]:
        params = {"stats": "gameLog", "group": group, "season": season or SEASON}
        return self.get_json(f"{MLB_BASE}/people/{player_id}/stats", params=params)

    def team_roster(self, team_id: int, season: Optional[int] = None) -> Dict[str, Any]:
        return self.get_json(
            f"{MLB_BASE}/teams/{team_id}/roster",
            params={"rosterType": "active", "season": season or SEASON},
        )

    def split_stats(self, player_id: int) -> Dict[str, Any]:
        return self.get_json(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "statSplits", "group": "hitting", "season": SEASON, "sitCodes": "vr,vl"},
        )

    def pitcher_split_stats(self, player_id: int) -> Dict[str, Any]:
        return self.get_json(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "statSplits", "group": "pitching", "season": SEASON, "sitCodes": "vl,vr"},
        )

    # FIVE MISSING SPLIT AXES (2026-08-23, data defect #4). The pitcher
    # modal's split control shipped with buttons for exactly the two axes
    # the slate published (handedness, recent form); Donovan named eight.
    # These three calls close the other five -- ordinary StatsAPI split
    # queries, the same statSplits endpoint pitcher_split_stats already
    # uses, no model change. sitCodes per MLB's docs: h/a home-away ("in
    # park"), d/n day-night, risp runners in scoring position, ac/bc
    # ahead/behind in count.
    def pitcher_situational_stats(self, player_id: int) -> Dict[str, Any]:
        return self.get_json(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "statSplits", "group": "pitching", "season": SEASON,
                    "sitCodes": "h,a,d,n,risp,ac,bc"},
        )

    def pitcher_month_stats(self, player_id: int) -> Dict[str, Any]:
        return self.get_json(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "byMonth", "group": "pitching", "season": SEASON},
        )

    def pitcher_dow_stats(self, player_id: int) -> Dict[str, Any]:
        return self.get_json(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "byDayOfWeek", "group": "pitching", "season": SEASON},
        )

    def venue(self, venue_id: int) -> Dict[str, Any]:
        # BUGFIX (2026-06-27): confirmed via a real run's diagnostic logging
        # that this endpoint, called bare, returns ONLY
        # {id, name, link, active, season} -- no 'location' key at all, on
        # EVERY venue, every time. The MLB Stats API requires an explicit
        # hydrate=location query param to include coordinates; without it,
        # get_venue_coords() was always going to fail, regardless of network
        # connectivity, API key status, or anything else downstream. This
        # was the actual root cause of weather being empty for the entire
        # slate -- not the OWM/Open-Meteo provider choice fixed earlier.
        # ── AND fieldInfo, FOR THE SAME REASON (2026-08-11) ───────────────
        #
        # The note above diagnosed this exact bug for coordinates and fixed
        # only that half. infer_roof() reads v["fieldInfo"]["roofType"], which
        # hydrate=location never returns, and then falls back:
        #
        #     if not roof: return "open"
        #
        # So EVERY park in baseball has been reported open-air, forever. The
        # archive proves it: `roof` is the string 'open' on all 3,511 rows that
        # carry it, and on 178 of 178 rows of a live slate — while the league
        # has seven retractable or domed parks (Tampa Bay, Milwaukee, Houston,
        # Toronto, Arizona, Miami, Texas). A dome cannot be 'open' 100% of the
        # time; a constant is the tell.
        #
        # It matters twice. Wind and temperature adjustments are applied to
        # games played INDOORS, where there is no wind and the temperature is
        # controlled. And enrich_weather_payload_for_website gates on
        # `has_roof_only_weather = roof in {closed, dome}` to decide that a
        # domed game legitimately HAS weather data without a fetch — a branch
        # that has therefore never once been taken.
        #
        # Same failure shape as the 0-for-unknown weather bug: a default that
        # silently answers a question nobody actually asked the API.
        #
        # FALLBACK IS DELIBERATE. If the API rejects the combined hydration we
        # must not lose coordinates too — that would re-break weather, which is
        # precisely what the note above was written about. So a response with
        # no venues falls back to the known-good location-only call.
        blob = self.get_json(f"{MLB_BASE}/venues/{venue_id}", params={"hydrate": "location,fieldInfo"})
        if not (blob or {}).get("venues"):
            return self.get_json(f"{MLB_BASE}/venues/{venue_id}", params={"hydrate": "location"})
        return blob


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "--"):
            return default
        result = float(value)
        # NaN check: NaN != NaN is always True in IEEE 754. Without this, a NaN
        # from pandas .mean() on an empty slice passes through undetected and
        # json.dumps() writes the invalid-JSON token `NaN`, breaking JSON.parse
        # in the frontend. Confirmed live consequence in build_pitch_type_json
        # (launch_speed sensor failures) and the main today.json serialization path.
        return default if result != result else result
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "--"):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_mlb_person_name(db: Optional["CacheDB"], player_id: Any, fallback: str = "—") -> str:
    """Resolve an MLBAM player id to a name, with cache.

    Used mostly for Statcast batter rows, where `player_name` is not safe to use
    as the opposing pitcher label. The real pitcher id is in the `pitcher` column.
    """
    pid = safe_int(player_id, 0)
    if not pid:
        return fallback
    key = f"person_name_v1:{pid}"
    try:
        if db is not None:
            cached = db.get(key, max_age_days=30)
            if isinstance(cached, dict) and cached.get("name"):
                return str(cached["name"])
    except Exception:
        pass
    name = fallback
    try:
        resp = requests.get(f"{MLB_BASE}/people/{pid}", timeout=TIMEOUT)
        resp.raise_for_status()
        people = resp.json().get("people") or []
        if people:
            name = str(people[0].get("fullName") or fallback)
    except Exception:
        name = fallback
    try:
        if db is not None and name and name != "—":
            db.set(key, {"player_id": pid, "name": name})
    except Exception:
        pass
    return name


def dedupe_statcast_bbe(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove duplicate Statcast BBE rows before writing logs/spray charts."""
    if frame is None or len(frame) == 0:
        return frame
    cols = [c for c in ["game_pk", "at_bat_number", "pitch_number", "pitcher", "batter"] if c in frame.columns]
    if len(cols) >= 3:
        return frame.drop_duplicates(subset=cols, keep="last")
    cols = [c for c in ["game_date", "at_bat_number", "pitch_number", "pitcher", "batter"] if c in frame.columns]
    if len(cols) >= 3:
        return frame.drop_duplicates(subset=cols, keep="last")
    return frame.drop_duplicates(keep="last")


def normalize_team_abbr(abbr: str) -> str:
    return TEAM_ABBR_FIXES.get((abbr or "").upper(), (abbr or "").upper())


def minmax_norm(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.5
    value = max(low, min(high, safe_float(value, low)))
    return (value - low) / (high - low)


def display_avg(value: float) -> str:
    return f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"


def infer_roof(venue_blob: Dict[str, Any], team_abbr: str = "") -> str:
    """The park's roof, from the API if it answered and from our own table if not.

    THE TABLE FALLBACK IS THE POINT (2026-08-11). This read only
    fieldInfo.roofType, which the venue fetch never hydrated, and then returned
    "open" — so every park in baseball was open-air on all 3,511 archived rows
    that carry it and 178 of 178 of a live slate, while the league has seven
    retractable roofs.

    The hydration is fixed in CacheDB.venue(), but the answer was ALREADY IN
    THIS FILE the whole time: PARK_FACTORS_V2 carries a "roof" key per park and
    correctly marks MIA, ARI, HOU, TOR, MIL, TEX and SEA as Retractable. So the
    fallback is now our own table rather than a guess, which means this returns
    the right answer even when the API is slow, rate-limited or reshaped —
    exactly the failure mode that produced the bug.

    Only reached when the API gives nothing, so a live roofType still wins:
    a retractable roof that is CLOSED tonight is a fact only the API knows,
    and the table can only say the park has one.
    """
    v = venue_blob.get("venues", [{}])[0] if "venues" in venue_blob else venue_blob
    roof = v.get("fieldInfo", {}).get("roofType") or v.get("roofType")
    if not roof and team_abbr:
        roof = (PARK_FACTORS_V2.get(str(team_abbr).upper(), {}) or {}).get("roof") or ""
    if not roof:
        return "open"
    txt = str(roof).lower()
    if "closed" in txt:
        return "closed"
    if "dome" in txt:
        return "dome"
    if "retract" in txt:
        return "retractable"
    return txt


def get_venue_coords(venue_blob: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    v = venue_blob.get("venues", [{}])[0] if "venues" in venue_blob else venue_blob
    coords = v.get("location", {}).get("defaultCoordinates", {})
    lat, lon = coords.get("latitude"), coords.get("longitude")
    # DIAGNOSTIC: weather has been silently empty for every player all month.
    # fetch_weather() is skipped entirely whenever lat/lon are None (see its
    # call site), so this is the first point where that failure can be seen.
    # Logging once per missing case (not every row) so this doesn't flood
    # the run output once the real cause is found and fixed.
    if lat is None or lon is None:
        venue_name = v.get("name", "unknown venue")
        has_location = "location" in v
        has_coords_key = "defaultCoordinates" in v.get("location", {})
        print(
            f"WARNING: venue coords missing for '{venue_name}' -- "
            f"has_location_key={has_location}, has_defaultCoordinates_key={has_coords_key}, "
            f"raw_venue_keys={list(v.keys())[:10]}",
            file=sys.stderr,
        )
    return lat, lon


# ── PARK OUTFIELD ORIENTATIONS ───────────────────────────────────────────────
# The compass bearing (degrees) that points FROM home plate TOWARD center field.
# Wind blowing FROM the opposite direction (toward CF) = blowing OUT = HR boost.
# Wind blowing FROM CF direction = blowing IN = HR penalty.
# Source: ballpark surveys + Google Maps verification.
PARK_CF_BEARING: Dict[str, float] = {
    "ARI": 315,  # Chase Field — CF toward NW
    "ATL": 15,   # Truist Park — CF toward NNE
    "BAL": 80,   # Camden Yards — CF toward E
    "BOS": 95,   # Fenway — CF toward ESE
    "CHC": 25,   # Wrigley — CF toward NNE
    "CIN": 340,  # Great American — CF toward NNW
    "CLE": 55,   # Progressive — CF toward NE
    "COL": 335,  # Coors — CF toward NNW
    "CWS": 5,    # Guaranteed Rate — CF toward N
    "DET": 35,   # Comerica — CF toward NNE
    "HOU": 10,   # Minute Maid — CF toward N
    "KC":  355,  # Kauffman — CF toward N
    "LAA": 5,    # Angel Stadium — CF toward N
    "LAD": 330,  # Dodger Stadium — CF toward NNW
    "MIA": 60,   # loanDepot — CF toward NE
    "MIL": 10,   # American Family — CF toward N
    "MIN": 350,  # Target Field — CF toward N
    "NYM": 345,  # Citi Field — CF toward NNW
    "NYY": 10,   # Yankee Stadium — CF toward N
    "ATH": 330,  # Oakland Coliseum — CF toward NNW
    "PHI": 5,    # Citizens Bank — CF toward N
    "PIT": 45,   # PNC Park — CF toward NE
    "SD":  320,  # Petco — CF toward NW
    "SF":  10,   # Oracle — CF toward N
    "SEA": 350,  # T-Mobile — CF toward N
    "STL": 15,   # Busch — CF toward NNE
    "TB":  350,  # Tropicana — CF toward N (dome)
    "TEX": 15,   # Globe Life — CF toward NNE
    "TOR": 5,    # Rogers Centre — CF toward N (retractable)
    "WSH": 345,  # Nationals Park — CF toward NNW
}


def wind_direction_vs_park(wind_deg: float, team: str) -> tuple[str, float]:
    """
    Given wind direction (meteorological: degrees wind is blowing FROM)
    and the home team abbreviation, return:
      - A human-readable label: "out to CF", "in from CF", "out to LF", etc.
      - A boost value: positive = HR boost, negative = HR penalty

    Meteorological convention: wind_deg = direction wind is coming FROM.
    e.g. wind_deg=180 means wind blowing FROM the south (heading north).

    We convert to the direction the wind is heading:
      wind_heading = (wind_deg + 180) % 360
    Then compare to the CF bearing to see if it's blowing out or in.
    """
    cf_bearing = PARK_CF_BEARING.get(team, 0.0)

    # Wind is heading this direction
    wind_heading = (wind_deg + 180) % 360

    # Angular difference between wind heading and CF bearing
    diff = (wind_heading - cf_bearing + 360) % 360
    if diff > 180:
        diff = 360 - diff  # fold to 0–180

    # diff ≈ 0   → blowing straight out to CF (max boost)
    # diff ≈ 90  → crosswind (neutral)
    # diff ≈ 180 → blowing straight in from CF (max penalty)

    if diff <= 30:
        label = "out to CF"
        boost = 0.06
    elif diff <= 60:
        label = "out to CF/corner"
        boost = 0.04
    elif diff <= 90:
        label = "crosswind (out)"
        boost = 0.015
    elif diff <= 120:
        label = "crosswind (in)"
        boost = -0.015
    elif diff <= 150:
        label = "in from CF/corner"
        boost = -0.04
    else:
        label = "in from CF"
        boost = -0.06

    return label, boost


def fetch_weather(lat: float, lon: float, game_time: str, roof: str, team: str = "") -> "WeatherSummary":
    """
    Fetch weather for a game using Open-Meteo (primary, free, no API key
    required) with OpenWeatherMap as a fallback.
    Adds wind direction analysis relative to the ballpark orientation.

    BUGFIX: this previously tried OWM first. Weather was confirmed empty for
    every player on a real, non-cached run (verified directly in a
    spray_cache.py output file: wind_mph/wind_deg/wind_direction_label were
    all null even though venue resolved correctly to a real, named ballpark
    -- ruling out venue-coordinate failure as the cause). The OWM key in
    this file has already been exposed in plaintext in chat history and
    repo commits; without being able to verify its current account status
    (rate-limited, revoked, or otherwise), it's an unreliable primary
    source. Open-Meteo needs no key at all, so flipping the priority order
    removes that single point of failure entirely rather than continuing to
    depend on a key whose validity can't be confirmed.
    """
    if roof.lower() in {"closed", "dome"}:
        return WeatherSummary(roof=roof, environment_boost=0.02, weather_source="dome")

    # ── Try Open-Meteo first (free, no key, nothing to expire/revoke) ───────
    meteo_result = _fetch_weather_open_meteo(lat, lon, game_time, roof, team)
    if meteo_result is not None and meteo_result.weather_source != "none":
        return meteo_result

    # ── Fallback to OpenWeatherMap, if the key happens to still work ─────────
    owm_result = _fetch_weather_owm(lat, lon, game_time, roof, team)
    if owm_result is not None:
        return owm_result

    return meteo_result if meteo_result is not None else WeatherSummary(roof=roof, weather_source="none")


def _fetch_weather_owm(lat: float, lon: float, game_time: str, roof: str, team: str) -> "Optional[WeatherSummary]":
    """Fetch from OpenWeatherMap 5-day/3-hour forecast endpoint.

    Opt-in only. With no OWM_API_KEY set this returns immediately and the bot
    runs on Open-Meteo alone -- which is the intended setup, since Open-Meteo
    is already the primary provider and needs no credentials. Skipping early
    also avoids firing a guaranteed-401 request at every venue on the slate.
    """
    if not OWM_API_KEY:
        return None
    try:
        # Parse game time to find the closest forecast bucket
        try:
            game_dt = dt.datetime.fromisoformat(game_time.replace("Z", "+00:00"))
        except Exception:
            game_dt = dt.datetime.utcnow()

        resp = requests.get(
            f"{OWM_BASE}/forecast",
            params={
                "lat": lat,
                "lon": lon,
                "appid": OWM_API_KEY,
                "units": "imperial",   # °F, mph
                "cnt": 40,             # up to 5 days of 3-hour buckets
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        buckets = data.get("list", [])
        if not buckets:
            return None

        # Pick the bucket closest to game time
        def bucket_dt(b):
            try:
                return dt.datetime.utcfromtimestamp(b["dt"])
            except Exception:
                return dt.datetime.utcnow()

        game_dt_naive = game_dt.replace(tzinfo=None) if game_dt.tzinfo else game_dt
        closest = min(buckets, key=lambda b: abs((bucket_dt(b) - game_dt_naive).total_seconds()))

        temp_f      = safe_float(closest.get("main", {}).get("temp"), None)
        feels_f     = safe_float(closest.get("main", {}).get("feels_like"), None)
        humidity    = safe_float(closest.get("main", {}).get("humidity"), None)
        wind_mph    = safe_float(closest.get("wind", {}).get("speed"), None)
        wind_deg    = safe_float(closest.get("wind", {}).get("deg"), None)
        # OWM gives pop (probability of precipitation) as 0.0–1.0
        precip_chance = safe_float(closest.get("pop"), None)

        # Wind direction vs park
        wind_label = ""
        wind_boost = 0.0
        if wind_deg is not None and team:
            wind_label, wind_boost = wind_direction_vs_park(wind_deg, team)
            # Scale boost by wind speed — 0 mph wind = no boost regardless of direction
            speed_factor = min(1.0, safe_float(wind_mph, 0) / 15.0)
            wind_boost = wind_boost * speed_factor

        # Environment boost (temp + wind direction)
        env = 0.0
        if temp_f is not None:
            if temp_f >= 85:
                env += 0.06
            elif temp_f >= 75:
                env += 0.03
            elif temp_f <= 50:
                env -= 0.03
        env += wind_boost

        return WeatherSummary(
            temp_f=temp_f,
            wind_mph=wind_mph,
            wind_deg=wind_deg,
            roof=roof,
            environment_boost=env,
            humidity=humidity,
            feels_like_f=feels_f,
            precip_chance=precip_chance,
            wind_direction_label=wind_label,
            wind_boost=wind_boost,
            weather_source="owm",
        )

    except Exception as exc:
        print(f"⚠️ OWM weather also failed ({exc}) — both providers exhausted, weather will be empty for this game", file=sys.stderr)
        return None


def _fetch_weather_open_meteo(lat: float, lon: float, game_time: str, roof: str, team: str) -> "WeatherSummary":
    """Original Open-Meteo fetch, now upgraded with wind direction analysis."""
    try:
        data = requests.get(
            OPEN_METEO,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m,apparent_temperature,precipitation_probability",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=TIMEOUT,
        ).json()
    except Exception as exc:
        # BUGFIX: this previously swallowed every exception with no trace at
        # all -- unlike the OWM fetch above it, which at least logs before
        # falling back. If OWM fails AND this fails, weather goes silently
        # empty for the whole slate with zero diagnostic output. Now logged.
        print(f"WARNING: Open-Meteo weather fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return WeatherSummary(roof=roof, weather_source="none")

    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])
    if not times:
        print(f"WARNING: Open-Meteo returned no hourly data for lat={lat}, lon={lon}", file=sys.stderr)
        return WeatherSummary(roof=roof, weather_source="none")

    temps       = hourly.get("temperature_2m", [])
    winds       = hourly.get("wind_speed_10m", [])
    wind_degs   = hourly.get("wind_direction_10m", [])
    humidities  = hourly.get("relative_humidity_2m", [])
    feels_like  = hourly.get("apparent_temperature", [])
    precip_prob = hourly.get("precipitation_probability", [])

    idx = 0
    try:
        # Match by full datetime, not just hour-of-day. With forecast_days=2 the
        # `times` array spans today AND tomorrow (48 entries). A pure hour-of-day
        # comparison always picks today's entry on ties (min() returns the first
        # minimum) -- so tomorrow-slate games silently pulled today's forecast.
        # Fix mirrors _fetch_weather_owm's already-correct total_seconds() pattern.
        target_dt = dt.datetime.fromisoformat(game_time.replace("Z", "+00:00")).replace(tzinfo=None)
        idx = min(
            range(len(times)),
            key=lambda i: abs((dt.datetime.fromisoformat(times[i]) - target_dt).total_seconds())
        )
    except Exception:
        pass

    temp      = temps[idx]      if idx < len(temps)       else None
    wind      = winds[idx]      if idx < len(winds)       else None
    wind_deg  = wind_degs[idx]  if idx < len(wind_degs)   else None
    humidity  = humidities[idx] if idx < len(humidities)  else None
    feels_f   = feels_like[idx] if idx < len(feels_like)  else None
    precip    = (precip_prob[idx] / 100.0) if idx < len(precip_prob) and precip_prob[idx] is not None else None

    # Wind direction vs park
    wind_label = ""
    wind_boost = 0.0
    if wind_deg is not None and team:
        wind_label, wind_boost = wind_direction_vs_park(wind_deg, team)
        speed_factor = min(1.0, safe_float(wind, 0) / 15.0)
        wind_boost = wind_boost * speed_factor

    env = 0.0
    if temp is not None:
        if temp >= 85:
            env += 0.06
        elif temp >= 75:
            env += 0.03
        elif temp <= 50:
            env -= 0.03
    env += wind_boost

    return WeatherSummary(
        temp_f=temp,
        wind_mph=wind,
        wind_deg=wind_deg,
        roof=roof,
        environment_boost=env,
        humidity=humidity,
        feels_like_f=feels_f,
        precip_chance=precip,
        wind_direction_label=wind_label,
        wind_boost=wind_boost,
        weather_source="open-meteo",
    )


def flatten_season_hitting(stat: Dict[str, Any]) -> Dict[str, float]:
    avg = safe_float(stat.get("avg"), 0.250)
    obp = safe_float(stat.get("obp"), avg + 0.06)
    slg = safe_float(stat.get("slg"), avg + 0.12)
    ops = safe_float(stat.get("ops"), obp + slg)
    pa = max(1, safe_int(stat.get("plateAppearances"), 0))
    return {
        "season_avg": avg,
        "season_obp": obp,
        "season_ops": ops,
        "season_slg": slg,
        "season_iso": max(0.0, slg - avg),
        "season_hr": safe_int(stat.get("homeRuns"), 0),
        "season_pa": pa,
        "season_bb_rate": safe_int(stat.get("baseOnBalls"), 0) / pa,
        # Docket #4-7 (2026-08-05): already present in this stat dict, never
        # read. Makes League Leaders exact instead of client-estimated.
        "season_tb": safe_int(stat.get("totalBases"), 0),
        "season_ab": safe_int(stat.get("atBats"), 0),
        "season_doubles": safe_int(stat.get("doubles"), 0),
        "season_triples": safe_int(stat.get("triples"), 0),
        "season_babip": safe_float(stat.get("babip"), 0.0),
        "season_k_rate": safe_int(stat.get("strikeOuts"), 0) / pa,
        # ── STOLEN BASES, v1 (2026-08-23; claude/moonshot-sb-research.md) ──
        # These two fields have been ON this stat blob in every pull the bot
        # has ever made and were never read -- the research pass's headline.
        # Attempt rate per game (not per PA: a steal is a baserunning event,
        # its opportunity is reaching base, and games is the denominator the
        # books quote SB props against). Zero new network calls.
        "season_sb": safe_int(stat.get("stolenBases"), 0),
        "season_cs": safe_int(stat.get("caughtStealing"), 0),
        "season_sb_attempt_rate": round(
            (safe_int(stat.get("stolenBases"), 0) + safe_int(stat.get("caughtStealing"), 0))
            / max(1, safe_int(stat.get("gamesPlayed"), 0)), 3),
        # Added per audit (2026-06-28): season-long RBI/runs were completely
        # absent despite hrr_v2 (literally "Hits, Runs, RBI") having zero
        # season-long production context -- only recency windows and a
        # season BASELINE (OBP/AVG/babip), nothing about actual season RBI/
        # run output. Same raw stat object season_hr already pulls from.
        "season_rbi": safe_int(stat.get("rbi"), 0),
        "season_runs": safe_int(stat.get("runs"), 0),
        "season_rbi_per_pa": round(safe_int(stat.get("rbi"), 0) / pa, 4),
        "season_runs_per_pa": round(safe_int(stat.get("runs"), 0) / pa, 4),
    }


def _parse_ip_to_float(ip_value: Any) -> float:
    try:
        raw = str(ip_value or "0").strip()
        if "." in raw:
            whole, frac = raw.split(".", 1)
            outs = safe_int(whole, 0) * 3 + min(2, safe_int(frac[:1], 0))
            return outs / 3.0
        return float(raw or 0.0)
    except Exception:
        return 0.0


def flatten_pitching(stat: Dict[str, Any]) -> Dict[str, float]:
    strikeouts = safe_int(stat.get("strikeOuts"), 0)
    bf = safe_int(stat.get("battersFaced"), 0)
    ip = _parse_ip_to_float(stat.get("inningsPitched"))
    return {
        "era": safe_float(stat.get("era"), 4.00),
        "whip": safe_float(stat.get("whip"), 1.30),
        "hr9": safe_float(stat.get("homeRunsPer9"), 1.10),
        "hr_allowed": safe_int(stat.get("homeRuns"), 0),
        "k_rate": round(strikeouts / bf, 4) if bf else 0.0,
        "k9": round((strikeouts / ip) * 9.0, 2) if ip > 0 else 0.0,
        "babip": safe_float(stat.get("babip"), 0.300),
    }


def compute_window_from_gamelog(gamelog_blob: Dict[str, Any], n: int) -> Dict[str, float]:
    stats_list = gamelog_blob.get("stats") or []
    first = stats_list[0] if stats_list else {}
    splits = first.get("splits") or []
    recent = splits[-n:] if len(splits) >= n else splits
    if not recent:
        return {"avg": 0.250, "hits": 0, "hr": 0, "xbh": 0, "runs": 0, "rbi": 0}
    hits = at_bats = hr = xbh = runs = rbi = 0
    for s in recent:
        st = s.get("stat", {}) or {}
        hits += safe_int(st.get("hits"), 0)
        at_bats += safe_int(st.get("atBats"), 0)
        hr += safe_int(st.get("homeRuns"), 0)
        xbh += safe_int(st.get("doubles"), 0) + safe_int(st.get("triples"), 0) + safe_int(st.get("homeRuns"), 0)
        runs += safe_int(st.get("runs"), 0)
        rbi += safe_int(st.get("rbi"), 0)
    return {"avg": hits / at_bats if at_bats else 0.250, "hits": hits, "hr": hr, "xbh": xbh, "runs": runs, "rbi": rbi}


def compute_pitcher_recent_starts(gamelog_blob: Dict[str, Any], n: int = 3) -> Dict[str, float]:
    """Pitcher's last N starts (by game appearance, not innings/PA), mirroring
    compute_window_from_gamelog's batter pattern. Added per audit
    (2026-06-27): pitchers previously only had season-long aggregates in this
    bot -- no recency window at all, unlike batters' last5/7/10. Real
    game-to-game variability in starting pitcher performance is a documented,
    non-chance effect (e.g. a pitcher can run a stretch of strong starts then
    a stretch of rough ones), so a recent-starts window is a genuinely
    different signal from the season average, not just a noisier version of it.

    Uses the same person_game_log endpoint already used for batters, just
    with group="pitching" -- that endpoint already existed and worked, it
    had simply never been called with the pitching group before.
    """
    stats_list = gamelog_blob.get("stats") or []
    first = stats_list[0] if stats_list else {}
    splits = first.get("splits") or []
    # Game log entries with 0 IP (e.g. a start cut short by rain, or a
    # non-pitching appearance artifact) shouldn't count as a "start."
    starts = [s for s in splits if _parse_ip_to_float((s.get("stat") or {}).get("inningsPitched")) > 0]
    recent = starts[-n:] if len(starts) >= n else starts
    if not recent:
        return {"era": 4.20, "whip": 1.30, "hr9": 1.10, "k9": 7.5, "ip_total": 0.0, "starts_found": 0, "status": "empty"}
    er = whip_baserunners = hr_allowed = strikeouts = ip_total = 0.0
    for s in recent:
        st = s.get("stat") or {}
        ip = _parse_ip_to_float(st.get("inningsPitched"))
        ip_total += ip
        er += safe_float(st.get("earnedRuns"), 0.0)
        whip_baserunners += safe_float(st.get("hits"), 0.0) + safe_float(st.get("baseOnBalls"), 0.0)
        hr_allowed += safe_float(st.get("homeRuns"), 0.0)
        strikeouts += safe_float(st.get("strikeOuts"), 0.0)
    if ip_total <= 0:
        return {"era": 4.20, "whip": 1.30, "hr9": 1.10, "k9": 7.5, "ip_total": 0.0, "starts_found": len(recent), "status": "no_innings"}
    return {
        "era": round((er * 9.0) / ip_total, 2),
        "whip": round(whip_baserunners / ip_total, 2),
        "hr9": round((hr_allowed * 9.0) / ip_total, 2),
        "k9": round((strikeouts * 9.0) / ip_total, 2),
        "ip_total": round(ip_total, 1),
        "starts_found": len(recent),
        "status": "ok",
    }


def games_since_last_hr(gamelog_blob: Dict[str, Any], max_lookback: int = 60) -> int:
    """Walk the gamelog from most-recent backward; return games since last HR.

    Returns 0 if HR was hit in the most recent game.
    Returns max_lookback if no HR found within the lookback window (effectively "very cold").
    Returns max_lookback if gamelog is empty.
    """
    stats_list = gamelog_blob.get("stats") or []
    first = stats_list[0] if stats_list else {}
    splits = first.get("splits") or []
    if not splits:
        return max_lookback
    # splits are oldest→newest. Reverse to walk newest→oldest.
    count = 0
    for s in reversed(splits[-max_lookback:]):
        st = s.get("stat", {}) or {}
        if safe_int(st.get("homeRuns"), 0) >= 1:
            return count
        count += 1
    return count  # no HR found in window


# ── THE BLANK PROFILE (2026-08-15) ──────────────────────────────────────────
#
# Donovan asked for a board of "all the players who blanked in their last
# game", with the book's price beside each man's own hit rate in that spot.
#
# The site cannot compute this. It would have to know what every hitter did in
# his LAST GAME — not his last five, which is what the slate publishes — and
# the only league-wide source for that is a per-player game log. The bot
# already pulls exactly that log for every hitter on the slate (see the
# `client.person_game_log(pid)` call that feeds last5/last7/last10), so the
# last game is one read off data already in hand and the follow-up rate is one
# walk over the same list. Doing it here costs zero extra requests; doing it
# browser-side would cost ~266.
#
# DEFINITIONS, because a board like this is only worth having if its claim is
# exact:
#
#   A GAME here means a game he CAME TO THE PLATE in — plateAppearances >= 1.
#
#   THIS WAS atBats >= 1 UNTIL DONOVAN CORRECTED IT (2026-08-16): "walk only
#   nights count as a blank too, still counts." He is right and the first
#   version was wrong. A man who walked twice and never got a hit had a
#   hitless night; skipping him because a walk is not an at-bat let a real
#   0-for disappear from his streak, and it silently shortened the streaks of
#   exactly the patient hitters most likely to be on this board. Plate
#   appearances is the honest unit for "did he bat and fail to hit".
#
#   A pinch-RUNNING appearance — on base without ever coming to the plate,
#   PA = 0 — is still skipped, because he genuinely never batted. That is the
#   same three-outcome discipline as My Picks: tracked-and-failed and
#   never-batted are different facts.
#
#   A BLANK is such a game with zero hits.
#
#   AFTER A BLANK counts every game in the log whose immediately preceding
#   batted game was a blank, then asks what he did in it. That is his own
#   measured bounce-back rate, not the league's and not a prior.
#
# Everything published here is a count or a raw line. No rate is published as
# a rate: `after_blank_n` rides with every numerator so the site can dim a
# 3-of-4 and refuse to print a percentage on nothing. A rate without its
# sample is the one number this project keeps banning.
def compute_blank_profile(gamelog_blob: Dict[str, Any], max_lookback: int = 162) -> Dict[str, Any]:
    stats_list = gamelog_blob.get("stats") or []
    first = stats_list[0] if stats_list else {}
    splits = first.get("splits") or []

    out: Dict[str, Any] = {
        "last_game_date": "", "last_game_ab": 0, "last_game_pa": 0, "last_game_hits": 0, "last_game_hr": 0,
        "last_game_tb": 0, "last_game_rbi": 0, "last_game_runs": 0, "last_game_hrr": 0,
        "blank_streak": 0, "after_blank_n": 0, "after_blank_hit": 0,
        "after_blank_hrr1": 0, "after_blank_hrr2": 0, "after_blank_tb2": 0,
        # ── THE CONTROL (2026-08-16, Donovan: "do the board compare side to
        # the book as well i like that") ────────────────────────────────────
        #
        # The board has always compared a hitter's after-a-blank rate to what
        # the BOOK charges. That answers "is he mispriced". It does NOT answer
        # the question the board's own name implies — "does blanking predict
        # anything at all" — because the only thing the after-blank rate was
        # ever measured against was a sportsbook line. A 68% could be a real
        # bounce-back or it could just be his rate, and nothing on the page
        # could tell the two apart.
        #
        # TWO baselines, because they answer different questions and mixing
        # them up is how this gets reported wrong:
        #
        #   overall_*   — every batted game in the log. This is "his normal
        #                 rate", the number a reader wants as context next to
        #                 the dot. It is NOT a clean control: the after-blank
        #                 games are a SUBSET of it, so the two groups overlap
        #                 and any real effect shows up attenuated.
        #
        #   after_hit_* — games whose immediately-preceding batted game had a
        #                 hit. This is the actual complement of after_blank
        #                 and the only honest input to a two-proportion test.
        #                 Same exclusion rule as after_blank: the first game of
        #                 the log belongs to NEITHER group, because nothing
        #                 precedes it.
        #
        # Same bars, same PA-gated denominator as after_blank throughout, or
        # the comparison means nothing.
        "overall_n": 0, "overall_hit": 0,
        "overall_hrr1": 0, "overall_hrr2": 0, "overall_tb2": 0,
        "after_hit_n": 0, "after_hit_hit": 0,
        "after_hit_hrr1": 0, "after_hit_hrr2": 0, "after_hit_tb2": 0,
        "blank_profile_status": "empty",
    }
    if not splits:
        return out

    # Games he came to the plate in, oldest→newest, capped at the lookback.
    games = []
    for s in splits[-max_lookback:]:
        st = s.get("stat", {}) or {}
        ab = safe_int(st.get("atBats"), 0)
        # plateAppearances is the gate (see the header). Fall back to AB+BB
        # when the split omits PA rather than dropping a real game — some
        # older gameLog rows carry the components but not the total.
        pa = safe_int(st.get("plateAppearances"), 0)
        if pa < 1:
            pa = ab + safe_int(st.get("baseOnBalls"), 0) + safe_int(st.get("hitByPitch"), 0)
        if pa < 1:
            continue
        hits = safe_int(st.get("hits"), 0)
        runs = safe_int(st.get("runs"), 0)
        rbi = safe_int(st.get("rbi"), 0)
        games.append({
            "date": str(s.get("date") or "")[:10],
            "ab": ab,
            "pa": pa,
            "hits": hits,
            "hr": safe_int(st.get("homeRuns"), 0),
            # totalBases is published on the split; fall back to hits (a
            # single apiece) rather than inventing extra-base credit.
            "tb": safe_int(st.get("totalBases"), hits),
            "rbi": rbi,
            "runs": runs,
            "hrr": hits + runs + rbi,
        })
    if not games:
        out["blank_profile_status"] = "no_batted_games"
        return out

    last = games[-1]
    out.update({
        "last_game_date": last["date"], "last_game_ab": last["ab"], "last_game_pa": last["pa"],
        "last_game_hits": last["hits"],
        "last_game_hr": last["hr"], "last_game_tb": last["tb"], "last_game_rbi": last["rbi"],
        "last_game_runs": last["runs"], "last_game_hrr": last["hrr"],
    })

    streak = 0
    for g in reversed(games):
        if g["hits"] == 0:
            streak += 1
        else:
            break
    out["blank_streak"] = streak

    # ONE WALK, THREE TALLIES, IDENTICAL BARS. Written as a helper rather than
    # three copies of the same four comparisons precisely because the whole
    # value of the control is that it is measured the same way as the thing it
    # controls for — three hand-rolled copies is three chances for one bar to
    # drift and turn a measurement artefact into a "finding".
    def _tally(prefix: str, g: Dict[str, Any]) -> None:
        out[f"{prefix}_n"] += 1
        if g["hits"] >= 1:
            out[f"{prefix}_hit"] += 1
        if g["hrr"] >= 1:
            out[f"{prefix}_hrr1"] += 1
        if g["hrr"] >= 2:
            out[f"{prefix}_hrr2"] += 1
        if g["tb"] >= 2:
            out[f"{prefix}_tb2"] += 1

    # His normal rate: every batted game, first one included. Context, not a
    # control — see the field comment above.
    for g in games:
        _tally("overall", g)

    # The two follow-up cohorts. i starts at 1, so the first game of the log is
    # in neither: nothing precedes it, and counting it would credit or blame
    # him for a game outside the window.
    for i in range(1, len(games)):
        _tally("after_blank" if games[i - 1]["hits"] == 0 else "after_hit", games[i])

    out["blank_profile_status"] = "ok"
    return out


def extract_lineup(team_box: Dict[str, Any]) -> List[Tuple[int, int]]:
    order = team_box.get("battingOrder") or []
    players = team_box.get("players", {}) or {}
    lineup: List[Tuple[int, int]] = []
    if order:
        for idx, pid in enumerate(order, start=1):
            k = str(pid)
            if not k.startswith("ID"):
                k = f"ID{k}"
            p = players.get(k)
            if p:
                lineup.append((safe_int((p.get("person") or {}).get("id"), 0), idx))
        if lineup:
            return lineup
    return []


def find_catcher(team_box: Dict[str, Any]) -> Tuple[int, str, str]:
    """(player_id, name, source) for the team's catcher tonight.

    2026-08-23. components/tabs/StealBoard.js has shipped with a written
    refusal at the bottom of it since the day it was built:

        Who is catching is not on this board — the slate does not carry the
        opposing catcher, and that is the other half of a steal; saying so is
        more useful than ranking as though it were counted.

    It IS on the boxscore, and always was. Every entry under team_box.players
    carries position.abbreviation, and this walks the posted batting order
    looking for "C". Walking the ORDER rather than the whole players dict
    matters: the dict includes the bench, so the first "C" in it is as likely
    to be the backup as the starter.

    `source` travels with the answer because the two ways of getting it are
    not equally good, and a caller that cannot tell them apart will present a
    guess as a fact:
      "lineup"  — he is in the posted batting order at C. This is the answer.
      "roster"  — no posted order yet, so the boxscore's own player list was
                  used; on a projected lineup this is the likeliest catcher
                  and not a confirmed one.
      ""        — nobody found. The caller must say so rather than fall back
                  to a league-average catcher, which is how a matchup against
                  the best throwing catcher in baseball ends up scored neutral.
    """
    players = team_box.get("players", {}) or {}

    def _named(p):
        person = p.get("person") or {}
        return safe_int(person.get("id"), 0), str(person.get("fullName") or "").strip()

    order = team_box.get("battingOrder") or []
    for pid in order:
        k = str(pid)
        if not k.startswith("ID"):
            k = f"ID{k}"
        p = players.get(k)
        if not p:
            continue
        if ((p.get("position") or {}).get("abbreviation") or "").upper() == "C":
            cid, name = _named(p)
            if cid:
                return cid, name, "lineup"

    # No posted order. Prefer whoever the boxscore lists at C with the most
    # plate appearances this season — the everyday catcher, not the first key
    # the dict happens to yield.
    best = (0, "", -1)
    for p in players.values():
        if ((p.get("position") or {}).get("abbreviation") or "").upper() != "C":
            continue
        cid, name = _named(p)
        if not cid:
            continue
        pa = safe_int((((p.get("seasonStats") or {}).get("batting") or {}).get("plateAppearances")), 0)
        if pa > best[2]:
            best = (cid, name, pa)
    if best[0]:
        return best[0], best[1], "roster"
    return 0, "", ""


def build_projected_lineup(client: MLBClient, team_box: Dict[str, Any], team_id: int) -> List[Tuple[int, int]]:
    players = team_box.get("players", {}) or {}
    candidates: List[Dict[str, Any]] = []

    def add(pid: int, pos: str, stat: Dict[str, Any]):
        if not pid or (pos or "").upper() in {"P", "TWP"}:
            return
        pa = safe_int(stat.get("plateAppearances"), 0)
        avg = safe_float(stat.get("avg"), 0.0)
        ops = safe_float(stat.get("ops"), 0.0)
        obp = safe_float(stat.get("obp"), 0.0)
        slg = safe_float(stat.get("slg"), 0.0)
        hr = safe_int(stat.get("homeRuns"), 0)
        if pa <= 0 and ops <= 0 and avg <= 0:
            return
        candidates.append({"player_id": pid, "avg": avg, "ops": ops, "obp": obp, "slg": slg, "hr": hr, "pa": pa})

    for p in players.values():
        add(
            safe_int((p.get("person") or {}).get("id"), 0),
            ((p.get("position") or {}).get("abbreviation") or ""),
            ((p.get("seasonStats") or {}).get("batting") or {}),
        )

    if len(candidates) < 9 and team_id:
        try:
            roster = client.team_roster(team_id)
            for entry in roster.get("roster", []):
                pid = safe_int((entry.get("person") or {}).get("id"), 0)
                if any(c["player_id"] == pid for c in candidates):
                    continue
                pos = ((entry.get("position") or {}).get("abbreviation")) or ""
                if pos.upper() in {"P", "TWP"}:
                    continue
                sblob = client.person_stats(pid, group="hitting", stat_type="season")
                stats_list = sblob.get("stats") or []
                first = stats_list[0] if stats_list else {}
                splits = first.get("splits") or []
                stat = (splits[0].get("stat") if splits else {}) or {}
                add(pid, pos, stat)
        except Exception:
            pass

    dedup = {c["player_id"]: c for c in candidates}
    pool = list(dedup.values())
    if not pool:
        return []

    ordered: List[Dict[str, Any]] = []

    def pop_best(items: List[Dict[str, Any]], keyfn):
        idx = max(range(len(items)), key=lambda i: keyfn(items[i]))
        return items.pop(idx)

    if pool:
        ordered.append(pop_best(pool, lambda x: (x["obp"] * 0.7 + min(x["pa"], 300) / 300 * 0.3, x["ops"])))
    if pool:
        ordered.append(pop_best(pool, lambda x: (x["obp"] * 0.55 + x["ops"] * 0.45, x["pa"])))
    if pool:
        ordered.append(pop_best(pool, lambda x: (x["ops"] * 0.55 + x["slg"] * 0.45, x["hr"], x["pa"])))
    if pool:
        ordered.append(pop_best(pool, lambda x: (x["slg"] * 0.60 + x["hr"] / 35.0 * 0.40, x["ops"], x["pa"])))
    pool.sort(key=lambda x: (x["ops"], x["obp"], x["pa"]), reverse=True)
    ordered.extend(pool[:5])

    return [(p["player_id"], idx + 1) for idx, p in enumerate(ordered[:9])]



# get_team_hitter_pool removed (audit 2026-06-29): never called. Duplicated build_projected_lineup N+1 API pattern.


# prefetch_hitter_data removed (audit 2026-06-29): never called.



def build_pitch_type_json(db: "CacheDB", all_rows: list, output_dir: Path) -> None:
    """Build per-batter and per-pitcher pitch type JSON files for the Pitch Lab dashboard tab.

    Output:
        public/data/pitch/batter_{player_id}.json
        public/data/pitch/pitcher_{player_id}.json
    """
    if statcast_batter is None or statcast_pitcher is None:
        print("⚠️  pybaseball not available — skipping pitch type JSON build", file=sys.stderr)
        return
    if not all_rows:
        return

    pitch_dir = Path(output_dir) / "pitch"
    pitch_dir.mkdir(parents=True, exist_ok=True)

    today_str    = TODAY.isoformat()
    season_start = SEASON_START.isoformat()
    end_date     = statcast_data_end_date(TODAY).isoformat()

    PITCH_NAMES = {
        "FF":"Four-seam FB","SI":"Sinker","FC":"Cutter","SL":"Slider","ST":"Sweeper",
        "CU":"Curveball","KC":"Knuckle Curve","CH":"Changeup","FS":"Splitter",
        "FO":"Forkball","KN":"Knuckleball","EP":"Eephus","CS":"Slow Curve",
        "SC":"Screwball","SV":"Slurve","FA":"Fastball","PO":"Pitchout",
    }
    NON_AB = {"walk","intent_walk","hit_by_pitch","sac_fly","sac_bunt","catcher_interf","none",""}
    HIT_EV = {"single","double","triple","home_run"}
    XBH_EV = {"double","triple","home_run"}
    K_EV   = {"strikeout","strikeout_double_play"}

    def pname(code):
        return PITCH_NAMES.get(str(code).upper().strip(), str(code))

    def spct(n, d):
        try:
            return round(float(n)/float(d),3) if d else 0.0
        except Exception:
            return 0.0

    def ecounts(events):
        out={"hits":0,"ab":0,"hr":0,"xbh":0,"k":0,"sf":0}
        for e in events:
            s=str(e).strip()
            if s in HIT_EV: out["hits"]+=1
            if s not in NON_AB: out["ab"]+=1
            if s=="home_run": out["hr"]+=1
            if s in XBH_EV: out["xbh"]+=1
            if s in K_EV: out["k"]+=1
            if s=="sac_fly": out["sf"]+=1
        return out

    def pull_rate(frame):
        try:
            if frame is None or len(frame)==0 or "hc_x" not in frame.columns or "stand" not in frame.columns:
                return 0.0
            tmp=frame.copy()
            tmp["hc_x"]=pd.to_numeric(tmp["hc_x"],errors="coerce")
            tmp=tmp[tmp["hc_x"].notna()]
            if not len(tmp): return 0.0
            pulled=((tmp["stand"]=="R")&(tmp["hc_x"]<125.0))|((tmp["stand"]=="L")&(tmp["hc_x"]>125.0))
            return round(float(pulled.mean()),3)
        except Exception:
            return 0.0

    def oppo_rate(frame):
        try:
            if frame is None or len(frame)==0 or "hc_x" not in frame.columns or "stand" not in frame.columns:
                return 0.0
            tmp=frame.copy()
            tmp["hc_x"]=pd.to_numeric(tmp["hc_x"],errors="coerce")
            tmp=tmp[tmp["hc_x"].notna()]
            if not len(tmp): return 0.0
            oppo=((tmp["stand"]=="R")&(tmp["hc_x"]>170.0))|((tmp["stand"]=="L")&(tmp["hc_x"]<80.0))
            return round(float(oppo.mean()),3)
        except Exception:
            return 0.0

    # ── COLLECT UNIQUE BATTERS & PITCHERS ────────────────────────────────────
    # Do not rediscover pitcher IDs by scanning cache names. That was the source
    # of wrong/missing arsenals when two cache rows or stale names collided.
    seen_batters: Dict[int,str] = {}
    seen_pitchers: Dict[int,str] = {}
    for row in all_rows:
        bid = safe_int(getattr(row, "player_id", 0), 0)
        bname = getattr(row, "name", f"Player {bid}")
        if bid and bid not in seen_batters:
            seen_batters[bid] = bname

        pid = safe_int(getattr(row, "pitcher_id", 0), 0)
        pname_str = str(getattr(row, "pitcher_name", "") or "")
        if pid and pname_str and pname_str != "TBD" and pid not in seen_pitchers:
            seen_pitchers[pid] = pname_str

    print(f"\n🎯 Pitch JSON build: {len(seen_batters)} batters | {len(seen_pitchers)} pitchers", file=sys.stderr)

    # ── BATTER FILES ─────────────────────────────────────────────────────────
    for pid, bname in seen_batters.items():
        ckey = f"pitch_type_batter_v4_pitchfix:{SEASON}:{pid}"
        cached = db.get(ckey, max_age_days=1)
        if cached is not None:
            (pitch_dir/f"batter_{pid}.json").write_text(json.dumps(cached,indent=2),encoding="utf-8")
            continue
        try:
            df = statcast_batter(season_start, end_date, pid)
        except Exception as exc:
            print(f"  ⚠️  statcast_batter {bname}: {exc}", file=sys.stderr)
            continue
        if df is None or len(df)==0:
            continue
        try:
            df=df.copy()
            df["launch_speed"]=pd.to_numeric(df.get("launch_speed"),errors="coerce")
            df["launch_angle"]=pd.to_numeric(df.get("launch_angle"),errors="coerce")
            df["hit_distance_sc"]=pd.to_numeric(df.get("hit_distance_sc"),errors="coerce")
            df["estimated_woba_using_speedangle"]=pd.to_numeric(df.get("estimated_woba_using_speedangle"),errors="coerce")
            total_p=max(1,len(df))
            bbe=df[df["type"]=="X"].copy() if "type" in df.columns else df.iloc[0:0].copy()
            bbe=dedupe_statcast_bbe(bbe)
            total_bbe=max(1,len(bbe))
            gb=spct((bbe.get("bb_type")=="ground_ball").sum(),total_bbe) if "bb_type" in bbe.columns else 0.0
            fb=spct((bbe.get("bb_type")=="fly_ball").sum(),total_bbe) if "bb_type" in bbe.columns else 0.0
            ld=spct((bbe.get("bb_type")=="line_drive").sum(),total_bbe) if "bb_type" in bbe.columns else 0.0
            pu=spct((bbe.get("bb_type")=="popup").sum(),total_bbe) if "bb_type" in bbe.columns else 0.0
            hh=float((bbe["launch_speed"]>=95).fillna(False).mean()) if len(bbe) else 0.0
            hr_fb=0.0
            if "bb_type" in bbe.columns and "events" in bbe.columns:
                fb_only=bbe[bbe["bb_type"]=="fly_ball"]
                if len(fb_only): hr_fb=spct((fb_only["events"]=="home_run").sum(),len(fb_only))
            pr=pull_rate(bbe); op=oppo_rate(bbe)
            top_stats={"gb_pct":round(gb*100,1),"fb_pct":round(fb*100,1),"ld_pct":round(ld*100,1),
                       "pu_pct":round(pu*100,1),"hard_hit_pct":round(hh*100,1),"hr_fb_pct":round(hr_fb*100,1),
                       "pull_pct":round(pr*100,1),"oppo_pct":round(op*100,1)}
            splits={}
            for hand,label in [("L","vs_LHP"),("R","vs_RHP")]:
                sub=df[df["p_throws"]==hand].copy() if "p_throws" in df.columns else df.iloc[0:0].copy()
                sb=sub[sub["type"]=="X"].copy() if "type" in sub.columns and len(sub) else sub.iloc[0:0].copy()
                se=list(sub["events"].fillna("")) if "events" in sub.columns else []
                c=ecounts(se)
                sw=pd.to_numeric(sub.get("estimated_woba_using_speedangle"),errors="coerce").dropna() if len(sub) else pd.Series(dtype=float)
                bh=sum(1 for e in se if e in {"single","double","triple"})
                bd=max(1,c["ab"]-c["k"]-c["hr"]+c["sf"])
                splits[label]={"ba":round(c["hits"]/c["ab"],3) if c["ab"] else 0.0,
                               "woba":round(float(sw.mean()),3) if len(sw) else 0.0,
                               "babip":round(bh/bd,3),"hr":c["hr"],"xbh":c["xbh"],
                               "k_pct":round(spct(c["k"],max(1,c["ab"]))*100,1),
                               "hard_hit_pct":round(float((sb["launch_speed"]>=95).fillna(False).mean())*100,1) if len(sb) else 0.0,
                               "pa":len(sub)}
            pitch_summary=[]
            if "pitch_type" in df.columns:
                for pt,grp in df.groupby("pitch_type"):
                    if not pt or str(pt) in ("nan","None",""): continue
                    gb2=grp[grp["type"]=="X"].copy() if "type" in grp.columns else grp.iloc[0:0].copy()
                    ge=list(grp["events"].fillna("")) if "events" in grp.columns else []
                    c=ecounts(ge)
                    bh=sum(1 for e in ge if e in {"single","double","triple"})
                    bd=max(1,c["ab"]-c["k"]-c["hr"]+c["sf"])
                    if "description" in grp.columns:
                        d2=grp["description"].fillna("")
                        gs=int(d2.str.contains("swing|foul|hit_into_play|missed_bunt",case=False,na=False).sum())
                        gw=int(d2.str.contains("swinging_strike",case=False,na=False).sum())
                    else:
                        gs=gw=0
                    gwo=pd.to_numeric(grp.get("estimated_woba_using_speedangle"),errors="coerce").dropna()
                    gev=gb2["launch_speed"].dropna() if len(gb2) else pd.Series(dtype=float)
                    pitch_summary.append({
                        "pitch_type":pname(pt),"pitch_code":str(pt),
                        "usage_pct":round(spct(len(grp),total_p)*100,1),"count":len(grp),
                        "ba":round(c["hits"]/c["ab"],3) if c["ab"] else 0.0,
                        "woba":round(float(gwo.mean()),3) if len(gwo) else 0.0,
                        "babip":round(bh/bd,3),"hr":c["hr"],"xbh":c["xbh"],
                        "k_pct":round(spct(c["k"],max(1,c["ab"]))*100,1),
                        "whiff_pct":round(spct(gw,max(1,gs))*100,1),
                        "hard_hit_pct":round(float((gb2["launch_speed"]>=95).fillna(False).mean())*100,1) if len(gb2) else 0.0,
                        "avg_ev":round(float(gev.mean()),1) if len(gev) else 0.0,
                        "fb_pct":round(float((gb2.get("bb_type")=="fly_ball").mean())*100,1) if "bb_type" in gb2.columns and len(gb2) else 0.0,
                        "pull_pct":round(pr*100,1),
                    })
                pitch_summary.sort(key=lambda x:x["usage_pct"],reverse=True)
            batted_log=[]
            if len(bbe):
                sc2=[c for c in ["game_date","at_bat_number","pitch_number"] if c in bbe.columns]
                bs=bbe.sort_values(sc2,ascending=False) if sc2 else bbe
                for _,r in bs.head(50).iterrows():
                    ev2=safe_float(r.get("launch_speed"),None) if r.get("launch_speed") is not None else None
                    la2=safe_float(r.get("launch_angle"),None) if r.get("launch_angle") is not None else None
                    di=safe_float(r.get("hit_distance_sc"),None) if r.get("hit_distance_sc") is not None else None
                    bat_spd=safe_float(r.get("bat_speed"),None) if "bat_speed" in bbe.columns and r.get("bat_speed") is not None else None
                    pitch_vel=safe_float(r.get("release_speed"),None) if r.get("release_speed") is not None else None
                    result=str(r.get("events","") or "")
                    traj=str(r.get("bb_type","") or "")
                    hc_x=safe_float(r.get("hc_x"),None) if r.get("hc_x") is not None else None
                    hc_y=safe_float(r.get("hc_y"),None) if r.get("hc_y") is not None else None
                    stand=str(r.get("stand","") or "")
                    pull_air=False
                    if hc_x is not None and traj in {"fly_ball","line_drive","popup"}:
                        pull_air = (stand == "R" and hc_x < 125.0) or (stand == "L" and hc_x > 125.0)
                    # Exact HR park carry is not always available from pybaseball. Keep field stable for dashboard.
                    hr_parks = "—"
                    opp_pid=safe_int(r.get("pitcher"),0)
                    opp_name=resolve_mlb_person_name(db, opp_pid, "—")
                    batted_log.append({"date":str(r.get("game_date",""))[:10],
                        "pitcher":opp_name,"pitcher_id":opp_pid,"arm":str(r.get("p_throws","?")),
                        "pitch_type":pname(str(r.get("pitch_type",""))),
                        "ev":round(ev2,2) if ev2 is not None else None,
                        "launch_angle":round(la2,2) if la2 is not None else None,
                        "distance":round(di,1) if di is not None else None,
                        "bat_speed":round(bat_spd,1) if bat_spd is not None else None,
                        "pitch_velocity":round(pitch_vel,1) if pitch_vel is not None else None,
                        "result":result,
                        "event":result,
                        "trajectory":traj,
                        "hc_x":round(hc_x,2) if hc_x is not None else None,
                        "hc_y":round(hc_y,2) if hc_y is not None else None,
                        "hr_parks":hr_parks,
                        "is_hr":result=="home_run",
                        "is_barrel":bool(ev2 is not None and la2 is not None and ev2>=98 and 24<=la2<=32),
                        "is_hard_hit":bool(ev2 is not None and ev2>=95),
                        "is_350_plus":bool(di is not None and di>=350),
                        "is_375_plus":bool(di is not None and di>=375),
                        "is_pull_air":bool(pull_air),
                        "bb_type":traj})
            game_log=[]
            if "game_date" in df.columns and "events" in df.columns:
                df["game_date"]=pd.to_datetime(df["game_date"],errors="coerce")
                try:
                    for gd,gdf in sorted(df.groupby(df["game_date"].dt.normalize()),key=lambda x:x[0],reverse=True)[:20]:
                        ge2=list(gdf["events"].fillna(""))
                        gc=ecounts(ge2)
                        game_log.append({"date":str(gd)[:10],"hits":gc["hits"],"hr":gc["hr"],"xbh":gc["xbh"]})
                    game_log.reverse()
                except Exception:
                    pass
            payload={"player_id":pid,"player_name":bname,"type":"batter","generated":today_str,
                     "top_stats":top_stats,"splits":splits,"pitch_type_summary":pitch_summary,
                     "batted_ball_log":batted_log,"contact_log":batted_log,"spray_chart":batted_log,"game_log":game_log}
            db.set(ckey,payload)
            (pitch_dir/f"batter_{pid}.json").write_text(json.dumps(payload,indent=2),encoding="utf-8")
            print(f"  ✅ batter_{pid}.json — {bname}", file=sys.stderr)
        except Exception as exc:
            print(f"  ⚠️  Batter build error {bname}: {exc}", file=sys.stderr)

    # ── PITCHER FILES ─────────────────────────────────────────────────────────
    for pid, pnm in seen_pitchers.items():
        ckey = f"pitch_type_pitcher_v3_pitchfix:{SEASON}:{pid}"
        cached = db.get(ckey, max_age_days=1)
        if cached is not None:
            (pitch_dir/f"pitcher_{pid}.json").write_text(json.dumps(cached,indent=2),encoding="utf-8")
            continue
        try:
            df = statcast_pitcher(season_start, end_date, pid)
        except Exception as exc:
            print(f"  ⚠️  statcast_pitcher {pnm}: {exc}", file=sys.stderr)
            continue
        if df is None or len(df)==0:
            continue
        try:
            df=df.copy()
            df["launch_speed"]=pd.to_numeric(df.get("launch_speed"),errors="coerce")
            df["launch_angle"]=pd.to_numeric(df.get("launch_angle"),errors="coerce")
            df["hit_distance_sc"]=pd.to_numeric(df.get("hit_distance_sc"),errors="coerce")
            df["estimated_woba_using_speedangle"]=pd.to_numeric(df.get("estimated_woba_using_speedangle"),errors="coerce")
            total_p=max(1,len(df))
            splits={}
            for hand,label in [("L","vs_LHB"),("R","vs_RHB")]:
                sub=df[df["stand"]==hand].copy() if "stand" in df.columns else df.iloc[0:0].copy()
                sb=sub[sub["type"]=="X"].copy() if "type" in sub.columns and len(sub) else sub.iloc[0:0].copy()
                se=list(sub["events"].fillna("")) if "events" in sub.columns else []
                c=ecounts(se)
                sw=pd.to_numeric(sub.get("estimated_woba_using_speedangle"),errors="coerce").dropna() if len(sub) else pd.Series(dtype=float)
                bh=sum(1 for e in se if e in {"single","double","triple"})
                bd=max(1,c["ab"]-c["k"]-c["hr"]+c["sf"])
                splits[label]={"ba_against":round(c["hits"]/c["ab"],3) if c["ab"] else 0.0,
                               "woba_against":round(float(sw.mean()),3) if len(sw) else 0.0,
                               "babip_against":round(bh/bd,3),"hr_given":c["hr"],"xbh_given":c["xbh"],
                               "k_pct":round(spct(c["k"],max(1,c["ab"]))*100,1),
                               "hard_hit_pct":round(float((sb["launch_speed"]>=95).fillna(False).mean())*100,1) if len(sb) else 0.0,
                               "pa":len(sub)}
            pitch_summary=[]
            if "pitch_type" in df.columns:
                for pt,grp in df.groupby("pitch_type"):
                    if not pt or str(pt) in ("nan","None",""): continue
                    gb2=grp[grp["type"]=="X"].copy() if "type" in grp.columns else grp.iloc[0:0].copy()
                    ge=list(grp["events"].fillna("")) if "events" in grp.columns else []
                    c=ecounts(ge)
                    bh=sum(1 for e in ge if e in {"single","double","triple"})
                    bd=max(1,c["ab"]-c["k"]-c["hr"]+c["sf"])
                    if "description" in grp.columns:
                        d2=grp["description"].fillna("")
                        ps=int(d2.str.contains("swing|foul|hit_into_play|missed_bunt",case=False,na=False).sum())
                        pw=int(d2.str.contains("swinging_strike",case=False,na=False).sum())
                    else:
                        ps=pw=0
                    pwo=pd.to_numeric(grp.get("estimated_woba_using_speedangle"),errors="coerce").dropna()
                    pev=gb2["launch_speed"].dropna() if len(gb2) else pd.Series(dtype=float)
                    pv=pd.to_numeric(grp.get("release_speed"),errors="coerce").dropna() if "release_speed" in grp.columns else pd.Series(dtype=float)
                    pitch_summary.append({
                        "pitch_type":pname(pt),"pitch_code":str(pt),
                        "usage_pct":round(spct(len(grp),total_p)*100,1),"count":len(grp),
                        "avg_velo":round(float(pv.mean()),1) if len(pv) else 0.0,
                        "ba_against":round(c["hits"]/c["ab"],3) if c["ab"] else 0.0,
                        "woba_against":round(float(pwo.mean()),3) if len(pwo) else 0.0,
                        "babip_against":round(bh/bd,3),"hr_given":c["hr"],"xbh_given":c["xbh"],
                        "k_pct":round(spct(c["k"],max(1,c["ab"]))*100,1),
                        "whiff_pct":round(spct(pw,max(1,ps))*100,1),
                        "hard_hit_pct":round(float((gb2["launch_speed"]>=95).fillna(False).mean())*100,1) if len(gb2) else 0.0,
                        "avg_ev_allowed":round(float(pev.mean()),1) if len(pev) else 0.0,
                    })
                pitch_summary.sort(key=lambda x:x["usage_pct"],reverse=True)
            payload={"player_id":pid,"player_name":pnm,"type":"pitcher","generated":today_str,
                     "splits":splits,"pitch_type_summary":pitch_summary}
            db.set(ckey,payload)
            (pitch_dir/f"pitcher_{pid}.json").write_text(json.dumps(payload,indent=2),encoding="utf-8")
            print(f"  ✅ pitcher_{pid}.json — {pnm}", file=sys.stderr)
        except Exception as exc:
            print(f"  ⚠️  Pitcher build error {pnm}: {exc}", file=sys.stderr)

    print("✅ Pitch JSON build complete", file=sys.stderr)


# build_projected_lineup_from_pool removed (audit 2026-06-29): never called. Duplicate of build_projected_lineup slot-assignment logic.



def _safe_team_stat_mean(values: List[float], fallback: float) -> float:
    clean = [safe_float(v, fallback) for v in values if v is not None]
    if not clean:
        return fallback
    return sum(clean) / len(clean)


def _lineup_neighbor_context(team_rows: List[HitterRecord]) -> None:
    ordered = sorted(team_rows, key=lambda r: r.lineup_spot)
    if not ordered:
        return

    def ob_proxy(rec: HitterRecord) -> float:
        return (
            0.45 * rec.season_obp +
            0.20 * rec.season_avg +
            0.20 * rec.babip +
            0.15 * min(1.0, rec.season_ops)
        )

    def conv_proxy(rec: HitterRecord) -> float:
        split_avg = rec.avg_vs_lhp if rec.pitcher_throws == "L" else rec.avg_vs_rhp
        return (
            0.34 * min(1.0, rec.season_ops) +
            0.28 * rec.season_obp +
            0.18 * split_avg +
            0.10 * rec.babip +
            0.10 * min(1.0, rec.season_slg)
        )

    n = len(ordered)
    for idx, rec in enumerate(ordered):
        before = ordered[max(0, idx - 2):idx]
        after = ordered[idx + 1:min(n, idx + 3)]

        pre_on = _safe_team_stat_mean([ob_proxy(x) for x in before], 0.33)
        pre_babip = _safe_team_stat_mean([x.babip for x in before], 0.300)
        post_conv = _safe_team_stat_mean([conv_proxy(x) for x in after], 0.34)
        post_babip = _safe_team_stat_mean([x.babip for x in after], 0.300)

        # Recent-form proxy for surrounding batters — are the guys around him
        # actually hot right now (not just good on the season)?
        def recent_form_proxy(rec_x: HitterRecord) -> float:
            return (
                0.45 * minmax_norm(rec_x.last5_hits, 0, 9) +
                0.30 * minmax_norm(rec_x.last10_hits, 0, 14) +
                0.25 * minmax_norm(getattr(rec_x, "last5_runs", 0) + getattr(rec_x, "last5_rbi", 0), 0, 10)
            )
        pre_recent = _safe_team_stat_mean([recent_form_proxy(x) for x in before], 0.30)
        post_recent = _safe_team_stat_mean([recent_form_proxy(x) for x in after], 0.30)
        rec.lineup_surrounding_recent = round((pre_recent + post_recent) / 2.0, 4)

        rec.lineup_pre_onbase = pre_on
        rec.lineup_pre_babip = pre_babip
        rec.lineup_post_convert = post_conv
        rec.lineup_post_babip = post_babip
        rec.lineup_context_before_count = len(before)
        rec.lineup_context_after_count = len(after)

        pre_score = (
            0.42 * minmax_norm(pre_on, 0.280, 0.410) +
            0.18 * pre_recent +
            0.18 * minmax_norm(pre_babip, 0.260, 0.350) +
            0.14 * minmax_norm(_safe_team_stat_mean([x.season_avg for x in before], 0.250), 0.210, 0.330) +
            0.08 * minmax_norm(_safe_team_stat_mean([x.season_ops for x in before], 0.720), 0.620, 0.980)
        )
        post_score = (
            0.36 * minmax_norm(post_conv, 0.280, 0.470) +
            0.18 * post_recent +
            0.16 * minmax_norm(post_babip, 0.260, 0.350) +
            0.16 * minmax_norm(_safe_team_stat_mean([x.season_avg for x in after], 0.250), 0.210, 0.330) +
            0.14 * minmax_norm(_safe_team_stat_mean([x.season_ops for x in after], 0.720), 0.620, 0.980)
        )
        count_boost = 0.94 + 0.03 * len(before) + 0.03 * len(after)
        rec.lineup_context_score = round(100 * (0.58 * pre_score + 0.42 * post_score) * count_boost, 2)


def _apply_lineup_context_to_team_rows(team_rows: List[HitterRecord]) -> None:
    if not team_rows:
        return
    _lineup_neighbor_context(team_rows)
    for rec in team_rows:
        k_contact = 1.0 - minmax_norm(rec.season_k_rate, 0.12, 0.34)
        own_contact = (
            0.22 * minmax_norm(rec.last5_hits, 0, 8) +
            0.17 * minmax_norm(rec.last10_hits, 0, 14) +
            0.16 * minmax_norm(rec.last5_runs + rec.last5_rbi, 0, 10) +
            0.13 * minmax_norm(rec.season_obp, 0.280, 0.420) +
            0.12 * minmax_norm(rec.season_avg, 0.200, 0.330) +
            0.10 * minmax_norm(rec.babip, 0.250, 0.360) +
            0.10 * k_contact
        )
        context_layer = (
            0.42 * minmax_norm(rec.lineup_pre_onbase, 0.280, 0.410) +
            0.16 * minmax_norm(rec.lineup_pre_babip, 0.260, 0.350) +
            0.28 * minmax_norm(rec.lineup_post_convert, 0.280, 0.470) +
            0.14 * minmax_norm(rec.lineup_post_babip, 0.260, 0.350)
        )
        spot_bonus = 1.0 if rec.lineup_spot in (1, 2, 3, 4, 5) else 0.92 if rec.lineup_spot == 6 else 0.84
        production_quality = (
            0.50 * minmax_norm(rec.season_ops, 0.620, 0.980) +
            0.28 * minmax_norm(rec.recent_xwoba, 0.280, 0.450) +
            0.22 * minmax_norm(rec.last5_runs + rec.last5_rbi, 0, 10)
        )
        revised_hrr = 100 * (
            0.48 * own_contact +    # reduced slightly to make room for context
            0.28 * context_layer +  # increased: lineup environment matters more for production
            0.18 * production_quality +
            0.06 * minmax_norm(rec.season_bb_rate, 0.04, 0.16)
        ) * spot_bonus
        # Heavier 0-for-5 protection for HRR.
        revised_hrr -= 8.0 * minmax_norm(rec.season_k_rate, 0.16, 0.34)
        rec.hrr_score = round(max(0.0, revised_hrr), 2)
        rec.hit_score = round(max(0.0, rec.hit_score - 3.0 * minmax_norm(rec.season_k_rate, 0.16, 0.34)), 2)
        rec.contact_score = round(max(0.0, rec.contact_score - 2.0 * minmax_norm(rec.season_k_rate, 0.16, 0.34)), 2)
        rec.overall_score = round(
            0.52 * rec.hr_score +
            0.25 * rec.hrr_score +
            0.15 * rec.hit_score +
            0.08 * rec.contact_score,
            2,
        )
        # BUGFIX (audit 2026-06-29): previously called apply_model_v2_layers(rec)
        # here, which re-ran the full scoring pipeline on already-mutated inputs.
        # On that second call, _cs_base (consistency_score) read the lineup-context
        # versions of hrr_score/hit_score/contact_score as if they were the clean
        # V2 baselines -- corrupting consistency_score and all five *_legacy fields
        # for every player, every slate. Confirmed in 8 downstream consumers
        # (pool ranking, pair selection, "Reliable" tag). The lineup-context
        # adjustments above are correct and should be kept; only the re-invocation
        # of the full scoring pipeline was wrong.
        # Now: sync the v2 fields and recompute overall_score cleanly from the
        # adjusted values -- same effect on the final numbers, without the
        # corrupted consistency_score/legacy snapshot side effects.
        rec.hrr_score_v2 = rec.hrr_score
        rec.hit_score_v2 = rec.hit_score
        rec.contact_score_v2 = rec.contact_score


def get_player_split_stats(client: MLBClient, db: CacheDB, player_id: int) -> Dict[str, float]:
    key = f"split_stats:{SEASON}:{player_id}"
    cached = db.get(key, max_age_days=7)
    if cached is not None:
        return cached
    out = {
        "avg_vs_rhp": 0.250, "avg_vs_lhp": 0.250,
        "iso_vs_rhp": 0.150, "iso_vs_lhp": 0.150,
    }
    try:
        blob = client.split_stats(player_id)
        stats = blob.get("stats") or []
        first = stats[0] if stats else {}
        for split in first.get("splits") or []:
            code = ((split.get("split") or {}).get("code")) or ""
            stat = split.get("stat", {}) or {}
            avg = safe_float(stat.get("avg"), 0.250)
            slg = safe_float(stat.get("slg"), 0.400)
            if code == "vr":
                out["avg_vs_rhp"] = avg
                out["iso_vs_rhp"] = max(0.0, slg - avg)
            elif code == "vl":
                out["avg_vs_lhp"] = avg
                out["iso_vs_lhp"] = max(0.0, slg - avg)
    except Exception:
        pass
    db.set(key, out)
    return out


def _sit_line(stat: Dict[str, Any]) -> Dict[str, Any]:
    """One split bucket -> the compact line the modal's control reads.
    Same IP-notation care as the handed splits (a .1/.2 inning is a third,
    not a decimal); ops read straight off the stat blob when MLB provides
    it, rebuilt from obp+slg when it does not; bf kept so the site can
    refuse to draw a conclusion on a 9-batter sample."""
    ip = _parse_ip_to_float(stat.get("inningsPitched"))
    hr = safe_float(stat.get("homeRuns"), 0.0)
    hits = safe_float(stat.get("hits"), 0.0)
    bb = safe_float(stat.get("baseOnBalls"), 0.0)
    ops = safe_float(stat.get("ops"), 0.0)
    if not ops:
        ops = safe_float(stat.get("obp"), 0.0) + safe_float(stat.get("slg"), 0.0)
    return {
        "ip": round(ip, 1),
        "bf": safe_int(stat.get("battersFaced"), 0),
        "hr": int(hr),
        "hr9": round(hr * 9.0 / ip, 2) if ip > 0 else None,
        "whip": round((hits + bb) / ip, 2) if ip > 0 else None,
        "ops": round(ops, 3) if ops else None,
        "avg": safe_float(stat.get("avg"), 0.0) or None,
    }


def parse_pitcher_situational_splits(client: MLBClient, db: CacheDB, pitcher_id: int) -> Dict[str, Any]:
    """DATA DEFECT #4 (2026-08-23): five of the eight split axes Donovan
    named for the pitcher modal published nothing -- in park (home/away),
    day/night, RISP, count state, calendar. The modal deliberately shipped
    without their buttons rather than faking them ("a dead pill, or one
    that silently falls back to the season line, answers a question it was
    not asked" -- claude/moonshot-pitcher-modal.md). This publishes all
    five as one dict, so each button becomes the promised one-line
    addition to the control's options array.

    Shape: { status, home, away, day, night, risp, ahead, behind,
             by_month: {"4".."10": line}, by_dow: {"Monday"..: line} }
    where every line is _sit_line's compact form. A bucket MLB returns
    nothing for is simply absent -- the site's rule stands: no data, no
    button. Cached 2 days, same as the handed splits beside it.
    """
    key = f"pitcher_sit_splits_v1:{SEASON}:{pitcher_id}"
    cached = db.get(key, max_age_days=2)
    if isinstance(cached, dict) and cached.get("status"):
        return cached

    CODE_NAMES = {"h": "home", "a": "away", "d": "day", "n": "night",
                  "risp": "risp", "ac": "ahead", "bc": "behind"}
    out: Dict[str, Any] = {"status": "empty"}
    try:
        blob = client.pitcher_situational_stats(pitcher_id)
        stats = blob.get("stats") or []
        first = stats[0] if stats else {}
        for split in first.get("splits") or []:
            code = str(((split.get("split") or {}).get("code")) or "").lower()
            name = CODE_NAMES.get(code)
            if not name:
                continue
            stat = split.get("stat", {}) or {}
            if stat:
                out[name] = _sit_line(stat)
        # calendar, axis 1: by month (the "July" half of the ask)
        mblob = client.pitcher_month_stats(pitcher_id)
        mstats = (mblob.get("stats") or [{}])[0]
        by_month: Dict[str, Any] = {}
        for split in mstats.get("splits") or []:
            m = split.get("month")
            stat = split.get("stat", {}) or {}
            if m is None or not stat:
                continue
            by_month[str(safe_int(m, 0))] = _sit_line(stat)
        if by_month:
            out["by_month"] = by_month
        # calendar, axis 2: by day of week (the "Wednesday / weekends" half)
        dblob = client.pitcher_dow_stats(pitcher_id)
        dstats = (dblob.get("stats") or [{}])[0]
        by_dow: Dict[str, Any] = {}
        for split in dstats.get("splits") or []:
            dow = split.get("dayOfWeek")
            if isinstance(dow, dict):
                dow = dow.get("description") or dow.get("name") or dow.get("id")
            stat = split.get("stat", {}) or {}
            if dow is None or not stat:
                continue
            by_dow[str(dow)] = _sit_line(stat)
        if by_dow:
            out["by_dow"] = by_dow
        out["status"] = "ok" if len(out) > 1 else "empty"
    except Exception as exc:
        out = {"status": f"error:{type(exc).__name__}"}

    try:
        db.set(key, out)
    except Exception:
        pass
    return out


def parse_pitcher_handed_splits(client: MLBClient, db: CacheDB, pitcher_id: int) -> Dict[str, float]:
    key = f"pitcher_splits:{SEASON}:{pitcher_id}"
    cached = db.get(key, max_age_days=2)
    if cached is not None:
        return cached

    out = {
        "hr9_vs_lhb": 1.05,
        "hr9_vs_rhb": 1.05,
        "whip_vs_lhb": 1.28,
        "whip_vs_rhb": 1.28,
        "slg_vs_lhb": 0.400, "slg_vs_rhb": 0.400,
        "ops_vs_lhb": 0.720, "ops_vs_rhb": 0.720,
        "avg_vs_lhb": 0.250, "avg_vs_rhb": 0.250,
        "babip_vs_lhb": 0.300, "babip_vs_rhb": 0.300,
        "iso_vs_lhb": 0.150, "iso_vs_rhb": 0.150,
        "bf_vs_lhb": 0, "bf_vs_rhb": 0,
        "bbe_vs_lhb": 0, "bbe_vs_rhb": 0,
        # Raw counts allowed by batter handedness (not rates) — season-to-date.
        # Pulled from the same pitcher_split_stats call below; HR was already
        # being fetched and discarded after computing hr9, XBH is 2B+3B+HR.
        "hr_vs_lhb": 0, "hr_vs_rhb": 0,
        "xbh_vs_lhb": 0, "xbh_vs_rhb": 0,
        "weak_side": "",
        "weak_side_score_lhb": 0.0,   # 0-100, how exploitable LHB are vs this pitcher
        "weak_side_score_rhb": 0.0,
        "weak_side_gap": 0.0,         # 0-1, how lopsided the split is
    }
    try:
        blob = client.pitcher_split_stats(pitcher_id)
        stats = blob.get("stats") or []
        first = stats[0] if stats else {}
        for split in first.get("splits") or []:
            code = ((split.get("split") or {}).get("code")) or ""
            stat = split.get("stat", {}) or {}
            hr = safe_float(stat.get("homeRuns"), 0.0)
            batters = max(1.0, safe_float(stat.get("battersFaced"), 0.0))
            # BUGFIX: was safe_float(inningsPitched) which reads MLB's "6.1"
            # notation as the literal decimal 6.1 instead of 6.333 (1 out = 1/3 IP).
            # _parse_ip_to_float already exists and is used correctly elsewhere.
            # The inflated denominator made hr9_vs_lhb/rhb and whip_vs_lhb/rhb
            # systematically worse-than-reality (smaller divisor = larger rate).
            ip = max(1.0, _parse_ip_to_float(stat.get("inningsPitched")))
            hits = safe_float(stat.get("hits"), 0.0)
            bb = safe_float(stat.get("baseOnBalls"), 0.0)
            ab = max(1.0, safe_float(stat.get("atBats"), 1.0))
            doubles = safe_float(stat.get("doubles"), 0.0)
            triples = safe_float(stat.get("triples"), 0.0)
            so = safe_float(stat.get("strikeOuts"), 0.0)
            avg = safe_float(stat.get("avg"), 0.250)
            slg = safe_float(stat.get("slg"), 0.400)
            obp = safe_float(stat.get("obp"), 0.320)
            ops = safe_float(stat.get("ops"), slg + obp)
            hr9 = (hr * 9.0) / ip
            whip = (hits + bb) / ip
            iso = max(0.0, slg - avg)
            # BABIP = (H - HR) / (AB - K - HR + SF); approximate without SF
            babip_den = max(1.0, ab - so - hr)
            babip = max(0.0, (hits - hr) / babip_den)
            if code == "vl":
                out.update({"hr9_vs_lhb": hr9, "whip_vs_lhb": whip, "slg_vs_lhb": slg,
                            "ops_vs_lhb": ops, "avg_vs_lhb": avg, "babip_vs_lhb": babip,
                            "iso_vs_lhb": iso, "bf_vs_lhb": int(batters),
                            "hr_vs_lhb": int(hr), "xbh_vs_lhb": int(doubles + triples + hr),
                            "bbe_vs_lhb": int(max(0.0, ab - so))})
            elif code == "vr":
                out.update({"hr9_vs_rhb": hr9, "whip_vs_rhb": whip, "slg_vs_rhb": slg,
                            "ops_vs_rhb": ops, "avg_vs_rhb": avg, "babip_vs_rhb": babip,
                            "iso_vs_rhb": iso, "bf_vs_rhb": int(batters),
                            "hr_vs_rhb": int(hr), "xbh_vs_rhb": int(doubles + triples + hr),
                            "bbe_vs_rhb": int(max(0.0, ab - so))})

        # Graded weakness score per side — blends everything available.
        # Higher = more exploitable for a batter on that side.
        # BA added per audit (2026-06-28): was available (avg_vs_lhb/rhb)
        # but never used in this formula. Other weights trimmed
        # proportionally to make room.
        def side_weakness(hr9, slg, ops, iso, babip, whip, ba):
            return 100 * (
                0.27 * minmax_norm(hr9, 0.70, 2.10) +
                0.22 * minmax_norm(slg, 0.350, 0.560) +
                0.16 * minmax_norm(iso, 0.110, 0.260) +
                0.13 * minmax_norm(ops, 0.640, 0.900) +
                0.07 * minmax_norm(babip, 0.270, 0.350) +
                0.05 * minmax_norm(whip, 1.05, 1.55) +
                0.10 * minmax_norm(ba, 0.220, 0.290)
            )
        bf_l, bf_r = out["bf_vs_lhb"], out["bf_vs_rhb"]
        bbe_l, bbe_r = out["bbe_vs_lhb"], out["bbe_vs_rhb"]
        # Confidence now scaled off BBE (balls in play, AB-SO) rather than BF
        # (batters faced) -- per audit (2026-06-28). BF includes walks/HBP
        # that never produced a batted ball at all, so it overstates the
        # real sample size behind a contact-quality-driven score like this
        # one. Full trust at 30+ BBE, partial down to 10.
        conf_l = min(1.0, max(0.0, (bbe_l - 10) / 20.0))
        conf_r = min(1.0, max(0.0, (bbe_r - 10) / 20.0))
        wk_l = side_weakness(out["hr9_vs_lhb"], out["slg_vs_lhb"], out["ops_vs_lhb"], out["iso_vs_lhb"], out["babip_vs_lhb"], out["whip_vs_lhb"], out["avg_vs_lhb"]) * conf_l
        wk_r = side_weakness(out["hr9_vs_rhb"], out["slg_vs_rhb"], out["ops_vs_rhb"], out["iso_vs_rhb"], out["babip_vs_rhb"], out["whip_vs_rhb"], out["avg_vs_rhb"]) * conf_r
        out["weak_side_score_lhb"] = round(wk_l, 1)
        out["weak_side_score_rhb"] = round(wk_r, 1)

        # Weak side: graded gap, threshold lowered to 12% (was 25%), min 15 BF each.
        MIN_BF = 15
        if bf_l >= MIN_BF and bf_r >= MIN_BF and (wk_l > 0 or wk_r > 0):
            hi, lo = max(wk_l, wk_r), min(wk_l, wk_r)
            out["weak_side_gap"] = round((hi - lo) / max(1.0, hi), 3)
            if wk_l >= wk_r * 1.12 and wk_l >= 45:
                out["weak_side"] = "LHB"
            elif wk_r >= wk_l * 1.12 and wk_r >= 45:
                out["weak_side"] = "RHB"
            else:
                out["weak_side"] = ""
        else:
            out["weak_side"] = ""
    except Exception:
        pass

    db.set(key, out)
    return out



# ══════════════════════════════════════════════════════════════════════════
# DOCKET #20 — EXPECTED HOME RUNS FROM CONTACT (the "luck" layer)
#
# One machine: league HR rate per (EV, LA) bucket, accumulated from the same
# per-batter season statcast pulls the bot already makes. Every tracked ball
# then has an xHR probability that depends only on how it left the bat — no
# park, no weather, which is the point. From that single table:
#   season_xhr / season_hr_luck / recent_xhr    per hitter
#   pitcher_xhr_allowed / pitcher_hr_luck       per starter
#   hr_class (no_doubter / likely / maybe)      per HR in the spray chart
# The spray-chart classes use LAST run's finalized table (physics doesn't
# change overnight); hitter/pitcher numbers use THIS run's. Buckets are 2 mph
# × 3°, buckets under 100 balls borrow their 3×3 neighborhood, and no player
# number prints under 50 tracked balls.
# ══════════════════════════════════════════════════════════════════════════

XHR_MIN_PLAYER_BBE = 50
XHR_MIN_BUCKET = 100
XHR_MIN_LEAGUE_BALLS = 5000

_XHR_ACCUM: Dict[str, List[int]] = {}
_XHR_BY_PID: Dict[int, Dict[str, Any]] = {}
_XHR_PITCHERS: Dict[int, Dict[str, Any]] = {}
_XHR_PREV: Optional[Dict[str, float]] = None
_XHR_PREV_LOADED = False


def _xhr_key(ev: float, la: float) -> str:
    return f"{int(round(ev / 2.0)) * 2}_{int(round(la / 3.0)) * 3}"


def build_xhr_hist(bbe_df) -> Dict[str, List[int]]:
    """{bucket: [balls, hr]} from a BBE frame; only rows with tracked EV+LA."""
    out: Dict[str, List[int]] = {}
    try:
        if bbe_df is None or len(bbe_df) == 0:
            return out
        sub = bbe_df[bbe_df["launch_speed"].notna() & bbe_df["launch_angle"].notna()]
        for _, b in sub.iterrows():
            ev = float(b["launch_speed"]); la = float(b["launch_angle"])
            if ev <= 0:
                continue
            k = _xhr_key(ev, la)
            cell = out.setdefault(k, [0, 0])
            cell[0] += 1
            if str(b.get("events", "")) == "home_run":
                cell[1] += 1
    except Exception:
        return out
    return out


def _xhr_neighbors(k: str) -> List[str]:
    try:
        ev, la = (int(x) for x in k.split("_"))
    except Exception:
        return [k]
    return [f"{ev + de}_{la + dl}" for de in (-2, 0, 2) for dl in (-3, 0, 3)]


def finalize_xhr_table(accum: Dict[str, List[int]]) -> Optional[Dict[str, float]]:
    total = sum(v[0] for v in accum.values())
    if total < XHR_MIN_LEAGUE_BALLS:
        return None
    table: Dict[str, float] = {}
    for k, (n, hr) in accum.items():
        if n >= XHR_MIN_BUCKET:
            table[k] = hr / n
        else:
            pn = ph = 0
            for nk in _xhr_neighbors(k):
                cell = accum.get(nk)
                if cell:
                    pn += cell[0]; ph += cell[1]
            table[k] = (ph / pn) if pn else 0.0
    return table


def xhr_expected(hist: Dict[str, List[int]], table: Dict[str, float]) -> float:
    exp = 0.0
    for k, (n, _hr) in hist.items():
        p = table.get(k)
        if p is None:
            pn = ph_rate = 0.0
            for nk in _xhr_neighbors(k):
                if nk in table:
                    pn += 1; ph_rate += table[nk]
            p = (ph_rate / pn) if pn else 0.0
        exp += n * p
    return exp


def _xhr_register_batter(pid: int, sc: Dict[str, Any]) -> None:
    hist = sc.get("xhr_hist") or {}
    if not isinstance(hist, dict) or not hist:
        return
    for k, cell in hist.items():
        try:
            n, hr = int(cell[0]), int(cell[1])
        except Exception:
            continue
        acc = _XHR_ACCUM.setdefault(k, [0, 0])
        acc[0] += n; acc[1] += hr
    _XHR_BY_PID[int(pid)] = {
        "hist": hist,
        "recent": sc.get("xhr_hist_recent") or {},
    }


def _xhr_register_pitcher(pitcher_id: int, psc: Dict[str, Any]) -> None:
    hist = psc.get("xhr_hist_allowed") or {}
    if isinstance(hist, dict) and hist:
        # NOT added to the league accumulator — every ball is already counted
        # once from the batter side; adding the pitcher view would double it.
        _XHR_PITCHERS[int(pitcher_id)] = {"hist": hist}


def _xhr_prev_table(db: CacheDB) -> Optional[Dict[str, float]]:
    global _XHR_PREV, _XHR_PREV_LOADED
    if not _XHR_PREV_LOADED:
        _XHR_PREV_LOADED = True
        try:
            blob = db.get(f"xhr_league_table:{SEASON}")
            if isinstance(blob, dict) and isinstance(blob.get("table"), dict):
                _XHR_PREV = blob["table"]
        except Exception:
            _XHR_PREV = None
    return _XHR_PREV


def classify_hr_prob(prob: Optional[float]) -> str:
    if prob is None:
        return ""
    if prob >= 0.97: return "no_doubter"
    if prob >= 0.60: return "likely"
    if prob >= 0.10: return "maybe"
    return "cheap"


def xhr_hr_class(db: CacheDB, ev: Optional[float], la: Optional[float]) -> str:
    """Doubt class for one HR, from LAST run's table. '' until one exists."""
    table = _xhr_prev_table(db)
    if not table or ev is None or la is None or ev <= 0:
        return ""
    k = _xhr_key(float(ev), float(la))
    p = table.get(k)
    if p is None:
        pn = pr = 0.0
        for nk in _xhr_neighbors(k):
            if nk in table:
                pn += 1; pr += table[nk]
        p = (pr / pn) if pn else None
    return classify_hr_prob(p)


def finalize_xhr_fields(rows: List["HitterRecord"], db: CacheDB) -> None:
    """After the build loop: finalize this run's league table, persist it for
    tomorrow's spray classes, and stamp hitter + facing-pitcher luck fields."""
    table = finalize_xhr_table(_XHR_ACCUM)
    if table is None:
        return
    try:
        total = sum(v[0] for v in _XHR_ACCUM.values())
        db.set(f"xhr_league_table:{SEASON}", {"table": table, "balls": total,
                                              "generated": dt.datetime.now().isoformat()})
    except Exception:
        pass
    for r in rows:
        reg = _XHR_BY_PID.get(safe_int(getattr(r, "player_id", 0), 0))
        if reg:
            hist = reg["hist"]
            bbe = sum(c[0] for c in hist.values())
            r.xhr_bbe = bbe
            if bbe >= XHR_MIN_PLAYER_BBE:
                exp = xhr_expected(hist, table)
                actual = sum(c[1] for c in hist.values())
                r.season_xhr = round(exp, 2)
                r.season_hr_luck = round(actual - exp, 2)
                rec_hist = reg.get("recent") or {}
                if rec_hist:
                    r.recent_xhr = round(xhr_expected(rec_hist, table), 2)
        preg = _XHR_PITCHERS.get(safe_int(getattr(r, "pitcher_id", 0), 0))
        if preg:
            hist = preg["hist"]
            bbe = sum(c[0] for c in hist.values())
            r.pitcher_xhr_bbe = bbe
            if bbe >= XHR_MIN_PLAYER_BBE:
                exp = xhr_expected(hist, table)
                actual = sum(c[1] for c in hist.values())
                r.pitcher_xhr_allowed = round(exp, 2)
                r.pitcher_hr_luck = round(actual - exp, 2)


def build_batter_statcast_profile(db: CacheDB, player_id: int, end_date: dt.date) -> Dict[str, Any]:
    key = f"batter_statcast_v10_power_metrics:{SEASON}:{player_id}:{end_date.isoformat()}"
    out = {
        "recent_350_num": 0,
        "recent_350_den": 1,
        "recent_distance_tracked": 0,
        "recent_375_num": 0,
        "recent_400_num": 0,
        "recent_max_distance": 0.0,
        "recent_avg_distance": 0.0,
        "recent_avg_hr_distance": 0.0,
        "season_max_distance": 0.0,
        "recent_ev": 88.5,
        "recent_hard_hit_rate": 0.0,
        "recent_sweet_spot_rate": 0.0,
        "recent_ideal_hr_contact": 0.0,
        "recent_fb_rate": 0.0,
        "recent_ld_rate": 0.0,
        "recent_gb_rate": 0.0,
        "recent_popup_rate": 0.0,
        "recent_barrel_rate": 0.0,
        "recent_xwoba": 0.320,
        "recent_pull_rate": 0.38,
        "recent_pull_air_rate": 0.0,
        "recent_squared_up_rate": None,
        "recent_squared_up_sample": 0,
        "recent_blast_rate": None,
        "recent_bat_tracking_status": "missing",
        "recent_bat_tracking_window": "",
        "l5_barrel_rate": 0.0,
        "l10_barrel_rate": 0.0,
        "l5_hard_hit_rate": 0.0,
        "l10_hard_hit_rate": 0.0,
        "l5_xwoba": 0.320,
        "l10_xwoba": 0.320,
        "l5_pull_rate": 0.38,
        "l10_pull_rate": 0.38,
        "l20pa_pa": 0,
        "l20pa_bbe": 0,
        "l20pa_hr": 0,
        "l20pa_xbh": 0,
        "l20pa_350_num": 0,
        "l20pa_350_den": 1,
        "l20pa_375_num": 0,
        "l20pa_hard_hit_rate": 0.0,
        "l20pa_ideal_hr_contact": 0.0,
        "l20pa_fb_rate": 0.0,
        "l20pa_barrel_rate": 0.0,
        "l20pa_xwoba": 0.320,
        "l20pa_pull_rate": 0.38,
        "l25pa_pa": 0,
        "l25pa_bbe": 0,
        "l25pa_avg_ev": 88.5,
        "l25pa_avg_la": 0.0,
        "l25pa_hard_hit_rate": 0.0,
        "l25pa_barrel_rate": 0.0,
        "l25pa_sweet_spot_rate": 0.0,
        "l25pa_ld_rate": 0.0,
        "l25pa_gb_rate": 0.0,
        "l25pa_fb_rate": 0.0,
        "l25pa_popup_rate": 0.0,
        "l25pa_air_rate": 0.0,
        "l25pa_300_plus": 0,
        "l25pa_375_plus": 0,
        "l25pa_avg_bat_speed": None,
        "l25pa_avg": 0.0,
        "babip": 0.300,
        "avg_bat_speed": None,
        "bbe_profile": {},
        "spray_chart": [],
        "contact_log": [],
        "batted_ball_log": [],
        "hr_shape_profile": {},
        "personal_shape_match": 0.0,
        "personal_shape_recent_rate": 0.0,
        "personal_shape_season_rate": 0.0,
        "personal_shape_status": "missing",
        "statcast_pull_status": "missing",
    }
    bat_tracking = build_recent_bat_tracking_lookup(db, end_date).get(str(player_id))
    if isinstance(bat_tracking, dict):
        out["recent_squared_up_rate"] = safe_float(bat_tracking.get("squared_up_per_bat_contact"), 0.0)
        out["recent_squared_up_sample"] = safe_int(bat_tracking.get("contact"), 0)
        out["recent_blast_rate"] = safe_float(bat_tracking.get("blast_per_bat_contact"), 0.0)
        out["recent_bat_tracking_status"] = "ok"
        out["recent_bat_tracking_window"] = str(bat_tracking.get("window") or "")
    cached = db.get(key, max_age_days=1)
    if cached is not None:
        merged = dict(out)
        if isinstance(cached, dict):
            merged.update(cached)
        # Backward-compatible normalization for older cache rows that lacked newer fields.
        if safe_int(merged.get("recent_distance_tracked"), 0) <= 0:
            merged["recent_distance_tracked"] = max(
                safe_int(merged.get("recent_350_den"), 1),
                safe_int(merged.get("recent_350_num"), 0),
                safe_int(merged.get("recent_375_num"), 0),
            )
        merged["recent_350_den"] = max(
            1,
            safe_int(merged.get("recent_350_den"), 1),
            safe_int(merged.get("recent_350_num"), 0),
            safe_int(merged.get("recent_375_num"), 0),
        )
        # Older cached bbe_profile blobs predate dist_300_plus/avg_bat_speed/
        # bat_speed_in_band_rate -- backfill so "Yesterdays Hitters" scoring
        # doesn't silently fall back to defaults on stale cache hits.
        _bp = merged.get("bbe_profile")
        if isinstance(_bp, dict):
            _bp.setdefault("dist_300_plus", _bp.get("dist_375_plus", 0))
            _bp.setdefault("avg_bat_speed", merged.get("avg_bat_speed"))
            _bp.setdefault("bat_speed_in_band_rate", 0.0)
        return merged

    if statcast_batter is None:
        db.set(key, out)
        return out
    try:
        df = statcast_batter(SEASON_START.isoformat(), end_date.isoformat(), player_id)
    except Exception:
        db.set(key, out)
        return out
    if df is None or len(df) == 0:
        db.set(key, out)
        return out

    try:
        df = df.copy()
        df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
        df["hit_distance_sc"] = pd.to_numeric(df.get("hit_distance_sc"), errors="coerce")
        df["launch_speed"] = pd.to_numeric(df.get("launch_speed"), errors="coerce")
        df["launch_angle"] = pd.to_numeric(df.get("launch_angle"), errors="coerce")
        if "bat_speed" in df.columns:
            df["bat_speed"] = pd.to_numeric(df.get("bat_speed"), errors="coerce")

        played = df["game_date"].dropna().dt.normalize()
        last_game_dates = list(played[played <= pd.Timestamp(end_date)].drop_duplicates().sort_values().tail(8))
        if last_game_dates:
            recent = df[df["game_date"].dt.normalize().isin(last_game_dates)].copy()
        else:
            recent = df[df["game_date"] >= pd.Timestamp(end_date - dt.timedelta(days=14))].copy()

        bbe = recent[recent["type"] == "X"].copy()
        bbe = dedupe_statcast_bbe(bbe)
        out["recent_350_den"] = max(1, int(len(bbe)))

        tracked_dist = bbe["hit_distance_sc"].notna() if len(bbe) else pd.Series(dtype=bool)
        out["recent_distance_tracked"] = int(tracked_dist.sum()) if len(bbe) else 0
        out["recent_350_num"] = int((bbe["hit_distance_sc"] >= 350).fillna(False).sum()) if len(bbe) else 0
        out["recent_375_num"] = int((bbe["hit_distance_sc"] >= 375).fillna(False).sum()) if len(bbe) else 0
        # Docket #19: real distances, not just bucket counts. Guard: aggregate
        # only rows where distance is tracked (>0) -- untracked balls write
        # NaN/0 and a max() over them ships a 0-ft "longest ball".
        out["recent_400_num"] = int((bbe["hit_distance_sc"] >= 400).fillna(False).sum()) if len(bbe) else 0
        _rd = bbe.loc[bbe["hit_distance_sc"] > 0, "hit_distance_sc"] if len(bbe) else None
        out["recent_max_distance"] = float(_rd.max()) if _rd is not None and len(_rd) else 0.0
        out["recent_avg_distance"] = round(float(_rd.mean()), 1) if _rd is not None and len(_rd) else 0.0
        _hd = bbe.loc[(bbe.get("events") == "home_run") & (bbe["hit_distance_sc"] > 0), "hit_distance_sc"] if len(bbe) else None
        out["recent_avg_hr_distance"] = round(float(_hd.mean()), 1) if _hd is not None and len(_hd) else 0.0
        _season_bbe = df[df["type"] == "X"] if "type" in df.columns else df
        _sd = _season_bbe.loc[_season_bbe["hit_distance_sc"] > 0, "hit_distance_sc"]
        out["season_max_distance"] = float(_sd.max()) if len(_sd) else 0.0
        # Docket #20: per-player (EV, LA) histograms — season for the league
        # table + his own xHR, recent window for recent_xhr.
        out["xhr_hist"] = build_xhr_hist(_season_bbe)
        out["xhr_hist_recent"] = build_xhr_hist(bbe)

        out["recent_fb_rate"] = float((bbe.get("bb_type") == "fly_ball").mean()) if len(bbe) else 0.0
        out["recent_ev"] = float(bbe["launch_speed"].dropna().mean()) if len(bbe) and bbe["launch_speed"].notna().any() else 88.5
        out["recent_hard_hit_rate"] = float((bbe["launch_speed"] >= 95).fillna(False).mean()) if len(bbe) else 0.0
        out["recent_sweet_spot_rate"] = float(((bbe["launch_angle"] >= 8) & (bbe["launch_angle"] <= 32)).fillna(False).mean()) if len(bbe) else 0.0

        # Keep 350+ alive as the broader power proxy. Treat 375+ as extra lift, not a hard gate.
        # Also stop requiring tracked distance for "ideal HR contact" so sparse distance data doesn't zero everything out.
        out["recent_ideal_hr_contact"] = float(
            (
                (bbe["launch_speed"] >= 97) &
                (bbe["launch_angle"] >= 18) &
                (bbe["launch_angle"] <= 36)
            ).fillna(False).mean()
        ) if len(bbe) else 0.0

        out["recent_barrel_rate"] = float(
            (
                (bbe["launch_speed"] >= 98) &
                (bbe["launch_angle"] >= 24) &
                (bbe["launch_angle"] <= 32)
            ).fillna(False).mean()
        ) if len(bbe) else 0.0

        # Expected production + pull tendency. These are process stats, not outcome stats.
        # xwOBA uses Baseball Savant's estimated_woba_using_speedangle when available.
        if len(recent) and "estimated_woba_using_speedangle" in recent.columns:
            xw = pd.to_numeric(recent["estimated_woba_using_speedangle"], errors="coerce").dropna()
            out["recent_xwoba"] = float(xw.mean()) if len(xw) else 0.320

        def _pulled_mask(frame: pd.DataFrame) -> pd.Series:
            if frame is None or len(frame) == 0 or "hc_x" not in frame.columns or "stand" not in frame.columns:
                return pd.Series(False, index=frame.index if frame is not None else None, dtype=bool)
            tmp = frame.copy()
            tmp["hc_x"] = pd.to_numeric(tmp["hc_x"], errors="coerce")
            return (
                ((tmp["stand"] == "R") & (tmp["hc_x"] < 125.0)) |
                ((tmp["stand"] == "L") & (tmp["hc_x"] > 125.0))
            ).fillna(False)

        def _pull_rate(frame: pd.DataFrame) -> float:
            if frame is None or len(frame) == 0 or "hc_x" not in frame.columns or "stand" not in frame.columns:
                return 0.38
            tracked = frame[pd.to_numeric(frame["hc_x"], errors="coerce").notna()]
            if len(tracked) == 0:
                return 0.38
            # Savant spray approximation: RHB pulled toward LF has lower hc_x, LHB pulled toward RF has higher hc_x.
            return float(_pulled_mask(tracked).mean())

        out["recent_pull_rate"] = _pull_rate(bbe)
        if len(bbe) and "bb_type" in bbe.columns and "hc_x" in bbe.columns and "stand" in bbe.columns:
            # PullAir% is a share of every BBE, matching the pitcher-side
            # pullair_allowed_pct definition.  It is not the pull rate within
            # air balls (that conditional rate remains in bbe_profile below).
            in_air = bbe["bb_type"].isin(["fly_ball", "line_drive"])
            out["recent_pull_air_rate"] = float((in_air & _pulled_mask(bbe)).sum() / len(bbe))
        out["statcast_pull_status"] = "ok" if len(bbe) and "hc_x" in bbe.columns else "missing_hc_x"

        # Full BBE profile + spray chart points for the website. Keep this inside the slate JSON,
        # not only in separate Pitch Lab files, so player modals never show blank data when the row has it.
        def _clean_num(value, digits=2):
            try:
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    return None
                v = float(value)
                if pd.isna(v):
                    return None
                return round(v, digits)
            except Exception:
                return None

        def _event_count(frame: pd.DataFrame, events_set) -> int:
            if frame is None or len(frame) == 0 or "events" not in frame.columns:
                return 0
            return int(frame["events"].fillna("").isin(events_set).sum())

        air_bbe = bbe[bbe.get("bb_type").isin(["fly_ball", "line_drive", "popup"])] if len(bbe) and "bb_type" in bbe.columns else bbe.iloc[0:0].copy()
        air_pull_rate = _pull_rate(air_bbe) if len(air_bbe) else 0.0
        xbh_count = _event_count(bbe, {"double", "triple", "home_run"})
        hr_count = _event_count(bbe, {"home_run"})
        avg_distance = safe_float(out.get("recent_avg_distance"), 0.0)
        max_distance = float(bbe["hit_distance_sc"].dropna().max()) if len(bbe) and bbe["hit_distance_sc"].notna().any() else 0.0
        max_ev = float(bbe["launch_speed"].dropna().max()) if len(bbe) and bbe["launch_speed"].notna().any() else 0.0
        avg_la = float(bbe["launch_angle"].dropna().mean()) if len(bbe) and bbe["launch_angle"].notna().any() else 0.0
        gb_rate = float((bbe.get("bb_type") == "ground_ball").mean()) if len(bbe) and "bb_type" in bbe.columns else 0.0
        ld_rate = float((bbe.get("bb_type") == "line_drive").mean()) if len(bbe) and "bb_type" in bbe.columns else 0.0
        pu_rate = float((bbe.get("bb_type") == "popup").mean()) if len(bbe) and "bb_type" in bbe.columns else 0.0
        # Promoted to top-level fields (2026-08-12, on request: "add ld% to
        # the filter for the board and watchlist") -- gb_rate/ld_rate/pu_rate
        # were already computed for bbe_profile below, just never exposed at
        # the top level HitterRecord reads from. Same recent window as
        # recent_fb_rate/recent_pull_rate, so a Band filter switched between
        # them compares apples to apples.
        out["recent_ld_rate"] = ld_rate
        out["recent_gb_rate"] = gb_rate
        out["recent_popup_rate"] = pu_rate

        # Bat speed: native Statcast column on the swing-tracking feed.
        # "Yesterdays Hitters" highlight criterion: a sweet-spot speed BAND
        # (70.2-80.2 mph), not a one-sided threshold -- too slow means
        # under-powered contact, too fast often means a max-effort/mistimed
        # swing on these specific recent balls in play, both of which read
        # worse for repeatable HR shape than a controlled, in-band swing.
        avg_bat_speed = None
        bat_speed_in_band_rate = 0.0
        if len(bbe) and "bat_speed" in bbe.columns:
            bs_vals = bbe["bat_speed"].dropna()
            if len(bs_vals):
                avg_bat_speed = float(bs_vals.mean())
                bat_speed_in_band_rate = float(((bs_vals >= 70.2) & (bs_vals <= 80.2)).mean())
        out["avg_bat_speed"] = round(avg_bat_speed, 1) if avg_bat_speed is not None else None

        out["bbe_profile"] = {
            "sample_bbe": int(len(bbe)),
            "tracked_distance": int(out.get("recent_distance_tracked", 0)),
            "avg_ev": round(float(out.get("recent_ev", 0.0)), 1),
            "max_ev": round(max_ev, 1),
            "avg_la": round(avg_la, 1),
            "avg_distance": round(avg_distance, 1),
            "max_distance": round(max_distance, 1),
            "hard_hit_rate": round(float(out.get("recent_hard_hit_rate", 0.0)), 3),
            "hard_hit_pct": round(float(out.get("recent_hard_hit_rate", 0.0)) * 100, 1),
            "barrel_rate": round(float(out.get("recent_barrel_rate", 0.0)), 3),
            "barrel_pct": round(float(out.get("recent_barrel_rate", 0.0)) * 100, 1),
            "sweet_spot_rate": round(float(out.get("recent_sweet_spot_rate", 0.0)), 3),
            "ideal_hr_contact_rate": round(float(out.get("recent_ideal_hr_contact", 0.0)), 3),
            "fb_rate": round(float(out.get("recent_fb_rate", 0.0)), 3),
            "fb_pct": round(float(out.get("recent_fb_rate", 0.0)) * 100, 1),
            "ld_rate": round(ld_rate, 3),
            "gb_rate": round(gb_rate, 3),
            "popup_rate": round(pu_rate, 3),
            "pull_rate": round(float(out.get("recent_pull_rate", 0.0)), 3),
            "pull_pct": round(float(out.get("recent_pull_rate", 0.0)) * 100, 1),
            "air_pull_rate": round(float(air_pull_rate), 3),
            "air_pull_pct": round(float(air_pull_rate) * 100, 1),
            "pull_air_rate": round(float(out.get("recent_pull_air_rate", 0.0)), 3),
            "pull_air_pct": round(float(out.get("recent_pull_air_rate", 0.0)) * 100, 1),
            "squared_up_rate": out.get("recent_squared_up_rate"),
            "squared_up_sample": int(out.get("recent_squared_up_sample", 0)),
            "blast_rate": out.get("recent_blast_rate"),
            "dist_350_plus": int(out.get("recent_350_num", 0)),
            "dist_375_plus": int(out.get("recent_375_num", 0)),
            "dist_400_plus": int((bbe["hit_distance_sc"] >= 400).fillna(False).sum()) if len(bbe) else 0,
            "dist_300_plus": int((bbe["hit_distance_sc"] >= 300).fillna(False).sum()) if len(bbe) else 0,
            "avg_bat_speed": round(avg_bat_speed, 1) if avg_bat_speed is not None else None,
            "bat_speed_in_band_rate": round(bat_speed_in_band_rate, 3),
            "hr": hr_count,
            "xbh": xbh_count,
        }

        # ── Full-season BBE for the spray chart / EV log (2026-08-06). Was
        # tail(30) game dates, which silently capped every EV log and spray
        # view at a month of balls while the df already held the whole season.
        # The site added an "All" window stop that was really "all of 30
        # days" — this makes All mean the season. Detail files are fetched
        # per-modal on demand, so the payload growth lands on one player at a
        # time, not the slate.
        spray_dates = list(played[played <= pd.Timestamp(end_date)].drop_duplicates().sort_values())
        if spray_dates:
            bbe_spray = df[df["game_date"].dt.normalize().isin(spray_dates)].copy()
            bbe_spray = bbe_spray[bbe_spray["type"] == "X"].copy()
            bbe_spray = dedupe_statcast_bbe(bbe_spray)
        else:
            bbe_spray = bbe.copy()

        spray_points: List[Dict[str, Any]] = []
        if len(bbe_spray):
            sort_cols = [c for c in ["game_date", "at_bat_number", "pitch_number"] if c in bbe_spray.columns]
            bbe_sorted = bbe_spray.sort_values(sort_cols, ascending=False, na_position="last") if sort_cols else bbe_spray
            for _, bp in bbe_sorted.head(120).iterrows():
                ev2 = _clean_num(bp.get("launch_speed"), 1)
                la2 = _clean_num(bp.get("launch_angle"), 1)
                dist2 = _clean_num(bp.get("hit_distance_sc"), 1)
                hc_x = _clean_num(bp.get("hc_x"), 2)
                hc_y = _clean_num(bp.get("hc_y"), 2)
                pitch_code = str(bp.get("pitch_type", "") or "")
                event = str(bp.get("events", "") or "")
                traj = str(bp.get("bb_type", "") or "")
                stand = str(bp.get("stand", "") or "")
                lane = spray_lane_from_hcx(hc_x)
                spray_side = spray_side_for_hand(lane, stand)
                pull_air = False
                if hc_x is not None and traj in {"fly_ball", "line_drive", "popup"}:
                    pull_air = spray_side in {"pull", "pull_center"}
                # 🪁 SOLVE ONCE, HERE, NOT IN EVERY BROWSER (2026-08-29).
                # Guarded per-ball, same reasoning as the module import above:
                # one bad row (a None ev/la/dist, a NaN, whatever) must lose
                # only that row's three fields, never the whole nightly run.
                # solve_flight() itself already declines (returns None) for a
                # grounder/chopper with no honest launch angle -- this except
                # is for anything that gets past that guard unexpectedly.
                flight = None
                if TRAJECTORY is not None:
                    try:
                        # NOT dist2 (hit_distance_sc/carry) -- the client plots
                        # this dot at the hc_x/hc_y polar radius, and fitting
                        # to carry would land the solved arc somewhere that
                        # dot isn't. See trajectory.py's plotted_radius_ft().
                        plotted_r = TRAJECTORY.plotted_radius_ft(hc_x, hc_y)
                        flight = TRAJECTORY.solve_flight(ev2, la2, plotted_r)
                    except Exception:
                        flight = None
                spray_points.append({
                    "date": str(bp.get("game_date", ""))[:10],
                    "apex_ft": flight["apex_ft"] if flight else None,
                    "hang_time_s": flight["hang_time_s"] if flight else None,
                    "traj_poly": flight["traj_poly"] if flight else None,
                    "pitch_type": pitch_code,
                    "pitch_name": pitch_code,
                    "event": event,
                    "result": event,
                    "bb_type": traj,
                    "trajectory": traj,
                    "ev": ev2,
                    "launch_angle": la2,
                    "la": la2,
                    "distance": dist2,
                    "hc_x": hc_x,
                    "hc_y": hc_y,
                    "lane": lane,
                    "spray_side": spray_side,
                    "stand": stand,
                    "pitcher": resolve_mlb_person_name(db, bp.get("pitcher"), "—"),
                    "pitcher_id": safe_int(bp.get("pitcher"), 0),
                    "arm": str(bp.get("p_throws", "?") or "?"),
                    "pitch_velocity": _clean_num(bp.get("release_speed"), 1),
                    "is_hr": event == "home_run",
                    "hr_class": xhr_hr_class(db, ev2, la2) if event == "home_run" else "",
                    "is_xbh": event in {"double", "triple", "home_run"},
                    "is_barrel": bool(ev2 is not None and la2 is not None and ev2 >= 98 and 24 <= la2 <= 32),
                    "is_hard_hit": bool(ev2 is not None and ev2 >= 95),
                    "is_350_plus": bool(dist2 is not None and dist2 >= 350),
                    "is_375_plus": bool(dist2 is not None and dist2 >= 375),
                    "is_400_plus": bool(dist2 is not None and dist2 >= 400),
                    "is_pull_air": bool(pull_air),
                })
        lane_totals: Dict[str, int] = {}
        lane_damage: Dict[str, int] = {}
        for sp in spray_points:
            ln = str(sp.get("lane") or "")
            if not ln:
                continue
            lane_totals[ln] = lane_totals.get(ln, 0) + 1
            if sp.get("is_hr") or sp.get("is_xbh") or sp.get("is_350_plus") or sp.get("is_hard_hit") or sp.get("is_barrel"):
                lane_damage[ln] = lane_damage.get(ln, 0) + 1
        best_lane = max(lane_damage.items(), key=lambda kv: (kv[1], lane_totals.get(kv[0], 0)))[0] if lane_damage else (max(lane_totals.items(), key=lambda kv: kv[1])[0] if lane_totals else "")
        if isinstance(out.get("bbe_profile"), dict):
            out["bbe_profile"]["best_lane"] = best_lane
            out["bbe_profile"]["lane_counts"] = lane_totals
            out["bbe_profile"]["lane_damage_counts"] = lane_damage
        out["spray_chart"] = spray_points
        out["contact_log"] = spray_points
        out["batted_ball_log"] = spray_points

        # ── PERSONAL HR SHAPE (2026-08-14) ──────────────────────────────────
        # What KIND of homers does HE hit, and is his recent contact trending
        # toward that shape? Bands mirror the site's lib/hrShape.js HR_CUTS
        # exactly (366ft / 428ft / 25deg / 34deg, distance rules tested
        # first) -- keep the two in sync BY HAND if either changes. The bands
        # are percentile slices of the archive's 801-homer distribution, not
        # physics; see hrShape.js's header for why the handoff's proposed
        # physics-flavored cuts were dropped. Computed from the SAME season
        # dataframe already in hand -- zero new pulls.
        HR_SHORT_FT, HR_LONG_FT, HR_FLAT_DEG, HR_STEEP_DEG = 366.0, 428.0, 25.0, 34.0

        def _hr_band(la_v, dist_v):
            if dist_v is not None and dist_v < HR_SHORT_FT:
                return "wall_scraper"
            if dist_v is not None and dist_v >= HR_LONG_FT:
                return "no_doubter"
            if la_v is None:
                return "standard" if dist_v is not None else None
            if la_v < HR_FLAT_DEG:
                return "laser"
            if la_v >= HR_STEEP_DEG:
                return "moonshot"
            return "standard"

        season_bbe_all = bbe_spray if len(bbe_spray) else bbe
        hr_rows = (
            season_bbe_all[season_bbe_all["events"].fillna("") == "home_run"]
            if len(season_bbe_all) and "events" in season_bbe_all.columns
            else season_bbe_all.iloc[0:0]
        )
        shape_counts = {"wall_scraper": 0, "laser": 0, "standard": 0, "moonshot": 0, "no_doubter": 0}
        hr_las = []
        for _, hrow in hr_rows.iterrows():
            la_v = _clean_num(hrow.get("launch_angle"), 1)
            dist_v = _clean_num(hrow.get("hit_distance_sc"), 1)
            band = _hr_band(la_v, dist_v)
            if band:
                shape_counts[band] += 1
            if la_v is not None:
                hr_las.append(float(la_v))
        n_hr_shaped = sum(shape_counts.values())
        shape_profile: Dict[str, Any] = {"n": n_hr_shaped}
        shape_profile.update(shape_counts)
        if len(hr_las) >= 3:
            # His own homer launch-angle window: median +/- max(4deg, half
            # the IQR). With 4-6 homers the IQR can collapse to a degree or
            # two, and a 2deg window would call almost nothing "his shape" --
            # the 4deg floor keeps the window usable at real sample sizes.
            s_las = sorted(hr_las)
            m = len(s_las)
            med = s_las[m // 2] if m % 2 else (s_las[m // 2 - 1] + s_las[m // 2]) / 2.0
            q1 = s_las[int(0.25 * (m - 1))]
            q3 = s_las[int(0.75 * (m - 1))]
            half = max(4.0, (q3 - q1) / 2.0)
            la_lo, la_hi = med - half, med + half
            shape_profile["la_lo"] = round(la_lo, 1)
            shape_profile["la_hi"] = round(la_hi, 1)

            def _shape_rate(frame):
                # Of the hard-hit balls (95+, the EV floor a homer basically
                # requires), what share left the bat inside HIS homer window?
                if frame is None or len(frame) == 0:
                    return 0.0, 0
                hard = frame[(frame["launch_speed"] >= 95).fillna(False)]
                den = int(len(hard))
                if den == 0:
                    return 0.0, 0
                inw = hard[((hard["launch_angle"] >= la_lo) & (hard["launch_angle"] <= la_hi)).fillna(False)]
                return float(len(inw)) / den, den

            season_rate, _season_den = _shape_rate(season_bbe_all)
            recent_rate, recent_den = _shape_rate(bbe)
            out["personal_shape_season_rate"] = round(season_rate, 3)
            out["personal_shape_recent_rate"] = round(recent_rate, 3)
            out["personal_shape_match"] = round(recent_rate - season_rate, 3)
            out["personal_shape_status"] = (
                "ok" if (n_hr_shaped >= 4 and recent_den >= 5)
                else "thin_recent" if n_hr_shaped >= 4
                else "thin_hr"
            )
        else:
            out["personal_shape_status"] = "no_hr" if n_hr_shaped == 0 else "thin_hr"
        out["hr_shape_profile"] = shape_profile

        played_dates = list(played[played <= pd.Timestamp(end_date)].drop_duplicates().sort_values())
        l10_dates = played_dates[-10:]
        l5_dates = played_dates[-5:]
        l10 = df[df["game_date"].dt.normalize().isin(l10_dates)].copy() if l10_dates else recent.copy()
        l5 = df[df["game_date"].dt.normalize().isin(l5_dates)].copy() if l5_dates else recent.copy()

        def _window_quality(frame: pd.DataFrame) -> Dict[str, float]:
            wb = frame[frame["type"] == "X"].copy() if len(frame) else frame.copy()
            if len(wb) == 0:
                return {"barrel": 0.0, "hard": 0.0, "xwoba": 0.320, "pull": 0.38}
            barrel_rate = float(((wb["launch_speed"] >= 98) & (wb["launch_angle"] >= 24) & (wb["launch_angle"] <= 32)).fillna(False).mean())
            hard_rate = float((wb["launch_speed"] >= 95).fillna(False).mean())
            if "estimated_woba_using_speedangle" in frame.columns:
                xw = pd.to_numeric(frame["estimated_woba_using_speedangle"], errors="coerce").dropna()
                xwoba = float(xw.mean()) if len(xw) else 0.320
            else:
                xwoba = 0.320
            return {"barrel": barrel_rate, "hard": hard_rate, "xwoba": xwoba, "pull": _pull_rate(wb)}

        q5 = _window_quality(l5)
        q10 = _window_quality(l10)
        out["l5_barrel_rate"] = q5["barrel"]
        out["l10_barrel_rate"] = q10["barrel"]
        out["l5_hard_hit_rate"] = q5["hard"]
        out["l10_hard_hit_rate"] = q10["hard"]
        out["l5_xwoba"] = q5["xwoba"]
        out["l10_xwoba"] = q10["xwoba"]
        out["l5_pull_rate"] = q5["pull"]
        out["l10_pull_rate"] = q10["pull"]

        # Last 20 PA window: most recent completed plate appearances.
        # Used by HRW to catch current swing/form without relying on calendar-day noise.
        pa_events = df[df["events"].notna()].copy() if "events" in df.columns else df.iloc[0:0].copy()
        if len(pa_events):
            sort_cols = [c for c in ["game_date", "at_bat_number", "pitch_number"] if c in pa_events.columns]
            pa_events = pa_events.sort_values(sort_cols, ascending=True, na_position="last") if sort_cols else pa_events
            l20pa = pa_events.tail(20).copy()
            out["l20pa_pa"] = int(len(l20pa))
            l20_bbe = l20pa[l20pa["type"] == "X"].copy() if "type" in l20pa.columns else l20pa.iloc[0:0].copy()
            out["l20pa_bbe"] = int(len(l20_bbe))
            out["l20pa_350_den"] = max(1, int(len(l20_bbe)))
            out["l20pa_350_num"] = int((l20_bbe["hit_distance_sc"] >= 350).fillna(False).sum()) if len(l20_bbe) else 0
            out["l20pa_375_num"] = int((l20_bbe["hit_distance_sc"] >= 375).fillna(False).sum()) if len(l20_bbe) else 0
            out["l20pa_fb_rate"] = float((l20_bbe.get("bb_type") == "fly_ball").mean()) if len(l20_bbe) else 0.0
            out["l20pa_hard_hit_rate"] = float((l20_bbe["launch_speed"] >= 95).fillna(False).mean()) if len(l20_bbe) else 0.0
            out["l20pa_ideal_hr_contact"] = float(((l20_bbe["launch_speed"] >= 97) & (l20_bbe["launch_angle"] >= 18) & (l20_bbe["launch_angle"] <= 36)).fillna(False).mean()) if len(l20_bbe) else 0.0
            out["l20pa_barrel_rate"] = float(((l20_bbe["launch_speed"] >= 98) & (l20_bbe["launch_angle"] >= 24) & (l20_bbe["launch_angle"] <= 32)).fillna(False).mean()) if len(l20_bbe) else 0.0
            if "estimated_woba_using_speedangle" in l20pa.columns:
                xw20 = pd.to_numeric(l20pa["estimated_woba_using_speedangle"], errors="coerce").dropna()
                out["l20pa_xwoba"] = float(xw20.mean()) if len(xw20) else 0.320
            out["l20pa_pull_rate"] = _pull_rate(l20_bbe)
            evs20 = l20pa["events"].fillna("") if "events" in l20pa.columns else []
            out["l20pa_hr"] = int(sum(1 for e in evs20 if e == "home_run"))
            out["l20pa_xbh"] = int(sum(1 for e in evs20 if e in {"double", "triple", "home_run"}))

            # Last 25 PA window: same construction as L20PA above, sized to 25
            # per user preference (2026-06-29) -- their stated favorite window
            # for "Yesterdays Hitters" style recency scoring. Built standalone
            # rather than derived from l20pa/l10/l5 since 25 doesn't nest
            # cleanly inside any of those.
            l25pa = pa_events.tail(25).copy()
            out["l25pa_pa"] = int(len(l25pa))
            l25_bbe = l25pa[l25pa["type"] == "X"].copy() if "type" in l25pa.columns else l25pa.iloc[0:0].copy()
            out["l25pa_bbe"] = int(len(l25_bbe))
            out["l25pa_avg_ev"] = float(l25_bbe["launch_speed"].dropna().mean()) if len(l25_bbe) and l25_bbe["launch_speed"].notna().any() else 88.5
            out["l25pa_avg_la"] = float(l25_bbe["launch_angle"].dropna().mean()) if len(l25_bbe) and l25_bbe["launch_angle"].notna().any() else 0.0
            out["l25pa_hard_hit_rate"] = float((l25_bbe["launch_speed"] >= 95).fillna(False).mean()) if len(l25_bbe) else 0.0
            out["l25pa_barrel_rate"] = float(((l25_bbe["launch_speed"] >= 98) & (l25_bbe["launch_angle"] >= 24) & (l25_bbe["launch_angle"] <= 32)).fillna(False).mean()) if len(l25_bbe) else 0.0
            out["l25pa_sweet_spot_rate"] = float(((l25_bbe["launch_angle"] >= 8) & (l25_bbe["launch_angle"] <= 32)).fillna(False).mean()) if len(l25_bbe) else 0.0
            out["l25pa_ld_rate"] = float((l25_bbe.get("bb_type") == "line_drive").mean()) if len(l25_bbe) and "bb_type" in l25_bbe.columns else 0.0
            out["l25pa_gb_rate"] = float((l25_bbe.get("bb_type") == "ground_ball").mean()) if len(l25_bbe) and "bb_type" in l25_bbe.columns else 0.0
            out["l25pa_fb_rate"] = float((l25_bbe.get("bb_type") == "fly_ball").mean()) if len(l25_bbe) and "bb_type" in l25_bbe.columns else 0.0
            out["l25pa_popup_rate"] = float((l25_bbe.get("bb_type") == "popup").mean()) if len(l25_bbe) and "bb_type" in l25_bbe.columns else 0.0
            out["l25pa_air_rate"] = out["l25pa_fb_rate"] + out["l25pa_ld_rate"] + out["l25pa_popup_rate"]
            out["l25pa_300_plus"] = int((l25_bbe["hit_distance_sc"] >= 300).fillna(False).sum()) if len(l25_bbe) else 0
            out["l25pa_375_plus"] = int((l25_bbe["hit_distance_sc"] >= 375).fillna(False).sum()) if len(l25_bbe) else 0
            if "bat_speed" in l25_bbe.columns:
                _bs25 = l25_bbe["bat_speed"].dropna()
                out["l25pa_avg_bat_speed"] = float(_bs25.mean()) if len(_bs25) else None
            else:
                out["l25pa_avg_bat_speed"] = None
            evs25 = l25pa["events"].fillna("") if "events" in l25pa.columns else []
            _ab25 = sum(1 for e in evs25 if e not in {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "none", ""})
            _hits25 = sum(1 for e in evs25 if e in {"single", "double", "triple", "home_run"})
            out["l25pa_avg"] = round(_hits25 / max(1, _ab25), 3) if _ab25 else 0.0

        events = df["events"].fillna("")
        ab = sum(1 for e in events if e not in {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "none", ""})
        hits = hr = 0
        for e in events:
            if e in {"single", "double", "triple", "home_run"}:
                hits += 1
            if e == "home_run":
                hr += 1
        strikeouts = int((events == "strikeout").sum()) + int((events == "strikeout_double_play").sum())
        sf = int((events == "sac_fly").sum())
        in_play_hits = hits - hr
        in_play_ab = max(1, ab - strikeouts - hr + sf)
        out["babip"] = in_play_hits / in_play_ab if in_play_ab else 0.300
    except Exception:
        pass
    db.set(key, out)
    return out



def build_pitcher_statcast_profile(db: CacheDB, pitcher_id: int, end_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Recent pitcher damage allowed profile.

    Uses a 5-game trend blended with an 8-game baseline:
    - displayed Statcast sample = last 5 pitcher appearances
    - ATK inputs = 60% last 5 + 40% last 8
    Rates are decimals, not percentages. Missing pulls stay marked as missing.
    """
    end_date = end_date or statcast_data_end_date(TODAY)
    # v7: adds gb_allowed/ld_allowed/popup_allowed (2026-08-12) -- bumped so
    # every pitcher re-pulls once instead of serving the placeholder defaults
    # below for up to a day under the old v6 cache key.
    key = f"pitcher_statcast_damage_v7_battedball:{SEASON}:{pitcher_id}:{end_date.isoformat()}"
    defaults = {
        "statcast_bbe": 0,
        "statcast_games": 0,
        "statcast_base_bbe": 0,
        "statcast_base_games": 0,
        "statcast_status": "missing",
        "ev_allowed": 88.5,
        "hardhit_allowed": 0.38,
        "barrel_allowed": 0.07,
        "statcast_fb_rate": 0.34,
        "gb_allowed": 0.42,
        "ld_allowed": 0.21,
        "popup_allowed": 0.05,
        "dist375_allowed": 0,
        "dist400_allowed": 0,
        "babip_statcast": 0.300,
        "xhr_hist_allowed": {},
        "trend_note": "5G trend / 8G baseline",
        # Fastball velocity tracking (per audit, 2026-06-27): real, well-
        # documented evidence that velocity decline signals fatigue and
        # correlates with higher contact rate / HR rate allowed. Since the
        # bot runs pregame, this can't measure in-game decline tonight --
        # instead compares his most recent start's average fastball velocity
        # against his season-long average, the same "early warning" approach
        # bettors already use with this exact data.
        "fb_velo_season_avg": None,
        "fb_velo_last_start": None,
        "fb_velo_delta": 0.0,
        "fb_velo_status": "missing",
        "barrels_allowed_count_season": 0,
        "hr_fb_pct": 0.100,
        "trend_direction": "unknown",
        "trend_reason": "",
    }
    cached = db.get(key, max_age_days=1)
    if isinstance(cached, dict):
        out = dict(defaults)
        out.update(cached)
        return out
    if statcast_pitcher is None or not pitcher_id:
        out = dict(defaults)
        out["statcast_status"] = "pybaseball_missing"
        return out
    try:
        df = statcast_pitcher(SEASON_START.isoformat(), end_date.isoformat(), pitcher_id)
    except Exception as exc:
        out = dict(defaults)
        out["statcast_status"] = f"error:{type(exc).__name__}"
        return out
    if df is None or len(df) == 0:
        out = dict(defaults)
        out["statcast_status"] = "empty"
        return out

    out = dict(defaults)
    try:
        df = df.copy()
        df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
        df["launch_speed"] = pd.to_numeric(df.get("launch_speed"), errors="coerce")
        df["launch_angle"] = pd.to_numeric(df.get("launch_angle"), errors="coerce")
        df["hit_distance_sc"] = pd.to_numeric(df.get("hit_distance_sc"), errors="coerce")

        # Docket #20: season-long contact-allowed histogram for xHR-allowed.
        try:
            _p_season = df[df["type"] == "X"] if "type" in df.columns else df.iloc[0:0]
            out["xhr_hist_allowed"] = build_xhr_hist(_p_season)
        except Exception:
            out["xhr_hist_allowed"] = {}

        played = df["game_date"].dropna().dt.normalize()
        played_dates = list(played[played <= pd.Timestamp(end_date)].drop_duplicates().sort_values())
        last8_dates = played_dates[-8:]
        last5_dates = played_dates[-5:]
        if not last5_dates:
            recent_all = df[df["game_date"] >= pd.Timestamp(end_date - dt.timedelta(days=30))].copy()
            played2 = recent_all["game_date"].dropna().dt.normalize()
            played_dates = list(played2.drop_duplicates().sort_values())
            last8_dates = played_dates[-8:]
            last5_dates = played_dates[-5:]

        def _frame_for_dates(dates: List[pd.Timestamp]) -> pd.DataFrame:
            if not dates:
                return df.iloc[0:0].copy()
            normalized = df["game_date"].dt.normalize()
            return df[normalized.isin(dates)].copy()

        trend = _frame_for_dates(last5_dates)
        base = _frame_for_dates(last8_dates)

        # Fastball velocity: season avg vs most recent start. Uses ALL
        # fastball-type pitches thrown (not just ones resulting in contact),
        # since velocity is a property of the pitch itself, not the outcome.
        try:
            if "release_speed" in df.columns and "pitch_type" in df.columns:
                fb_types = {"FF", "SI", "FC"}  # 4-seam, sinker, cutter
                fb_df = df[df["pitch_type"].isin(fb_types)].copy()
                fb_df["release_speed"] = pd.to_numeric(fb_df["release_speed"], errors="coerce")
                fb_df = fb_df.dropna(subset=["release_speed"])
                if len(fb_df) >= 10 and played_dates:
                    season_avg_velo = float(fb_df["release_speed"].mean())
                    last_start_date = played_dates[-1]
                    last_start_normalized = fb_df["game_date"].dt.normalize()
                    last_start_fb = fb_df[last_start_normalized == last_start_date]
                    if len(last_start_fb) >= 5:
                        last_start_velo = float(last_start_fb["release_speed"].mean())
                        out["fb_velo_season_avg"] = round(season_avg_velo, 1)
                        out["fb_velo_last_start"] = round(last_start_velo, 1)
                        out["fb_velo_delta"] = round(last_start_velo - season_avg_velo, 2)
                        out["fb_velo_status"] = "ok"
        except Exception:
            pass

        def _metrics(frame: pd.DataFrame) -> Dict[str, Any]:
            metrics = {
                "bbe": 0,
                "games": 0,
                "ev": 88.5,
                "hard": 0.38,
                "barrel": 0.07,
                "fb": 0.34,
                "gb": 0.42,
                "ld": 0.21,
                "pu": 0.05,
                "dist375": 0,
                "dist400": 0,
                "babip": 0.300,
            }
            if frame is None or len(frame) == 0:
                return metrics
            gd = frame["game_date"].dropna().dt.normalize()
            metrics["games"] = int(gd.drop_duplicates().shape[0]) if len(gd) else 0
            bbe = frame[frame.get("type") == "X"].copy() if "type" in frame.columns else frame.iloc[0:0].copy()
            metrics["bbe"] = int(len(bbe))
            if len(bbe):
                metrics["ev"] = float(bbe["launch_speed"].dropna().mean()) if bbe["launch_speed"].notna().any() else 88.5
                metrics["hard"] = float((bbe["launch_speed"] >= 95).fillna(False).mean())
                metrics["barrel"] = float(((bbe["launch_speed"] >= 98) & (bbe["launch_angle"] >= 24) & (bbe["launch_angle"] <= 32)).fillna(False).mean())
                if "bb_type" in bbe.columns:
                    metrics["fb"] = float((bbe["bb_type"] == "fly_ball").mean())
                    # Same bb_type column the fb rate above already reads --
                    # PITCHER BATTED-BALL PROFILE (2026-08-12), completes the
                    # four-way split (gb/ld/fb/popup) the site only had fb for.
                    metrics["gb"] = float((bbe["bb_type"] == "ground_ball").mean())
                    metrics["ld"] = float((bbe["bb_type"] == "line_drive").mean())
                    metrics["pu"] = float((bbe["bb_type"] == "popup").mean())
                else:
                    metrics["fb"] = float((bbe["launch_angle"] >= 25).fillna(False).mean())
                metrics["dist375"] = int((bbe["hit_distance_sc"] >= 375).fillna(False).sum())
                metrics["dist400"] = int((bbe["hit_distance_sc"] >= 400).fillna(False).sum())

            events = frame["events"].fillna("") if "events" in frame.columns else []
            hits = sum(1 for e in events if e in {"single", "double", "triple", "home_run"})
            hrs = sum(1 for e in events if e == "home_run")
            strikeouts = sum(1 for e in events if e in {"strikeout", "strikeout_double_play"})
            sac_flies = sum(1 for e in events if e == "sac_fly")
            ab = sum(1 for e in events if e not in {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "none", ""})
            denom = max(1, ab - strikeouts - hrs + sac_flies)
            metrics["babip"] = float((hits - hrs) / denom) if denom else 0.300
            return metrics

        m5 = _metrics(trend)
        m8 = _metrics(base)
        if m5["bbe"] <= 0 and m8["bbe"] <= 0:
            out["statcast_status"] = "empty_bbe"
            return out

        def _blend(key_name: str, fallback: float) -> float:
            a = safe_float(m5.get(key_name), fallback)
            b = safe_float(m8.get(key_name), fallback)
            if m5["bbe"] <= 0:
                return b
            if m8["bbe"] <= 0:
                return a
            return 0.60 * a + 0.40 * b

        out["statcast_status"] = "ok"
        out["statcast_bbe"] = int(m5["bbe"] if m5["bbe"] > 0 else m8["bbe"])
        out["statcast_games"] = int(m5["games"] if m5["games"] > 0 else m8["games"])
        out["statcast_base_bbe"] = int(m8["bbe"])
        out["statcast_base_games"] = int(m8["games"])
        out["ev_allowed"] = float(_blend("ev", 88.5))
        out["hardhit_allowed"] = float(_blend("hard", 0.38))
        out["barrel_allowed"] = float(_blend("barrel", 0.07))
        out["statcast_fb_rate"] = float(_blend("fb", 0.34))
        out["gb_allowed"] = float(_blend("gb", 0.42))
        out["ld_allowed"] = float(_blend("ld", 0.21))
        out["popup_allowed"] = float(_blend("pu", 0.05))
        # For display and longest-HR environment, show the sharper 5G trend counts.
        out["dist375_allowed"] = int(m5["dist375"] if m5["bbe"] > 0 else m8["dist375"])
        out["dist400_allowed"] = int(m5["dist400"] if m5["bbe"] > 0 else m8["dist400"])
        out["babip_statcast"] = float(_blend("babip", 0.300))

        # ── Season-wide barrels count + HR/FB% (per audit, 2026-07-07 request) ──
        # Reuses the same full-season `df` already in hand -- no new pull.
        season_bbe = df[df.get("type") == "X"].copy() if "type" in df.columns else df.iloc[0:0].copy()
        if len(season_bbe):
            barrels_mask = (
                (season_bbe["launch_speed"] >= 98) & (season_bbe["launch_angle"] >= 24) & (season_bbe["launch_angle"] <= 32)
            ).fillna(False)
            out["barrels_allowed_count_season"] = int(barrels_mask.sum())
            fb_mask = (season_bbe.get("bb_type") == "fly_ball") if "bb_type" in season_bbe.columns else (season_bbe["launch_angle"] >= 25).fillna(False)
            fb_count = int(fb_mask.sum())
            season_events = df["events"].fillna("") if "events" in df.columns else []
            season_hr = int(sum(1 for e in season_events if e == "home_run"))
            out["hr_fb_pct"] = round(season_hr / fb_count, 4) if fb_count > 0 else 0.100
        else:
            out["barrels_allowed_count_season"] = 0
            out["hr_fb_pct"] = 0.100

        # ── Trend flag: comparing the 5-game trend to the 8-game baseline ──
        # "worsening" = a real target (getting hit harder lately than his own
        # normal); "improving" = fading away from being one. Only fires with
        # enough BBE on both sides to mean something (>=8 each), otherwise
        # stays "unknown" rather than reacting to noise.
        if m5["bbe"] >= 8 and m8["bbe"] >= 8:
            barrel_delta = m5["barrel"] - m8["barrel"]
            hard_delta = m5["hard"] - m8["hard"]
            ev_delta = m5["ev"] - m8["ev"]
            worsening_signals = sum([barrel_delta >= 0.04, hard_delta >= 0.06, ev_delta >= 1.5])
            improving_signals = sum([barrel_delta <= -0.04, hard_delta <= -0.06, ev_delta <= -1.5])
            if worsening_signals >= 2:
                out["trend_direction"] = "worsening"
                out["trend_reason"] = f"Barrel {m8['barrel']*100:.0f}%→{m5['barrel']*100:.0f}% | HH {m8['hard']*100:.0f}%→{m5['hard']*100:.0f}% | EV {m8['ev']:.1f}→{m5['ev']:.1f} (last 5 vs last 8)"
            elif improving_signals >= 2:
                out["trend_direction"] = "improving"
                out["trend_reason"] = f"Barrel {m8['barrel']*100:.0f}%→{m5['barrel']*100:.0f}% | HH {m8['hard']*100:.0f}%→{m5['hard']*100:.0f}% | EV {m8['ev']:.1f}→{m5['ev']:.1f} (last 5 vs last 8)"
            else:
                out["trend_direction"] = "stable"
                out["trend_reason"] = ""
        else:
            out["trend_direction"] = "unknown"
            out["trend_reason"] = ""
    except Exception as exc:
        out["statcast_status"] = f"parse_error:{type(exc).__name__}"
    if out.get("statcast_status") == "ok":
        db.set(key, out)
    return out


def build_pitcher_advanced_stats(db: CacheDB, pitcher_id: int, end_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Advanced pitch-level stats: meatball%, putaway%, swstr%, 1stPS%, whiff%, pullair allowed.

    Pulls from Statcast via pybaseball.statcast_pitcher. All rates are decimals.
    Cached for 1 day. League-average defaults returned if pybaseball missing or
    sample is too low (<200 pitches).
    """
    end_date = end_date or statcast_data_end_date(TODAY)
    key = f"pitcher_advanced_stats_v1:{SEASON}:{pitcher_id}:{end_date.isoformat()}"
    defaults = {
        "meatball_pct": 0.070,
        # THE HAND SPLIT (2026-08-23). Donovan: "meat ball percent needs to be
        # used in hr for sure hand splits and everything. wtf ." He is right to
        # be annoyed: meatball% has been on every row since it was added and the
        # only thing that ever read it was a 0.12 slice of the pitcher_damage
        # sub-score -- as ONE number, the same against a lefty and a righty.
        #
        # A middle-middle rate is not one number. An arm with a good slider away
        # to same-side bats and nothing but a straight fastball to the other side
        # gives up the heart of the plate to ONE of them, and averaging the two
        # hides exactly the matchup we are here to find.
        #
        # This costs no new network call. The dataframe below already carries
        # `stand` -- the pullair-allowed block a few lines down has been reading
        # it for months -- so both sides come out of the same pull.
        "meatball_pct_vs_lhb": 0.070,
        "meatball_pct_vs_rhb": 0.070,
        "meatball_pitches_vs_lhb": 0,
        "meatball_pitches_vs_rhb": 0,
        "meatball_side_status": "missing",
        "putaway_pct": 0.180,
        "swstr_pct": 0.110,
        "first_pitch_strike_pct": 0.600,
        "whiff_pct": 0.240,
        "pullair_allowed_pct": 0.220,
        "advanced_stats_sample": 0,
        "advanced_stats_status": "missing",
    }
    cached = db.get(key, max_age_days=1)
    if isinstance(cached, dict):
        out = dict(defaults)
        out.update(cached)
        return out
    if statcast_pitcher is None or not pitcher_id:
        out = dict(defaults)
        out["advanced_stats_status"] = "pybaseball_missing"
        return out

    try:
        df = statcast_pitcher(SEASON_START.isoformat(), end_date.isoformat(), pitcher_id)
    except Exception as exc:
        out = dict(defaults)
        out["advanced_stats_status"] = f"error:{type(exc).__name__}"
        return out

    if df is None or len(df) == 0:
        out = dict(defaults)
        out["advanced_stats_status"] = "empty"
        return out

    out = dict(defaults)
    try:
        df = df.copy()
        total_pitches = int(len(df))
        if total_pitches < 200:
            out["advanced_stats_status"] = f"low_sample:{total_pitches}"
            out["advanced_stats_sample"] = total_pitches
            return out

        # Description-based fields. statcast_pitcher returns per-pitch rows with
        # 'description' (e.g. 'swinging_strike', 'called_strike', 'foul', 'hit_into_play'),
        # 'type' ('S'/'B'/'X'), 'strikes', 'balls', and 'plate_x'/'plate_z' for zone.
        desc = df.get("description", pd.Series([""] * total_pitches)).astype(str).fillna("")
        ptype = df.get("type", pd.Series([""] * total_pitches)).astype(str).fillna("")
        strikes_col = pd.to_numeric(df.get("strikes"), errors="coerce").fillna(0)
        balls_col = pd.to_numeric(df.get("balls"), errors="coerce").fillna(0)

        # SwStr% — swinging strikes per total pitches.
        swstr_mask = desc.isin(["swinging_strike", "swinging_strike_blocked", "missed_bunt"])
        out["swstr_pct"] = float(swstr_mask.sum() / total_pitches)

        # Whiff% — swings-and-miss / total swings.
        swing_descs = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip",
                       "hit_into_play", "hit_into_play_no_out", "hit_into_play_score", "missed_bunt"}
        swings_mask = desc.isin(list(swing_descs))
        n_swings = int(swings_mask.sum())
        if n_swings > 0:
            whiff_descs = {"swinging_strike", "swinging_strike_blocked", "missed_bunt"}
            n_whiffs = int(desc.isin(list(whiff_descs)).sum())
            out["whiff_pct"] = float(n_whiffs / n_swings)

        # 1st-pitch strike% — strikes on pitches where balls==0 AND strikes==0.
        first_pitch_mask = (strikes_col == 0) & (balls_col == 0)
        n_first = int(first_pitch_mask.sum())
        if n_first > 0:
            # A strike on first pitch = called_strike, swinging_strike, foul (counts as strike), or hit_into_play.
            first_strike_descs = {"called_strike", "swinging_strike", "swinging_strike_blocked",
                                  "foul", "foul_tip", "hit_into_play", "hit_into_play_no_out", "hit_into_play_score"}
            first_strike_mask = first_pitch_mask & desc.isin(list(first_strike_descs))
            out["first_pitch_strike_pct"] = float(first_strike_mask.sum() / n_first)

        # Putaway% — strikeouts / 2-strike pitches.
        two_strike_mask = strikes_col == 2
        n_two_strike = int(two_strike_mask.sum())
        if n_two_strike > 0:
            events_col = df.get("events", pd.Series([""] * total_pitches)).astype(str).fillna("")
            ks = (events_col.isin(["strikeout", "strikeout_double_play"]) & two_strike_mask).sum()
            out["putaway_pct"] = float(ks / n_two_strike)

        # Meatball% — pitches in the middle-middle zone.
        # Savant defines meatball as zone 5 OR ((|plate_x| < 0.55) AND (2.2 < plate_z < 3.3)).
        zone_col = pd.to_numeric(df.get("zone"), errors="coerce")
        plate_x = pd.to_numeric(df.get("plate_x"), errors="coerce")
        plate_z = pd.to_numeric(df.get("plate_z"), errors="coerce")
        meatball_zone_mask = (zone_col == 5)
        meatball_coord_mask = (plate_x.abs() < 0.55) & (plate_z > 2.2) & (plate_z < 3.3)
        meatball_mask = (meatball_zone_mask | meatball_coord_mask).fillna(False)
        out["meatball_pct"] = float(meatball_mask.sum() / total_pitches)

        # ── Meatball BY THE HAND HE IS FACING ──────────────────────────────
        # Same mask, sliced by `stand`. A side needs its own floor: at 200
        # total pitches a pitcher can have 190 against righties and 10 against
        # lefties, and 1-for-10 would publish as a 10% meatball rate against
        # LHB -- a number with the shape of a finding and the content of noise.
        # 150 is the floor; under it the side inherits his overall rate, which
        # is the honest neutral ("no evidence he is different to this side"),
        # and the status field says which sides actually cleared it.
        MIN_SIDE_PITCHES = 150
        stand_all = df.get("stand", pd.Series([""] * total_pitches)).astype(str).fillna("")
        _sides_ok = []
        for _hand, _key in (("L", "lhb"), ("R", "rhb")):
            side_mask = (stand_all == _hand)
            n_side = int(side_mask.sum())
            out["meatball_pitches_vs_%s" % _key] = n_side
            if n_side >= MIN_SIDE_PITCHES:
                out["meatball_pct_vs_%s" % _key] = float((meatball_mask & side_mask).sum() / n_side)
                _sides_ok.append(_hand)
            else:
                out["meatball_pct_vs_%s" % _key] = out["meatball_pct"]
        if len(_sides_ok) == 2:
            out["meatball_side_status"] = "ok"
        elif len(_sides_ok) == 1:
            out["meatball_side_status"] = "one_side:%s" % _sides_ok[0]
        else:
            out["meatball_side_status"] = "low_sample"

        # Pull-air allowed — balls in play that are pulled AND in the air (FB/LD).
        # bb_type: ground_ball / line_drive / fly_ball / popup.
        # hc_x < 125 typically = pull for RHB; > 125 = pull for LHB. Use stand to flip.
        bbe_mask = (ptype == "X")
        n_bbe = int(bbe_mask.sum())
        if n_bbe > 0:
            bbe_df = df[bbe_mask].copy()
            bb_type = bbe_df.get("bb_type", pd.Series([""] * len(bbe_df))).astype(str).fillna("")
            in_air = bb_type.isin(["fly_ball", "line_drive"])
            stand = bbe_df.get("stand", pd.Series([""] * len(bbe_df))).astype(str).fillna("")
            hc_x = pd.to_numeric(bbe_df.get("hc_x"), errors="coerce")
            pull_mask_R = (stand == "R") & (hc_x < 125.0)
            pull_mask_L = (stand == "L") & (hc_x > 125.0)
            pull_mask = (pull_mask_R | pull_mask_L).fillna(False)
            pullair_mask = (in_air & pull_mask)
            out["pullair_allowed_pct"] = float(pullair_mask.sum() / n_bbe)

        out["advanced_stats_sample"] = total_pitches
        out["advanced_stats_status"] = "ok"
    except Exception as exc:
        out["advanced_stats_status"] = f"parse_error:{type(exc).__name__}"

    if out.get("advanced_stats_status") == "ok":
        db.set(key, out)
    return out


def build_pitcher_pitch_mix(db: CacheDB, pitcher_id: int, end_date: Optional[dt.date] = None, batter_side: str = "") -> Dict[str, Any]:
    """Pitcher arsenal usage from Statcast pitch_type.

    Cloud-safe output:
    - writes true usage percentages
    - writes status/sample/debug fields
    - avoids metadata keys being treated like pitches

    NEW: when batter_side="L" or "R", filters Statcast rows to only the matching
    handedness. Pitchers often use very different mixes vs LHB vs RHB (sliders to
    same-side, changeups to opposite-side, etc.) so this scopes mix + per-pitch
    damage to what the actual hitter will see today.
    """
    end_date = end_date or statcast_data_end_date(TODAY)
    side_key = batter_side.upper() if batter_side and batter_side.upper() in ("L", "R") else "ALL"
    key = f"pitcher_pitch_mix_v4_handed:{SEASON}:{pitcher_id}:{end_date.isoformat()}:{side_key}"

    defaults = {
        "mix": {},
        "usage": {},
        "pitcher_pitch_usage": {},
        "pitcher_pitch_usage_pct": {},
        "pitcher_arsenal": {},
        "primary_mix": "Mix N/A",
        "pitcher_arsenal_summary": "Mix N/A",
        "sample_pitches": 0,
        "pitcher_pitch_mix_sample": 0,
        "pitch_type_summary": [],
        "pitcher_pitch_type_summary": [],
        "per_pitch_damage": {},
        "status": "missing",
        "pitcher_pitch_mix_status": "missing",
        "pybaseball_available": bool(statcast_pitcher is not None),
        "source_window": "",
        "debug_message": "",
    }

    cached = db.get(key, max_age_days=1)
    if isinstance(cached, dict):
        out = dict(defaults)
        out.update(cached)
        return out

    if statcast_pitcher is None:
        out = dict(defaults)
        out["status"] = "pybaseball_missing"
        out["pitcher_pitch_mix_status"] = "pybaseball_missing"
        out["debug_message"] = "pybaseball.statcast_pitcher is not available in this environment"
        db.set(key, out)
        return out

    if not pitcher_id:
        out = dict(defaults)
        out["status"] = "missing_pitcher_id"
        out["pitcher_pitch_mix_status"] = "missing_pitcher_id"
        out["debug_message"] = "No probable pitcher id was available"
        db.set(key, out)
        return out

    start_date = max(SEASON_START, end_date - dt.timedelta(days=120))
    try:
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        df = statcast_pitcher(start_str, end_str, pitcher_id)
    except Exception as exc:
        out = dict(defaults)
        out["status"] = f"error:{type(exc).__name__}"
        out["pitcher_pitch_mix_status"] = out["status"]
        out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
        out["debug_message"] = str(exc)[:250]
        db.set(key, out)
        return out

    if df is None or len(df) == 0:
        out = dict(defaults)
        out["status"] = "empty"
        out["pitcher_pitch_mix_status"] = "empty"
        out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
        out["debug_message"] = "Statcast returned zero rows for pitcher"
        db.set(key, out)
        return out

    # Apply hitter-handedness filter when provided. Many pitchers throw materially
    # different mixes vs LHB vs RHB; this scopes the data to the matchup at hand.
    if side_key in ("L", "R") and "stand" in df.columns:
        df = df[df["stand"] == side_key].copy()
        if len(df) == 0:
            out = dict(defaults)
            out["status"] = f"empty_vs_{side_key}HB"
            out["pitcher_pitch_mix_status"] = out["status"]
            out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
            out["debug_message"] = f"Zero pitches to {side_key}HB in window"
            db.set(key, out)
            return out

    if "pitch_type" not in df.columns:
        out = dict(defaults)
        out["status"] = "missing_pitch_type_column"
        out["pitcher_pitch_mix_status"] = "missing_pitch_type_column"
        out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
        out["debug_message"] = f"Columns returned: {list(df.columns)[:20]}"
        db.set(key, out)
        return out

    try:
        df = df.copy()
        valid = df["pitch_type"].dropna().astype(str)
        valid = valid[~valid.str.lower().isin(["", "nan", "none", "null"])]

        pitch_counts = valid.value_counts()
        total = int(pitch_counts.sum())

        # Pre-compute per-pitch DAMAGE ALLOWED — HR, EV, barrel% per pitch type.
        # This is the missing piece for the pitch-type-match flag: we know what the
        # pitcher throws, now we also know which of those pitches he gets HIT on.
        per_pitch_damage: Dict[str, Dict[str, Any]] = {}
        try:
            df_dmg = df.copy()
            df_dmg["pitch_type"] = df_dmg["pitch_type"].astype(str).str.upper().str.strip()
            df_dmg["events"] = df_dmg.get("events", "").fillna("")
            df_dmg["launch_speed"] = pd.to_numeric(df_dmg.get("launch_speed"), errors="coerce")
            df_dmg["launch_angle"] = pd.to_numeric(df_dmg.get("launch_angle"), errors="coerce")
            if "type" in df_dmg.columns:
                bbe_all = df_dmg[df_dmg["type"] == "X"].copy()
                for ptype, grp in bbe_all.groupby("pitch_type"):
                    n_bbe = int(len(grp))
                    if n_bbe < 5:
                        continue  # noise filter — need at least 5 BBE on the pitch
                    hrs = int((grp["events"] == "home_run").sum())
                    avg_ev = float(grp["launch_speed"].dropna().mean()) if grp["launch_speed"].notna().any() else 0.0
                    barrel_like = float(((grp["launch_speed"] >= 98) & (grp["launch_angle"] >= 18) & (grp["launch_angle"] <= 35)).fillna(False).mean())
                    hard_hit = float((grp["launch_speed"] >= 95).fillna(False).mean())
                    xwoba_vals = pd.to_numeric(grp.get("estimated_woba_using_speedangle"), errors="coerce").dropna() if "estimated_woba_using_speedangle" in grp.columns else []
                    xwoba = round(float(xwoba_vals.mean()), 3) if len(xwoba_vals) > 0 else None
                    per_pitch_damage[ptype] = {
                        "bbe": n_bbe,
                        "hr": hrs,
                        "hr_per_bbe": round(hrs / max(1, n_bbe), 4),
                        "avg_ev": round(avg_ev, 1),
                        # Renamed barrel_rate -> barrel_like_rate: this uses
                        # the wider 18-35 degree LA window (not the strict
                        # 24-32 used everywhere else in the file) to avoid
                        # starving small per-pitch-type samples of any signal.
                        # Previously named "barrel_rate" identically to the
                        # strict definition, causing p_barrel * 50 in
                        # pitch_type_match_score to silently read ~3.4x the
                        # expected value. Now matches the file's own convention
                        # (_aggregate_batter_pitch_groups already used this name).
                        "barrel_like_rate": round(barrel_like, 3),
                        "hard_hit_rate": round(hard_hit, 3),
                        "xwoba": xwoba,
                    }
        except Exception:
            per_pitch_damage = {}

        if total <= 0:
            out = dict(defaults)
            out["status"] = "empty_pitch_type"
            out["pitcher_pitch_mix_status"] = "empty_pitch_type"
            out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
            out["debug_message"] = "Rows existed but no usable pitch_type values were found"
            db.set(key, out)
            return out

        # Only allow real pitch codes. This prevents sample_pitches / metadata rows from becoming fake pitches.
        allowed_pitch_codes = {
            "FF", "SI", "FC", "SL", "ST", "CU", "KC", "CH", "FS",
            "FO", "KN", "EP", "CS", "SC", "SV", "FA"
        }

        valid_pitch_counts = {str(p).upper().strip(): int(c) for p, c in pitch_counts.items() if str(p).upper().strip() in allowed_pitch_codes}
        valid_total = int(sum(valid_pitch_counts.values()))

        mix = {}
        for code, count in valid_pitch_counts.items():
            pct = round(100.0 * int(count) / max(1, valid_total), 1)
            if 0 < pct <= 100:
                mix[code] = pct

        if not mix:
            out = dict(defaults)
            out["status"] = "no_valid_pitch_codes"
            out["pitcher_pitch_mix_status"] = "no_valid_pitch_codes"
            out["sample_pitches"] = total
            out["pitcher_pitch_mix_sample"] = total
            out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
            out["debug_message"] = f"Pitch types found but none matched allowed codes: {list(pitch_counts.index)[:12]}"
            db.set(key, out)
            return out

        sorted_mix = dict(sorted(mix.items(), key=lambda x: x[1], reverse=True))
        top_pitches = list(sorted_mix.items())[:4]
        primary_mix = " | ".join([f"{p} {v:.0f}%" for p, v in top_pitches])

        pitch_type_summary = []
        for pitch, usage in sorted_mix.items():
            count = int(pitch_counts.get(pitch, 0))
            dmg = per_pitch_damage.get(pitch, {})
            pitch_type_summary.append({
                "pitch_type": pitch,
                "pitch_code": pitch,
                "usage_pct": round(float(usage), 1),
                "usage": round(float(usage), 1),
                "count": count,
                # NEW: per-pitch damage allowed
                "bbe_allowed": dmg.get("bbe", 0),
                "hr_allowed": dmg.get("hr", 0),
                "hr_per_bbe": dmg.get("hr_per_bbe", 0.0),
                "avg_ev_allowed": dmg.get("avg_ev", 0.0),
                "barrel_rate_allowed": dmg.get("barrel_rate", 0.0),
                "hard_hit_rate_allowed": dmg.get("hard_hit_rate", 0.0),
                "xwoba_allowed": dmg.get("xwoba"),
            })

        out = {
            "mix": sorted_mix,
            "usage": sorted_mix,
            "pitcher_pitch_usage": sorted_mix,
            "pitcher_pitch_usage_pct": sorted_mix,
            "pitcher_arsenal": sorted_mix,
            "primary_mix": primary_mix,
            "pitcher_arsenal_summary": primary_mix,
            "sample_pitches": valid_total,
            "pitcher_pitch_mix_sample": valid_total,
            "pitch_type_summary": pitch_type_summary,
            "pitcher_pitch_type_summary": pitch_type_summary,
            "per_pitch_damage": per_pitch_damage,  # NEW — keyed by pitch code
            "batter_side": side_key,  # NEW — which side this mix is scoped to ('L', 'R', or 'ALL')
            "status": "ok",
            "pitcher_pitch_mix_status": "ok",
            "pybaseball_available": True,
            "source_window": f"{start_date.isoformat()} to {end_date.isoformat()}",
            "debug_message": f"Loaded {valid_total} valid pitch-code pitches from {total} Statcast rows; {len(sorted_mix)} pitch types; {len(per_pitch_damage)} with damage data",
        }

        db.set(key, out)
        return out

    except Exception as exc:
        out = dict(defaults)
        out["status"] = f"parse_error:{type(exc).__name__}"
        out["pitcher_pitch_mix_status"] = out["status"]
        out["source_window"] = f"{start_date.isoformat()} to {end_date.isoformat()}"
        out["debug_message"] = str(exc)[:250]
        db.set(key, out)
        return out


def _aggregate_batter_pitch_groups(df: "pd.DataFrame") -> Dict[str, Any]:
    """Core per-pitch-type aggregation, extracted so it can run once on the
    full batter df and again on LHP/RHP-filtered slices (see
    build_batter_pitch_type_profile below). Behavior is unchanged from the
    original inline version -- only the LHP/RHP split is new.
    """
    by_pitch: Dict[str, Dict[str, Any]] = {}
    for pitch_type, group in df.groupby("pitch_type"):
        if pitch_type is None or str(pitch_type).lower() == "nan":
            continue
        bbe = group[group.get("type") == "X"].copy() if "type" in group.columns else group.iloc[0:0].copy()
        events = group["events"].fillna("") if "events" in group.columns else []
        hr = int(sum(1 for e in events if e == "home_run"))
        xbh = int(sum(1 for e in events if e in {"double", "triple", "home_run"}))
        # missed_bunt is a genuine swing-and-miss (bunt attempt where the batter
        # whiffed) -- included here for consistency with build_pitcher_advanced_stats
        # which already handles it correctly. Previously absent from this function
        # and chase_rate below, causing slight undercount of swings/whiffs on any
        # pitcher-batter sample with bunt attempts.
        whiffs = int(sum(1 for e in group.get("description", []) if e in {"swinging_strike", "swinging_strike_blocked", "missed_bunt"})) if "description" in group.columns else 0
        swings = int(group["description"].isin(["swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "hit_into_play", "missed_bunt"]).sum()) if "description" in group.columns else 0
        if len(bbe):
            avg_ev = float(bbe["launch_speed"].dropna().mean()) if bbe["launch_speed"].notna().any() else 0.0
            avg_la = float(bbe["launch_angle"].dropna().mean()) if bbe["launch_angle"].notna().any() else 0.0
            max_dist = int(bbe["hit_distance_sc"].max()) if bbe["hit_distance_sc"].notna().any() else 0
            hard_hit = float((bbe["launch_speed"] >= 95).fillna(False).mean())
            barrel_like = float(((bbe["launch_speed"] >= 98) & (bbe["launch_angle"] >= 18) & (bbe["launch_angle"] <= 35)).fillna(False).mean())
            ev_97_mask = (bbe["launch_speed"] >= 97).fillna(False)
            barrel_mask = ((bbe["launch_speed"] >= 98) & (bbe["launch_angle"] >= 18) & (bbe["launch_angle"] <= 35)).fillna(False)
            dist_350_mask = (bbe["hit_distance_sc"] >= 350).fillna(False)
            dist_375_mask = (bbe["hit_distance_sc"] >= 375).fillna(False)
            good_contact_mask = (ev_97_mask | barrel_mask | dist_350_mask)
            good_contact_rate = float(good_contact_mask.mean())
            hh_97_count = int(ev_97_mask.sum())
            dist_350_count = int(dist_350_mask.sum())
            dist_375_count = int(dist_375_mask.sum())
            gb_rate = float((bbe.get("bb_type") == "ground_ball").mean()) if "bb_type" in bbe.columns else 0.0
            fb_rate = float((bbe.get("bb_type") == "fly_ball").mean()) if "bb_type" in bbe.columns else 0.0
            ld_rate = float((bbe.get("bb_type") == "line_drive").mean()) if "bb_type" in bbe.columns else 0.0
            popup_rate = float((bbe.get("bb_type") == "popup").mean()) if "bb_type" in bbe.columns else 0.0
            if "hc_x" in bbe.columns and "stand" in bbe.columns and "bb_type" in bbe.columns:
                air = bbe[bbe["bb_type"].isin(["fly_ball", "line_drive", "popup"])].copy()
                if len(air):
                    air["hc_x"] = pd.to_numeric(air.get("hc_x"), errors="coerce")
                    air = air[air["hc_x"].notna()]
                    pulled_air = ((air["stand"] == "R") & (air["hc_x"] < 125.0)) | ((air["stand"] == "L") & (air["hc_x"] > 125.0))
                    air_pull_rate = float(pulled_air.mean()) if len(air) else 0.0
                else:
                    air_pull_rate = 0.0
            else:
                air_pull_rate = 0.0

            _NON_AB = {"walk","hit_by_pitch","catcher_interf","sac_fly","sac_bunt","none",""}
            _HIT_EV = {"single","double","triple","home_run"}
            _K_EV   = {"strikeout","strikeout_double_play"}
            _ev_list = list(events) if hasattr(events, '__iter__') else []
            _hits_n  = sum(1 for e in _ev_list if str(e).strip() in _HIT_EV)
            _ab_n    = sum(1 for e in _ev_list if str(e).strip() not in _NON_AB)
            _k_n     = sum(1 for e in _ev_list if str(e).strip() in _K_EV)
            _bb_n    = sum(1 for e in _ev_list if str(e).strip() == "walk")
            _pa_n    = max(1, int(len(group)))
            ba       = round(_hits_n / max(1, _ab_n), 3)
            k_rate   = round(_k_n / _pa_n, 3)
            bb_rate  = round(_bb_n / _pa_n, 3)
            swstr_rate = round(whiffs / max(1, int(len(group))), 3)
            xwoba_vals = pd.to_numeric(bbe.get("estimated_woba_using_speedangle"), errors="coerce").dropna() if "estimated_woba_using_speedangle" in bbe.columns else []
            xwoba = round(float(xwoba_vals.mean()), 3) if len(xwoba_vals) > 0 else None
            bat_spd_vals = pd.to_numeric(bbe.get("bat_speed"), errors="coerce").dropna() if "bat_speed" in bbe.columns else []
            avg_bat_speed = round(float(bat_spd_vals.mean()), 1) if len(bat_spd_vals) > 0 else None
        else:
            avg_ev = avg_la = hard_hit = barrel_like = 0.0
            max_dist = 0
            gb_rate = fb_rate = ld_rate = popup_rate = air_pull_rate = 0.0
            good_contact_rate = 0.0
            hh_97_count = dist_350_count = dist_375_count = 0
            ba = k_rate = bb_rate = swstr_rate = 0.0
            xwoba = avg_bat_speed = None
        by_pitch[str(pitch_type)] = {
            "seen": int(len(group)),
            "bbe": int(len(bbe)),
            "avg_ev": round(avg_ev, 1),
            "avg_la": round(avg_la, 1),
            "max_dist": max_dist,
            "hr": hr,
            "hr_per_bbe": round(hr / max(1, int(len(bbe))), 4),
            "xbh": xbh,
            "hard_hit_rate": round(hard_hit, 3),
            "barrel_like_rate": round(barrel_like, 3),
            "good_contact_rate": round(good_contact_rate, 3),
            "hh_97_count": hh_97_count,
            "dist_350_count": dist_350_count,
            "dist_375_count": dist_375_count,
            "gb_rate": round(gb_rate, 3),
            "fb_rate": round(fb_rate, 3),
            "ld_rate": round(ld_rate, 3),
            "popup_rate": round(popup_rate, 3),
            "air_pull_rate": round(air_pull_rate, 3),
            "whiff_rate": round(whiffs / max(1, swings), 3) if swings else 0.0,
            "ba": ba,
            "k_rate": k_rate,
            "bb_rate": bb_rate,
            "swstr_rate": swstr_rate,
            "xwoba": xwoba,
            "avg_bat_speed": avg_bat_speed,
            "zone_pct": round(
                float(pd.to_numeric(group.get("zone"), errors="coerce").between(1, 9).sum() / max(1, len(group))), 3
            ) if "zone" in group.columns else None,
            "chase_rate": round(
                float(
                    group[
                        pd.to_numeric(group.get("zone"), errors="coerce").between(11, 14).fillna(False) &
                        group.get("description", pd.Series(dtype=str)).isin({"swinging_strike","swinging_strike_blocked","foul","foul_tip","hit_into_play","hit_into_play_no_out","hit_into_play_score","missed_bunt"})
                    ].shape[0] / max(1, pd.to_numeric(group.get("zone"), errors="coerce").between(11, 14).sum())
                ), 3
            ) if "zone" in group.columns and "description" in group.columns else None,
            "avg_pitch_velo": round(
                float(pd.to_numeric(group.get("release_speed"), errors="coerce").dropna().mean()), 1
            ) if "release_speed" in group.columns and pd.to_numeric(group.get("release_speed"), errors="coerce").notna().any() else None,
        }

    pitch_type_summary = []
    for code, vals in by_pitch.items():
        seen = safe_int(vals.get("seen"), 0)
        bbe_n = safe_int(vals.get("bbe"), 0)
        pitch_type_summary.append({
            "pitch_type": code,
            "pitch_code": code,
            "usage_pct": 0.0,
            "count": seen,
            "seen": seen,
            "bbe": bbe_n,
            "avg_ev": vals.get("avg_ev", 0.0),
            "avg_la": vals.get("avg_la", 0.0),
            "max_dist": vals.get("max_dist", 0),
            "hr": vals.get("hr", 0),
            "hr_per_bbe": vals.get("hr_per_bbe", 0.0),
            "xbh": vals.get("xbh", 0),
            "hard_hit_pct": round(safe_float(vals.get("hard_hit_rate"), 0.0) * 100, 1),
            "barrel_pct": round(safe_float(vals.get("barrel_like_rate"), 0.0) * 100, 1),
            "gb_pct": round(safe_float(vals.get("gb_rate"), 0.0) * 100, 1),
            "fb_pct": round(safe_float(vals.get("fb_rate"), 0.0) * 100, 1),
            "ld_pct": round(safe_float(vals.get("ld_rate"), 0.0) * 100, 1),
            "popup_pct": round(safe_float(vals.get("popup_rate"), 0.0) * 100, 1),
            "air_pull_pct": round(safe_float(vals.get("air_pull_rate"), 0.0) * 100, 1),
            "whiff_pct": round(safe_float(vals.get("whiff_rate"), 0.0) * 100, 1),
            "ba": vals.get("ba", 0.0),
            "k_pct": round(safe_float(vals.get("k_rate"), 0.0) * 100, 1),
            "bb_pct": round(safe_float(vals.get("bb_rate"), 0.0) * 100, 1),
            "swstr_pct": round(safe_float(vals.get("swstr_rate"), 0.0) * 100, 1),
            "xwoba": vals.get("xwoba"),
            "avg_bat_speed": vals.get("avg_bat_speed"),
            "zone_pct": vals.get("zone_pct"),
            "chase_rate": vals.get("chase_rate"),
            "avg_pitch_velo": vals.get("avg_pitch_velo"),
        })
    pitch_type_summary.sort(key=lambda x: (x.get("hr", 0), x.get("xbh", 0), x.get("avg_ev", 0), x.get("seen", 0)), reverse=True)
    return {"by_pitch": by_pitch, "pitch_type_summary": pitch_type_summary, "status": "ok" if by_pitch else "empty_by_pitch"}


def build_batter_pitch_type_profile(db: CacheDB, player_id: int, end_date: dt.date) -> Dict[str, Any]:
    """Batter damage profile by Statcast pitch_type.

    This lets the model compare the batter's strengths against the pitcher's actual arsenal.
    Small pitch-type samples are kept, but the fit score ignores samples under the threshold.

    NEW: also splits by pitcher handedness (vs LHP / vs RHP), mirroring the
    pitcher-side vs_lhb/vs_rhb split already in place, so PitchBreakdown.js
    can show the batter's pitch-type performance specifically against the
    hand of pitcher they're facing today, not just an all-time blend.
    """
    key = f"batter_pitch_type_v2_pitchfix:{SEASON}:{player_id}:{end_date.isoformat()}"
    defaults = {"by_pitch": {}, "status": "missing", "vs_lhp": {"by_pitch": {}, "status": "missing"}, "vs_rhp": {"by_pitch": {}, "status": "missing"}, "recent10_games": {"by_pitch": {}, "status": "missing"}}
    cached = db.get(key, max_age_days=1)
    if isinstance(cached, dict):
        out = dict(defaults)
        out.update(cached)
        return out
    if statcast_batter is None:
        out = dict(defaults)
        out["status"] = "pybaseball_missing"
        return out
    try:
        start_date = max(SEASON_START, end_date - dt.timedelta(days=120))
        df = statcast_batter(start_date.isoformat(), end_date.isoformat(), player_id)
    except Exception as exc:
        out = dict(defaults)
        out["status"] = f"error:{type(exc).__name__}"
        return out
    if df is None or len(df) == 0 or "pitch_type" not in df.columns:
        out = dict(defaults)
        out["status"] = "empty"
        return out
    try:
        df = df.copy()
        df["launch_speed"] = pd.to_numeric(df.get("launch_speed"), errors="coerce")
        df["launch_angle"] = pd.to_numeric(df.get("launch_angle"), errors="coerce")
        df["hit_distance_sc"] = pd.to_numeric(df.get("hit_distance_sc"), errors="coerce")
        df["events"] = df.get("events", "").fillna("")

        out = _aggregate_batter_pitch_groups(df)

        # vs LHP / vs RHP splits, same aggregation, pre-filtered by p_throws.
        if "p_throws" in df.columns:
            df_vs_l = df[df["p_throws"] == "L"]
            df_vs_r = df[df["p_throws"] == "R"]
            out["vs_lhp"] = _aggregate_batter_pitch_groups(df_vs_l) if len(df_vs_l) else {"by_pitch": {}, "pitch_type_summary": [], "status": "empty"}
            out["vs_rhp"] = _aggregate_batter_pitch_groups(df_vs_r) if len(df_vs_r) else {"by_pitch": {}, "pitch_type_summary": [], "status": "empty"}
        else:
            out["vs_lhp"] = {"by_pitch": {}, "pitch_type_summary": [], "status": "no_p_throws_column"}
            out["vs_rhp"] = {"by_pitch": {}, "pitch_type_summary": [], "status": "no_p_throws_column"}

        # Recent-games (last 10 games played) per-pitch-type slice. Reuses the
        # same aggregation as the full-window/handed splits above, just on a
        # date-filtered slice -- added per audit (2026-06-27) so trap detection
        # can check whether a pitch-specific groundball/contact problem is a
        # recent thing or a longer-standing pattern that's faded.
        if "game_date" in df.columns:
            _gd = pd.to_datetime(df["game_date"], errors="coerce")
            _recent_dates = sorted(_gd.dropna().unique())[-10:]
            if _recent_dates:
                df_recent10 = df[_gd.isin(_recent_dates)]
                out["recent10_games"] = _aggregate_batter_pitch_groups(df_recent10) if len(df_recent10) else {"by_pitch": {}, "pitch_type_summary": [], "status": "empty"}
            else:
                out["recent10_games"] = {"by_pitch": {}, "pitch_type_summary": [], "status": "empty"}
        else:
            out["recent10_games"] = {"by_pitch": {}, "pitch_type_summary": [], "status": "no_game_date_column"}
    except Exception as exc:
        out = dict(defaults)
        out["status"] = f"parse_error:{type(exc).__name__}"
    if out.get("status") == "ok":
        db.set(key, out)
    return out


def calculate_pitch_mix_fit(pitcher_mix: Dict[str, float], batter_by_pitch: Dict[str, Dict[str, Any]], pitcher_per_pitch_damage: Optional[Dict[str, Dict[str, Any]]] = None, batter_by_pitch_recent10: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """HR Score 2.0 pitch-type fit.

    Usage-weighted hitter damage vs the pitcher's actual arsenal.
    A good hitter profile against a pitch thrown 5% of the time is not overvalued,
    while a groundball profile against a pitch thrown 35%+ becomes a trap.

    NEW: when pitcher_per_pitch_damage is provided (HR/EV/barrel allowed by pitch type),
    detects PITCH-TYPE MATCH — hitter's crush pitch is also a pitch the pitcher gets hit on.

    NEW (2026-06-27): when batter_by_pitch_recent10 is provided (last 10 games'
    per-pitch-type breakdown), trap detection checks whether a season-long
    groundball problem on the pitcher's primary pitch is STILL showing up
    recently. If the batter's recent groundball rate on that pitch has
    improved meaningfully, the trap penalty is softened -- a stale, faded
    pattern shouldn't carry full weight against him today.
    """
    if pitcher_per_pitch_damage is None:
        pitcher_per_pitch_damage = {}
    if batter_by_pitch_recent10 is None:
        batter_by_pitch_recent10 = {}

    if not pitcher_mix or not batter_by_pitch:
        return {"score": 50.0, "note": "PMix N/A", "sample": 0, "pitch_trap": False, "trap_reason": "", "best_damage_pitch": "", "best_damage_usage": 0.0, "strong_pitch_signal": False,
                "pitch_type_match_flag": False, "pitch_type_match_code": "", "pitch_type_match_note": "", "pitch_type_match_score": 0.0}

    sorted_mix = sorted([(str(k), safe_float(v, 0.0)) for k, v in pitcher_mix.items()], key=lambda kv: kv[1], reverse=True)
    top_pitch = sorted_mix[0] if sorted_mix else ("", 0.0)
    top2 = sorted_mix[:2]

    total_weight = 0.0
    weighted_score = 0.0
    crush_pitches: List[str] = []
    risk_pitches: List[str] = []
    sample_seen = 0
    pitch_details: List[Dict[str, Any]] = []
    damage_scores: Dict[str, float] = {}

    for pitch_type, usage in sorted_mix:
        b = batter_by_pitch.get(pitch_type)
        if not isinstance(b, dict):
            continue
        seen = safe_int(b.get("seen"), 0)
        bbe = safe_int(b.get("bbe"), 0)
        # Missing / low sample should be neutral, not zero. It lowers confidence elsewhere.
        if seen < 12 or bbe < 3:
            pitch_score = 50.0
            sample_seen += seen
            weight = usage / 100.0
            weighted_score += pitch_score * weight
            total_weight += weight
            pitch_details.append({"pitch": pitch_type, "usage": usage, "score": pitch_score, "sample": seen, "low_sample": True})
            continue

        sample_seen += seen
        avg_ev = safe_float(b.get("avg_ev"), 0.0)
        avg_la = safe_float(b.get("avg_la"), 0.0)
        hr = safe_int(b.get("hr"), 0)
        xbh = safe_int(b.get("xbh"), 0)
        hard_hit = safe_float(b.get("hard_hit_rate"), 0.0)
        barrel_like = safe_float(b.get("barrel_like_rate"), 0.0)
        gb_rate = safe_float(b.get("gb_rate"), 0.0)
        ld_rate = safe_float(b.get("ld_rate"), 0.0)
        air_pull = safe_float(b.get("air_pull_rate"), 0.0)
        max_dist = safe_int(b.get("max_dist"), 0)
        whiff_rate = safe_float(b.get("whiff_rate"), 0.0)
        swstr_rate = safe_float(b.get("swstr_rate"), 0.0)
        hr_per_bbe = safe_float(b.get("hr_per_bbe"), 0.0)

        pitch_score = 50.0
        if avg_ev >= 91:
            pitch_score += 12
        elif avg_ev >= 88:
            pitch_score += 6
        elif avg_ev and avg_ev < 84:
            pitch_score -= 8

        if 20 <= avg_la <= 34:
            pitch_score += 12
        elif 10 <= avg_la < 20 or 34 < avg_la <= 40:
            pitch_score += 4
        elif avg_la < 0:
            pitch_score -= 20
        elif avg_la < 10:
            pitch_score -= 10

        if hard_hit >= 0.40:
            pitch_score += 10
        elif hard_hit >= 0.32:
            pitch_score += 5
        elif hard_hit and hard_hit < 0.22:
            pitch_score -= 6
        if barrel_like >= 0.12:
            pitch_score += 12
        elif barrel_like >= 0.08:
            pitch_score += 6
        elif barrel_like is not None and barrel_like < 0.03:
            pitch_score -= 8
        if gb_rate >= 0.55:
            pitch_score -= 15
        elif gb_rate >= 0.48:
            pitch_score -= 8
        # Line-drive rate: hard, well-struck contact that isn't a popup or a
        # grounder. Real signal distinct from barrel/hard-hit (a liner can be
        # 92mph and still be a clean knock) -- added per audit (2026-06-27),
        # data already existed in by_pitch but was unused here.
        if ld_rate >= 0.25:
            pitch_score += 6
        elif ld_rate >= 0.18:
            pitch_score += 3
        elif ld_rate is not None and ld_rate < 0.10:
            pitch_score -= 4
        if air_pull >= 0.35:
            pitch_score += 8
        elif air_pull and air_pull < 0.20:
            pitch_score -= 5
        # HR signal: rate-based only (hr_per_bbe). The raw HR-count tiers that
        # previously sat here (hr>=2: +10, hr==1: +5) were removed because they
        # stacked additively with these rate tiers for the same underlying events --
        # a player with 2 HR in 15 BBE cleared BOTH the hr>=2 tier (+10) AND the
        # hr_per_bbe>=0.12 tier (+6), getting +16 for one piece of evidence.
        # The comment introducing hr_per_bbe explicitly framed it as "a sharper
        # signal than the flat HR-count tiers above" -- now it actually replaces them.
        if hr_per_bbe >= 0.12:
            pitch_score += 6
        elif hr_per_bbe >= 0.06:
            pitch_score += 3
        if xbh >= 3:
            pitch_score += 4
        if max_dist >= 400:
            pitch_score += 6
        elif max_dist >= 375:
            pitch_score += 3
        if whiff_rate >= 0.35:
            pitch_score -= 6
        # Swing-and-miss rate specifically (whiffs per pitch seen, not per
        # swing like whiff_rate above) -- catches pitches a batter struggles
        # to even get the bat on, distinct from contact-quality-once-engaged.
        if swstr_rate >= 0.20:
            pitch_score -= 5

        pitch_score = max(15.0, min(95.0, pitch_score))
        damage_scores[pitch_type] = pitch_score
        weight = usage / 100.0
        weighted_score += pitch_score * weight
        total_weight += weight
        if pitch_score >= 70:
            crush_pitches.append(pitch_type)
        elif pitch_score <= 43:
            risk_pitches.append(pitch_type)
        pitch_details.append({"pitch": pitch_type, "usage": usage, "score": round(pitch_score, 1), "sample": seen, "bbe": bbe, "gb_rate": round(gb_rate, 3), "avg_la": round(avg_la, 1), "air_pull_rate": round(air_pull, 3)})

    if total_weight <= 0:
        return {"score": 50.0, "note": "Low PMix sample", "sample": sample_seen, "pitch_trap": False, "trap_reason": "", "best_damage_pitch": "", "best_damage_usage": 0.0, "strong_pitch_signal": False,
                "pitch_type_match_flag": False, "pitch_type_match_code": "", "pitch_type_match_note": "", "pitch_type_match_score": 0.0}

    final_score = weighted_score / total_weight
    trap_reason = ""
    pitch_trap = False
    if top_pitch[0] in batter_by_pitch:
        b = batter_by_pitch.get(top_pitch[0], {}) or {}
        seen = safe_int(b.get("seen"), 0)
        bbe = safe_int(b.get("bbe"), 0)
        gb = safe_float(b.get("gb_rate"), 0.0)
        if top_pitch[1] >= 35 and seen >= 12 and bbe >= 3 and gb >= 0.50:
            # Recency check: has this specific groundball problem faded in the
            # last 10 games, or is it still showing up? Simple comparison,
            # kept lean since this only needs a yes/no read, not a full model.
            r10 = batter_by_pitch_recent10.get(top_pitch[0], {}) or {}
            r10_bbe = safe_int(r10.get("bbe"), 0)
            r10_gb = safe_float(r10.get("gb_rate"), None)
            recency_note = ""
            penalty = 20
            if r10_bbe >= 3 and r10_gb is not None:
                if r10_gb <= gb - 0.15:
                    # Meaningfully improved recently -- soften, don't erase.
                    penalty = 10
                    recency_note = f", but recent GB only {r10_gb*100:.0f}% (improving)"
                elif r10_gb >= gb:
                    recency_note = f", still {r10_gb*100:.0f}% GB in last 10 games"
            final_score -= penalty
            pitch_trap = True
            trap_reason = f"Pitcher's primary pitch produces {gb*100:.0f}% GB rate vs this hitter{recency_note}"

    if not pitch_trap and len(top2) >= 2 and sum(u for _, u in top2) >= 60:
        top2_gb = []
        top2_pts = []
        for pt, _usage in top2:
            b = batter_by_pitch.get(pt, {}) or {}
            if safe_int(b.get("seen"), 0) >= 12 and safe_int(b.get("bbe"), 0) >= 3:
                top2_gb.append(safe_float(b.get("gb_rate"), 0.0))
                top2_pts.append(pt)
        if len(top2_gb) == 2 and all(x >= 0.45 for x in top2_gb):
            # Recency check, same idea as trap 1: only soften if BOTH pitches
            # show recent improvement -- one improving while the other is
            # still bad isn't enough to call the overall trap stale.
            penalty = 14
            recency_note = ""
            both_improved = True
            any_recent_data = False
            for pt, season_gb in zip(top2_pts, top2_gb):
                r10 = batter_by_pitch_recent10.get(pt, {}) or {}
                r10_bbe = safe_int(r10.get("bbe"), 0)
                r10_gb = safe_float(r10.get("gb_rate"), None)
                if r10_bbe >= 3 and r10_gb is not None:
                    any_recent_data = True
                    if r10_gb > season_gb - 0.15:
                        both_improved = False
                else:
                    both_improved = False
            if any_recent_data and both_improved:
                penalty = 7
                recency_note = " (improving on both recently)"
            final_score -= penalty
            pitch_trap = True
            trap_reason = f"Top two pitcher pitches both create groundball risk{recency_note}"

    # Top-2 damage match: instead of just checking how OFTEN the pitcher
    # throws his single best-damage pitch, check whether the batter's top 2
    # best-damage pitches are ALSO pitches the pitcher himself gets hit hard
    # on (pitcher_per_pitch_damage hard_hit_rate >= league-avg 36%) -- per
    # audit (2026-06-27). This is a closer match to "is this a real
    # exploitable matchup" than usage frequency alone: a rarely-thrown pitch
    # the pitcher gets crushed on every time it's seen is still a real risk
    # for him, while a frequently-thrown pitch he handles fine isn't a trap
    # just because the batter happens to score well on that pitch type.
    LEAGUE_AVG_HH_DAMAGE = 0.36
    top2_damage_pitches = sorted(damage_scores.items(), key=lambda kv: kv[1], reverse=True)[:2]
    best_damage_pitch = top2_damage_pitches[0][0] if top2_damage_pitches else ""
    best_damage_score = top2_damage_pitches[0][1] if top2_damage_pitches else -1.0
    usage_lookup = {pt: usage for pt, usage in sorted_mix}
    best_damage_usage = safe_float(usage_lookup.get(best_damage_pitch), 0.0)

    matched_pitches = []
    for pt, score in top2_damage_pitches:
        if score < 68:
            continue
        pitcher_dmg = (pitcher_per_pitch_damage or {}).get(pt, {}) or {}
        pitcher_hh = safe_float(pitcher_dmg.get("hard_hit_rate"), 0.0)
        pitcher_bbe = safe_int(pitcher_dmg.get("bbe"), 0)
        if pitcher_bbe >= 5 and pitcher_hh >= LEAGUE_AVG_HH_DAMAGE:
            matched_pitches.append(pt)

    if len(matched_pitches) >= 2:
        # Both of his top-2 pitches are confirmed pitcher weaknesses.
        final_score += 12
    elif len(matched_pitches) == 1:
        final_score += 7
    elif best_damage_pitch and best_damage_usage < 10 and best_damage_score >= 68:
        # No pitcher-damage confirmation, AND his only good pitch is barely
        # thrown -- same trap logic as before, kept as a fallback.
        final_score -= 10
        if not pitch_trap:
            pitch_trap = True
            trap_reason = f"Best damage pitch only {best_damage_usage:.0f}% of pitcher's arsenal"

    final_score = round(max(15.0, min(95.0, final_score)), 1)
    if crush_pitches:
        note = "Crush " + "/".join(crush_pitches[:2])
    elif risk_pitches:
        note = "Risk " + "/".join(risk_pitches[:2])
    else:
        note = "Neutral mix"

    # ─── PITCH-TYPE MATCH detection (v2 — user's rules) ────────────────────
    # Fires when ALL of the following are true:
    #   1. Batter's good-contact rate on the pitch ≥ 50%
    #      (good contact = 97+ EV OR barrel OR 350+ ft)
    #   2. Pitcher's hard-hit rate allowed on the pitch ≥ 36% (league avg)
    #   3. Pitch is thrown ≥ 5% by the pitcher to this hitter's side
    #   4. Both sides have ≥ 5 BBE on the pitch (noise filter)
    # Match score capped at 120 for separation between decent and elite matches.
    LEAGUE_AVG_HH = 0.36
    pitch_type_match_flag = False
    pitch_type_match_code = ""
    pitch_type_match_note = ""
    pitch_type_match_score = 0.0
    if pitcher_per_pitch_damage and batter_by_pitch:
        best_match_score = 0.0
        for pt, batter_data in batter_by_pitch.items():
            batter_bbe = safe_int(batter_data.get("bbe"), 0)
            if batter_bbe < 5:
                continue
            good_contact = safe_float(batter_data.get("good_contact_rate"), 0.0)
            if good_contact < 0.50:
                continue  # batter doesn't make damaging contact on this pitch
            pitcher_usage = safe_float(usage_lookup.get(pt), 0.0)
            if pitcher_usage < 5.0:
                continue
            pdmg = pitcher_per_pitch_damage.get(pt, {}) or {}
            p_bbe = safe_int(pdmg.get("bbe"), 0)
            if p_bbe < 5:
                continue
            p_hh = safe_float(pdmg.get("hard_hit_rate"), 0.0)
            if p_hh < LEAGUE_AVG_HH:
                continue  # pitcher doesn't get hit hard on this pitch
            # Score: hitter good-contact% × pitcher HH-allowed × usage × HR factor
            h_ev = safe_float(batter_data.get("avg_ev"), 0.0)
            h_hr = safe_int(batter_data.get("hr"), 0)
            h_hh_count = safe_int(batter_data.get("hh_97_count"), 0)
            p_hr = safe_int(pdmg.get("hr"), 0)
            p_barrel = safe_float(pdmg.get("barrel_like_rate"), 0.0)
            match_score = min(120.0,
                (good_contact * 60) +           # batter quality (0-60)
                ((p_hh - LEAGUE_AVG_HH) * 200) + # pitcher vulnerability over league (0-30ish)
                (p_hr * 4) +                     # pitcher HR allowed on pitch
                (h_hr * 3) +                     # batter HR on pitch
                (pitcher_usage * 0.3) +          # usage weight
                (p_barrel * 50)                  # pitcher barrel-allowed bonus
            )
            if match_score > best_match_score:
                best_match_score = match_score
                pitch_type_match_flag = True
                pitch_type_match_code = pt
                pitch_type_match_score = round(match_score, 1)
                pitch_type_match_note = (
                    f"vs {pt}: batter {good_contact*100:.0f}% good-contact ({h_hh_count}HH 97+, {h_hr}HR) "
                    f"| pitcher {pitcher_usage:.0f}% usage, {p_hh*100:.0f}% HH allowed (lg avg {LEAGUE_AVG_HH*100:.0f}%), {p_hr}HR"
                )

    return {
        "score": final_score,
        "note": note,
        "sample": sample_seen,
        "pitch_trap": pitch_trap,
        "trap_reason": trap_reason,
        "best_damage_pitch": best_damage_pitch,
        "best_damage_usage": round(best_damage_usage, 1),
        "strong_pitch_signal": bool(best_damage_pitch and best_damage_usage >= 20 and final_score >= 60),
        "details": pitch_details[:8],
        # NEW pitch-type match fields
        "pitch_type_match_flag": pitch_type_match_flag,
        "pitch_type_match_code": pitch_type_match_code,
        "pitch_type_match_note": pitch_type_match_note,
        "pitch_type_match_score": pitch_type_match_score,
    }


def pitcher_attack_score_and_tag(flat: Dict[str, float], split_meta: Dict[str, float], psc: Dict[str, Any]) -> Tuple[float, str]:
    if safe_int(psc.get("statcast_bbe"), 0) <= 0 or str(psc.get("statcast_status", "missing")) != "ok":
        return 0.0, "Statcast N/A"
    fb = safe_float(psc.get("statcast_fb_rate"), 0.34)
    barrel = safe_float(psc.get("barrel_allowed"), 0.07)
    hard = safe_float(psc.get("hardhit_allowed"), 0.38)
    ev = safe_float(psc.get("ev_allowed"), 88.5)
    hr9 = safe_float(flat.get("hr9"), 1.10)
    score = 100 * (
        0.30 * minmax_norm(fb, 0.28, 0.48) +
        0.25 * minmax_norm(barrel, 0.03, 0.13) +
        0.20 * minmax_norm(hard, 0.30, 0.52) +
        0.15 * minmax_norm(ev, 86.0, 92.5) +
        0.10 * minmax_norm(hr9, 0.70, 2.00)
    )
    babip = safe_float(psc.get("babip_statcast"), safe_float(flat.get("babip"), 0.300))
    if barrel >= 0.08 and fb >= 0.38 and babip < 0.270:
        tag = "💣 BLOWUP INCOMING"
    elif barrel >= 0.08 and fb >= 0.38:
        tag = "🔥 HR ENVIRONMENT"
    elif hard >= 0.42 and ev >= 90.0:
        tag = "⚠️ HARD CONTACT"
    elif fb < 0.32 and barrel < 0.06:
        tag = "🧊 GB/TRAP"
    else:
        tag = "Neutral"
    return round(score, 1), tag



# ── V2 LINEUP-SPOT DAMAGE ───────────────────────────────────────────────────
def _spot_damage_label(score: float) -> str:
    if score >= 68:
        return "HOT"
    if score >= 56:
        return "WARM"
    if score <= 34:
        return "PITCHER ADV"
    return "NEUTRAL"


def _spot_zone_for_lineup_spot(spot: int) -> str:
    # BUGFIX: previously fell through to "bottom" for spot=0/None (unconfirmed
    # lineup), silently treating an unknown batting spot as a real bottom-of-
    # order hitter and borrowing that zone's pitcher-damage numbers for him.
    # lineup_spot_risk_label() already had a proper "unknown" case for the
    # same input -- this brings the two in line. Both call sites already
    # degrade safely on an unrecognized key (a guarded loop that never passes
    # an invalid spot, and a dict .get(..., {}) with an empty-dict default),
    # so this is a behavior fix with no new failure mode.
    s = int(spot or 0)
    if s in (1, 2, 3):
        return "top"
    if s in (4, 5, 6):
        return "middle"
    if s in (7, 8, 9):
        return "bottom"
    return "unknown"


def _empty_spot_damage(spot: int) -> Dict[str, Any]:
    return {
        "spot": int(spot), "pa": 0, "ab": 0, "hits": 0, "xbh": 0, "hr": 0, "bb": 0, "k": 0, "tb": 0,
        "avg": 0.0, "slg": 0.0, "iso": 0.0, "hr_rate": 0.0, "xbh_rate": 0.0, "k_rate": 0.0,
        "bbe": 0, "hard_hit_rate": 0.0, "barrel_rate": 0.0, "avg_ev": 0.0,
        "damage_score": 0.0, "label": "Unknown", "sample": "no sample",
        "reason": f"No historical pitcher sample found for lineup spot #{spot}",
    }


def _weak_spot_reason_for(pitcher: "PitcherSummary", spot: int) -> str:
    """Plain-language reason behind the ⭐ weak-spot flag, built from the same
    per-lineup-spot damage data that decides the flag itself, so the card can
    show *why* instead of just a boolean."""
    if spot not in pitcher.weak_spots:
        return ""
    spot_data = (getattr(pitcher, "lineup_spot_damage", {}) or {}).get(str(spot), {})
    if not spot_data:
        return "Weak lineup spot vs this pitcher (limited sample)."
    pa = safe_int(spot_data.get("pa"), 0)
    hr = safe_int(spot_data.get("hr"), 0)
    slg = safe_float(spot_data.get("slg"), 0.0)
    if pa < 4:
        return "Weak lineup spot vs this pitcher (limited sample)."
    if hr > 0:
        # display_avg, not a hand-rolled "." + int(slg*1000) (fixed 2026-08-11).
        # That pattern is right below .999 and WRONG at or above it: 1.034 SLG
        # printed as ".1034". It therefore broke on exactly the rows this
        # sentence exists to flag — a spot a pitcher is getting destroyed at is
        # a spot slugging over 1.000. 6 of the 40 weak-spot lines on the
        # 2026-08-11 slate were malformed, including a 1.250. display_avg has
        # been in this file since line 1619 and does the leading-zero rule
        # properly; these two call sites just never used it.
        return f"Pitcher has allowed {hr} HR to the #{spot} spot in {pa} PA this season ({display_avg(slg)} SLG)."
    return f"Pitcher allows a {display_avg(slg)} SLG to the #{spot} spot in {pa} PA this season."


def _finalize_spot_damage(bucket: Dict[str, Any], label_name: str = "") -> Dict[str, Any]:
    pa = max(0, safe_int(bucket.get("pa"), 0))
    ab = max(0, safe_int(bucket.get("ab"), 0))
    hits = max(0, safe_int(bucket.get("hits"), 0))
    xbh = max(0, safe_int(bucket.get("xbh"), 0))
    hr = max(0, safe_int(bucket.get("hr"), 0))
    k = max(0, safe_int(bucket.get("k"), 0))
    tb = max(0, safe_int(bucket.get("tb"), 0))
    bbe = max(0, safe_int(bucket.get("bbe"), 0))
    hard = max(0, safe_int(bucket.get("hard"), 0))
    barrels = max(0, safe_int(bucket.get("barrels"), 0))
    ev_sum = safe_float(bucket.get("ev_sum"), 0.0)

    avg = hits / ab if ab else 0.0
    slg = tb / ab if ab else 0.0
    iso = max(0.0, slg - avg) if ab else 0.0
    hr_rate = hr / pa if pa else 0.0
    xbh_rate = xbh / pa if pa else 0.0
    k_rate = k / pa if pa else 0.0
    hard_hit_rate = hard / bbe if bbe else 0.0
    barrel_rate = barrels / bbe if bbe else 0.0
    avg_ev = ev_sum / bbe if bbe else 0.0

    raw = 100 * (
        0.28 * minmax_norm(hr_rate, 0.000, 0.085) +
        0.18 * minmax_norm(xbh_rate, 0.035, 0.270) +
        0.18 * minmax_norm(slg, 0.300, 0.680) +
        0.14 * minmax_norm(iso, 0.070, 0.310) +
        0.12 * minmax_norm(hard_hit_rate, 0.280, 0.540) +
        0.10 * minmax_norm(barrel_rate, 0.020, 0.140)
    )
    sample_factor = 0.58 + 0.42 * min(1.0, pa / 32.0)
    score = round(raw * sample_factor, 1) if pa else 0.0
    label = _spot_damage_label(score) if pa else "Unknown"
    sample = "strong" if pa >= 30 else "medium" if pa >= 14 else "light" if pa >= 4 else "tiny" if pa else "no sample"
    who = label_name or (f"spot #{bucket.get('spot')}" if bucket.get("spot") else "zone")
    reason = (
        f"{who}: {pa} PA, {slg:.3f} SLG, {iso:.3f} ISO, "
        f"HR rate {hr_rate*100:.1f}%, XBH rate {xbh_rate*100:.1f}%, "
        f"HH {hard_hit_rate*100:.1f}%"
    ) if pa else f"No historical pitcher sample found for {who}"
    return {
        **{k: v for k, v in bucket.items() if k not in {"hard", "barrels", "ev_sum"}},
        "pa": pa, "ab": ab, "hits": hits, "xbh": xbh, "hr": hr, "k": k, "tb": tb,
        "avg": round(avg, 3), "slg": round(slg, 3), "iso": round(iso, 3),
        "hr_rate": round(hr_rate, 4), "xbh_rate": round(xbh_rate, 4), "k_rate": round(k_rate, 4),
        "bbe": bbe, "hard_hit_rate": round(hard_hit_rate, 4), "barrel_rate": round(barrel_rate, 4),
        "avg_ev": round(avg_ev, 1), "damage_score": score, "label": label, "sample": sample, "reason": reason,
    }


def build_pitcher_lineup_spot_damage(client: MLBClient, db: CacheDB, pitcher_id: int, end_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """True V2 pitcher damage allowed by batting-order spot.

    Uses Statcast pitcher events for the season, maps batter IDs back to each game's official battingOrder
    from the MLB live game feed, then calculates damage allowed by spots 1-9 and zones 1-3/4-6/7-9.
    This is meant for the Matchups tab color/hover logic.
    """
    end_date = end_date or statcast_data_end_date(TODAY)
    key = f"pitcher_lineup_spot_damage_v1:{SEASON}:{pitcher_id}:{end_date.isoformat()}"
    cached = db.get(key, max_age_days=2)
    if isinstance(cached, dict) and cached.get("spots"):
        return cached

    spots = {str(i): _empty_spot_damage(i) for i in range(1, 10)}
    zones = {
        "top": {"zone": "top", "spots": [1, 2, 3], "pa": 0, "ab": 0, "hits": 0, "xbh": 0, "hr": 0, "bb": 0, "k": 0, "tb": 0, "bbe": 0, "hard": 0, "barrels": 0, "ev_sum": 0.0},
        "middle": {"zone": "middle", "spots": [4, 5, 6], "pa": 0, "ab": 0, "hits": 0, "xbh": 0, "hr": 0, "bb": 0, "k": 0, "tb": 0, "bbe": 0, "hard": 0, "barrels": 0, "ev_sum": 0.0},
        "bottom": {"zone": "bottom", "spots": [7, 8, 9], "pa": 0, "ab": 0, "hits": 0, "xbh": 0, "hr": 0, "bb": 0, "k": 0, "tb": 0, "bbe": 0, "hard": 0, "barrels": 0, "ev_sum": 0.0},
    }
    payload = {"status": "missing", "pitcher_id": pitcher_id, "generated": dt.datetime.now().isoformat(), "spots": spots, "zones": zones, "weak_spots": []}
    if statcast_pitcher is None or not pitcher_id:
        return payload

    try:
        df = statcast_pitcher(SEASON_START.isoformat(), end_date.isoformat(), pitcher_id)
    except Exception as exc:
        payload["status"] = f"statcast_error:{type(exc).__name__}"
        return payload
    if df is None or len(df) == 0:
        payload["status"] = "empty_statcast"
        return payload

    NON_AB = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "none", ""}
    HITS = {"single", "double", "triple", "home_run"}
    XBH = {"double", "triple", "home_run"}
    WALKS = {"walk", "intent_walk"}
    KS = {"strikeout", "strikeout_double_play"}
    TB = {"single": 1, "double": 2, "triple": 3, "home_run": 4}

    try:
        df = df.copy()
        if "game_date" in df.columns:
            df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
            played = df["game_date"].dropna().dt.normalize()
            last_dates = list(played[played <= pd.Timestamp(end_date)].drop_duplicates().sort_values().tail(12))
            if last_dates:
                df = df[df["game_date"].dt.normalize().isin(last_dates)].copy()
        for col in ("game_pk", "batter", "at_bat_number", "launch_speed", "launch_angle"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        event_rows = df[df.get("events").notna()].copy() if "events" in df.columns else df.iloc[0:0].copy()
        if len(event_rows) == 0:
            payload["status"] = "empty_events"
            return payload

        lineup_maps: Dict[int, Dict[int, int]] = {}
        for gpk in sorted(event_rows["game_pk"].dropna().astype(int).unique().tolist()) if "game_pk" in event_rows.columns else []:
            try:
                feed = client.live_game(int(gpk))
                teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {}) or {}
                mapping: Dict[int, int] = {}
                for side in ("home", "away"):
                    order = teams.get(side, {}).get("battingOrder") or []
                    for idx, raw_pid in enumerate(order, start=1):
                        pid = safe_int(str(raw_pid).replace("ID", ""), 0)
                        if pid:
                            mapping[pid] = idx
                lineup_maps[int(gpk)] = mapping
            except Exception:
                lineup_maps[int(gpk)] = {}

        for _, r in event_rows.iterrows():
            gpk = safe_int(r.get("game_pk"), 0)
            batter = safe_int(r.get("batter"), 0)
            spot = lineup_maps.get(gpk, {}).get(batter)
            if not spot or spot < 1 or spot > 9:
                continue
            event = str(r.get("events", "") or "").strip()
            b = spots[str(spot)]
            z = zones[_spot_zone_for_lineup_spot(spot)]
            for bucket in (b, z):
                bucket["pa"] = safe_int(bucket.get("pa"), 0) + 1
                if event not in NON_AB:
                    bucket["ab"] = safe_int(bucket.get("ab"), 0) + 1
                if event in HITS:
                    bucket["hits"] = safe_int(bucket.get("hits"), 0) + 1
                if event in XBH:
                    bucket["xbh"] = safe_int(bucket.get("xbh"), 0) + 1
                if event == "home_run":
                    bucket["hr"] = safe_int(bucket.get("hr"), 0) + 1
                if event in WALKS:
                    bucket["bb"] = safe_int(bucket.get("bb"), 0) + 1
                if event in KS:
                    bucket["k"] = safe_int(bucket.get("k"), 0) + 1
                bucket["tb"] = safe_int(bucket.get("tb"), 0) + TB.get(event, 0)
                ev = safe_float(r.get("launch_speed"), 0.0)
                la = safe_float(r.get("launch_angle"), 0.0)
                if ev > 0:
                    bucket["bbe"] = safe_int(bucket.get("bbe"), 0) + 1
                    bucket["ev_sum"] = safe_float(bucket.get("ev_sum"), 0.0) + ev
                    if ev >= 95:
                        bucket["hard"] = safe_int(bucket.get("hard"), 0) + 1
                    if ev >= 98 and 24 <= la <= 32:
                        bucket["barrels"] = safe_int(bucket.get("barrels"), 0) + 1

        final_spots = {str(i): _finalize_spot_damage(spots[str(i)], f"spot #{i}") for i in range(1, 10)}
        zone_names = {"top": "top order 1-3", "middle": "middle order 4-6", "bottom": "bottom order 7-9"}
        final_zones = {k: _finalize_spot_damage(v, zone_names.get(k, k)) for k, v in zones.items()}
        weak_spots = [int(k) for k, v in final_spots.items() if safe_float(v.get("damage_score"), 0) >= 58 and safe_int(v.get("pa"), 0) >= 4]
        if not weak_spots:
            weak_spots = [int(k) for k, v in sorted(final_spots.items(), key=lambda kv: safe_float(kv[1].get("damage_score"), 0), reverse=True)[:3] if safe_int(v.get("pa"), 0) >= 3]
        payload = {
            "status": "ok",
            "pitcher_id": pitcher_id,
            "generated": dt.datetime.now().isoformat(),
            "sample_games": len(lineup_maps),
            "spots": final_spots,
            "zones": final_zones,
            "weak_spots": weak_spots,
        }
        db.set(key, payload)
        return payload
    except Exception as exc:
        payload["status"] = f"parse_error:{type(exc).__name__}"
        return payload
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_weak_spots_from_rates(hr9: float, whip: float) -> tuple:
    """Fallback weak-spot estimation when real lineup-spot damage data is unavailable.
    Extracted (audit 2026-06-29) from the two hand-copied blocks in build_pitcher_profile
    that previously each contained this logic inline -- ensures thresholds can only
    drift if both places are updated, which would have been silently inconsistent before.
    """
    if hr9 >= 1.45 or whip >= 1.40:
        return (1, 2, 3, 4, 5)
    if hr9 >= 1.20 or whip >= 1.28:
        return (2, 3, 4, 5)
    return (2, 3, 4)


def compute_pitcher_extended_stats(stat: Dict[str, Any], flat: Dict[str, float], psc: Dict[str, Any]) -> Dict[str, Any]:
    """FIP, AVG/OBP/SLG/ISO/wOBA against, TB allowed, BB count/%, season
    barrels count, HR/FB% -- built entirely from data already fetched
    elsewhere (the official season `stat` blob already pulled in
    build_pitcher_profile, plus the Statcast `psc` profile already pulled
    for hardhit/barrel/EV). Zero new network calls.

    FIP uses a fixed 3.10 constant since the exact seasonal MLB constant
    isn't available in this environment -- treat pitcher_fip as an
    approximation, not the official FanGraphs number. wOBA against uses
    standard fixed linear weights (not exact year-specific constants),
    same caveat.
    """
    FIP_CONSTANT = 3.10

    ab = safe_float(stat.get("atBats"), 0.0)
    bb = safe_float(stat.get("baseOnBalls"), 0.0)
    ibb = safe_float(stat.get("intentionalWalks"), 0.0)
    hbp = safe_float(stat.get("hitBatsmen"), 0.0)
    sf = safe_float(stat.get("sacFlies"), 0.0)
    hits = safe_float(stat.get("hits"), 0.0)
    doubles = safe_float(stat.get("doubles"), 0.0)
    triples = safe_float(stat.get("triples"), 0.0)
    hr = safe_float(stat.get("homeRuns"), flat.get("hr_allowed", 0.0))
    so = safe_float(stat.get("strikeOuts"), 0.0)
    tb = safe_float(stat.get("totalBases"), 0.0)
    ip = _parse_ip_to_float(stat.get("inningsPitched"))
    bf = safe_float(stat.get("battersFaced"), 0.0)

    avg_against = safe_float(stat.get("avg"), (hits / ab) if ab else 0.250)
    obp_against = safe_float(stat.get("obp"), ((hits + bb + hbp) / max(1.0, ab + bb + hbp + sf)) if (ab or bb) else 0.320)
    slg_against = safe_float(stat.get("slg"), (tb / ab) if ab else 0.400)
    ops_against = safe_float(stat.get("ops"), obp_against + slg_against)
    iso_against = max(0.0, slg_against - avg_against)

    if tb <= 0 and ab > 0:
        singles_for_tb = max(0.0, hits - doubles - triples - hr)
        tb = singles_for_tb + 2 * doubles + 3 * triples + 4 * hr

    singles = max(0.0, hits - doubles - triples - hr)
    woba_denom = max(1.0, ab + bb - ibb + sf + hbp)
    woba_against = (
        0.690 * (bb - ibb) + 0.722 * hbp + 0.888 * singles + 1.271 * doubles + 1.616 * triples + 2.101 * hr
    ) / woba_denom if woba_denom else 0.320

    fip = ((13 * hr + 3 * (bb + hbp) - 2 * so) / ip) + FIP_CONSTANT if ip > 0 else 4.00
    bb_pct = (bb / bf) if bf else 0.080
    # BB/9 (2026-08-12): same ip already pulled for FIP above, walks already
    # pulled for bb_pct above -- zero new fields fetched.
    bb9 = (bb * 9.0 / ip) if ip > 0 else 3.20

    # ── THE RUNNING GAME (2026-08-23) ─────────────────────────────────────
    #
    # Donovan asked for wild pitches, pickoffs and pitcher stolen-bases-against
    # among six stats "we need to see if we can find anywhere". Four of the six
    # were never anywhere else — they were in THIS dict's own input the whole
    # time. `stat` is StatsAPI's season pitching blob, already fetched by
    # build_pitcher_profile, and it carries wildPitches, pickoffs, balks,
    # stolenBases and caughtStealing as top-level keys. Verified verbatim
    # against statsapi.mlb.com before a line of this was written, which is the
    # standing rule here: this repo has the receipt for what guessing an API's
    # shape costs (the odds pipeline needed "eight round trips of failure" to
    # learn one provider's response).
    #
    # Zero new network calls, same as everything else in this function.
    #
    # RATES, NOT COUNTS, for the ones that scale with innings. A reliever with
    # 3 wild pitches in 40 innings is wilder than a starter with 5 in 180, and
    # a board sorted on the raw count says the opposite. The counts ride along
    # too, because a rate over 12 innings is not a rate and the reader needs
    # the denominator to know that.
    wild_pitches = safe_int(stat.get("wildPitches"), 0)
    pickoffs = safe_int(stat.get("pickoffs"), 0)
    balks = safe_int(stat.get("balks"), 0)
    sb_against = safe_int(stat.get("stolenBases"), 0)
    cs_against = safe_int(stat.get("caughtStealing"), 0)
    sb_attempts_against = sb_against + cs_against
    # A pitcher's caught-stealing rate is NOT his alone — the catcher throws
    # the ball. It is published as the pair's number and named that way
    # everywhere it surfaces, because attributing it to the arm would be a
    # quiet lie about who is doing the work.
    cs_rate_against = (cs_against / sb_attempts_against) if sb_attempts_against >= 5 else None
    wp9 = (wild_pitches * 9.0 / ip) if ip > 0 else None
    # Pickoffs per time on base, not per inning: the chance to pick a man off
    # only exists when there is a man on. Baserunners allowed is the honest
    # denominator and it is already here (hits + walks + hit batsmen).
    baserunners = hits + bb + hbp
    pickoff_rate = (pickoffs / baserunners) if baserunners >= 20 else None

    return {
        "fip": round(fip, 2),
        "avg_against": round(avg_against, 3),
        "obp_against": round(obp_against, 3),
        "slg_against": round(slg_against, 3),
        "ops_against": round(ops_against, 3),
        "iso_against": round(iso_against, 3),
        "woba_against": round(woba_against, 3),
        "tb_allowed": int(tb),
        "bb_allowed": int(bb),
        "bb_pct": round(bb_pct, 3),
        "bb9": round(bb9, 2),
        "barrels_allowed_count": safe_int(psc.get("barrels_allowed_count_season"), 0),
        "hr_fb_pct": round(safe_float(psc.get("hr_fb_pct"), 0.100), 3),
        "extended_stats_status": "ok" if (ab > 0 or bf > 0) else "missing",
        # ── the running game, off the same blob ──────────────────────────
        "wild_pitches": wild_pitches,
        "pickoffs": pickoffs,
        "balks": balks,
        "sb_against": sb_against,
        "cs_against": cs_against,
        "sb_attempts_against": sb_attempts_against,
        # None, not 0.0, under the sample floors. A pitcher who has faced two
        # attempts has no caught-stealing rate, and publishing 0.0 for him
        # would put him at the bottom of every board beside arms who genuinely
        # cannot hold a runner. The site renders None as an em-dash.
        "cs_rate_against": round(cs_rate_against, 3) if cs_rate_against is not None else None,
        "wp9": round(wp9, 2) if wp9 is not None else None,
        "pickoff_rate": round(pickoff_rate, 4) if pickoff_rate is not None else None,
        "running_game_status": "ok" if ip > 0 else "missing",
    }


# ── TBD PITCHER RESOLVER (2026-08-06, "ensure the bot does its best") ──
# When the schedule has no probable yet, two fallbacks before giving up:
#   A. the game's own live feed (sometimes populated before the schedule)
#   B. rotation inference — the team's starter from 4–6 days ago who hasn't
#      pitched since, i.e. the guy whose turn it is. Marked PROJECTED so the
#      site can say so instead of presenting a guess as a fact.
PROJECTED_PITCHERS: set = set()

def resolve_probable_pitcher(game_pk: int, sched_id: int, team_id: int, slate_date_: dt.date) -> tuple:
    """Returns (pitcher_id, projected_bool)."""
    if sched_id:
        return sched_id, False
    try:
        # Fallback A: live feed probables
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
            params={"fields": "gameData,probablePitchers,home,away,id,teams,team"},
            timeout=15,
        )
        if r.ok:
            gd = (r.json() or {}).get("gameData", {}) or {}
            pp = gd.get("probablePitchers", {}) or {}
            teams = gd.get("teams", {}) or {}
            for side in ("home", "away"):
                if safe_int(((teams.get(side) or {}).get("id")), 0) == team_id:
                    pid = safe_int((pp.get(side) or {}).get("id"), 0)
                    if pid:
                        return pid, False
    except Exception:
        pass
    try:
        # Fallback B: rotation inference from the team's last 6 days
        start = (slate_date_ - dt.timedelta(days=6)).isoformat()
        end = (slate_date_ - dt.timedelta(days=1)).isoformat()
        r = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "teamId": team_id, "startDate": start, "endDate": end,
                    "hydrate": "probablePitcher"},
            timeout=15,
        )
        if r.ok:
            starts = []  # (date, pitcher_id)
            for d in (r.json() or {}).get("dates", []):
                for g in d.get("games", []):
                    for side in ("home", "away"):
                        t = (g.get("teams", {}) or {}).get(side, {}) or {}
                        if safe_int((t.get("team") or {}).get("id"), 0) == team_id:
                            pid = safe_int((t.get("probablePitcher") or {}).get("id"), 0)
                            if pid:
                                starts.append((d.get("date", ""), pid))
            if starts:
                starts.sort()
                recent3 = {pid for dstr, pid in starts if dstr >= (slate_date_ - dt.timedelta(days=3)).isoformat()}
                # whose turn: the starter 4-6 days back who hasn't gone since
                for dstr, pid in starts:  # oldest first = most rest
                    if pid not in recent3:
                        PROJECTED_PITCHERS.add(pid)
                        print(f"  probable TBD for team {team_id} — projecting rotation arm {pid} (last started {dstr})", file=sys.stderr)
                        return pid, True
    except Exception:
        pass
    return 0, False


def build_pitcher_profile(client: MLBClient, db: CacheDB, pitcher_id: int, team_abbr: str, data_end_date: Optional[dt.date] = None) -> PitcherSummary:
    if not pitcher_id:
        return PitcherSummary(0, "TBD", team_abbr)
    data_end_date = data_end_date or statcast_data_end_date(TODAY)
    key = f"pitcher_profile_v5_l5l8:{SEASON}:{pitcher_id}:{data_end_date.isoformat()}"
    cached = db.get(key, max_age_days=1)
    if cached is not None:
        allowed = {k: cached[k] for k in PitcherSummary.__dataclass_fields__.keys() if k in cached}
        if not allowed.get("lineup_spot_damage") or not allowed.get("lineup_zone_damage"):
            spot_damage = build_pitcher_lineup_spot_damage(client, db, pitcher_id, data_end_date)
            allowed["lineup_spot_damage"] = spot_damage.get("spots", {})
            allowed["lineup_zone_damage"] = spot_damage.get("zones", {})
            if spot_damage.get("weak_spots"):
                allowed["weak_spots"] = tuple(spot_damage.get("weak_spots") or ())
        if not allowed.get("weak_spots"):
            hr9 = safe_float(allowed.get("hr9"), 1.10)
            whip = safe_float(allowed.get("whip"), 1.30)
            allowed["weak_spots"] = _estimate_weak_spots_from_rates(hr9, whip)
        # DATA DEFECT #2 FIX (2026-08-22): pitcher_xhr_allowed/pitcher_hr_luck
        # were 0 on every starter of every published slate. Root cause: this
        # early cache return sits ABOVE the _xhr_register_pitcher() call in
        # the cold path below, so on any run where this profile is served
        # from cache -- every run after the day's first, ~12 of 13 today.yml
        # runs -- no pitcher was ever registered in _XHR_PITCHERS, and
        # finalize_xhr_fields() found nothing to stamp. The batter half of
        # the identical machinery works because its register call sits in
        # the always-run per-hitter loop (mlb_dashboard main), not behind a
        # profile cache hit. Registration is a per-RUN accumulator side
        # effect and must happen on every path that produces a profile;
        # build_pitcher_statcast_profile() is itself day-cached, so on a
        # cache hit this costs one db read.
        _xhr_register_pitcher(pitcher_id, build_pitcher_statcast_profile(db, pitcher_id, data_end_date))
        return PitcherSummary(**allowed)

    person_blob = client.person(pitcher_id)
    person = (person_blob.get("people") or [{}])[0]
    # Instrumentation (2026-08-13): this call had no try/except at all --
    # unlike the hitter-side equivalent a few thousand lines down, which
    # already catches and falls back to stat={}. A raised exception here
    # would propagate out of this function with nothing catching it before
    # the per-game loop that calls build_pitcher_profile(), so this also
    # closes a real (if apparently rare in practice) crash risk, not just
    # an instrumentation gap.
    try:
        sblob = client.person_stats(pitcher_id, group="pitching", stat_type="season")
        stats_list = sblob.get("stats") or []
        first = stats_list[0] if stats_list else {}
        splits = first.get("splits") or []
        stat = (splits[0].get("stat") if splits else {}) or {}
        season_stats_status = "ok" if stat else "empty"
    except Exception as exc:
        stat = {}
        season_stats_status = f"error:{type(exc).__name__}"
    flat = flatten_pitching(stat)
    split_meta = parse_pitcher_handed_splits(client, db, pitcher_id)
    sit_splits = parse_pitcher_situational_splits(client, db, pitcher_id)
    psc = build_pitcher_statcast_profile(db, pitcher_id, data_end_date)
    _xhr_register_pitcher(pitcher_id, psc)
    advanced = build_pitcher_advanced_stats(db, pitcher_id, data_end_date)
    spot_damage = build_pitcher_lineup_spot_damage(client, db, pitcher_id, data_end_date)
    attack_score, attack_tag = pitcher_attack_score_and_tag(flat, split_meta, psc)
    try:
        p_glog = client.person_game_log(pitcher_id, group="pitching")
        l3 = compute_pitcher_recent_starts(p_glog, 3)
    except Exception:
        l3 = {"era": flat["era"], "whip": flat["whip"], "hr9": flat["hr9"], "starts_found": 0, "status": "fetch_error"}

    if spot_damage.get("weak_spots"):
        weak_spots = tuple(safe_int(x, 0) for x in spot_damage.get("weak_spots", []) if safe_int(x, 0))
    else:
        weak_spots = _estimate_weak_spots_from_rates(flat["hr9"], flat["whip"])

    extended = compute_pitcher_extended_stats(stat, flat, psc)

    payload = {
        "player_id": pitcher_id,
        "name": person.get("fullName", f"Pitcher {pitcher_id}"),
        "team_abbr": team_abbr,
        "throws": (person.get("pitchHand") or {}).get("code", "?"),
        "era": flat["era"],
        "whip": flat["whip"],
        "season_stats_status": season_stats_status,
        "hr9": flat["hr9"],
        "hr_allowed": flat["hr_allowed"],
        "k_rate": flat.get("k_rate", 0.0),
        "k9": flat.get("k9", 0.0),
        "babip": safe_float(psc.get("babip_statcast"), flat.get("babip", 0.300)),
        "weak_side": split_meta.get("weak_side", ""),
        "fb_rate": safe_float(psc.get("statcast_fb_rate"), 0.34 + 0.08 * minmax_norm(flat["hr9"], 0.6, 1.8)),
        "statcast_bbe": safe_int(psc.get("statcast_bbe"), 0),
        "statcast_games": safe_int(psc.get("statcast_games"), 0),
        "statcast_base_bbe": safe_int(psc.get("statcast_base_bbe"), 0),
        "statcast_base_games": safe_int(psc.get("statcast_base_games"), 0),
        "statcast_status": str(psc.get("statcast_status", "missing")),
        "ev_allowed": safe_float(psc.get("ev_allowed"), 88.5),
        "fb_velo_delta": safe_float(psc.get("fb_velo_delta"), 0.0),
        "fb_velo_status": str(psc.get("fb_velo_status", "missing")),
        "hardhit_allowed": safe_float(psc.get("hardhit_allowed"), 0.38),
        "barrel_allowed": safe_float(psc.get("barrel_allowed"), 0.07),
        "statcast_fb_rate": safe_float(psc.get("statcast_fb_rate"), 0.34),
        "gb_allowed": safe_float(psc.get("gb_allowed"), 0.42),
        "ld_allowed": safe_float(psc.get("ld_allowed"), 0.21),
        "popup_allowed": safe_float(psc.get("popup_allowed"), 0.05),
        "dist375_allowed": safe_int(psc.get("dist375_allowed"), 0),
        "dist400_allowed": safe_int(psc.get("dist400_allowed"), 0),
        "pitcher_attack_score": attack_score,
        "pitcher_attack_tag": attack_tag,
        "hr9_vs_lhb": split_meta.get("hr9_vs_lhb", flat["hr9"]),
        "hr9_vs_rhb": split_meta.get("hr9_vs_rhb", flat["hr9"]),
        "whip_vs_lhb": split_meta.get("whip_vs_lhb", flat["whip"]),
        "whip_vs_rhb": split_meta.get("whip_vs_rhb", flat["whip"]),
        "hr_vs_lhb": split_meta.get("hr_vs_lhb", 0),
        "hr_vs_rhb": split_meta.get("hr_vs_rhb", 0),
        "xbh_vs_lhb": split_meta.get("xbh_vs_lhb", 0),
        "xbh_vs_rhb": split_meta.get("xbh_vs_rhb", 0),
        "weak_side_score_lhb": split_meta.get("weak_side_score_lhb", 0.0),
        "weak_side_score_rhb": split_meta.get("weak_side_score_rhb", 0.0),
        "weak_side_gap": split_meta.get("weak_side_gap", 0.0),
        "l3_era": l3.get("era", flat["era"]),
        "l3_whip": l3.get("whip", flat["whip"]),
        "l3_hr9": l3.get("hr9", flat["hr9"]),
        "l3_starts_found": safe_int(l3.get("starts_found"), 0),
        "slug_vs_lhb": split_meta.get("slg_vs_lhb", 0.400),
        "slug_vs_rhb": split_meta.get("slg_vs_rhb", 0.400),
        "ops_vs_lhb": split_meta.get("ops_vs_lhb", 0.720),
        "ops_vs_rhb": split_meta.get("ops_vs_rhb", 0.720),
        "weak_spots": weak_spots,
        "lineup_spot_damage": spot_damage.get("spots", {}),
        "lineup_zone_damage": spot_damage.get("zones", {}),
        "situational_splits": sit_splits,
        "meatball_pct": safe_float(advanced.get("meatball_pct"), 0.070),
        "meatball_pct_vs_lhb": safe_float(advanced.get("meatball_pct_vs_lhb"), safe_float(advanced.get("meatball_pct"), 0.070)),
        "meatball_pct_vs_rhb": safe_float(advanced.get("meatball_pct_vs_rhb"), safe_float(advanced.get("meatball_pct"), 0.070)),
        "meatball_pitches_vs_lhb": safe_int(advanced.get("meatball_pitches_vs_lhb"), 0),
        "meatball_pitches_vs_rhb": safe_int(advanced.get("meatball_pitches_vs_rhb"), 0),
        "meatball_side_status": str(advanced.get("meatball_side_status", "missing")),
        "putaway_pct": safe_float(advanced.get("putaway_pct"), 0.180),
        "swstr_pct": safe_float(advanced.get("swstr_pct"), 0.110),
        "first_pitch_strike_pct": safe_float(advanced.get("first_pitch_strike_pct"), 0.600),
        "whiff_pct": safe_float(advanced.get("whiff_pct"), 0.240),
        "pullair_allowed_pct": safe_float(advanced.get("pullair_allowed_pct"), 0.220),
        "advanced_stats_sample": safe_int(advanced.get("advanced_stats_sample"), 0),
        "advanced_stats_status": str(advanced.get("advanced_stats_status", "missing")),
        "fip": extended["fip"],
        "avg_against": extended["avg_against"],
        "obp_against": extended["obp_against"],
        "slg_against": extended["slg_against"],
        "ops_against": extended["ops_against"],
        "iso_against": extended["iso_against"],
        "woba_against": extended["woba_against"],
        "tb_allowed": extended["tb_allowed"],
        "bb_allowed": extended["bb_allowed"],
        "bb_pct": extended["bb_pct"],
        # ── THE RUNNING GAME (2026-08-23) ───────────────────────────────
        # Copied one-for-one out of `extended`, and copied here rather than
        # left to a dataclass default because DATA DEFECT #3 (the note four
        # lines down) is exactly what happens when a computed field is
        # returned and never carried: every starter published the constant
        # 3.20 for BB/9 for eleven days and it looked like a rate the whole
        # time. Nine fields, nine lines, no cleverness.
        "wild_pitches": extended["wild_pitches"],
        "pickoffs": extended["pickoffs"],
        "balks": extended["balks"],
        "sb_against": extended["sb_against"],
        "cs_against": extended["cs_against"],
        "sb_attempts_against": extended["sb_attempts_against"],
        "cs_rate_against": extended["cs_rate_against"],
        "wp9": extended["wp9"],
        "pickoff_rate": extended["pickoff_rate"],
        "running_game_status": extended["running_game_status"],
        # DATA DEFECT #3 FIX (2026-08-23). compute_pitcher_extended_stats()
        # has computed a real BB/9 since 2026-08-12 and returned it in this
        # very dict -- but this kwargs block never copied it, so
        # PitcherSummary.bb9 stayed at its dataclass default and every
        # published starter carried the constant 3.20 (30 of 30 on the
        # 2026-08-22 slate; bb_pct, copied one line up, had 24 distinct
        # values from the same walk counts). A field that looks like a rate
        # and is a constant will be built on again -- this is the one line
        # that was missing.
        "bb9": extended["bb9"],
        "barrels_allowed_count": extended["barrels_allowed_count"],
        "hr_fb_pct": extended["hr_fb_pct"],
        "extended_stats_status": extended["extended_stats_status"],
        "trend_direction": str(psc.get("trend_direction", "unknown")),
        "trend_reason": str(psc.get("trend_reason", "")),
    }
    clean_payload = {k: payload[k] for k in PitcherSummary.__dataclass_fields__.keys() if k in payload}
    db.set(key, clean_payload)
    return PitcherSummary(**clean_payload)


def build_batter_vs_pitcher_profile(db: CacheDB, batter_id: int, pitcher_id: int, end_date: dt.date) -> Dict[str, Any]:
    """Batter vs probable pitcher from Statcast pitch-level events.

    Info-only layer for the website and a tiny score bump when sample is real.
    Extended (per audit, 2026-07-07 request) with BABIP/wOBA/ISO/OBP/K%/BB%/
    barrels/hard-hit-count against this specific pitcher -- all computed from
    the SAME `vs` Statcast slice already being pulled here, so no new fetch.
    wOBA uses standard fixed linear weights (not exact year-specific
    constants) -- same approximation caveat as the pitcher-side wOBA.
    """
    key = f"bvp_v2_extended:{SEASON}:{batter_id}:{pitcher_id}:{end_date.isoformat()}"
    defaults = {
        "pa": 0, "ab": 0, "hits": 0, "hr": 0, "xbh": 0,
        "avg": 0.0, "ops": 0.0, "note": "No BvP sample", "status": "missing",
        "babip": 0.300, "woba": 0.320, "iso": 0.150, "obp": 0.320,
        "k_pct": 0.220, "bb_pct": 0.080, "barrels": 0, "hard_hit": 0,
    }
    cached = db.get(key, max_age_days=2)
    if isinstance(cached, dict):
        out = dict(defaults)
        out.update(cached)
        return out
    if statcast_batter is None or not batter_id or not pitcher_id:
        out = dict(defaults)
        out["status"] = "pybaseball_missing"
        db.set(key, out)
        return out
    try:
        start_date = max(SEASON_START, end_date - dt.timedelta(days=730))
        df = statcast_batter(start_date.isoformat(), end_date.isoformat(), batter_id)
        if df is None or len(df) == 0 or "pitcher" not in df.columns:
            out = dict(defaults)
            out["status"] = "empty"
            db.set(key, out)
            return out
        df = df.copy()
        df["pitcher"] = pd.to_numeric(df.get("pitcher"), errors="coerce").fillna(0).astype(int)
        vs = df[df["pitcher"] == int(pitcher_id)].copy()
        if len(vs) == 0:
            out = dict(defaults)
            out["status"] = "empty_vs_pitcher"
            db.set(key, out)
            return out
        events = vs["events"].fillna("") if "events" in vs.columns else []
        pa_events = [str(e) for e in events if str(e) not in {"", "nan", "None"}]
        hits = sum(1 for e in pa_events if e in {"single", "double", "triple", "home_run"})
        doubles = sum(1 for e in pa_events if e == "double")
        triples = sum(1 for e in pa_events if e == "triple")
        hrs = sum(1 for e in pa_events if e == "home_run")
        bb_only = sum(1 for e in pa_events if e == "walk")
        ibb = sum(1 for e in pa_events if e == "intent_walk")
        walks = bb_only + ibb
        hbp = sum(1 for e in pa_events if e == "hit_by_pitch")
        sf = sum(1 for e in pa_events if e == "sac_fly")
        strikeouts = sum(1 for e in pa_events if e in {"strikeout", "strikeout_double_play"})
        ab = sum(1 for e in pa_events if e not in {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf"})
        pa = len(pa_events)
        singles = max(0, hits - doubles - triples - hrs)
        tb = singles + 2*doubles + 3*triples + 4*hrs
        avg = hits / ab if ab else 0.0
        obp = (hits + walks + hbp) / max(1, ab + walks + hbp + sf)
        slg = tb / ab if ab else 0.0
        ops = obp + slg
        iso = max(0.0, slg - avg)

        babip_denom = max(1, ab - strikeouts - hrs + sf)
        babip = (hits - hrs) / babip_denom if babip_denom else 0.300

        woba_denom = max(1, ab + walks - ibb + sf + hbp)
        woba = (
            0.690 * bb_only + 0.722 * hbp + 0.888 * singles + 1.271 * doubles + 1.616 * triples + 2.101 * hrs
        ) / woba_denom if woba_denom else 0.320

        k_pct = strikeouts / pa if pa else 0.220
        bb_pct = walks / pa if pa else 0.080

        bbe = vs[vs.get("type") == "X"].copy() if "type" in vs.columns else vs.iloc[0:0].copy()
        if len(bbe):
            bbe["launch_speed"] = pd.to_numeric(bbe.get("launch_speed"), errors="coerce")
            bbe["launch_angle"] = pd.to_numeric(bbe.get("launch_angle"), errors="coerce")
            barrels = int(((bbe["launch_speed"] >= 98) & (bbe["launch_angle"] >= 24) & (bbe["launch_angle"] <= 32)).fillna(False).sum())
            hard_hit = int((bbe["launch_speed"] >= 95).fillna(False).sum())
        else:
            barrels = 0
            hard_hit = 0

        if pa >= 10:
            note = "BvP usable sample"
        elif pa >= 4:
            note = "Small BvP sample"
        elif pa > 0:
            note = "Tiny BvP sample"
        else:
            note = "No BvP sample"
        out = {
            "pa": int(pa), "ab": int(ab), "hits": int(hits), "hr": int(hrs),
            "xbh": int(doubles + triples + hrs), "avg": round(avg, 3), "ops": round(ops, 3),
            "note": note, "status": "ok" if pa else "empty_vs_pitcher",
            "babip": round(babip, 3), "woba": round(woba, 3), "iso": round(iso, 3),
            "obp": round(obp, 3), "k_pct": round(k_pct, 3), "bb_pct": round(bb_pct, 3),
            "barrels": barrels, "hard_hit": hard_hit,
        }
    except Exception as exc:
        out = dict(defaults)
        out["status"] = f"error:{type(exc).__name__}"
    db.set(key, out)
    return out


def build_team_bullpen_profile(client: MLBClient, db: CacheDB, team_id: int, team_abbr: str, probable_starter_id: int, end_date: dt.date, batter_side: str = "") -> Dict[str, Any]:
    """Lightweight active-bullpen approximation for dashboard and scoring.

    MLB public API does not provide a clean probable bullpen file, so this uses active pitchers
    other than the probable starter and averages their season rates. It is intentionally a
    small layer, not the full model driver.

    NEW (2026-06-27): adds bullpen pitch-mix, both per-reliever and team-
    blended. Previously this function had zero pitch-type or handedness
    awareness at all -- just team-wide ERA/WHIP/HR9. Reuses
    build_pitcher_pitch_mix() per reliever (same function already used for
    starters) and aggregates into an IP-weighted team arsenal, with the same
    batter_side scoping starters already get.
    """
    side_key = batter_side.upper() if batter_side and batter_side.upper() in ("L", "R") else "ALL"
    key = f"bullpen_profile_v2_pitchmix:{SEASON}:{team_id}:{probable_starter_id}:{end_date.isoformat()}:{side_key}"
    defaults = {
        "era": 4.20, "whip": 1.30, "hr9": 1.10, "quality": "average", "attack_score": 50.0,
        "sample_pitchers": 0,
        "relievers": [],          # per-reliever: {pitcher_id, name, ip, mix, sample_pitches}
        "team_mix": {},           # IP-weighted blended arsenal across the whole bullpen
        "team_mix_sample_pitches": 0,
        "team_mix_status": "missing",
    }
    cached = db.get(key, max_age_days=1)
    if isinstance(cached, dict):
        out = dict(defaults)
        out.update(cached)
        return out
    if not team_id:
        return dict(defaults)
    rows = []
    relievers_out = []
    try:
        roster = client.team_roster(team_id)
        for entry in roster.get("roster", []):
            person = entry.get("person") or {}
            pos = ((entry.get("position") or {}).get("abbreviation") or "").upper()
            pid = safe_int(person.get("id"), 0)
            if not pid or pid == probable_starter_id or pos not in {"P", "TWP"}:
                continue
            try:
                sblob = client.person_stats(pid, group="pitching", stat_type="season")
                stats_list = sblob.get("stats") or []
                first = stats_list[0] if stats_list else {}
                splits = first.get("splits") or []
                stat = (splits[0].get("stat") if splits else {}) or {}
                # BUGFIX: was safe_float(inningsPitched) -- same IP notation
                # bug as parse_pitcher_handed_splits. Small impact here (affects
                # relative IP-weighting between relievers in the blended bullpen
                # ERA/WHIP/HR9) but same root cause, same fix.
                ip = max(0.0, _parse_ip_to_float(stat.get("inningsPitched")))
                # Avoid tiny samples unless the team has nothing else.
                if ip < 3:
                    continue
                rows.append({
                    "era": safe_float(stat.get("era"), 4.20),
                    "whip": safe_float(stat.get("whip"), 1.30),
                    "hr9": safe_float(stat.get("homeRunsPer9"), 1.10),
                    "ip": ip,
                })
                # Per-reliever pitch mix, same handedness scoping as starters.
                try:
                    r_mix = build_pitcher_pitch_mix(db, pid, end_date, batter_side)
                except Exception:
                    r_mix = {"mix": {}, "sample_pitches": 0}
                relievers_out.append({
                    "pitcher_id": pid,
                    # `clean` does not exist in this module — it was a
                    # NameError swallowed by the enclosing `except Exception:
                    # continue`, which meant EVERY reliever was silently
                    # dropped from the bullpen mix since 2026-07-25. Found by
                    # pyflakes while hunting today's crash.
                    "name": str(person.get("fullName") or ""),
                    "ip": ip,
                    "mix": r_mix.get("mix", {}),
                    "sample_pitches": safe_int(r_mix.get("sample_pitches"), 0),
                })
            except Exception:
                continue
    except Exception:
        rows = []
    if not rows:
        out = dict(defaults)
        db.set(key, out)
        return out
    weight_sum = sum(max(1.0, r["ip"]) for r in rows)
    era = sum(r["era"] * max(1.0, r["ip"]) for r in rows) / weight_sum
    whip = sum(r["whip"] * max(1.0, r["ip"]) for r in rows) / weight_sum
    hr9 = sum(r["hr9"] * max(1.0, r["ip"]) for r in rows) / weight_sum

    # Team-blended pitch mix: IP-weighted average across all relievers with
    # a usable pitch-mix sample. A reliever with no Statcast data just
    # doesn't contribute to the blend (rather than dragging it toward zeros).
    pitch_totals: Dict[str, float] = {}
    mix_weight_sum = 0.0
    team_mix_sample = 0
    for r in relievers_out:
        if not r["mix"] or r["sample_pitches"] < 20:
            continue
        w = max(1.0, r["ip"])
        mix_weight_sum += w
        team_mix_sample += r["sample_pitches"]
        for pt, pct in r["mix"].items():
            pitch_totals[pt] = pitch_totals.get(pt, 0.0) + safe_float(pct, 0.0) * w
    team_mix = {pt: round(total / mix_weight_sum, 1) for pt, total in pitch_totals.items()} if mix_weight_sum > 0 else {}
    team_mix_status = "ok" if team_mix else "no_reliever_pitch_data"

    attack = 100 * (
        0.38 * minmax_norm(era, 3.20, 5.80) +
        0.34 * minmax_norm(whip, 1.05, 1.55) +
        0.28 * minmax_norm(hr9, 0.70, 1.70)
    )
    if attack >= 67:
        quality = "weak"
    elif attack <= 38:
        quality = "strong"
    else:
        quality = "average"
    out = {
        "era": round(era, 2), "whip": round(whip, 2), "hr9": round(hr9, 2),
        "quality": quality, "attack_score": round(attack, 1), "sample_pitchers": len(rows),
        "relievers": relievers_out,
        "team_mix": team_mix,
        "team_mix_sample_pitches": team_mix_sample,
        "team_mix_status": team_mix_status,
    }
    db.set(key, out)
    return out




def hrw_zone_score_value(score: float) -> float:
    """Curved HRW timing score. The graded sample favored 55-70 more than extreme 80+."""
    s = safe_float(score, 0.0)
    if 55.0 <= s <= 70.0:
        return 78.0 - abs(s - 62.5) * 0.45
    if 70.0 < s <= 80.0:
        return 70.0
    if s > 80.0:
        return 64.0
    if 45.0 <= s < 55.0:
        return 55.0
    return 38.0


def hrw_zone_label(score: float) -> str:
    s = safe_float(score, 0.0)
    if 55.0 <= s <= 70.0:
        return "sweet_spot"
    if 70.0 < s <= 80.0:
        return "strong_capped"
    if s > 80.0:
        return "volatile_hot"
    if 45.0 <= s < 55.0:
        return "watch"
    return "cold"


def capped_pitcher_hr9_score(hr9: float) -> float:
    """HR/9 sweet spot layer: useful above 1.0, but capped so extreme HR/9 doesn't overboost."""
    h = safe_float(hr9, 1.10)
    if h < 0.70:
        return 35.0
    if h < 1.00:
        return 50.0 + 20.0 * minmax_norm(h, 0.70, 1.00)
    if h <= 1.35:
        return 72.0 + 12.0 * minmax_norm(h, 1.00, 1.35)
    if h <= 1.60:
        return 80.0
    return 76.0


def pmix_gate_label(score: float, sample: int = 0) -> str:
    p = safe_float(score, 50.0)
    n = safe_int(sample, 0)
    if n and n < 20:
        return "low_sample"
    if p >= 80:
        return "elite"
    if p >= 70:
        return "pass"
    if p >= 60:
        return "neutral"
    if p >= 50:
        return "risk"
    return "trap"


def pmix_gate_multiplier(score: float, sample: int = 0) -> float:
    gate = pmix_gate_label(score, sample)
    if gate == "elite":
        return 1.08
    if gate == "pass":
        return 1.06
    if gate == "neutral":
        return 1.00
    if gate == "risk":
        return 0.94
    if gate == "trap":
        return 0.88
    return 0.97


def lineup_spot_risk_label(spot: int) -> str:
    s = safe_int(spot, 0)
    if 1 <= s <= 4:
        return "clean"
    if s == 5:
        return "neutral"
    if s in (6, 7):
        return "prove_it"
    if s >= 8:
        return "deep_lineup"
    return "unknown"


def soft_lineup_opportunity_multiplier(rec: "HitterRecord") -> float:
    """Soft opportunity flag only. Do not hard-ban power bats in lower spots."""
    spot = safe_int(getattr(rec, "lineup_spot", 0), 0)
    strong_override = (
        safe_int(getattr(rec, "last5_hr", 0), 0) >= 2
        or safe_float(getattr(rec, "pitch_mix_score", 50.0), 50.0) >= 70.0
        or safe_int(getattr(rec, "recent_375_num", 0), 0) >= 3
        or safe_float(getattr(rec, "season_iso", 0.0), 0.0) >= 0.250
    )
    if 1 <= spot <= 4:
        return 1.025
    if spot == 5:
        return 1.00
    if spot in (6, 7):
        return 0.99 if strong_override else 0.96
    if spot >= 8:
        return 0.98 if strong_override else 0.94
    return 1.0


def top_board_bucket_for(rec: "HitterRecord") -> str:
    if getattr(rec, "true_avoid_hr", False):
        return "True Avoid HR"
    if getattr(rec, "power_watch_flag", False):
        return "Power Watch"
    if safe_float(getattr(rec, "damage_conversion_score", 0.0), 0.0) >= 68.0:
        return "Damage Conversion"
    if getattr(rec, "high_confidence_hr_flag", False):
        return "High Confidence HR"
    if safe_float(getattr(rec, "pitch_mix_score", 50.0), 50.0) >= 70.0 and safe_int(getattr(rec, "last5_hr", 0), 0) >= 1:
        return "PMix + Hot Form"
    if safe_float(getattr(rec, "alt_hr_score", 0.0), 0.0) >= 58.0 and safe_float(getattr(rec, "hr_score_v2", 0.0), 0.0) < 45.0:
        return "Alt Power"
    if getattr(rec, "trap_risk_flag", False):
        return "Trap Risk"
    return "Pure HR"


def top_board_tags_for(rec: "HitterRecord") -> List[str]:
    tags: List[str] = []
    pmix = safe_float(getattr(rec, "pitch_mix_score", 50.0), 50.0)
    if pmix >= 80:
        tags.append("PMix Elite")
    elif pmix >= 70:
        tags.append("PMix Pass")
    elif pmix < 60:
        tags.append("PMix Risk")
    if safe_int(getattr(rec, "last5_hr", 0), 0) >= 3:
        tags.append("L5 HR 3+")
    elif safe_int(getattr(rec, "last5_hr", 0), 0) >= 2:
        tags.append("L5 HR 2+")
    if safe_int(getattr(rec, "recent_375_num", 0), 0) >= 3:
        tags.append("375+ Contact")
    if safe_float(getattr(rec, "hr_per_pa", 0.0), 0.0) >= 0.060:
        tags.append("HR/PA Elite")
    elif safe_float(getattr(rec, "hr_per_pa", 0.0), 0.0) >= 0.045:
        tags.append("HR/PA Strong")
    if hrw_zone_label(safe_float(getattr(rec, "hrw_score", 0.0), 0.0)) == "sweet_spot":
        tags.append("HRW Sweet Spot")
    elif hrw_zone_label(safe_float(getattr(rec, "hrw_score", 0.0), 0.0)) == "volatile_hot":
        tags.append("HRW Volatile")
    if safe_float(getattr(rec, "bullpen_attack_score", 50.0), 50.0) >= 62 or str(getattr(rec, "bullpen_quality", "")).lower() == "weak":
        tags.append("Bullpen Boost")
    if safe_float(getattr(rec, "pitcher_hardhit_allowed", 0.0), 0.0) >= 0.42 or "HARD" in str(getattr(rec, "pitcher_attack_tag", "")):
        tags.append("Hard Contact Pitcher")
    if safe_int(getattr(rec, "bvp_pa", 0), 0) >= 4:
        tags.append("BvP Note")
    dc = safe_float(getattr(rec, "damage_conversion_score", 0.0), 0.0)
    if dc >= 78:
        tags.append("DC Elite")
    elif dc >= 66:
        tags.append("Damage Fit")
    elif dc < 44:
        tags.append("Low DC")
    if getattr(rec, "power_watch_flag", False):
        tags.append("Power Watch")
    if getattr(rec, "true_avoid_hr", False):
        tags.append("True Avoid")
    if getattr(rec, "trap_risk_flag", False) and not getattr(rec, "power_watch_flag", False):
        tags.append("Trap Risk")
    return list(dict.fromkeys(tags))[:6]


def _hr2_clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, safe_float(value, 0.0)))


def _spot_damage_for_batter(h: "HitterRecord") -> Dict[str, float]:
    """
    Pull the pitcher's damage-allowed stats for THIS batter's actual lineup spot.
    The pitcher profile already stores per-spot slg/iso/hr_rate/xbh_rate/damage_score
    (built in build_pitcher_lineup_spot_damage). Returns normalized 0-1 values that
    can be folded into any score. Falls back to neutral if no sample.
    """
    spot = safe_int(getattr(h, "lineup_spot", 0), 0)
    spot_map = getattr(h, "pitcher_lineup_spot_damage", {}) or {}
    cell = spot_map.get(str(spot)) or spot_map.get(spot) or {}
    pa = safe_int(cell.get("pa"), 0)
    if pa < 4:
        # not enough sample at this exact spot — neutral
        return {"damage": 0.5, "slg": 0.5, "xbh": 0.5, "hr": 0.5, "weight": 0.4}
    damage = minmax_norm(safe_float(cell.get("damage_score"), 50.0), 35.0, 80.0)
    slg    = minmax_norm(safe_float(cell.get("slg"), 0.400), 0.330, 0.620)
    xbh    = minmax_norm(safe_float(cell.get("xbh_rate"), 0.08), 0.035, 0.250)
    hr     = minmax_norm(safe_float(cell.get("hr_rate"), 0.03), 0.000, 0.080)
    # weight rises with sample size — full trust at 24+ PA
    weight = 0.4 + 0.6 * min(1.0, pa / 24.0)
    return {"damage": damage, "slg": slg, "xbh": xbh, "hr": hr, "weight": weight}


def _hr2_score_launch_angle(avg_la: float) -> float:
    la = safe_float(avg_la, 0.0)
    if 24 <= la <= 32:
        return 100.0
    if 20 <= la < 24 or 32 < la <= 34:
        return 86.0
    if 18 <= la < 20 or 34 < la <= 38:
        return 68.0
    if 10 <= la < 18 or 38 < la <= 42:
        return 42.0
    if la < 5:
        return 12.0
    return 28.0


def _hr2_lineup_multiplier(rec: "HitterRecord", raw_shape_score: float) -> float:
    spot = safe_int(getattr(rec, "lineup_spot", 0), 0)
    if 1 <= spot <= 4:
        mult = 1.00
    elif spot == 5:
        mult = 0.90
    elif spot in (6, 7):
        mult = 0.75
    elif spot in (8, 9):
        mult = 0.55
    else:
        mult = 0.65
    strong_override = raw_shape_score >= 60 or safe_int(getattr(rec, "recent_375_num", 0), 0) >= 2 or safe_int(getattr(rec, "last5_hr", 0), 0) >= 2
    if strong_override:
        mult = max(mult, 0.80)
    if not bool(getattr(rec, "lineup_confirmed", False)):
        mult *= 0.88
    return mult


def _hr2_confidence(score: float) -> str:
    if score >= 68:
        return "Elite"
    if score >= 55:
        return "Strong"
    if score >= 42:
        return "Moderate"
    return "Weak"


def _hr2_first_reasons(rec: "HitterRecord", bbe: Dict[str, Any], pitch_fit: float, trap_reason: str, hidden: bool) -> List[str]:
    reasons: List[str] = []
    d375 = safe_int(bbe.get("dist_375_plus"), safe_int(getattr(rec, "recent_375_num", 0), 0))
    d400 = safe_int(bbe.get("dist_400_plus"), 0)
    air_pull = safe_float(bbe.get("air_pull_rate"), safe_float((getattr(rec, "park_fit", {}) or {}).get("pull_air_rate"), 0.0))
    fb = safe_float(bbe.get("fb_rate"), safe_float(getattr(rec, "recent_fb_rate", 0.0), 0.0))
    barrel = safe_float(bbe.get("barrel_rate"), safe_float(getattr(rec, "recent_barrel_rate", 0.0), 0.0))
    avg_la = safe_float(bbe.get("avg_la"), 0.0)
    max_ev = safe_float(bbe.get("max_ev"), safe_float(getattr(rec, "recent_ev", 0.0), 0.0))
    max_dist = safe_float(bbe.get("max_distance"), 0.0)
    if hidden:
        reasons.append("Underrated play — pitch matchup and contact quality say more than his name suggests")
    # 2026-08-12 (Donovan: "repeaded words ... it needs to be intentional and
    # useful"): every branch below used to append flat tier text with nothing
    # tying it to the player who earned it — two guys who both cleared "barrel
    # >= 0.12" read as the identical sentence. d400 was worse: an f-string
    # with no variable in it at all. Every number here (max_dist, pitch_fit,
    # air_pull, barrel, max_ev/avg_la, pitcher_hr9) was already sitting in
    # scope; this just uses it, so the SAME gate now reads differently per
    # player instead of copy-pasting.
    if d400 >= 1:
        reasons.append(f"Reached {max_dist:.0f} feet recently — true leave-yard power" if max_dist >= 400
                        else f"Hit {d400} ball{'s' if d400 != 1 else ''} 400+ feet recently — true leave-yard power")
    elif d375 >= 2:
        reasons.append(f"Hit {d375} balls 375+ feet recently — legitimate power")
    if pitch_fit >= 70:
        reasons.append(f"His best damage pitch matches what this pitcher throws most (fit {pitch_fit:.0f})")
    elif pitch_fit >= 60:
        reasons.append(f"The pitch mix gives him a playable power lane (fit {pitch_fit:.0f})")
    if air_pull >= 0.40:
        reasons.append(f"Pulls the ball in the air {air_pull*100:.0f}% of the time — good HR shape")
    elif air_pull >= 0.28 or fb >= 0.35:
        reasons.append(f"Getting enough balls in the air for a HR path ({air_pull*100:.0f}% air pull)")
    if barrel >= 0.12:
        reasons.append(f"Barreling the ball at an elite rate right now ({barrel*100:.0f}%)")
    if max_ev >= 97 and 20 <= avg_la <= 35:
        reasons.append(f"Exit velocity and launch angle are both in the ideal home run window ({max_ev:.0f} mph, {avg_la:.0f}°)")
    if max_dist >= 390 and len(reasons) < 3:
        reasons.append(f"Recent distance ceiling is already near home run range ({max_dist:.0f} ft)")
    if safe_float(getattr(rec, "pitcher_hr9", 0.0), 0.0) >= 1.40 and len(reasons) < 3:
        reasons.append(f"Facing a pitcher giving up home runs at a high rate ({safe_float(getattr(rec, 'pitcher_hr9', 0.0), 0.0):.2f} HR/9)")
    if safe_int(getattr(rec, "last10_hr", 0), 0) >= 2 and len(reasons) < 3:
        reasons.append(f"Hot home run form right now — hit {getattr(rec, 'last10_hr', 0)} HRs in last 10 games")
    if trap_reason and len(reasons) < 3:
        reasons.append(trap_reason)
    # Padding for thin-signal players was 3 flat strings picked by list
    # position only — nothing about them referenced the player, so every
    # thin-signal player on a slate read identically. hr_score_v2 is already
    # set on rec by the time this runs (assigned right before this call), and
    # pitch_fit is already a param here, so the two most-used filler slots can
    # at least state the real number instead of a bare adjective. The third
    # slot is left as a plain closer — it only fires alongside two already-
    # specific lines, so it reads as a wrap-up rather than the repeated text.
    score = safe_float(getattr(rec, "hr_score_v2", 0.0), 0.0)
    while len(reasons) < 3:
        if len(reasons) == 0:
            reasons.append(f"Power path is playable at a {score:.0f} score, but not fully confirmed")
        elif len(reasons) == 1:
            reasons.append(f"Pitch-mix fit is {pitch_fit:.0f} — check the risk warning before using him for a HR slip")
        else:
            reasons.append("Better as a smaller exposure unless the full shape confirms")
    return reasons[:3]


def _hr2_best_bet_and_label(rec: "HitterRecord", score: float, pitch_fit: float, trap: bool, hidden: bool, strong_confirmed: bool) -> Tuple[str, str]:
    if trap:
        return "Avoid for HR", "Be Careful"
    if hidden:
        return "HR", "Hidden HR Value"

    iso         = safe_float(getattr(rec, "season_iso", 0.0), 0.0)
    bbe         = getattr(rec, "bbe_profile", {}) or {}
    barrel_rate = safe_float(bbe.get("barrel_rate"), safe_float(getattr(rec, "recent_barrel_rate", 0.0), 0.0))
    avg_ev      = safe_float(bbe.get("avg_ev"), safe_float(getattr(rec, "recent_ev", 0.0), 0.0))
    ideal_hr    = safe_float(getattr(rec, "recent_ideal_hr_contact", 0.0), 0.0)
    cs          = bool(getattr(rec, "hrw_score", 0) < 55 and avg_ev >= 92 and barrel_rate >= 0.06)
    gate_signals = sum([
        iso >= 0.180,
        getattr(rec, "hrw_score", 0) >= 70 or cs,
        getattr(rec, "last5_hr", 0) >= 1 or getattr(rec, "last10_hr", 0) >= 3,
        safe_float(getattr(rec, "batted_ball_power_score", 0.0), 0.0) >= 80,
        ideal_hr >= 0.15 or barrel_rate >= 0.08,
    ])
    hrr = safe_float(getattr(rec, "hrr_score_v2", getattr(rec, "hrr_score", 0.0)), 0.0)

    # Strong HR Look: needs ISO floor + full signal stack + confirmed shape
    # Data shows it was being over-awarded without ISO — now requires it
    if score >= 62 and pitch_fit >= 62 and strong_confirmed and iso >= 0.180 and gate_signals >= 3:
        return "HR", "Strong HR Look"
    if score >= 58 and gate_signals >= 3 and iso >= 0.180:
        return "HR", "HR Look"
    if score >= 55 and gate_signals >= 2:
        return "HR or HRR", "Safer Production Play"
    if score >= 52 and gate_signals == 1:
        # Power floor present, just no recency — Power Watch not full avoid.
        # (Previously had a dead iso>=0.200 sub-branch here with identical
        # return value -- removed audit 2026-06-29, it was fully subsumed.)
        return "HRR + HR Sprinkle", "Power Watch"
    if score >= 42 and gate_signals == 0 and iso < 0.160:
        return "HRR / XBH", "Production Only"
    if 45 <= score <= 61 and hrr >= 60:
        return "HR or HRR", "Safer Production Play"
    if 42 <= score <= 54 and pitch_fit < 55 and iso >= 0.200:
        return "HR", "Longshot HR Only"
    if score < 45 and hrr >= 62:
        return "HRR / Hits", "Better for HRR"
    if score >= 55:
        return "HR or HRR", "Safer Production Play"
    return "HRR / Hits", "Better for HRR"


def _v31_best_pitch_detail(h: HitterRecord) -> Dict[str, Any]:
    """Find the strongest hitter damage pitch that also matters in today's pitcher mix."""
    pm = getattr(h, "pitch_mix_matchup", {}) or {}
    best_name = str(pm.get("best_damage_pitch") or "").strip()
    pitcher_mix = pm.get("pitcher_mix", {}) or (getattr(h, "pitcher_pitch_mix", {}) or {}).get("mix", {}) or {}
    by_pitch = pm.get("batter_by_pitch", {}) or (getattr(h, "batter_pitch_type_profile", {}) or {}).get("by_pitch", {}) or {}

    # Normalize pitcher mix values to 0-1 usage.
    mix_norm: Dict[str, float] = {}
    for k, v in (pitcher_mix or {}).items():
        usage = safe_float(v, 0.0)
        if usage > 1.0:
            usage = usage / 100.0
        mix_norm[str(k)] = max(0.0, min(1.0, usage))

    candidates: List[Tuple[float, str, Dict[str, Any], float]] = []
    for pitch_name, data in (by_pitch or {}).items():
        if not isinstance(data, dict):
            continue
        usage = mix_norm.get(str(pitch_name), safe_float(data.get("usage"), 0.0))
        if usage > 1.0:
            usage = usage / 100.0
        slg = safe_float(data.get("slg"), 0.0)
        iso = safe_float(data.get("iso"), max(0.0, slg - safe_float(data.get("ba"), 0.0)))
        barrel = safe_float(data.get("barrel_rate", data.get("barrel", 0.0)), 0.0)
        hh = safe_float(data.get("hard_hit_rate", data.get("hh", 0.0)), 0.0)
        ev = safe_float(data.get("ev", data.get("avg_ev", 0.0)), 0.0)
        la = safe_float(data.get("la", data.get("avg_la", 0.0)), 0.0)
        pitch_score = (
            34 * minmax_norm(iso, 0.060, 0.420) +
            22 * minmax_norm(slg, 0.300, 0.850) +
            16 * minmax_norm(barrel, 0.04, 0.22) +
            12 * minmax_norm(hh, 0.30, 0.65) +
            10 * minmax_norm(ev, 86, 98) +
            6 * (1.0 if 16 <= la <= 35 else minmax_norm(la, 8, 28))
        )
        # Only boost damage pitches the pitcher actually uses.
        usage_mult = 0.55 + 0.45 * minmax_norm(usage, 0.08, 0.32)
        candidates.append((pitch_score * usage_mult, str(pitch_name), data, usage))

    if candidates:
        candidates.sort(reverse=True, key=lambda x: x[0])
        score, name, data, usage = candidates[0]
    else:
        name = best_name
        data = {}
        usage = safe_float(pm.get("best_damage_usage"), 0.0)
        if usage > 1.0:
            usage = usage / 100.0
        score = safe_float(getattr(h, "pitch_mix_score", 50.0), 50.0)

    return {"name": name, "data": data, "usage": max(0.0, min(1.0, usage)), "score": round(score, 1)}


def compute_damage_conversion_v31(h: HitterRecord) -> HitterRecord:
    """V31 core layer: can this hitter convert today's pitcher mistake into HR-shaped damage?"""
    bbe = getattr(h, "bbe_profile", {}) or {}
    park_fit = getattr(h, "park_fit", {}) or {}
    pm = getattr(h, "pitch_mix_matchup", {}) or {}
    best = _v31_best_pitch_detail(h)
    pitch_data = best.get("data", {}) or {}

    pitch_fit = safe_float(getattr(h, "pitch_mix_score", 50.0), 50.0)
    pitch_usage = safe_float(best.get("usage"), 0.0)
    best_pitch = str(best.get("name") or pm.get("best_damage_pitch") or "").strip()

    d350 = safe_int(bbe.get("dist_350_plus"), safe_int(getattr(h, "recent_350_num", 0), 0))
    d375 = safe_int(bbe.get("dist_375_plus"), safe_int(getattr(h, "recent_375_num", 0), 0))
    d400 = safe_int(bbe.get("dist_400_plus"), 0)
    tracked = max(1, safe_int(bbe.get("sample_bbe"), safe_int(getattr(h, "recent_350_den", 1), 1)))
    avg_ev = safe_float(bbe.get("avg_ev"), safe_float(getattr(h, "recent_ev", 0.0), 0.0))
    max_ev = safe_float(bbe.get("max_ev"), avg_ev)
    max_dist = safe_float(bbe.get("max_distance"), 0.0)
    avg_la = safe_float(bbe.get("avg_la"), 0.0)
    barrel = safe_float(bbe.get("barrel_rate"), safe_float(getattr(h, "recent_barrel_rate", 0.0), 0.0))
    hard_hit = safe_float(bbe.get("hard_hit_rate"), safe_float(getattr(h, "recent_hard_hit_rate", 0.0), 0.0))
    air_pull = safe_float(bbe.get("air_pull_rate"), safe_float(park_fit.get("pull_air_rate"), 0.0))
    gb_rate = safe_float(bbe.get("gb_rate"), 0.0)
    fb_rate = safe_float(bbe.get("fb_rate"), safe_float(getattr(h, "recent_fb_rate", 0.0), 0.0))

    pitch_iso = safe_float(pitch_data.get("iso"), 0.0)
    pitch_slg = safe_float(pitch_data.get("slg"), 0.0)
    pitch_barrel = safe_float(pitch_data.get("barrel_rate", pitch_data.get("barrel", 0.0)), 0.0)
    pitch_hh = safe_float(pitch_data.get("hard_hit_rate", pitch_data.get("hh", 0.0)), 0.0)
    pitch_ev = safe_float(pitch_data.get("ev", pitch_data.get("avg_ev", 0.0)), 0.0)
    pitch_la = safe_float(pitch_data.get("la", pitch_data.get("avg_la", 0.0)), 0.0)

    hitter_pitch_damage = (
        30 * minmax_norm(pitch_iso or max(getattr(h, "season_iso", 0.0), getattr(h, "iso_vs_lhp", 0.0), getattr(h, "iso_vs_rhp", 0.0)), 0.080, 0.420) +
        20 * minmax_norm(pitch_slg or getattr(h, "season_slg", 0.0), 0.320, 0.850) +
        18 * minmax_norm(max(pitch_barrel, barrel), 0.03, 0.20) +
        14 * minmax_norm(max(pitch_hh, hard_hit), 0.28, 0.65) +
        10 * minmax_norm(max(pitch_ev, avg_ev), 86.0, 99.0) +
        8 * (1.0 if 16 <= (pitch_la or avg_la) <= 35 else minmax_norm((pitch_la or avg_la), 8, 28))
    )

    recent_shape = (
        24 * minmax_norm(d375 / tracked, 0.02, 0.22) +
        15 * minmax_norm(d400, 0, 2) +
        18 * minmax_norm(max_ev, 94, 111) +
        14 * (1.0 if 18 <= avg_la <= 35 else minmax_norm(avg_la, 8, 28)) +
        12 * minmax_norm(air_pull, 0.10, 0.42) +
        10 * minmax_norm(fb_rate, 0.22, 0.52) +
        7 * minmax_norm(barrel, 0.03, 0.18)
    )

    pitcher_mistake = (
        26 * minmax_norm(getattr(h, "pitcher_barrel_allowed", 0.0), 0.03, 0.13) +
        22 * minmax_norm(getattr(h, "pitcher_hardhit_allowed", 0.0), 0.30, 0.52) +
        15 * minmax_norm(getattr(h, "pitcher_ev_allowed", 0.0), 86.0, 92.8) +
        12 * minmax_norm(getattr(h, "pitcher_statcast_fb_rate", getattr(h, "pitcher_fb_rate", 0.0)), 0.26, 0.48) +
        12 * minmax_norm(getattr(h, "pitcher_hr9", 0.0), 0.70, 2.00) +
        8 * minmax_norm(getattr(h, "pitcher_375_allowed", 0), 0, 12) +
        5 * minmax_norm(getattr(h, "pitcher_400_allowed", 0), 0, 5)
    )
    # Missing pitcher Statcast should be neutral-ish, not fake strong.
    if safe_int(getattr(h, "pitcher_statcast_bbe", 0), 0) <= 0 or str(getattr(h, "pitcher_statcast_status", "")).lower() != "ok":
        pitcher_mistake = 55 * minmax_norm(getattr(h, "pitcher_hr9", 0.0), 0.70, 2.00) + 45 * minmax_norm(getattr(h, "pitcher_fb_rate", 0.0), 0.26, 0.48)

    usage_score = 100 * minmax_norm(pitch_usage, 0.08, 0.32)
    overlap = 0.55 * pitch_fit + 0.25 * usage_score + 0.20 * safe_float(best.get("score"), pitch_fit)
    park_weather = safe_float(park_fit.get("score"), 50.0)
    if park_weather <= 0:
        park_weather = 50.0

    damage_conversion = (
        0.30 * hitter_pitch_damage +
        0.24 * recent_shape +
        0.22 * pitcher_mistake +
        0.14 * overlap +
        0.10 * park_weather
    )

    # Ground-ball and K risks should trim, not auto-kill, unless no damage path exists.
    if gb_rate >= 0.55 and d375 == 0:
        damage_conversion -= 8
    elif gb_rate >= 0.50:
        damage_conversion -= 4
    if safe_float(getattr(h, "season_k_rate", 0.0), 0.0) >= 0.34 and pitch_fit < 62:
        damage_conversion -= 5
    if tracked < 6:
        damage_conversion -= 4

    damage_conversion = round(_hr2_clip(damage_conversion), 1)

    reasons: List[str] = []
    if best_pitch:
        if pitch_usage >= 0.18:
            reasons.append(f"Damage pitch overlap: {best_pitch} ({round(pitch_usage*100):.0f}% usage)")
        else:
            reasons.append(f"Damage pitch noted: {best_pitch}, but usage is light")
    if d400 >= 1 or max_dist >= 400:
        reasons.append("Recent 400+ ft ceiling")
    elif d375 >= 2:
        reasons.append(f"{d375} recent 375+ balls")
    if max_ev >= 108:
        reasons.append("Elite max EV")
    if air_pull >= 0.28:
        reasons.append("Pull-air HR path")
    if pitcher_mistake >= 62:
        reasons.append("Pitcher allows mistake damage")
    if park_weather >= 65:
        reasons.append("Park/spray fit helps")
    if gb_rate >= 0.50:
        reasons.append("Ground-ball drag")
    if safe_float(getattr(h, "season_k_rate", 0.0), 0.0) >= 0.30:
        reasons.append("K-risk volatility")
    if not reasons:
        reasons.append("Neutral conversion profile")

    if damage_conversion >= 78:
        label = "Elite mistake punisher"
    elif damage_conversion >= 66:
        label = "Strong mistake punisher"
    elif damage_conversion >= 55:
        label = "Playable damage path"
    elif damage_conversion >= 44:
        label = "Thin damage path"
    else:
        label = "Low HR conversion"

    h.damage_conversion_score = damage_conversion
    h.damage_conversion_label = label
    h.damage_conversion_reasons = reasons[:5]
    h.best_damage_pitch_v31 = best_pitch
    h.pitcher_mistake_pitch_v31 = best_pitch if pitch_usage >= 0.12 else ""
    h.pitcher_mistake_match = bool(damage_conversion >= 55 and pitch_usage >= 0.12)
    return h


def apply_decision_engine_v31(h: HitterRecord) -> HitterRecord:
    """Final role layer: HR Bet vs Power Watch vs HRR/XBH vs True Avoid."""
    compute_damage_conversion_v31(h)
    bbe = getattr(h, "bbe_profile", {}) or {}
    park_fit = getattr(h, "park_fit", {}) or {}
    dc = safe_float(getattr(h, "damage_conversion_score", 0.0), 0.0)
    hr = safe_float(getattr(h, "hr_score_v2", getattr(h, "hr_score", 0.0)), 0.0)
    hrr = safe_float(getattr(h, "hrr_score_v2", getattr(h, "hrr_score", 0.0)), 0.0)
    pitch_fit = safe_float(getattr(h, "pitch_mix_score", 50.0), 50.0)
    d375 = safe_int(bbe.get("dist_375_plus"), safe_int(getattr(h, "recent_375_num", 0), 0))
    d400 = safe_int(bbe.get("dist_400_plus"), 0)
    max_ev = safe_float(bbe.get("max_ev"), safe_float(getattr(h, "recent_ev", 0.0), 0.0))
    barrel = safe_float(bbe.get("barrel_rate"), safe_float(getattr(h, "recent_barrel_rate", 0.0), 0.0))
    air_pull = safe_float(bbe.get("air_pull_rate"), safe_float(park_fit.get("pull_air_rate"), 0.0))
    gb_rate = safe_float(bbe.get("gb_rate"), 0.0)
    park_score = safe_float(park_fit.get("score"), 50.0)
    k_rate = safe_float(getattr(h, "season_k_rate", 0.0), 0.0)
    iso = safe_float(getattr(h, "season_iso", 0.0), 0.0)
    hrpa = safe_float(getattr(h, "hr_per_pa", 0.0), 0.0)

    raw_power = bool(iso >= 0.220 or hrpa >= 0.035 or d375 >= 2 or d400 >= 1 or max_ev >= 108 or barrel >= 0.11)

    mistake_setup = bool(getattr(h, "mistake_pitch_setup_flag", False))
    ambush_setup = bool(getattr(h, "ambush_setup_flag", False))
    pitch_match = bool(getattr(h, "pitch_type_match_flag", False))
    pitcher_hr9_high = safe_float(getattr(h, "pitcher_hr9", 1.10), 1.10) >= 1.50
    # Per-hitter, as of 2026-08-23: an arm at 7.9% overall who runs 9.6% to
    # lefties clears this gate for a lefty and does not for a righty, which is
    # the whole point of a platoon read. meatball_vs_hand() falls back to the
    # overall rate whenever the split is not real, so nothing changes for the
    # arms that have no usable split.
    pitcher_meatball_high = meatball_vs_hand(h)[0] >= 0.085

    conversion_override = bool(
        dc >= 65
        or (dc >= 58 and raw_power)
        or mistake_setup
        or (pitcher_hr9_high and pitcher_meatball_high)
        or (ambush_setup and (raw_power or iso >= 0.160))
        or pitch_match
        # ISO power floor override — .200+ bats are too dangerous to hard-avoid
        # even with bad matchup shape. They become Power Caution not True Avoid.
        or iso >= 0.200
    )

    # Tightened no_damage_path: requires BOTH weak contact shape AND weak power floor.
    # Previously iso < 0.180 could trigger with just dc < 44 + one more flag.
    # Now requires iso < 0.160 (true weak power) + stricter contact floor.
    no_damage_path = bool(
        dc < 40 and
        pitch_fit < 50 and
        d375 == 0 and d400 == 0 and
        max_ev < 102 and
        iso < 0.160
    )
    # GB/K/park condition unchanged — still need one of these to confirm true avoid
    true_avoid = bool(no_damage_path and (gb_rate >= 0.50 or k_rate >= 0.32 or park_score < 40))

    risk_flags: List[str] = []
    avoid_reasons: List[str] = []
    decision_reasons: List[str] = []
    if k_rate >= 0.30:
        risk_flags.append(f"{k_rate*100:.0f}% K rate")
    if gb_rate >= 0.50:
        risk_flags.append(f"{gb_rate*100:.0f}% GB rate")
    if pitch_fit < 55:
        risk_flags.append(f"Pitch fit only {pitch_fit:.0f}")
    if park_score < 45:
        risk_flags.append(f"Park fit only {park_score:.0f}")
    if safe_int(getattr(h, "pitch_mix_sample", 0), 0) < 12:
        risk_flags.append("Pitch sample <12")

    if conversion_override:
        if mistake_setup:
            decision_reasons.append("Pitcher mistake-setup overrides Avoid")
        elif pitcher_hr9_high and pitcher_meatball_high:
            decision_reasons.append(f"Pitcher HR/9 {safe_float(getattr(h,'pitcher_hr9',1.1),1.1):.2f} + meatball {safe_float(getattr(h,'pitcher_meatball_pct',0.07),0.07)*100:.1f}% — HR path open")
        elif ambush_setup:
            decision_reasons.append("Ambush setup + power profile overrides Avoid")
        elif pitch_match:
            decision_reasons.append(f"🎯 PITCH-MATCH: {getattr(h, 'pitch_type_match_note', '')}")
        elif dc >= 65:
            decision_reasons.append(f"Damage conversion {dc:.0f} keeps HR path alive")
        elif raw_power:
            decision_reasons.append("Raw power override")
    decision_reasons.extend(getattr(h, "damage_conversion_reasons", [])[:3])

    if true_avoid and not conversion_override:
        final_role = "⛔ True Avoid HR"
        best_use = "Avoid HR"
        # Build SPECIFIC reasons so the user knows exactly why this fired.
        specific = []
        if dc < 44:
            specific.append(f"Damage conversion only {dc:.0f}")
        if d375 == 0 and d400 == 0:
            specific.append("No balls 375+ ft recently")
        if max_ev < 104:
            specific.append(f"Max EV only {max_ev:.0f} mph")
        if iso < 0.180:
            specific.append(f"ISO only {display_avg(iso)}")
        if gb_rate >= 0.48:
            specific.append(f"{gb_rate*100:.0f}% GB rate")
        if k_rate >= 0.30:
            specific.append(f"{k_rate*100:.0f}% K rate")
        if park_score < 45:
            specific.append(f"Park fit {park_score:.0f}")
        avoid_reasons = specific[:5] if specific else ["No clear HR damage path"]
    elif hr >= 62 and dc >= 58 and (pitch_fit >= 60 or raw_power) and not (gb_rate >= 0.58 and k_rate >= 0.34):
        # Threshold validated against 7 days of graded results: damage_conversion_score's
        # actual top decile starts at ~58.4 (25%+ real HR rate), not 64 -- the old 64 bar
        # combined with an hr_score that didn't yet include dc/pitch-match was letting
        # weak plays earn the bot's highest-conviction tag (6.2% actual hit rate, worse
        # than every other tier including the lower-conviction HR Lean at 25.2%).
        # Emoji changed 🧨→🏆: 🧨 is freed up for the pick_type "HR" slot, and 🏆 reads
        # as "top tier" without the visual collision the old scheme had.
        final_role = "💎 HR Bet"
        best_use = "HR Straight / HR Pair"
    elif dc >= 66 and raw_power:
        # Emoji changed 👀→🔭: 👀 was double-booked with the HRW 50-59 timing band
        # (see hrw_emoji()), so the same symbol meant two unrelated things depending
        # on context. 🔭 ("watching from a distance / speculative") replaces it here.
        final_role = "🔭 Power Watch"
        best_use = "HRR + small HR exposure"
    elif hr >= 52 and dc >= 55:
        final_role = "📈 HR Lean"
        best_use = "HRR + HR exposure"
    elif hrr >= 60 or (dc >= 50 and not raw_power):
        final_role = "🧲 HRR / XBH"
        best_use = "HRR / Total Bases"
    elif raw_power and dc >= 48:
        final_role = "🔭 Power Watch"
        best_use = "Pool only / longshot HR"
    else:
        final_role = "🧭 Contact / Monitor"
        best_use = "Hit / contact only"

    h.true_avoid_hr = bool(final_role.startswith("⛔"))
    h.power_watch_flag = bool("Power Watch" in final_role)
    h.hrr_xbh_flag = bool("HRR" in final_role or "XBH" in final_role)
    h.final_hr_role = final_role
    h.best_use = best_use
    h.decision_reasons = list(dict.fromkeys(decision_reasons))[:5]
    h.avoid_hr_reasons = list(dict.fromkeys(avoid_reasons))[:5]
    h.risk_flags_v31 = list(dict.fromkeys(risk_flags))[:5]

    # Replace the old hard Avoid display behavior: Avoid only means TRUE avoid now.
    if h.true_avoid_hr:
        h.best_bet_type = "Avoid HR"
        # ⛔ prefix added to beginner_label: this is a HARD score suppression
        # (hr_score capped at 30, overall_score at 35 below), not a soft caution
        # flag like the ⚠️ trap warnings used elsewhere. The no-entry circle
        # signals "this score has been actively capped," distinct from ⚠️
        # which just means "a caution exists but the score wasn't forced down."
        h.beginner_label = "⛔ True Avoid HR"
        h.risk_reason = "; ".join(h.avoid_hr_reasons) or h.risk_reason
        h.trap_flag = True
        h.trap_risk_flag = True
        # RANK-IMPACTING FIX: previously True Avoid only docked top_board_score_v2,
        # a field nothing sorts the official Top 15 / pick by. hr_score and
        # overall_score (what pick_top() actually sorts on) never saw this flag,
        # so flagged players could still rank into Top 15 or win the pick.
        h.hr_score = round(min(h.hr_score, 30.0), 2)
        h.hr_score_v2 = h.hr_score
        h.overall_score = round(min(getattr(h, "overall_score", 100.0), 35.0), 2)
    elif h.power_watch_flag:
        h.best_bet_type = "HRR + HR Sprinkle"
        h.beginner_label = "Power Watch"
        if h.trap_flag:
            h.trap_reason = "Risky power, not true avoid"
        h.trap_flag = False
        h.trap_risk_flag = False
    elif h.hrr_xbh_flag:
        h.best_bet_type = "HRR / XBH"
        h.beginner_label = "HRR / XBH Lean"
    return h

LEAGUE_ISO = 0.160        # ~MLB league ISO; the shrink target for small samples
LEAGUE_HR_PER_PA = 0.031  # ~MLB league HR per PA
LEAGUE_SLG = 0.400        # ~MLB league SLG
SHRINK_K_PA = 150.0       # PAs of league-average "prior" a rate must overcome


def shrink_to_league(rate: float, pa: float, league: float, k: float = SHRINK_K_PA) -> float:
    """Shrink a small-sample rate toward league average by sample size.

    (pa * rate + k * league) / (pa + k) -- the standard empirical-Bayes
    shrinkage shape. At pa=0 you get exactly the league rate; at pa=k the
    player's own number carries half the weight; by ~450 PA it carries 75%.

    THE VEEN BUG (2026-08-17, fixed 2026-08-23). Zac Veen: 15 PA, 2 HR, so
    "ISO .500" -- and season_power fed that raw number through
    minmax_norm(iso, .08, .38), which saturates at anything >= .38. A
    15-PA fluke outranked Max Muncy (435 PA, ISO .249, 25 HR) on the HR
    board, took the HR badge for his game, and Muncy homered. Measured on
    the archive: in 17 of 42 games carrying both a sub-100-PA bat and a
    200+-PA veteran, the small-sample bat outscored every veteran in the
    game. A 15-PA ISO is noise; this makes the model treat it as noise.

    K=150 is chosen from first principles (the PA scale where individual
    ISO starts to stabilize), NOT from a sweep over the graded archive --
    the archive's feature columns leak (roadmap step 9) and tuning K on it
    would repeat the season_power-0.24 mistake. Worked example at K=150,
    league ISO .160: Veen 15 PA .500 -> .191; Muncy 435 PA .249 -> .226.
    The fluke drops below the real bat and nobody's real number moves much.

    Donovan's objective on record (2026-08-22): PRECISION -- and a false
    top-of-board is precision's single worst failure.
    """
    pa = max(0.0, safe_float(pa, 0.0))
    r = safe_float(rate, 0.0)
    denom = pa + k
    if denom <= 0:
        return league
    return (pa * r + k * league) / denom


def _pitch_match_term(raw: float) -> float:
    """0-100 hr_blend term from pitch_type_match_score (raw 0-120).

    MISSING-DATA SENTINEL FIX (2026-08-22). A raw score of exactly 0 is
    calculate_pitch_mix_fit()'s DEFAULT -- it means no candidate pitch
    cleared the five sequential sample gates (batter bbe>=5 on the pitch,
    good-contact>=.50, pitcher usage>=5%, pitcher bbe>=5 on it, HH>league
    average). Every one of those is a sample-size gate, so on a real slate
    the 0 is overwhelmingly "not enough per-pitch data," not a measured bad
    matchup: 65.0% of 695 rated player-nights sat at exactly 0 on
    2026-08-20..22's pre-game logs, including 20 of those nights' 47 actual
    homerers. Feeding that 0 straight through minmax_norm scored "we don't
    know" as the literal floor of the term -- while the sibling PMix term's
    own N/A path returns a neutral 50 ("PMix N/A", calculate_pitch_mix_fit's
    empty-input default). One missing-data policy, both matchup terms:
    missing scores neutral. A real qualifying match (raw > 0) is unchanged.
    In practice real matches score raw >55 (the gate arithmetic guarantees
    good_contact*60 >= 30 plus positive vulnerability terms; 3 of 695
    observed rows fell in (0,50]), so the neutral floor does not invert any
    meaningful measured signal.

    Weights untouched; model_version stays mlb_hr_v3. This IS a scoring-
    config change and config_hash moves with it by design -- the function is
    listed in _HR_CONFIG_FORMULA_FUNCS, so runs before and after are
    distinguishable in the archive (see docs/MODELS.md provenance rules).
    """
    if raw <= 0.0:
        return 50.0
    return _hr2_clip(minmax_norm(raw, 0, 120) * 100)


def apply_model_v2_layers(h: HitterRecord) -> HitterRecord:
    """HR Score 2.0: replace broad HR score with true HR-shape score.

    The website still sees one field: hr_score. The old broad score is preserved as
    hr_score_old / hr_score_delta for backend testing and trap detection.
    """
    split_avg = h.avg_vs_lhp if h.pitcher_throws == "L" else h.avg_vs_rhp
    split_iso = h.iso_vs_lhp if h.pitcher_throws == "L" else h.iso_vs_rhp
    season_pa = max(1, safe_int(h.season_pa, 1))
    h.hr_per_pa = round(safe_float(h.season_hr, 0.0) / season_pa, 4)
    h.hr_pa_score = round(100 * minmax_norm(h.hr_per_pa, 0.015, 0.085), 1)
    old_hr_score = safe_float(getattr(h, "hr_score", 0.0), 0.0)

    bbe = h.bbe_profile if isinstance(h.bbe_profile, dict) else {}
    tracked = max(1, safe_int(bbe.get("tracked_distance"), safe_int(getattr(h, "recent_distance_tracked", 0), 0)) or safe_int(getattr(h, "recent_350_den", 1), 1))
    sample_bbe = max(1, safe_int(bbe.get("sample_bbe"), safe_int(getattr(h, "recent_350_den", 1), 1)))
    d375 = safe_int(bbe.get("dist_375_plus"), safe_int(getattr(h, "recent_375_num", 0), 0))
    d400 = safe_int(bbe.get("dist_400_plus"), 0)
    r375 = d375 / max(1, tracked)
    r400 = d400 / max(1, tracked)
    max_ev = safe_float(bbe.get("max_ev"), safe_float(getattr(h, "recent_ev", 88.5), 88.5))
    avg_ev = safe_float(bbe.get("avg_ev"), safe_float(getattr(h, "recent_ev", 88.5), 88.5))
    avg_la = safe_float(bbe.get("avg_la"), 0.0)
    max_distance = safe_float(bbe.get("max_distance"), 0.0)
    hard_rate = safe_float(bbe.get("hard_hit_rate"), safe_float(getattr(h, "recent_hard_hit_rate", 0.0), 0.0))
    barrel_rate = safe_float(bbe.get("barrel_rate"), safe_float(getattr(h, "recent_barrel_rate", 0.0), 0.0))
    ideal_hr = safe_float(bbe.get("ideal_hr_contact_rate"), safe_float(getattr(h, "recent_ideal_hr_contact", 0.0), 0.0))
    # Pitcher damage allowed at THIS batter's lineup spot (shared across all scores)
    _spot = _spot_damage_for_batter(h)
    # Weak-side edge: graded 0-1, blends the pitcher's weakness to this batter's
    # side. Bonus scales with how exploitable + how lopsided the split is.
    # SWITCH HITTERS (fixed 2026-07-31). Every platoon term below compared
    # h.bats directly against "L"/"R", so a switch hitter -- bats == "S" --
    # failed all of them: never matched the pitcher's weak side, always took
    # the neutral WHIP fallback, and always took the 0.4 side_match penalty
    # instead of the 1.0 bonus. A switch hitter has the platoon edge in EVERY
    # matchup, so it was exactly backwards. They are ~11% of a slate and were
    # shut out of the top 25 of the HR board entirely.
    #
    # A switch hitter bats opposite the arm: left against a righty, right
    # against a lefty.
    _eff_bats = h.bats
    if (h.bats or "").upper()[:1] == "S":
        _eff_bats = "R" if (h.pitcher_throws or "").upper()[:1] == "L" else "L"

    _wk_side_norm = minmax_norm(safe_float(getattr(h, "pitcher_weak_side_score", 0.0), 0.0), 40.0, 85.0)
    _wk_gap = safe_float(getattr(h, "pitcher_weak_side_gap", 0.0), 0.0)
    _is_weak_side = (h.pitcher_weak_side == "LHB" and _eff_bats == "L") or (h.pitcher_weak_side == "RHB" and _eff_bats == "R")
    _weak_side_edge = _wk_side_norm * (0.6 + 0.4 * min(1.0, _wk_gap / 0.30))
    # Side-specific WHIP as its own visible factor (was previously only
    # baked into pitcher_weak_side_score's upstream composite, not directly
    # readable here) -- added per audit (2026-06-27). A high side-specific
    # WHIP means more traffic/baserunners against this batter's hand
    # specifically, a distinct signal from pure power weakness.
    _side_whip = h.pitcher_whip_vs_lhb if _eff_bats == "L" else h.pitcher_whip_vs_rhb if _eff_bats == "R" else 1.28
    _whip_boost = minmax_norm(safe_float(_side_whip, 1.28), 1.15, 1.55) * 0.15  # up to +15% multiplier
    _weak_side_edge = min(1.0, _weak_side_edge * (1.0 + _whip_boost))
    if _is_weak_side:
        _weak_side_edge = min(1.0, _weak_side_edge * 1.25)
    h.weak_side_bonus = round(_weak_side_edge, 3)
    fb_rate = safe_float(bbe.get("fb_rate"), safe_float(getattr(h, "recent_fb_rate", 0.0), 0.0))
    ld_rate = safe_float(bbe.get("ld_rate"), 0.0)
    gb_rate = safe_float(bbe.get("gb_rate"), 0.0)
    popup_rate = safe_float(bbe.get("popup_rate"), 0.0)

    # ── "Yesterdays Hitters" custom highlight, ported in from the user's
    # external highlight tool (per request, 2026-06-29; rebuilt onto a true
    # L25PA window per follow-up request, 2026-06-29 -- their stated
    # favorite recency window, distinct from the bot's existing L20PA/L10/L5
    # windows). All-criteria-match gate: BA >=.24, HH% >=45%, LD% >=12.5%,
    # sweet-spot LA% >=25%, bat speed in the 70.2-80.2 band, Air% >=40%, avg
    # EV >=89, 300+ ft count >=2, 375+ ft count >=1 -- all measured over the
    # batter's last 25 PA, not season-to-date. Graded 0-9 (one point per
    # criterion) so partial matches still nudge hr_raw, with a separate
    # explicit bonus below if every single criterion clears at once --
    # matching the highlight tool's own "all must match" semantics.
    _l25_pa_n = safe_int(getattr(h, "l25pa_pa", 0), 0)
    _yh_ba = safe_float(getattr(h, "l25pa_avg", 0.0), 0.0)
    _yh_hh = safe_float(getattr(h, "l25pa_hard_hit_rate", 0.0), 0.0)
    _yh_ld = safe_float(getattr(h, "l25pa_ld_rate", 0.0), 0.0)
    _yh_sweetspot = safe_float(getattr(h, "l25pa_sweet_spot_rate", 0.0), 0.0)
    _yh_bat_speed = getattr(h, "l25pa_avg_bat_speed", None)
    _yh_air = safe_float(getattr(h, "l25pa_air_rate", 0.0), 0.0)
    _yh_ev = safe_float(getattr(h, "l25pa_avg_ev", 88.5), 88.5)
    _yh_300plus = safe_int(getattr(h, "l25pa_300_plus", 0), 0)
    _yh_375plus = safe_int(getattr(h, "l25pa_375_plus", 0), 0)

    _yh_checks = [
        _yh_ba >= 0.24,
        _yh_hh >= 0.45,
        _yh_ld >= 0.125,
        _yh_sweetspot >= 0.25,
        (_yh_bat_speed is not None and 70.2 <= _yh_bat_speed <= 80.2),
        _yh_air >= 0.40,
        _yh_ev >= 89.0,
        _yh_300plus >= 2,
        _yh_375plus >= 1,
    ]
    _yh_hits = sum(_yh_checks)
    _yh_raw_score = 100.0 * (_yh_hits / len(_yh_checks))
    # Below ~15 PA the L25PA window is too thin to trust -- shrink toward a
    # neutral 50 rather than letting a 3-PA sample swing the full range.
    # (Inline shrinkage, not the module's shrink_rate helper, which is a
    # local closure defined inside score_hitter() and out of scope here.)
    _yh_shrink_weight = min(1.0, _l25_pa_n / 15.0)
    yesterdays_hitters_score = _yh_shrink_weight * _yh_raw_score + (1.0 - _yh_shrink_weight) * 50.0
    yesterdays_hitters_all_match = bool(_yh_hits == len(_yh_checks) and _l25_pa_n >= 15)
    h.yesterdays_hitters_score = round(yesterdays_hitters_score, 1)
    h.yesterdays_hitters_all_match = yesterdays_hitters_all_match


    air_pull = safe_float(bbe.get("air_pull_rate"), safe_float((h.park_fit or {}).get("pull_air_rate"), 0.0)) if isinstance(getattr(h, "park_fit", {}), dict) else safe_float(bbe.get("air_pull_rate"), 0.0)
    raw_pull = safe_float(bbe.get("pull_rate"), safe_float(getattr(h, "recent_pull_rate", 0.0), 0.0))
    hr_fb_rate = 0.0
    if fb_rate > 0:
        hr_fb_rate = min(1.0, safe_int(bbe.get("hr"), 0) / max(1, int(round(fb_rate * sample_bbe))))
    barrel_per_fb = barrel_rate / max(0.05, fb_rate) if fb_rate else 0.0

    missing_flags: List[str] = []
    if safe_int(bbe.get("sample_bbe"), 0) <= 0:
        missing_flags.append("recent_bbe")
    if safe_int(getattr(h, "pitch_mix_sample", 0), 0) < 20:
        missing_flags.append("pitch_mix_low_sample")
    if str(getattr(h, "weather_source", "none")) == "none":
        missing_flags.append("weather")

    # 98+ EV rate, computed directly from the batted-ball log since no
    # standalone field exists for this (98+ previously only appeared bundled
    # into the barrel launch-angle window). Used below as an escape hatch for
    # the flat-launch-angle penalty -- a guy hitting the ball 98+ hard isn't
    # truly "flat," his sample's just dominated by liners/grounders that still
    # carry real exit velocity.
    ev_shape = max_ev if not (avg_ev >= 95 and avg_la < 10) else 88.5
    _bb_log = h.batted_ball_log if isinstance(h.batted_ball_log, list) else []
    _ev_vals = [safe_float(b.get("ev"), 0.0) for b in _bb_log if isinstance(b, dict) and b.get("ev")]
    hh98_rate = (sum(1 for v in _ev_vals if v >= 98) / len(_ev_vals)) if _ev_vals else 0.0

    # 35% recent batted-ball damage, shape-adjusted.
    # Reweighted per audit (2026-06-27): corrected from 114% to 100% total.
    # barrel_rate and barrel_per_fb emphasized (16%/10%, up from 14%/8%) per
    # request. hr_per_pa added as a new component -- season-long HR rate,
    # distinct from the recency-focused stats around it. max_distance and
    # the L5/L10 trend trimmed (least stable single-event/secondary signals)
    # to fund the above without inflating the total.
    batted_shape = 100 * (
        0.13 * minmax_norm(r375, 0.02, 0.24) +
        0.07 * minmax_norm(r400, 0.00, 0.10) +
        0.11 * minmax_norm(ev_shape, 88.0, 110.0) +
        0.05 * minmax_norm(avg_ev, 86.0, 96.0) +
        0.07 * minmax_norm(hard_rate, 0.28, 0.65) +
        0.16 * minmax_norm(barrel_rate, 0.03, 0.20) +
        0.10 * minmax_norm(barrel_per_fb, 0.08, 0.45) +
        0.06 * minmax_norm(hr_fb_rate, 0.02, 0.28) +
        0.13 * minmax_norm(ideal_hr, 0.05, 0.25) +
        0.03 * minmax_norm(max_distance, 330, 430) +
        0.03 * minmax_norm(getattr(h, "l5_barrel_rate", 0.0) - getattr(h, "l10_barrel_rate", 0.0), -0.06, 0.10) +
        0.06 * minmax_norm(safe_float(getattr(h, "hr_per_pa", 0.0), 0.0), 0.015, 0.085)
    )

    # Flat launch angle: severity cut from 35% to 30% (per audit), with an
    # escape hatch -- if hard_rate or hh98_rate is genuinely strong, a low
    # average LA is more likely an artifact of a liner-heavy sample than a
    # real "can't get the ball in the air" problem, so skip the penalty.
    flat_la_escape = hard_rate >= 0.45 or hh98_rate >= 0.20
    if avg_la < 8 and not flat_la_escape:
        batted_shape *= 0.70
    if popup_rate >= 0.30:
        batted_shape *= 0.70
    elif popup_rate >= 0.20:
        batted_shape *= 0.85
    if gb_rate >= 0.55:
        batted_shape *= 0.72
    elif gb_rate >= 0.48:
        batted_shape *= 0.88

    # 375+ bonus now gated: a deep-ball count only earns the flat bonus if it
    # comes WITH either zero recent HRs (so it's not double-rewarding a guy
    # already cashing in via the recent-HR bonus below) or 3+ recent hits
    # (showing the deep contact is part of a real hot stretch, not a single
    # fluke ball in an otherwise empty sample).
    last10_hr_n = safe_int(getattr(h, "last10_hr", 0), 0)
    last10_hits_n = safe_int(getattr(h, "last10_hits", 0), 0)
    if d375 >= 2 and (last10_hr_n == 0 or last10_hits_n >= 3):
        batted_shape += 8
    # 400+ bonus halved per audit (6 -> 3) -- still meaningful, less able to
    # single-handedly swing the score on one outlier ball.
    batted_shape += 3 if d400 >= 1 else 0

    # Recent-HR bonus, rescaled: old scale (last10_hr * 5, capped at 10) maxed
    # out at just 2 HRs and treated every HR identically. New scale rewards
    # the first confirmed HR meaningfully (it's the strongest single signal
    # that his power is live right now), gives a smaller marginal bump per
    # additional HR (diminishing returns -- 4 HRs in 10 games isn't twice as
    # predictive as 2), and raises the cap slightly for a genuine power run.
    if last10_hr_n >= 1:
        batted_shape += min(14, 7 + (last10_hr_n - 1) * 3.5)

    # ── HR "dueness" / hot-cold cycle, from hr_per_pa ───────────────────────
    # Estimate this player's normal games-between-HRs cycle from his season
    # hr_per_pa (at ~4.2 PA/game, the rough MLB average), then compare to his
    # actual games_since_last_hr. A modest overshoot past his normal cycle
    # reads as genuinely "due" (boost); a large overshoot (4x+ his normal
    # cycle) more likely means something's actually wrong -- a slump, a role
    # change, an injury -- not just normal variance, so it gets no bonus
    # rather than an ever-larger one.
    _hrpa_for_cycle = max(0.012, safe_float(getattr(h, "hr_per_pa", 0.0), 0.0))
    _expected_cycle_games = (1.0 / _hrpa_for_cycle) / 4.2
    _games_since_hr = safe_int(getattr(h, "games_since_last_hr", 60), 60)
    _due_ratio = _games_since_hr / max(1.0, _expected_cycle_games)
    dueness_bonus = 0.0
    if 1.3 <= _due_ratio <= 2.5:
        dueness_bonus = minmax_norm(_due_ratio, 1.3, 2.5) * 7.0       # ramps up to +7
    elif 2.5 < _due_ratio <= 4.0:
        dueness_bonus = 7.0 * (1.0 - minmax_norm(_due_ratio, 2.5, 4.0))  # tapers back to 0
    # ratio > 4.0 or < 1.3: no bonus -- either recently hot (already captured
    # by the recent-HR bonus above) or likely cold rather than "due."
    batted_shape += dueness_bonus
    h.hr_due_ratio = round(_due_ratio, 2)  # exposed for debugging/frontend display

    batted_shape = _hr2_clip(batted_shape)

    # ── Hard override: stacked bad-shape signals ────────────────────────────
    # If 2+ of {flat LA, popup-heavy, groundball-heavy} fire together, the
    # individual multipliers (0.70 x 0.70 x 0.72, etc.) already crush the
    # score substantially, but per audit this combination shouldn't be
    # trusted as a real HR signal at all -- it's closer to a coin flip than
    # a graded pick. Force the HR-relevant score down hard regardless of how
    # the multipliers landed, rather than letting compounding multiplication
    # alone decide how low is low enough. This flag is HR-specific and does
    # NOT touch hit_score/contact_score, which have their own logic.
    _bad_signal_count = sum([
        avg_la < 8 and not flat_la_escape,
        popup_rate >= 0.20,
        gb_rate >= 0.48,
    ])
    h.hr_unreliable_shape_flag = _bad_signal_count >= 2
    if h.hr_unreliable_shape_flag:
        batted_shape = min(batted_shape, 22.0)



    # 25% pitch-type fit, already usage-weighted by calculate_pitch_mix_fit.
    pitch_fit = safe_float(getattr(h, "pitch_mix_score", 50.0), 50.0)
    pmix_blob = h.pitch_mix_matchup if isinstance(h.pitch_mix_matchup, dict) else {}
    pitch_trap = bool(pmix_blob.get("pitch_trap", False))
    pitch_trap_reason = str(pmix_blob.get("trap_reason", ""))
    best_damage_usage = safe_float(pmix_blob.get("best_damage_usage"), 0.0)
    strong_pitch_signal = bool(pmix_blob.get("strong_pitch_signal", False)) or (pitch_fit >= 60 and best_damage_usage >= 20)

    # 15% pitcher HR damage allowed.
    # Now folds in meatball% + pullair-allowed% as direct mistake-pitch signals,
    # which are stronger HR predictors than HR/9 alone. Both default to league avg
    # when sample is missing, so the score doesn't collapse.
    meatball = safe_float(getattr(h, "pitcher_meatball_pct", 0.070), 0.070)
    pullair_allowed = safe_float(getattr(h, "pitcher_pullair_allowed_pct", 0.220), 0.220)
    has_advanced = str(getattr(h, "pitcher_advanced_stats_status", "missing")) == "ok"

    # ── MEATBALL, BY THE HAND HE IS FACING (2026-08-23) ────────────────────
    #
    # Donovan: "meat ball percent needs to be used in hr for sure hand splits
    # and everything. wtf ." Two separate things happen here, and they are
    # deliberately different in kind:
    #
    #   1. The 0.12 meatball slice of pitcher_damage below stops reading the
    #      pitcher's OVERALL middle-middle rate and starts reading his rate
    #      AGAINST THIS BAT'S SIDE. That is not a tuning move -- the weight is
    #      untouched, hr_blend still sums to 1.00, and no other term shifts. It
    #      is the same term being fed the correct number. The standing rule
    #      ("no hr_blend weight moves before 9c, ~2026-09-22, because the
    #      graded archive still manufactures tuning signals out of its own
    #      leak") is about WEIGHTS, and this changes none of them.
    #
    #   2. The genuinely new signal -- the meatball EDGE, crossed with whether
    #      this particular bat can punish a mistake -- lands as
    #      meatball_fit_score: published, archived, graded, and worth exactly
    #      zero points in hr_raw until it has a few weeks of nights behind it.
    #      That is the same path personal_shape_match took, and the reason is
    #      the same: a term that has never been measured does not get to move
    #      the board just because the idea is good.
    #
    # _eff_bats is resolved ~200 lines above (switch hitters bat opposite the
    # arm, so they hold the platoon edge in every matchup).
    # "both_real" means BOTH sides cleared the 150-pitch floor, so the GAP
    # between them is a fact rather than an artifact of one thin sample. The
    # rate itself is usable one side at a time; the edge is not.
    meatball_hand, _mb_other, _mb_both_real = meatball_vs_hand(h)
    h.meatball_pct_vs_hand = round(meatball_hand, 4)
    h.meatball_edge_pp = round(100.0 * (meatball_hand - _mb_other), 2) if _mb_both_real else 0.0

    if str(getattr(h, "pitcher_statcast_status", "missing")) == "ok" and safe_int(getattr(h, "pitcher_statcast_bbe", 0), 0) > 0:
        # When advanced stats are present, give them ~25% of the pitcher_damage layer.
        # When missing, weights collapse back into the existing HR/9-driven blend.
        if has_advanced:
            pitcher_damage = 100 * (
                0.18 * minmax_norm(getattr(h, "pitcher_hr9", 1.10), 0.70, 2.00) +
                0.13 * minmax_norm(getattr(h, "pitcher_barrel_allowed", 0.07), 0.03, 0.13) +
                0.11 * minmax_norm(getattr(h, "pitcher_hardhit_allowed", 0.38), 0.30, 0.52) +
                0.08 * minmax_norm(getattr(h, "pitcher_ev_allowed", 88.5), 86.0, 92.5) +
                0.04 * minmax_norm(getattr(h, "pitcher_statcast_fb_rate", 0.34), 0.28, 0.48) +
                0.03 * minmax_norm(getattr(h, "pitcher_375_allowed", 0), 0, 8) +
                # Mistake-pitch path: high meatball + high pullair allowed → HRs.
                # meatball_hand, not meatball: his middle-middle rate against
                # THIS bat's side (see the block above). Same weight, same
                # blend, correct input.
                0.12 * minmax_norm(meatball_hand, 0.040, 0.105) +
                0.09 * minmax_norm(pullair_allowed, 0.16, 0.32) +
                # NEW (2026-07-07): wOBA against (season damage rate, direct
                # signal of how hard he's actually being hit) and FIP (his
                # true-talent level independent of ballpark/defense noise).
                # Both reuse the already-fetched official season stat blob --
                # see compute_pitcher_extended_stats -- no new network calls.
                0.14 * minmax_norm(getattr(h, "pitcher_woba_against", 0.320), 0.290, 0.380) +
                0.08 * minmax_norm(getattr(h, "pitcher_fip", 4.00), 2.50, 5.50)
            )
        else:
            pitcher_damage = 100 * (
                0.30 * minmax_norm(getattr(h, "pitcher_hr9", 1.10), 0.70, 2.00) +
                0.22 * minmax_norm(getattr(h, "pitcher_barrel_allowed", 0.07), 0.03, 0.13) +
                0.20 * minmax_norm(getattr(h, "pitcher_hardhit_allowed", 0.38), 0.30, 0.52) +
                0.15 * minmax_norm(getattr(h, "pitcher_ev_allowed", 88.5), 86.0, 92.5) +
                0.08 * minmax_norm(getattr(h, "pitcher_statcast_fb_rate", 0.34), 0.28, 0.48) +
                0.05 * minmax_norm(getattr(h, "pitcher_375_allowed", 0), 0, 8)
            )
            missing_flags.append("pitcher_advanced_stats")
    else:
        pitcher_damage = 100 * (0.58 * minmax_norm(getattr(h, "pitcher_hr9", 1.10), 0.70, 2.00) + 0.42 * minmax_norm(getattr(h, "pitcher_whip", 1.30), 1.05, 1.60)) * 0.70
        missing_flags.append("pitcher_statcast")
    # Recent-form adjustment (per audit, 2026-06-27): pitcher_damage above is
    # entirely season-long. Pitchers previously had no recency window at all
    # (unlike batters' last5/7/10) despite documented real game-to-game
    # variability beyond chance. Blend in last-3-starts ERA/WHIP/HR9 as a
    # modest multiplier on top of the season-based blend -- a pitcher
    # running hot or cold lately shifts the score, but the season-long
    # baseline still dominates since 3 starts is a small sample.
    l3_starts = safe_int(getattr(h, "pitcher_l3_starts_found", 0), 0)
    if l3_starts >= 2:
        l3_damage_proxy = 100 * (
            0.45 * minmax_norm(getattr(h, "pitcher_l3_hr9", 1.10), 0.70, 2.20) +
            0.30 * minmax_norm(getattr(h, "pitcher_l3_whip", 1.30), 1.00, 1.70) +
            0.25 * minmax_norm(getattr(h, "pitcher_l3_era", 4.20), 2.50, 6.50)
        )
        # Small blend -- season-long stays dominant, recent form nudges it.
        recent_weight = 0.18 if l3_starts >= 3 else 0.10
        pitcher_damage = (1 - recent_weight) * pitcher_damage + recent_weight * l3_damage_proxy
    # Fastball velocity decline check (per audit, 2026-06-27): compares his
    # most recent start's average fastball velocity to his season average.
    # Documented thresholds -- a ~1.0 MPH drop is an early warning, 1.5-2.0+
    # MPH is the actionable fatigue/decline signal (more contact, fewer
    # whiffs, higher HR rate allowed). Only a bonus to pitcher_damage (never
    # a penalty for velocity being UP -- that's normal start-to-start noise,
    # not a documented "he's extra tough today" effect).
    if str(getattr(h, "pitcher_fb_velo_status", "missing")) == "ok":
        velo_delta = safe_float(getattr(h, "pitcher_fb_velo_delta", 0.0), 0.0)
        if velo_delta <= -2.0:
            pitcher_damage = min(100.0, pitcher_damage * 1.12)
        elif velo_delta <= -1.5:
            pitcher_damage = min(100.0, pitcher_damage * 1.08)
        elif velo_delta <= -1.0:
            pitcher_damage = min(100.0, pitcher_damage * 1.04)
    # Weak-side edge is handled via the dedicated _weak_side_edge term in hr_raw
    # (0.05 * _weak_side_edge, computed at lines ~5354-5368 above). The block
    # that previously also added edge_pts directly to pitcher_damage was removed
    # (audit 2026-06-29) -- it caused the same underlying signal to count twice
    # in hr_raw: once as a direct component and again indirectly through the
    # 12%-weighted pitcher_damage sub-score, adding ~5.2 duplicate points on
    # a strong weak-side match.
    if h.pitcher_era <= 2.50 and h.pitcher_whip <= 1.05:
        pitcher_damage *= 0.78
    elif h.pitcher_era <= 3.20 and h.pitcher_whip <= 1.15:
        pitcher_damage *= 0.88
    pitcher_damage = _hr2_clip(pitcher_damage)

    # ─── MATCHUP TAGS from advanced stats ───────────────────────────────
    # Mistake-pitch setup: pitcher coughs up the meatball + pulled-air HR shape.
    # Per-hitter flag, so it reads the per-hitter rate. An arm at 6.2%
    # overall who runs 9.4% to lefties IS a mistake-pitch setup for a lefty
    # and was not being called one.
    h.mistake_pitch_setup_flag = bool(has_advanced and meatball_hand >= 0.080 and pullair_allowed >= 0.255)

    # ── THE RUNNING GAME: THE OTHER GRADED COLUMN ──────────────────────────
    #
    # Donovan, 2026-08-23: "pitcher meatballs wild pitches abs challenge record
    # for cather and pitcher and batter. defense stats and casught stelaing
    # perceantage and pitcher catch stealign and pitcher pick off rate. add
    # those ... then also build a model with them."
    #
    # This is the model on the running-game half of that list. It answers one
    # question — how good a steal spot is this man tonight — and it is worth
    # ZERO points in hr_raw or any other blend. A steal model has no business
    # inside a home-run score, and the standing no-weights-before-9c rule
    # covers the rest.
    #
    # FIVE TERMS, and the last one is the one everybody forgets. A steal needs
    # a runner who RUNS, a runner who SUCCEEDS, an arm that can be run on, a
    # catcher who cannot throw — and a man who REACHES BASE. A 42-steal hitter
    # batting .190 is not tonight's steal; his rate is real and his opportunity
    # is not. StealBoard.js has said that in words since it was built; this
    # puts it in the number.
    #
    # NOTHING HERE IS INVENTED WHERE DATA IS MISSING. Each term contributes
    # only if its input exists, the weights are renormalised over what actually
    # landed, and `steal_risk_status` says "thin" whenever the pitcher or
    # catcher half is absent. A score built on the runner alone is a volume
    # ranking wearing a matchup's clothes, and it says so.
    _sr_terms = []
    _sr_add = lambda w, v: _sr_terms.append((w, max(0.0, min(1.0, v))))   # noqa: E731

    _run_att = safe_float(getattr(h, "season_sb_attempt_rate", 0.0), 0.0)
    _run_sb = safe_int(getattr(h, "season_sb", 0), 0)
    _run_cs = safe_int(getattr(h, "season_cs", 0), 0)
    _run_tries = _run_sb + _run_cs
    # ── THE RUNNER IS A GATE, NOT A TERM ──────────────────────────────────
    # Caught in the first run of this model: a hitter with NO attempt history
    # scored 78.7 — the highest on the test slate — because with both runner
    # terms absent the renormalisation handed his whole score to the arm and
    # the catcher. A soft arm and a weak-throwing catcher are a great steal
    # spot for somebody who runs; for a man who has not attempted a base all
    # season they are a fact about two other people.
    #
    # So it gates. No attempt history, no steal score, and the note says which
    # of the two reasons it is. This is the same shape as meatball_fit_score's
    # "missing" path: an unmeasured thing must never outrank a measured one.
    _has_runner = (_run_tries > 0) or (_run_att > 0)
    if _has_runner and _run_att > 0:
        # The bot publishes this as a rate; some slates have carried it as a
        # per-100 number, so normalise rather than trust the scale.
        _sr_add(0.34, minmax_norm(_run_att if _run_att <= 1 else _run_att / 100.0, 0.02, 0.28))
    elif _run_tries:
        _sr_add(0.34, minmax_norm(_run_tries, 2, 40))
    if _run_tries >= 5:
        # Break-even on a stolen base is about 75%; below it the attempt costs
        # more than it wins, which is the line StealBoard already colours to.
        _sr_add(0.18, minmax_norm(_run_sb / _run_tries, 0.55, 0.90))
    # The arm. Runners going against him, and how rarely he checks them.
    _p_att = safe_int(getattr(h, "pitcher_sb_attempts_against", 0), 0)
    _p_pick = getattr(h, "pitcher_pickoff_rate", None)
    _p_wp9 = getattr(h, "pitcher_wp9", None)
    _arm_ok = False
    if _p_att >= 5:
        _sr_add(0.14, minmax_norm(_p_att, 3, 30))
        _arm_ok = True
    if _p_pick is not None:
        # LOW pickoff rate is the runner's friend, so this term inverts.
        _sr_add(0.06, 1.0 - minmax_norm(safe_float(_p_pick, 0.0), 0.0, 0.06))
        _arm_ok = True
    if _p_wp9 is not None:
        # A wild pitch moves the runner for free. Not a steal, but the same
        # bet's neighbour, and it is on the list he asked for.
        _sr_add(0.06, minmax_norm(safe_float(_p_wp9, 0.0), 0.10, 0.80))
        _arm_ok = True
    # The catcher. A weak thrower raises the risk, so this inverts too, and it
    # is measured against what Statcast EXPECTED of his throws where possible —
    # a catcher behind arms who never hold a runner posts a poor raw rate that
    # is not his fault.
    _cat_rate = getattr(h, "opp_catcher_cs_rate", None)
    _cat_exp = getattr(h, "opp_catcher_cs_rate_expected", None)
    _cat_ok = False
    if _cat_rate is not None:
        _sr_add(0.16, 1.0 - minmax_norm(safe_float(_cat_rate, 0.20), 0.10, 0.35))
        _cat_ok = True
        if _cat_exp is not None:
            # Over- or under-performing his expectation, worth a small nudge.
            _sr_add(0.06, 1.0 - minmax_norm(safe_float(_cat_rate, 0.2) - safe_float(_cat_exp, 0.2), -0.10, 0.10))
    # ── AND THE HALF NOBODY COUNTS: HE HAS TO GET ON ──────────────────────
    # A MULTIPLIER, not a term, and the first version had it wrong. As a 0.10
    # additive slice, a 42-steal man with a .262 on-base scored 75.5 against
    # 75.6 for a .352 runner — the model was saying they were the same bet
    # while its own comment said one of them "is not tonight's steal". You
    # cannot steal first base, so opportunity SCALES the whole thing rather
    # than adding a tenth of it.
    #
    # Floor of 0.55 rather than 0: a poor on-base still reaches sometimes, and
    # a multiplier that can hit zero would rank a .240 burner below a catcher
    # who never runs. Ceiling 1.0 — reaching base is not a bonus, it is the
    # precondition, so the best it can do is fail to penalise.
    _obp = safe_float(getattr(h, "season_obp", 0.0), 0.0)
    _reach = (0.55 + 0.45 * minmax_norm(_obp, 0.270, 0.390)) if _obp > 0 else 1.0

    _sr_w = sum(w for w, _ in _sr_terms)
    if not _has_runner:
        h.steal_risk_score = 0.0
        h.steal_risk_status = "no_runner"
        h.steal_risk_note = ("no stolen-base attempt on his record this season — "
                             "the arm and the catcher are somebody else's matchup")
    elif _sr_w <= 0:
        h.steal_risk_score = 0.0
        h.steal_risk_status = "missing"
        h.steal_risk_note = "nothing published for this runner tonight"
    else:
        _raw = 100.0 * sum(w * v for w, v in _sr_terms) / _sr_w
        h.steal_risk_score = round(_raw * _reach, 1)
        h.steal_risk_status = "ok" if (_arm_ok and _cat_ok) else "thin"
        _cat_word = (f"catcher {100 * safe_float(_cat_rate, 0.0):.0f}% CS" if _cat_ok
                     else "catcher unmeasured")
        _arm_word = (f"{_p_att} attempts against this arm" if _p_att >= 5
                     else "no run history against this arm")
        _reach_word = (f" · {_obp:.3f} OBP".replace("0.", ".") if _obp > 0 else "")
        h.steal_risk_note = (f"{_run_sb} SB / {_run_tries} tries · {_arm_word}"
                             f" · {_cat_word}{_reach_word}")

    # ── MEATBALL FIT: THE GRADED COLUMN ────────────────────────────────────
    #
    # A meatball is only a homer if somebody punishes it. Half of this score is
    # the pitcher (how often the heart of the plate opens up to this side, and
    # how much more this side sees it than the other), half is the bat (what it
    # does with a mistake, and whether it is squaring anything up right now).
    #
    # It is worth ZERO points in hr_raw. It is written to the row, whitelisted
    # into the graded archive, and shown on the site as a column -- and in a few
    # weeks the question "do high-fit bats homer more often, holding hr_score
    # fixed" becomes answerable off real nights instead of off a hunch. If the
    # answer is no, it is deleted. That is the deal.
    if not has_advanced:
        h.meatball_fit_score = 0.0
        h.meatball_fit_status = "missing"
        h.meatball_fit_note = "no Statcast pitch data for this arm — not a cold matchup, an unmeasured one"
    else:
        _mb_edge_term = minmax_norm(max(0.0, h.meatball_edge_pp), 0.0, 3.0)
        _mb_fit = 100.0 * (
            0.42 * minmax_norm(meatball_hand, 0.040, 0.105) +
            0.18 * _mb_edge_term +
            0.25 * minmax_norm(safe_float(split_iso, 0.150), 0.090, 0.300) +
            0.15 * minmax_norm(safe_float(barrel_rate, 0.060), 0.030, 0.150)
        )
        h.meatball_fit_score = round(max(0.0, min(100.0, _mb_fit)), 1)
        h.meatball_fit_status = "ok" if _mb_both_real else "no_side_split"
        _mb_side_word = "lefties" if _eff_bats == "L" else "righties" if _eff_bats == "R" else "this side"
        if _mb_both_real:
            _mb_gap = h.meatball_edge_pp
            _mb_gap_txt = ("%+0.1fpp vs the other side" % _mb_gap) if abs(_mb_gap) >= 0.3 else "even both ways"
            h.meatball_fit_note = "%.1f%% middle-middle to %s (%s)" % (100.0 * meatball_hand, _mb_side_word, _mb_gap_txt)
        else:
            h.meatball_fit_note = "%.1f%% middle-middle overall — not enough pitches to split by hand" % (100.0 * meatball_hand)

    # ── HR PACE FLAG: HONEST DUENESS x A PITCHER GIVING THEM UP RIGHT NOW ──
    #
    # due_score() (removed 2026-08-24, see where it used to live below)
    # blended six shape/contact-quality terms into one continuous number,
    # and the archive taught it that "last5_hr==0"
    # was informative -- which the archive leak (bots/leak_scan.py,
    # 2026-08-23) proved was tautological (last5_hr is refreshed AFTER the
    # game in graded rows, so a player who homered tonight always shows
    # last5_hr>=1). Only one ingredient in that blend was honest on its own
    # terms: the expected-value gap -- at his own season HR/PA rate, how
    # many HRs he "should" have hit over his recent PA window versus how
    # many he actually hit. That gap survives here as a boolean flag,
    # matched with the one thing that makes "due" mean something TONIGHT
    # instead of just narrating the last two weeks: an opposing pitcher who
    # is CURRENTLY giving up home runs at an elevated rate over his last 3
    # starts, not his season number. Gated on pitcher_l3_starts_found >= 2
    # -- under two starts pitcher_l3_hr9 is too thin to trust, and this flag
    # simply does not fire rather than quietly falling back to a season
    # rate that isn't "recent" anymore.
    _hp_season_pa = max(1, safe_int(h.season_pa, 1))
    _hp_hr_per_pa = safe_float(h.season_hr, 0.0) / _hp_season_pa
    _hp_recent_pa = safe_int(getattr(h, "l20pa_pa", 0), 0) or max(0, safe_int(h.recent_350_den, 0))
    _hp_recent_hr = safe_int(getattr(h, "l20pa_hr", 0), 0) or safe_int(h.last5_hr, 0)
    _hp_expected = _hp_recent_pa * _hp_hr_per_pa if _hp_recent_pa else 0.0
    h.hr_pace_gap = round(max(0.0, _hp_expected - _hp_recent_hr), 2)

    _hp_l3_starts = safe_int(getattr(h, "pitcher_l3_starts_found", 0), 0)
    _hp_pitcher_hot = _hp_l3_starts >= 2 and safe_float(getattr(h, "pitcher_l3_hr9", 1.10), 1.10) >= 1.30

    # Sample floor on the hitter side too -- a "gap" over 4 PA is noise, not
    # dueness, no matter how large the raw number looks.
    h.hr_pace_flag = bool(_hp_recent_pa >= 15 and h.hr_pace_gap >= 0.75 and _hp_pitcher_hot)
    if h.hr_pace_flag:
        h.hr_pace_note = ("%.2f HRs behind his own season pace over his last %d PA, into a "
                          "pitcher allowing %.2f HR/9 across his last %d starts"
                          % (h.hr_pace_gap, _hp_recent_pa,
                             safe_float(getattr(h, "pitcher_l3_hr9", 1.10), 1.10), _hp_l3_starts))
    elif _hp_recent_pa < 15:
        h.hr_pace_note = "recent sample too thin to call it (%d PA, need 15)" % _hp_recent_pa
    elif _hp_l3_starts < 2:
        h.hr_pace_note = "no recent-pitcher read yet (need 2+ starts, have %d)" % _hp_l3_starts
    elif not _hp_pitcher_hot:
        h.hr_pace_note = "pitcher not currently HR-prone by his last 3 starts"
    else:
        h.hr_pace_note = "on pace or ahead of his own season rate"

    # Ambush setup: pitcher behind in counts + hitter swings early at fastballs.
    # We don't yet have per-hitter 1st-pitch swing% from a feed, so we infer it
    # from lineup-spot tendencies (1, 4, 5 spots ambush more) and recent K%.
    fps = safe_float(getattr(h, "pitcher_first_pitch_strike_pct", 0.600), 0.600)
    inferred_hitter_fps = 0.28 + (0.10 if h.lineup_spot in (1, 4, 5) else 0.0) - (0.08 if h.season_k_rate >= 0.28 else 0.0)
    h.hitter_first_pitch_swing_pct = max(0.10, min(0.55, inferred_hitter_fps))
    h.ambush_setup_flag = bool(has_advanced and fps <= 0.555 and h.hitter_first_pitch_swing_pct >= 0.30)

    # K-trap: pitcher finishes hitters off + hitter has K issues = HR pick that
    # never makes contact. Triggers an HR-score haircut later — BUT a genuinely
    # hot bat overrides this. Per-user rule: HOT wins over K-trap.
    putaway = safe_float(getattr(h, "pitcher_putaway_pct", 0.180), 0.180)
    swstr = safe_float(getattr(h, "pitcher_swstr_pct", 0.110), 0.110)
    _hot_override = (h.last5_hr >= 1) or (h.last7_hr >= 2) or (h.last5_xbh >= 3) or (h.last10_hr >= 3)
    h.k_trap_flag = bool(has_advanced and putaway >= 0.225 and swstr >= 0.130 and h.season_k_rate >= 0.26 and not _hot_override)

    # Apply ambush + mistake bonuses to pitcher_damage (HR side).
    if h.mistake_pitch_setup_flag:
        pitcher_damage = _hr2_clip(pitcher_damage + 5)
    if h.ambush_setup_flag:
        pitcher_damage = _hr2_clip(pitcher_damage + 3)
    # MINI-BOT AUDIT (2026-08-08): the +6 pitch-match bonus here was a
    # straight double-count — the same signal already enters hr_raw as
    # pitch_match_term at its own weight. Removed; one signal, one entry.


    # 10% pull-air / launch shape.
    pull_launch = 100 * (
        0.30 * minmax_norm(air_pull, 0.12, 0.45) +
        0.22 * minmax_norm(fb_rate, 0.18, 0.48) +
        0.18 * (_hr2_score_launch_angle(avg_la) / 100.0) +
        0.12 * minmax_norm(ld_rate, 0.10, 0.35) +
        0.10 * (1.0 - minmax_norm(gb_rate, 0.32, 0.58)) +
        0.08 * max(0.0, 1.0 - popup_rate * 2.0)
    )
    if gb_rate >= 0.50:
        pull_launch -= 12
    if popup_rate >= 0.20:
        pull_launch -= 8
    pull_launch = _hr2_clip(pull_launch)

    # 7% park/weather carry — now uses the per-stat HR park factor (range ~0.82–1.20).
    # Old code used a single 95–113 number; new code maps the HR factor directly.
    park_fit_score = safe_float((h.park_fit or {}).get("score"), 50.0) if isinstance(h.park_fit, dict) else 50.0
    temp = safe_float(getattr(h, "weather_temp_f", None), 70.0)
    temp_score = 50.0
    if temp >= 85: temp_score = 88.0
    elif temp >= 75: temp_score = 68.0
    elif temp <= 50: temp_score = 30.0
    wind_score = 50.0 + safe_float(getattr(h, "weather_wind_boost", 0.0), 0.0) * 500
    hr_park = safe_float(getattr(h, "park_hr_factor", 1.00), 1.00)
    # MINI-BOT AUDIT (2026-08-08, B6): these summed to 0.93, quietly
    # depressing every park/weather score ~7%. Wind takes the difference.
    park_weather = 100 * (
        0.40 * minmax_norm(hr_park, 0.82, 1.20) +
        0.35 * (temp_score / 100.0) +
        0.25 * minmax_norm(wind_score, 20, 80)
    )
    # Weather x park interaction (per audit, 2026-06-27): previously the three
    # terms above were purely additive -- a hot, wind-blowing-out day scored
    # identically whether it happened in Coors or a deep pitcher's park.
    # Real HR carry depends on BOTH: good conditions matter more in a park
    # already set up to let the ball travel, and matter less where it isn't.
    # Park factor still stands as its own baseline term above (so a
    # hitter's park keeps real value even on a calm, neutral-weather day) --
    # this adds a smaller, capped bonus/penalty on top when weather and park
    # lean the SAME direction (amplify) or OPPOSITE directions (dampen).
    _conditions = (0.55 * (temp_score - 50) + 0.45 * (wind_score - 50)) / 50.0  # roughly -1..+1
    _park_lean = (hr_park - 1.00) / 0.20  # roughly -0.9..+1.0
    _weather_park_interaction = max(-6.0, min(8.0, (_conditions * _park_lean) * 8.0))
    park_weather += _weather_park_interaction
    if park_fit_score >= 65:
        park_weather += 4
    elif park_fit_score < 40:
        park_weather -= 5
    if isinstance(h.park_fit, dict) and safe_float(h.park_fit.get("pull_lane_distance"), 999) <= 325 and air_pull >= 0.25:
        park_weather += 3
    park_weather = _hr2_clip(park_weather)

    # 5% lineup opportunity. 3% season power baseline.
    lineup_raw = 100.0 if h.lineup_confirmed else 65.0
    if 1 <= h.lineup_spot <= 4: lineup_raw *= 1.00
    elif h.lineup_spot == 5: lineup_raw *= 0.90
    elif h.lineup_spot in (6, 7): lineup_raw *= 0.75
    elif h.lineup_spot in (8, 9): lineup_raw *= 0.55
    if d375 >= 2 or h.last5_hr >= 2 or batted_shape >= 60:
        lineup_raw = max(lineup_raw, 80.0)
    lineup_opportunity = _hr2_clip(lineup_raw)
    # SMALL-SAMPLE SHRINKAGE (2026-08-23, the Veen bug -- see
    # shrink_to_league's docstring). All three inputs are season rates and
    # all three saturate their minmax bands on a handful of hot PAs, so all
    # three shrink toward league by season_pa. split_iso (the vs-hand ISO)
    # rides inside the max() and is shrunk by the same season_pa -- a side
    # split has FEWER PAs than the season line, so this under-shrinks it
    # slightly rather than inventing a per-side PA the record doesn't carry;
    # the direction is conservative and stated.
    _sp_pa = safe_float(getattr(h, "season_pa", 0.0), 0.0)
    _sp_iso = shrink_to_league(max(h.season_iso, split_iso), _sp_pa, LEAGUE_ISO)
    _sp_hrpa = shrink_to_league(h.hr_per_pa, _sp_pa, LEAGUE_HR_PER_PA)
    _sp_slg = shrink_to_league(h.season_slg, _sp_pa, LEAGUE_SLG)
    season_power = 100 * (0.50 * minmax_norm(_sp_iso, 0.08, 0.38) + 0.30 * minmax_norm(_sp_hrpa, 0.015, 0.085) + 0.20 * minmax_norm(_sp_slg, 0.330, 0.700))
    season_power = _hr2_clip(season_power)

    # ── VALIDATED-SIGNAL TERMS ──────────────────────────────────────────────
    # Added after correlating 7 days of graded results against every component
    # the bot computes. damage_conversion_score and pitch_type_match_score's
    # top deciles outperformed hr_score's own top decile (25-26% actual HR
    # rate vs 12.3%), yet neither fed into hr_raw before now -- damage
    # conversion ran only after hr_score was finalized, and pitch-type match
    # was capped at a +6 sub-bonus to pitcher_damage. Pulling both in directly,
    # with weight proportional to what they showed.
    damage_conversion_score = safe_float(getattr(h, "damage_conversion_score", None), 0.0)
    if not damage_conversion_score:
        compute_damage_conversion_v31(h)
        damage_conversion_score = safe_float(getattr(h, "damage_conversion_score", 50.0), 50.0)
    pitch_match_score_raw = safe_float(getattr(h, "pitch_type_match_score", 0.0), 0.0)
    # Missing-data sentinel handled in _pitch_match_term (2026-08-22): a raw
    # 0 means "no qualifying per-pitch sample," and now scores the same
    # neutral 50 as PMix's own N/A path instead of the floor of the term.
    pitch_match_term = _pitch_match_term(pitch_match_score_raw)

    # Weak-spot x contact-quality interaction: batting in the pitcher's
    # documented weak lineup spot/zone only matters if the hitter also shows
    # real recent contact quality (BBE-sample HRs or elite max EV) -- the
    # combination measured ~5pp better than either alone in backtesting.
    sample_hr_for_interaction = safe_int(bbe.get("hr"), 0)
    good_contact_signal = bool(sample_hr_for_interaction >= 2 or max_ev >= 108)
    # MINI-BOT AUDIT (2026-08-08): rebuilt as the ALIGNED STACK — the
    # largest clean interaction in the 38-day archive: weak_spot alone
    # 14.7%, pitch_match alone 16.5%, BOTH 22.3% (n=184), both + ISO ≥
    # .200 → 27.4% (n=106) vs 14.3% base. Tiered by how much of the stack
    # is present; weight raised 0.01 → 0.06 in MODEL_WEIGHTS to match.
    _stack_n = sum([
        1 if h.weak_spot_flag else 0,
        1 if getattr(h, "pitch_type_match_flag", False) else 0,
        1 if safe_float(getattr(h, "season_iso", 0.0), 0.0) >= 0.200 else 0,
    ])
    weak_spot_interaction = {3: 100.0, 2: 65.0, 1: 45.0}.get(_stack_n, 35.0)

    # Pitcher trend flag (per audit, 2026-07-07): is he getting hit harder
    # lately than his own season baseline? Built in
    # build_pitcher_statcast_profile from the 5-game-trend-vs-8-game-baseline
    # split. "worsening" = a real target to lean into; "improving" pulls back.
    trend_dir = str(getattr(h, "pitcher_trend_direction", "unknown"))
    pitcher_trend_term = 100.0 if trend_dir == "worsening" else (20.0 if trend_dir == "improving" else 50.0)

    # Batter-vs-this-pitcher signal (per audit, 2026-07-07): only trusted with
    # a real sample (10+ PA against this specific pitcher); otherwise stays
    # neutral so a 2-PA fluke can't swing the score.
    bvp_pa_n = safe_int(getattr(h, "bvp_pa", 0), 0)
    if bvp_pa_n >= 10:
        bvp_term = 100 * minmax_norm(safe_float(getattr(h, "bvp_woba", 0.320), 0.320), 0.280, 0.420)
    else:
        bvp_term = 50.0

    # Recency-first HR blend, reworked: batted_shape and pitch_fit (pitch_mix_score)
    # trimmed from a combined 63% to 41% to make room for damage_conversion_score
    # and pitch_type_match_score, the two strongest single predictors found in
    # backtesting. season_power restored from 0% -- its top decile alone (ISO/SLG)
    # ran 24%+ actual HR rate, it should not have been zeroed out.
    #
    # SLIGHT RE-WEIGHT (per-tier backtest, 22 days / 241 HR_PICKS rows):
    # pitch_type_match_score > 0 (a documented batter-vs-pitch exploit) hit
    # 23.9% vs 9.5% when absent -- the single largest separator found among
    # HR_PICKS specifically. pitch_fit (pitch_mix_score) is the coarser,
    # arsenal-level version of the same underlying signal, so 3pp moved from
    # pitch_fit (15% -> 12%) into pitch_match_term (5% -> 8%) rather than
    # disturbing any other term.
    # bullpen_pitch_fit: same calculate_pitch_mix_fit logic run against the
    # opposing bullpen's IP-weighted arsenal (per audit, 2026-06-27). Kept as
    # a small, secondary signal since the batter faces the starter first and
    # most often -- this just adds a bit of credit/risk for how he'd match up
    # if the at-bat happens later in the game. Funded by trimming
    # lineup_opportunity and weak_spot_interaction by 1pt each (both already
    # smaller, lower-confidence components).
    bullpen_pitch_fit_norm = 100 * minmax_norm(safe_float(getattr(h, "bullpen_pitch_fit", 50.0), 50.0), 35, 75)
    # pa_per_hr term (per audit, 2026-07-25): reuse the already-computed
    # hr_pa_score (100*minmax_norm(hr_per_pa, 0.015, 0.085)) rather than
    # re-deriving a new normalization -- it's already in the right
    # direction/scale (higher = more HR per PA = better) and already
    # exercised elsewhere in the codebase (hr_pa_tier, etc.), so this is
    # the lowest-risk way to wire in the strongest raw signal found in the
    # 4/27-7/24 backtest. Defaults to a neutral 50.0 if missing.
    pa_per_hr_term = safe_float(getattr(h, "hr_pa_score", 50.0), 50.0)

    # K-RATE. Normalised over the realistic league span (14%-32%); a hitter
    # who never strikes out is usually a slap hitter, not a power threat.
    k_rate_term = 100.0 * minmax_norm(
        safe_float(getattr(h, "season_k_rate", 0.0), 0.0), 0.14, 0.32)

    # TIMES THROUGH THE ORDER. How many looks this spot gets at the starter.
    # A starter faces ~24 batters in a typical outing; sharper arms go deeper,
    # so the estimate is nudged by his WHIP. Spot s gets its Nth look on
    # batter s + 9*(N-1), so the third look needs the arm to reach s + 18.
    # NOTE: named _tto_spot, not _spot -- apply_model_v2_layers already binds
    # _spot to the lineup-spot damage DICT further up, and shadowing it with
    # an int broke every later use of _spot["weight"].
    _tto_spot = safe_int(getattr(h, "lineup_spot", 5), 5) or 5
    _tto_whip = safe_float(getattr(h, "pitcher_whip", 1.28), 1.28) or 1.28
    # WHIP 1.10 -> ~26 batters faced, WHIP 1.50 -> ~21.
    _est_bf = 24.0 + (1.28 - _tto_whip) * 12.0
    _est_bf = max(18.0, min(30.0, _est_bf))
    # Fractional credit rather than a cliff: a spot sitting one batter short
    # of a third look is not the same as one sitting six short.
    _third_look = max(0.0, min(1.0, (_est_bf - (_tto_spot + 18.0)) / 4.0 + 0.5))
    times_through_term = 100.0 * _third_look

    _w = MODEL_WEIGHTS["hr_blend"]
    # The two 2026-08-09 residual terms. Both normalise against league-ish
    # ranges rather than the slate, so a night where every arm is good does not
    # manufacture a strong signal out of the least-bad one.
    pitcher_side_prod = 100.0 * (
        0.60 * minmax_norm(safe_float(getattr(h, "pitcher_side_ops", 0.720), 0.720), 0.620, 0.900)
        + 0.40 * minmax_norm(safe_float(getattr(h, "pitcher_side_slug", 0.400), 0.400), 0.330, 0.560)
    )
    # ⚠️ COMPUTED INLINE, NOT READ OFF THE RECORD. h.recent_hr_form_score is
    # assigned ~230 lines BELOW this point, so reading it here returned the
    # dataclass default of 0.0 on every first pass — the 0.05 weight was dead
    # and, worse, the other terms had already been scaled down to fund it, so
    # the blend was quietly running at 0.95 mass. Silent, and exactly the class
    # of bug that does not raise.
    #
    # Same formula as the assignment below; duplicated on purpose rather than
    # reordered, because moving that assignment up would put it before the
    # inputs IT depends on and just move the bug.
    recent_form = 100.0 * (
        0.34 * minmax_norm(h.last5_hr, 0, 3)
        + 0.22 * minmax_norm(h.last10_hr, 0, 5)
        + 0.22 * minmax_norm(h.l20pa_hr, 0, 3)
        + 0.22 * minmax_norm(h.last5_xbh + h.l20pa_xbh, 0, 7)
    )
    hr_raw = (
        _w["pitcher_side_prod"] * pitcher_side_prod +
        _w["recent_form"] * recent_form +
        _w["batted_shape"] * batted_shape +
        _w["pitch_fit"] * pitch_fit +
        _w["pitcher_damage"] * pitcher_damage +
        _w["pull_launch"] * pull_launch +
        _w["park_weather"] * park_weather +
        _w["lineup_opportunity"] * lineup_opportunity +
        _w["season_power"] * season_power +
        _w["damage_conversion_score"] * damage_conversion_score +
        _w["pitch_match_term"] * pitch_match_term +
        _w["weak_spot_interaction"] * weak_spot_interaction +
        _w["bullpen_pitch_fit"] * bullpen_pitch_fit_norm +
        _w["yesterdays_hitters_score"] * yesterdays_hitters_score +
        _w["pitcher_trend"] * pitcher_trend_term +
        _w["bvp_signal"] * bvp_term +
        _w["pa_per_hr"] * pa_per_hr_term +
        _w["k_rate"] * k_rate_term +
        _w["times_through"] * times_through_term
    )

    # weak_spot_bonus: five-tier graded score (0.034/0.028/0.020/0.016/0.010/0.0)
    # computed in score_hitter based on spot-damage heat, power-gate, and side-match
    # quality. Applied here as a post-blend additive scaled to hr_raw's 0-100 range.
    # Previously declared, computed, and then silently never consumed anywhere
    # (audit 2026-06-29). Wired in now as a small additive (max +3.4 points)
    # so the tiered nuance it encodes actually reaches the final hr_score.
    _wsb = safe_float(getattr(h, "weak_spot_bonus", 0.0), 0.0)
    if _wsb > 0:
        hr_raw = min(100.0, hr_raw + _wsb * 100.0)

    trap_reasons: List[str] = []
    hard_cap = 100.0

    # LINE-DRIVE HR LOOK: some hitters run a real HR total inside their recent
    # BBE sample (low-ball, high-EV pull/line-drive bombers) while their *other*
    # contact is mostly grounders/topped balls — this drags the average launch
    # angle down into "flat" territory even though the power is clearly real and
    # current. Average LA can't see this bimodal shape, so detect it directly:
    # 2+ HRs OR 2+ balls 400ft+ already sitting in the sample, paired with a
    # legitimate max EV, means "flat LA" is measuring his outs, not his power.
    sample_hr = safe_int(bbe.get("hr"), 0)
    line_drive_hr_profile = bool((sample_hr >= 2 or d400 >= 2) and max_ev >= 105)

    if pitch_trap:
        hr_raw *= 0.72
        trap_reasons.append(pitch_trap_reason or "Pitch-type groundball trap")
    if popup_rate >= 0.30:
        hr_raw *= 0.78
        trap_reasons.append(f"Popup-heavy recently ({popup_rate*100:.0f}% popup in L10)")
    if avg_la < 5 and not line_drive_hr_profile:
        hr_raw *= 0.70
        trap_reasons.append(f"Negative/flat launch angle recently ({avg_la:.1f}° avg LA)")
    same_hand = (h.bats == h.pitcher_throws) if h.bats in {"L", "R"} and h.pitcher_throws in {"L", "R"} else False
    if same_hand and split_iso < 0.100:
        hr_raw *= 0.80
        trap_reasons.append(f"{h.bats}HB vs same-hand pitcher — weak split (ISO {split_iso:.3f})")
    if fb_rate < 0.15 and ld_rate < 0.15:
        hr_raw *= 0.82
        trap_reasons.append("No air contact recently")
    if h.pitcher_era <= 2.50 and h.pitcher_whip <= 1.05:
        hr_raw *= 0.86
        trap_reasons.append("Elite pitcher suppresses power")
    if avg_ev >= 95 and avg_la < 10 and gb_rate >= 0.50 and not line_drive_hr_profile:
        hard_cap = min(hard_cap, 45.0)
        trap_reasons.append(f"High exit velocity but hits it on the ground ({gb_rate*100:.0f}% GB in L10)")
    if old_hr_score >= 42 and air_pull < 0.20:
        trap_reasons.append(f"Pulls grounders not air — overall pull {raw_pull*100:.0f}% but air pull only {air_pull*100:.0f}%")
    if old_hr_score >= 42 and pitch_fit < 48:
        trap_reasons.append("Strong broad profile but HR shape missing today")
    if old_hr_score >= 42 and popup_rate >= 0.25:
        trap_reasons.append(f"Popup-heavy recently ({popup_rate*100:.0f}% popup in L10)")
    if old_hr_score >= 42 and avg_la < 10 and h.season_iso >= 0.200 and not line_drive_hr_profile:
        trap_reasons.append(f"Power name but flat launch angle recently ({avg_la:.1f}° avg LA)")
    if line_drive_hr_profile and old_hr_score >= 42 and avg_la < 10:
        trap_reasons.append(f"Line-drive HR look — {sample_hr} HR in recent sample despite {avg_la:.1f}° avg LA")
    if raw_pull >= 0.45 and air_pull < 0.22:
        trap_reasons.append(f"Pulls grounders not air — overall pull {raw_pull*100:.0f}% but air pull only {air_pull*100:.0f}%")
    # K-trap: pitcher finishes hitters off + hitter has K issues.
    if getattr(h, "k_trap_flag", False):
        hr_raw *= 0.84
        trap_reasons.append(f"K-trap: {putaway*100:.0f}% PutAway + {swstr*100:.0f}% SwStr vs {h.season_k_rate*100:.0f}% K%")

    hr_score_new = round(max(0.0, min(hard_cap, hr_raw)), 2)
    if old_hr_score >= 42 and hr_score_new < 38:
        trap_reasons.append("Strong broad profile but HR shape missing today")
    trap_flag = bool(trap_reasons)

    signal1 = bool(max_ev >= 97 and 20 <= avg_la <= 35)
    signal2 = bool(air_pull >= 0.28 or fb_rate >= 0.35)
    signal3 = bool(strong_pitch_signal or pitch_fit >= 60)
    strong_confirmed = signal1 and signal2 and signal3
    if hr_score_new >= 60 and not strong_confirmed:
        hr_score_new = min(hr_score_new, 59.0)

    # ── HR GATE (v2) ──────────────────────────────────────────────────────────
    # 5 signals. ISO is now a first-class gate signal — season power floor
    # matters independently of recency, confirmed by May+June combined data.
    # Contact-starved override: cold HRW but still barreling/high EV counts as
    # a half-recency signal so power bats in a contact drought aren't punished
    # the same as genuinely weak profiles.
    ideal_hr_contact = safe_float(getattr(h, "recent_ideal_hr_contact", 0.0), 0.0)
    contact_starved  = bool(h.hrw_score < 55 and avg_ev >= 92 and barrel_rate >= 0.06)
    _gt = MODEL_WEIGHTS["hr_gate_thresholds"]
    gate_iso     = bool(h.season_iso >= _gt["iso"])
    gate_hrw     = bool(h.hrw_score >= _gt["hrw"] or contact_starved)
    gate_form    = bool(h.last5_hr >= _gt["form_last5_hr"] or h.last10_hr >= _gt["form_last10_hr"])
    gate_power   = bool(batted_shape >= _gt["batted_shape"])
    gate_ihr     = bool(ideal_hr_contact >= _gt["ideal_hr_contact"] or barrel_rate >= _gt["barrel_rate"])
    gate_signals = sum([gate_iso, gate_hrw, gate_form, gate_power, gate_ihr])

    # Penalty is asymmetric by ISO — penalize truly weak profiles hard,
    # let power bats in cold stretches off easier.
    # 0-of-5, ISO <.160 → -18 (true weak profile)
    # 0-of-5, ISO ≥.160 → -8  (power name, just cold — contact-starved case)
    # 1-of-5             → -6
    # 2-of-5             → no change (passes)
    # 3-of-5             → +3
    # 4-of-5             → +5
    # 5-of-5             → +8
    if gate_signals == 0:
        penalty = -18.0 if h.season_iso < 0.160 else -8.0
        hr_score_new = max(0.0, hr_score_new + penalty)
        if not any("gate" in r for r in trap_reasons):
            trap_reasons.append(f"No HR gate signals — ISO {h.season_iso:.3f}, cold HRW, no recent form")
        trap_flag = True
    elif gate_signals == 1:
        hr_score_new = max(0.0, hr_score_new - 6.0)
        if hr_score_new < 46:
            # BUGFIX: this set trap_flag=True directly, but the unconditional
            # `trap_flag = bool(trap_reasons)` recompute a few lines below
            # silently reverted it back to False whenever trap_reasons was
            # still empty -- which it always was here, since (unlike the
            # gate_signals==0 branch above) nothing was ever appended to
            # trap_reasons in this branch. Confirmed in real data: 59 rows
            # last month had hr_score_v2 < 46 with trap_flag=False and an
            # empty trap_reason, consistent with this exact code path firing
            # and then being silently undone. Appending a reason here, same
            # pattern as the gate_signals==0 branch, makes the flag survive.
            if not any("gate" in r for r in trap_reasons):
                trap_reasons.append(f"Only 1 of 5 HR gate signals — score dropped below 46 after penalty")
            trap_flag = True
    elif gate_signals == 3:
        hr_score_new = min(100.0, hr_score_new + 3.0)
    elif gate_signals == 4:
        hr_score_new = min(100.0, hr_score_new + 5.0)
    elif gate_signals == 5:
        hr_score_new = min(100.0, hr_score_new + 8.0)
    # gate_signals == 2 → passes clean

    # "Yesterdays Hitters" all-criteria-match bonus: a separate, additive
    # bonus on top of the blend weight above, mirroring the highlight tool's
    # own "all must match" all-or-nothing semantics rather than just letting
    # the small 2% blend weight carry it. Kept modest (+5) since the 9
    # underlying criteria already get partial credit through the blend.
    if yesterdays_hitters_all_match:
        hr_score_new = min(100.0, hr_score_new + 5.0)

    hr_score_new = round(hr_score_new, 2)
    # Additive, not a blind overwrite: trap_flag may already be True from an
    # explicit branch above even if trap_reasons is momentarily empty (the
    # bug this guarded against -- see gate_signals==1 fix above). OR-ing
    # preserves any earlier explicit True instead of silently reverting it.
    trap_flag = trap_flag or bool(trap_reasons)

    hidden = bool(
        old_hr_score < 42 and hr_score_new >= 48 and not trap_flag and (
            pitch_fit >= 68 or d375 >= 2 or (air_pull >= 0.38 and fb_rate >= 0.35) or (22 <= avg_la <= 34 and max_ev >= 96) or d400 >= 1 or (safe_int(bbe.get("hr"), 0) >= 1 and max_ev >= 100)
        )
    )

    h.hr_score_old = round(old_hr_score, 2)
    h.hr_score_legacy = round(old_hr_score, 2)
    h.hr_score_v2 = round(hr_score_new, 2)
    h.hr_score_delta = round(h.hr_score_v2 - h.hr_score_old, 2)
    h.hr_confidence_tier = _hr2_confidence(h.hr_score_v2)
    h.trap_flag = trap_flag
    h.trap_risk_flag = trap_flag
    h.trap_reason = trap_reasons[0] if trap_reasons else ""
    h.hidden_hr_value = hidden
    h.hidden_value_reason = "Underrated play — pitch matchup and contact quality say more than his name suggests" if hidden else ""
    h.best_bet_type, h.beginner_label = _hr2_best_bet_and_label(h, h.hr_score_v2, pitch_fit, trap_flag, hidden, strong_confirmed)
    reasons = _hr2_first_reasons(h, bbe, pitch_fit, h.trap_reason, hidden)
    h.simple_reason_1, h.simple_reason_2, h.simple_reason_3 = reasons
    # 2026-08-12: the three non-trap branches below used to be flat strings —
    # "Playable power, but one of the key HR-shape signals is missing" showed
    # up byte-for-byte on unrelated players the same night (spotted directly
    # in a screenshot: Jake McCarthy and Victor Mesa Jr. both carried it).
    # h.hr_score_v2 and h.lineup_spot are already set above; using them here
    # costs nothing and means two players in the same branch no longer read
    # as one copy-pasted sentence.
    if trap_flag:
        h.risk_reason = h.trap_reason
    elif not strong_confirmed and h.hr_score_v2 >= 45:
        h.risk_reason = f"Playable power ({h.hr_score_v2:.0f} score), but one of the key HR-shape signals is missing"
    elif h.lineup_spot >= 7:
        h.risk_reason = f"Batting #{h.lineup_spot} — a lower lineup spot can reduce plate appearances"
    else:
        h.risk_reason = f"No major HR-shape trap found ({h.hr_score_v2:.0f} score)"
    h.advanced_reason = f"HR2 {h.hr_score_v2:.1f}: BBE {batted_shape:.1f}, PMix {pitch_fit:.1f}, P-DMG {pitcher_damage:.1f}, Air/LA {pull_launch:.1f}, Park {park_weather:.1f}; old {old_hr_score:.1f} Δ {h.hr_score_delta:+.1f}"
    h.pitch_fit_summary = f"{h.pitch_mix_note} vs {h.pitcher_primary_mix}"
    h.park_fit_summary = str((h.park_fit or {}).get("reason", "Park fit neutral")) if isinstance(h.park_fit, dict) else "Park fit neutral"
    h.hr_shape_components = {
        "batted_ball_damage": round(batted_shape, 1),
        "pitch_type_fit": round(pitch_fit, 1),
        "pitcher_hr_damage": round(pitcher_damage, 1),
        "pull_air_launch": round(pull_launch, 1),
        "park_weather": round(park_weather, 1),
        "lineup_opportunity": round(lineup_opportunity, 1),
        "season_power_baseline": round(season_power, 1),
        "confirmation_signals": {"ideal_ev_la": signal1, "air_or_fb": signal2, "pitch_fit": signal3},
        "raw_pull_rate": round(raw_pull, 3),
        "air_pull_rate": round(air_pull, 3),
        "gb_rate": round(gb_rate, 3),
        "popup_rate": round(popup_rate, 3),
        "avg_la": round(avg_la, 1),
        "max_ev": round(max_ev, 1),
        "max_distance": round(max_distance, 1),
    }
    h.missing_data_flags = list(dict.fromkeys(missing_flags))
    h.confidence_penalty_reason = ", ".join(h.missing_data_flags) if h.missing_data_flags else ""

    # Keep existing non-HR scores compatible, but keep HR shape as the single displayed HR score.
    h.hit_score_legacy = h.hit_score
    h.hrr_score_legacy = h.hrr_score
    h.contact_score_legacy = h.contact_score
    h.overall_score_legacy = h.overall_score

    k_floor = 1.0 - minmax_norm(h.season_k_rate, 0.12, 0.34)
    lineup_top = 1.0 if h.lineup_spot in (1, 2, 3, 4, 5) else 0.72 if h.lineup_spot == 6 else 0.50
    pitcher_traffic = (
        0.36 * minmax_norm(h.pitcher_whip, 1.05, 1.60) +
        0.24 * minmax_norm(h.pitcher_babip, 0.250, 0.340) +
        0.20 * minmax_norm(h.pitcher_era, 3.00, 5.80) +
        0.20 * minmax_norm(h.bullpen_whip, 1.05, 1.55)
    )
    h.recent_hr_form_score = round(100 * (0.34 * minmax_norm(h.last5_hr, 0, 3) + 0.22 * minmax_norm(h.last10_hr, 0, 5) + 0.22 * minmax_norm(h.l20pa_hr, 0, 3) + 0.22 * minmax_norm(h.last5_xbh + h.l20pa_xbh, 0, 7)), 1)
    h.batted_ball_power_score = round(batted_shape, 1)
    h.matchup_power_score = round(0.55 * pitch_fit + 0.45 * pitcher_damage, 1)
    h.pitch_mix_boost = round(pitch_fit - 50.0, 1)
    h.batter_vs_bullpen_score = round(100 * (0.58 * minmax_norm(batted_shape, 35, 85) + 0.42 * minmax_norm(h.bullpen_attack_score, 35, 75)), 1)

    # Recency-first hit_v2: shifts internal mix from ~30% recent → 50% recent.
    # Added split_avg weight boost (+5%) and lineup_top promotion for spots 1-2
    # to target +15% base hit rate on top picks.
    # Hit score: recency leads, now includes pitch-mix fit (how well his bat
    # matches what this pitcher throws) and pitcher's hit-allowed at his spot.
    pmix_norm = minmax_norm(safe_float(getattr(h, "pitch_mix_score", 50.0), 50.0), 45.0, 90.0)
    # Reweighted per audit (2026-06-27): recent_results trimmed 43%->29% to
    # fund increases across the rest -- baseline_contact, lineup_context,
    # pitch_mix_fit, pitcher_traffic, weak_side_edge, and lineup_spot_dmg all
    # moved up. Recency still matters most for "is he getting hits right
    # now," but no longer dominates this heavily.
    # pitcher_attack_score added per audit (2026-06-28): the real composite
    # pitcher-vulnerability signal (flyball/barrel/hard-hit/EV/HR9 allowed)
    # was completely absent from hit_v2/hrr_v2 despite being a clean,
    # already-computed 0-100 field. Kept small here since it's fundamentally
    # a POWER/loud-contact signal, not a contact/OBP signal -- hit_v2 is
    # about contact skill, so this only gets a light touch (funded by a
    # small trim to pitcher_traffic, which already covers pitcher quality).
    hit_v2 = 100 * (
        0.29 * (0.40 * minmax_norm(h.last5_hits, 0, 9) + 0.28 * minmax_norm(h.last10_hits, 0, 14) + 0.18 * minmax_norm(h.last7_hits, 0, 11) + 0.14 * minmax_norm(h.recent_xwoba, 0.280, 0.460)) +
        0.24 * (0.35 * k_floor + 0.28 * minmax_norm(h.season_avg, 0.200, 0.330) + 0.25 * minmax_norm(split_avg, 0.180, 0.360) + 0.12 * minmax_norm(h.babip, 0.250, 0.370)) +
        0.17 * (0.50 * lineup_top + 0.30 * (1.0 if h.lineup_confirmed else 0.65) + 0.20 * minmax_norm(h.season_pa, 35, 220)) +
        0.10 * pmix_norm +
        0.05 * pitcher_traffic +
        0.05 * _weak_side_edge +
        0.08 * (_spot["weight"] * _spot["slg"] + (1 - _spot["weight"]) * 0.5) +
        0.02 * minmax_norm(safe_float(getattr(h, "pitcher_attack_score", 50.0), 50.0), 35, 75)
    )
    h.hit_score_v2 = round(_hr2_clip(hit_v2), 2)

    # 2026-08-13, Donovan: "how can we use the data we have to find players
    # that will get two hits and how can we flag or tag them." He named the
    # weak_spot star as "a good indicator" -- it is, but checked line by
    # line, damage_score (what actually drives weak_spot_flag) is 28% HR
    # rate + 18% XBH rate + 18% SLG + 14% ISO + 12% hard-hit% + 10% barrel% --
    # zero plain-contact signal in it. Reusing it here would miss exactly the
    # games this flag exists to catch: two seeing-eye singles trips no power
    # threshold at all.
    #
    # Built the way hit_score_v2 just above already validates "gets a hit":
    # contact skill (K-rate, season AVG, BABIP, recent hit volume) -- plus
    # the piece that matters more for a SECOND hit than a first: raw PA
    # volume. A second hit needs a second (often third) at-bat to land in,
    # and lineup spot is the real driver of that -- a leadoff man sees
    # roughly a full extra plate appearance a game over the 9-hole across a
    # season. Pitcher side is rebuilt from the contact stats weak_spot
    # deliberately doesn't use -- WHIP, AVG/OBP allowed, BABIP allowed --
    # instead of copying its HR/XBH-anchored damage_score.
    #
    # HONEST CAVEAT, carried into the site-side tooltip too: unlike
    # weak_spot_flag (18.0% vs 13.9% across the graded archive) this is a
    # brand-new signal with zero games graded yet. First cut, not a proven
    # one, until there's a real sample to check it against.
    _pa_volume = {
        1: 1.00, 2: 0.97, 3: 0.94, 4: 0.90, 5: 0.86,
        6: 0.80, 7: 0.74, 8: 0.68, 9: 0.62,
    }.get(int(h.lineup_spot or 0), 0.75)
    _contact_skill = (
        0.32 * k_floor +
        0.28 * minmax_norm(h.season_avg, 0.200, 0.330) +
        0.24 * minmax_norm(h.babip, 0.250, 0.370) +
        0.16 * (0.5 * minmax_norm(h.last5_hits, 0, 9) + 0.5 * minmax_norm(h.last10_hits, 0, 14))
    )
    _pitcher_hittable = (
        0.35 * minmax_norm(h.pitcher_whip, 1.05, 1.60) +
        0.30 * minmax_norm(h.pitcher_avg_against, 0.220, 0.290) +
        0.20 * minmax_norm(h.pitcher_obp_against, 0.280, 0.360) +
        0.15 * minmax_norm(h.pitcher_babip, 0.260, 0.330)
    )
    multi_hit_raw = 100 * (0.50 * _contact_skill + 0.30 * _pa_volume + 0.20 * _pitcher_hittable)
    h.multi_hit_score = round(_hr2_clip(multi_hit_raw), 2)
    _multi_hit_gate = h.season_avg >= 0.270 or h.babip >= 0.300 or h.last10_hits >= 10
    h.multi_hit_flag = bool(h.multi_hit_score >= 62 and _multi_hit_gate and _pa_volume >= 0.80)
    h.multi_hit_reason = (
        f"Strong contact skill, lineup spot {h.lineup_spot} (real PA volume), "
        f"and a hittable arm (multi-hit score {h.multi_hit_score:.0f})"
    ) if h.multi_hit_flag else ""

    # HRR score: weights spread wider. Lineup context (which now includes
    # surrounding-batter recent form) is the lead. Fly-ball rate added — air
    # contact drives XBH/runs. Pitcher damage at his spot folded in.
    #
    # Reweighted per audit (2026-06-27): lineup_context, pitcher_traffic,
    # season_baseline, lineup_spot_xbh, and recent_xbh_blend all bumped up;
    # funded by trimming recent_production (36%->30%), flyball_rate (6%->2%),
    # and weak_side_edge (4%->1%, the component itself is now stronger via
    # the WHIP factor added earlier even though its weight here is smaller).
    # season_baseline also now includes batter BABIP and pitcher BABIP
    # (neither was in this layer before, both already exist as live fields).
    fb_norm = minmax_norm(safe_float(getattr(h, "recent_fb_rate", getattr(h, "fb_rate", 0.34)), 0.34), 0.20, 0.50)
    # Reworked per audit (2026-06-28): recent_production now runs/RBI only --
    # hits removed (that's hit_score's job, not HRR's). flyball rate,
    # weak_side_edge, and pitcher_attack_score converted from small additive
    # slivers (2%/1%/4%) into multipliers instead -- each was too small to
    # matter as a flat slice but makes more sense as a "this specific
    # context nudges the whole score up or down a bit" factor. Remaining
    # additive weights rebalanced to fill the freed 7%.
    hrr_v2 = 100 * (
        0.32 * (0.35 * minmax_norm(h.last5_runs, 0, 7) + 0.35 * minmax_norm(h.last5_rbi, 0, 7) + 0.30 * minmax_norm(h.last7_runs + h.last7_rbi, 0, 14)) +
        0.32 * minmax_norm(h.lineup_context_score, 35, 90) +
        0.12 * pitcher_traffic +
        0.09 * (0.40 * minmax_norm(h.season_obp, 0.280, 0.430) + 0.22 * k_floor + 0.18 * minmax_norm(split_avg, 0.180, 0.360) + 0.12 * minmax_norm(h.babip, 0.250, 0.370) + 0.08 * minmax_norm(h.pitcher_babip, 0.250, 0.340)) +
        0.07 * (_spot["weight"] * _spot["xbh"] + (1 - _spot["weight"]) * 0.5) +
        0.04 * (0.40 * minmax_norm(h.last5_xbh, 0, 5) + 0.30 * minmax_norm(ideal_hr, 0.04, 0.22) + 0.30 * minmax_norm(h.bullpen_attack_score, 35, 75)) +
        # Season-long RBI/runs production rate, added per audit (2026-06-28)
        # -- the genuine season-long stabilizer this formula was missing
        # entirely (it's named "Hits, Runs, RBI" but had zero season RBI/run
        # context, only recency windows + an OBP/AVG-focused baseline).
        0.04 * (0.50 * minmax_norm(h.season_rbi_per_pa, 0.06, 0.18) + 0.50 * minmax_norm(h.season_runs_per_pa, 0.08, 0.20))
    )
    # Multiplier conversions: each nudges the score by a small, bounded
    # amount rather than diluting the additive blend with a tiny slice.
    _hrr_fb_mult = 0.97 + 0.08 * fb_norm                                                              # 0.97x - 1.05x
    _hrr_weak_side_mult = 0.98 + 0.06 * minmax_norm(_weak_side_edge, 0.0, 1.0)                         # 0.98x - 1.04x
    _hrr_pitcher_attack_mult = 0.95 + 0.12 * minmax_norm(safe_float(getattr(h, "pitcher_attack_score", 50.0), 50.0), 35, 75)  # 0.95x - 1.07x
    hrr_v2 *= _hrr_fb_mult * _hrr_weak_side_mult * _hrr_pitcher_attack_mult
    if h.lineup_spot >= 7:
        hrr_v2 *= 0.90
    elif h.lineup_spot <= 2:
        hrr_v2 *= 1.06
    h.hrr_score_v2 = round(_hr2_clip(hrr_v2), 2)

    # contact_v2 / TB score: ISO directly in the formula as power floor.
    # Boosts TB ceiling for high-ISO + high-EV players regardless of HRW zone.
    park_bar = safe_float(getattr(h, "park_barrel_factor", 1.00), 1.00)
    # ISO recency fix (per audit, 2026-06-27): was season-only, inconsistent
    # with the rest of this formula's recency lean. Blends season ISO with a
    # recent-power proxy (barrel rate + hard-hit rate, both already recency
    # tracked) so a real recent power dip or surge actually moves this term.
    _recent_power_for_iso = 0.5 * minmax_norm(h.recent_barrel_rate, 0.03, 0.18) + 0.5 * minmax_norm(h.recent_hard_hit_rate, 0.28, 0.55)
    iso_power_boost = 0.65 * minmax_norm(h.season_iso, 0.120, 0.320) + 0.35 * _recent_power_for_iso

    # Arm-side split for the contact-quality blend (per audit, 2026-06-27):
    # avg_ev/hard_rate/barrel_rate were all-time (not split by today's
    # pitcher's hand) -- same gap already fixed for pitch_mix_score. Computed
    # here as a usage-weighted average across the batter's handed by_pitch
    # slice, falling back to the existing all-time values on thin samples.
    def _handed_contact_avg(metric_key: str, fallback: float) -> float:
        handed_key = "vs_lhp" if h.pitcher_throws == "L" else "vs_rhp" if h.pitcher_throws == "R" else None
        if not handed_key:
            return fallback
        slice_data = (h.batter_pitch_type_profile or {}).get(handed_key, {}) or {}
        by_pitch_handed = slice_data.get("by_pitch", {}) or {}
        total_seen = sum(safe_int(v.get("seen"), 0) for v in by_pitch_handed.values())
        if total_seen < 40:
            return fallback
        weighted_sum = sum(safe_float(v.get(metric_key), 0.0) * safe_int(v.get("seen"), 0) for v in by_pitch_handed.values())
        return weighted_sum / total_seen if total_seen else fallback

    avg_ev_handed = _handed_contact_avg("avg_ev", avg_ev)
    hard_rate_handed = _handed_contact_avg("hard_hit_rate", hard_rate)
    barrel_rate_handed = _handed_contact_avg("barrel_like_rate", barrel_rate)

    # REWEIGHT (2026-07-31). Measured against 3,265 graded player-days, the
    # composite predicted an actual extra-base hit at r=+0.029 (t=1.69) --
    # not significant. The block structure was why: recent_barrel_rate
    # (r=+0.003), avg exit velo (r=+0.000) and hard-hit rate (r=+0.028) made
    # up 40% of the score and none of them measurably predicts XBH, while
    # last5_xbh (r=+0.097, t=5.58) and last10_xbh (r=+0.078) carried only 28%.
    # Quality-of-contact rates describe a hitter's TYPICAL contact; an
    # extra-base hit is one swing in one game. So the blocks swap emphasis.
    #   xbh history 0.28->0.40, quality 0.40->0.26, iso 0.14->0.16,
    #   pitcher 0.10->0.12, spot/k_floor 0.04->0.03 each.  Sums to 1.00.
    # Caveat: contact_score can't be recomputed from the archive, so unlike
    # the recency re-anchor this is reasoned from component correlations
    # rather than backtested end to end. Grade the Contact tier's XBH rate in
    # ~2 weeks; git log has the old weights.
    # ── CONTACT v3 (2026-08-09) ──────────────────────────────────────────────
    # Donovan: "contact score needs to find XBH, triples for sure and doubles.
    # Rebuild it based on everything you can find from the results."
    #
    # WHY IT NEEDED REBUILDING, not re-weighting. Backtested for the first time
    # across 37 graded nights: the top 20 by contact_score got 2+ total bases
    # 41.5% of the time against 41.1% for twenty names drawn at RANDOM off the
    # same slate. It won 16 of 32 decisive nights. That is a coin flip with a
    # formula attached. The v2 note above admitted it had never been graded
    # end to end -- it was reasoned from component correlations -- and this is
    # what that turned out to be worth.
    #
    # THE STRUCTURAL MISTAKE, which the archive makes obvious. v2 was built out
    # of HOME RUN ingredients: quality of contact, barrels, exit velocity, ISO.
    # But split the outcome up and the pieces point opposite ways. Of the 40.3%
    # of picks that clear 2 TB, 15.0% did it WITH a homer, 8.7% with two
    # singles, and 16.6% with an actual double or triple. And against that last
    # group -- the extra-base hit this market is named for -- power is
    # NEGATIVELY correlated:
    #
    #     season_hr      top decile 11.0% vs bottom 24.2%   -13.2
    #     season_iso                10.6%            22.6%   -11.9
    #     hr_per_pa                  9.0%            21.0%   -11.9
    #
    # Which is obvious once seen: a 40-homer bat turns a double into a homer.
    # v2 was loading up on the one trait that converts the outcome away.
    #
    # WHAT ACTUALLY SEPARATES 2+ TB, measured top vs bottom decile over 3,454
    # graded picks:
    #
    #     park_hits_factor          52.1% vs 31.2%   +20.8   (strongest field)
    #     park_k_factor             26.4%    45.1%   -18.7
    #     pitcher_putaway_pct       28.5%    45.1%   -16.7
    #     last7_xbh                 42.4%    28.6%   +13.8
    #     last10_hits               44.7%    31.5%   +13.2
    #     pitcher_hr9               45.4%    32.5%   +12.9
    #     last10_xbh                42.6%    31.0%   +11.6
    #
    # Recent extra-base FORM, the arm's contact-allowed profile, and the
    # building. Note park_hits_factor is the single best field on the board and
    # v2 scaled by park_BARREL_factor instead -- which measures -11.1 against
    # extra-base hits. It was multiplying by the wrong park number.
    #
    # WEIGHTS. Fit on the first 22 nights only and tested on the last 15, so
    # nothing below saw its own test set. Each term's weight is its measured
    # top-vs-bottom-tercile lift on the training half; terms below +0.02 were
    # dropped rather than kept at a small weight.
    #
    # HOW WELL IT WORKS, and this is the part not to oversell. On the 15
    # held-out nights v3 beat v2 in all nine (outcome x cutoff) configurations
    # tested -- 2+ TB, 3+ TB and XBH, at top 10/20/40 -- by +1.0 to +10.0
    # points. It did NOT reliably beat a random shuffle in any of them.
    #
    # So: strictly better than what shipped, built on measurement instead of
    # reasoning, and STILL NOT PROVEN to sort. My read is that the ceiling here
    # is structural rather than a weighting problem. By the time the bot has
    # designated ~100 hitters the pool is homogeneous, the base rate is 40%,
    # and whether one of them gets two bases in one game is close to a coin
    # flip. The two biggest signals above -- park_hits_factor and
    # pitcher_side_slug -- are GAME-level, not hitter-level, which hints the
    # honest version of this market may be "which games produce bases" rather
    # than "which hitter does". That is a bigger change than a formula and it
    # should be Donovan's call, so v3 ships as the better formula and the site
    # should keep saying what it is.
    #
    # Re-run: python3 bots/backtest_all.py --dir ~/Desktop/results --only markets
    _pk_hits = safe_float(getattr(h, "park_hits_factor", 1.00), 1.00)
    _pk_k = safe_float(getattr(h, "park_k_factor", 1.00), 1.00)
    _putaway = safe_float(getattr(h, "pitcher_putaway_pct", 0.180), 0.180)
    _fps = safe_float(getattr(h, "pitcher_first_pitch_strike_pct", 0.60), 0.60)
    contact_v3 = 100 * (
        # ── recent extra-base form, 0.55 ── the three windows that measured
        # strongest and, unlike a rate, describe the event itself.
        0.55 * (
            0.38 * minmax_norm(h.last5_xbh, 0, 5) +
            0.35 * minmax_norm(h.last7_xbh, 0, 5) +
            0.22 * minmax_norm(h.last10_xbh, 0, 7) +
            0.05 * minmax_norm(h.l20pa_xbh, 0, 4)
        ) +
        # ── the arm's contact-allowed profile, 0.24 ── who gives up loud
        # contact, and (inverted) who ends at-bats before contact happens.
        0.24 * (
            0.28 * minmax_norm(getattr(h, "pitcher_375_allowed", 0), 0, 8) +
            0.22 * (1.0 - minmax_norm(_putaway, 0.14, 0.28)) +
            0.18 * minmax_norm(h.pitcher_whip, 1.05, 1.60) +
            0.13 * minmax_norm(h.pitcher_hardhit_allowed, 0.30, 0.52) +
            0.11 * minmax_norm(h.pitcher_hr9, 0.6, 2.2) +
            0.08 * (1.0 - minmax_norm(_fps, 0.55, 0.68))
        ) +
        # ── opportunity and hit volume, 0.13 ── more trips, more chances.
        0.13 * (
            0.55 * (1.0 - minmax_norm(safe_int(getattr(h, "lineup_spot", 5), 5), 1, 9)) +
            0.45 * minmax_norm(h.last10_hits, 0, 14)
        ) +
        # ── power, 0.08 and no more ── a homer IS four bases, so this cannot
        # be zero. It was 0.16 plus a whole quality-of-contact block, and that
        # is what pushed the score toward hitters who convert doubles away.
        0.08 * iso_power_boost
    ) * (
        # ── the building ── park_hits_factor, not park_barrel_factor. This
        # multiplier is deliberately wider than v2's (0.92-1.08) because it is
        # the strongest measured field on the board, not a garnish.
        (0.88 + 0.24 * minmax_norm(_pk_hits, 0.94, 1.08))
        # a park that eats at-bats with strikeouts takes bases off the table
        * (1.04 - 0.08 * minmax_norm(_pk_k, 0.94, 1.08))
    )
    h.contact_score_v2 = round(_hr2_clip(contact_v3), 2)

    hrw_timing_score = hrw_zone_score_value(h.hrw_score)
    h.best_blend_score = round(0.45*h.hr_score_v2 + 0.15*hrw_timing_score + 0.15*pitch_fit + 0.10*h.hrr_score_v2 + 0.10*h.recent_hr_form_score + 0.05*h.matchup_power_score, 2)
    h.alt_hr_score = round(_hr2_clip(0.40*h.batted_ball_power_score + 0.24*pitch_fit + 0.14*pitcher_damage + 0.12*pull_launch + 0.10*park_weather - max(0, h.hr_score_v2 - 45)*0.10), 2)

    # overall_score: ISO now a direct component (confirmed strongest stable signal
    # across both May and June). HRW back to 0.08 — too volatile month-to-month
    # to carry 0.14. Recency still leads via hr_score which already embeds it.
    _lineup_mult = 1.0 if h.lineup_spot <= 2 else 0.95 if h.lineup_spot <= 4 else 0.85 if h.lineup_spot == 5 else 0.74 if h.lineup_spot in (6, 7) else 0.60
    _iso_component = minmax_norm(h.season_iso, 0.080, 0.320) * 100
    _overall_raw = (
        0.38 * h.hr_score_v2 +
        0.20 * h.hrr_score_v2 +
        0.13 * h.hit_score_v2 +
        0.06 * _iso_component +
        0.08 * hrw_timing_score +
        0.08 * h.contact_score_v2 +
        0.07 * pitch_fit
    ) * _lineup_mult

    top_board_raw = 0.45*h.hr_score_v2 + 0.20*pitch_fit + 0.15*h.batted_ball_power_score + 0.10*pull_launch + 0.05*park_weather + 0.05*hrw_timing_score
    # MINI-BOT AUDIT (2026-08-08): the -8 trap penalty is retired — graded
    # 15.5% vs 15.3% (708/1344 flagged), zero discrimination, while docking
    # a third of the board 8 points. trap_flag stays as a display caution.
    if hidden:
        top_board_raw += 5
    h.top_board_score_v2 = round(_hr2_clip(top_board_raw), 2)
    h.pmix_gate = pmix_gate_label(pitch_fit, h.pitch_mix_sample)
    h.hrw_zone = hrw_zone_label(h.hrw_score)
    h.lineup_spot_risk = lineup_spot_risk_label(h.lineup_spot)
    h.high_confidence_hr_flag = bool(h.beginner_label == "Strong HR Look")
    h.high_confidence_hr_score = round(_hr2_clip(0.65*h.top_board_score_v2 + 0.20*pitch_fit + 0.15*pull_launch), 2)
    h.top_board_bucket = top_board_bucket_for(h)
    if hidden:
        h.top_board_bucket = "Hidden HR Value"
    elif trap_flag:
        h.top_board_bucket = "Trap Risk"
    h.top_board_tags = top_board_tags_for(h)
    if h.hidden_hr_value and "Hidden HR Value" not in h.top_board_tags:
        h.top_board_tags.insert(0, "Hidden HR Value")
    h.top_board_rank_reason = ", ".join(h.top_board_tags[:4]) if h.top_board_tags else h.beginner_label

    h.hr_score = h.hr_score_v2
    h.hit_score = h.hit_score_v2
    h.hrr_score = h.hrr_score_v2
    h.contact_score = h.contact_score_v2
    h.overall_score = round(_hr2_clip(_overall_raw), 2)

    # ─── RECENCY-FIRST MULTIPLIER ──────────────────────────────────────────
    # Applied to all four scores after the V2 layers compute them.
    # Asymmetric per user preference:
    #   Hot bonus: up to +12% — reward genuine recent surges aggressively
    #   Cold cut: capped at -6% and only fires for TRUE cold, defined as 10 games
    #     with no HRs AND no XBH AND ≤6 hits total (≈ 8+ games no HR, no multi-hit games).
    #   Short slumps don't count — variance can look like cold without being cold.
    l5_hot = (h.last5_hr >= 1) or (h.last5_hits >= 5) or (h.last5_xbh >= 2)
    l7_hot = (h.last7_hr >= 1) or (h.last7_xbh >= 3) or (h.last7_hits >= 7)
    l10_hot = (h.last10_hr >= 2) or (h.last10_xbh >= 4)
    quality_rising = (h.l5_barrel_rate - h.l10_barrel_rate) >= 0.04 or (h.l5_xwoba - h.l10_xwoba) >= 0.030

    # TRUE COLD: 10 games with no HRs, no XBH, and ≤6 hits total.
    # This requires sustained no-production, not a 5-game variance dip.
    true_cold = (h.last10_hr == 0) and (h.last10_xbh <= 1) and (h.last10_hits <= 6)
    quality_falling = (h.l5_barrel_rate - h.l10_barrel_rate) <= -0.04 or (h.l5_xwoba - h.l10_xwoba) <= -0.030

    _rm = MODEL_WEIGHTS["recency_multiplier"]
    recency_mult = _rm["neutral"]
    if l5_hot and l7_hot and quality_rising:
        recency_mult = _rm["hot_strong"]
    elif l5_hot and (l7_hot or quality_rising):
        recency_mult = _rm["hot_medium"]
    elif l5_hot or l7_hot or l10_hot:
        recency_mult = _rm["hot_light"]
    elif true_cold and quality_falling:
        recency_mult = _rm["cold_strong"]
    elif true_cold:
        recency_mult = _rm["cold_light"]

    # HR side gets the full swing; hit/HRR/contact get a softened version.
    # Reasoning: HR is high-variance and rides hot/cold streaks hard; hit-rate
    # picks need season anchors to stay stable, so a smaller multiplier here.
    hr_mult = recency_mult
    soft_mult = 1.0 + (recency_mult - 1.0) * _rm["soft_side_share"]

    # ── SHADOW SCORE (A/B test, added 2026-07-13) ──────────────────────────
    # Hypothesis from 3-day grading: recency boosts hurt (hot guys regress,
    # slumping stars still homer). Shadow = the SAME hr_score but:
    #   1. NO recency multiplier applied, and
    #   2. re-anchored 20% to season_power (stable power baseline), because
    #      season_power only carries 0.08 weight in the live blend.
    # Logged alongside hr_score for 30 days -- do not change the live score
    # until the shadow wins or loses on real graded picks.
    # RE-ANCHOR (2026-07-31): the 7/13 shadow test ran on three days and
    # concluded recency boosts hurt, so the 7/14 swap dropped recency from the
    # live score entirely. Five days of graded picks (313 unique player-days)
    # say the opposite, and say it loudly -- HR rate by HRs-in-last-5 runs
    # 2.8% / 9.8% / 30.4% / 46.7% (n=109/133/56/15), and last5_hr correlates
    # with actually homering at r=+0.351 (t=6.6) versus r=+0.139 (t=2.5) for
    # the full hr_score blend. The composite was predicting worse than one of
    # its own ingredients because that ingredient had been removed.
    #
    # Re-anchoring 30% to recent_hr_form_score. Measured on the graded days,
    # re-ranking the top 15 of each slate: 25.3% -> 29.3% HR rate, and the
    # top 8 goes 22.5% -> 32.5%. The gain is a broad plateau from 20% to 40%
    # (peak 35%), not a knife-edge, which is why 30% is safe to sit on; it is
    # deliberately one notch below the measured optimum because five days is
    # a thin sample. 50% is worse than 30% on the top 15, so the rest of the
    # blend is carrying real information -- this is a re-anchor, not a
    # replacement.
    #
    # Revisit after ~2 more weeks. If the top-of-board HR rate drops, lower
    # the 0.30; git log has the previous formula.
    _hr_form_anchor = 0.30
    _corrected_hr = round(_hr2_clip(
        (1.0 - _hr_form_anchor) * (0.80 * h.hr_score + 0.20 * season_power)
        + _hr_form_anchor * safe_float(getattr(h, "recent_hr_form_score", 0.0), 0.0)
    ), 2)

    # ── LONGEST HR SCORE (added 2026-07-13) ────────────────────────────────
    # "Who hits the FARTHEST ball tonight" -- a distance metric, not an HR
    # probability metric. Four recent-window power/distance signals:
    #   35% rate of 400ft+ batted balls (tail events predict tail events)
    #   25% recent avg exit velo (the engine)
    #   22% rate of 350ft+ batted balls (deep-contact volume)
    #   18% recent avg batted-ball distance (overall carry)
    # Small samples shrink toward a neutral 40 (full trust at 20 tracked BBE).
    _lhr_d350 = safe_int(bbe.get("dist_350_plus"), safe_int(getattr(h, "recent_350_num", 0), 0))
    _lhr_avg_dist = safe_float(bbe.get("avg_distance"), 0.0)
    _lhr_raw = 100.0 * (
        0.35 * minmax_norm(r400, 0.00, 0.10)
        + 0.25 * minmax_norm(avg_ev, 86.0, 94.0)
        + 0.22 * minmax_norm(_lhr_d350 / max(1, tracked), 0.10, 0.40)
        + 0.18 * minmax_norm(_lhr_avg_dist, 150.0, 210.0)
    )
    _lhr_conf = min(1.0, tracked / 20.0)
    h.longest_hr_score = round(_lhr_conf * _lhr_raw + (1.0 - _lhr_conf) * 40.0, 2)

    # ── MODEL SWAP (2026-07-14, per user decision) ─────────────────────────
    # The corrected model (no recency multiplier, 20% season-power anchor) is
    # now LIVE as hr_score. The old recency-first score is preserved in
    # hr_score_shadow so the tracker's A/B keeps running — in tracker reports,
    # LIVE = corrected model, SHADOW = old recency-first model.
    _old_model_hr = round(_hr2_clip(h.hr_score * hr_mult), 2)
    h.hr_score = _corrected_hr
    h.hr_score_shadow = _old_model_hr
    h.hit_score = round(_hr2_clip(h.hit_score * soft_mult), 2)
    h.hrr_score = round(_hr2_clip(h.hrr_score * soft_mult), 2)
    h.contact_score = round(_hr2_clip(h.contact_score * soft_mult), 2)

    # Keep the V2 fields in sync so anything reading hr_score_v2 also sees the bump.
    h.hr_score_v2 = h.hr_score
    h.hit_score_v2 = h.hit_score
    h.hrr_score_v2 = h.hrr_score
    h.contact_score_v2 = h.contact_score
    # Recompute overall_score using the now-recency-adjusted components.
    h.overall_score = round(0.42*h.hr_score + 0.20*h.hrr_score + 0.15*h.hit_score + 0.10*h.contact_score + 0.08*hrw_zone_score_value(h.hrw_score) + 0.05*pitch_fit, 2)

    # consistency_score (per audit, 2026-06-27): a genuine second opinion
    # distinct from overall_score's pure-upside focus. Rewards balance
    # across the four component scores (penalizes a lopsided one-trick
    # profile even if its average matches a well-rounded player's),
    # discounts low-sample-size players (reintroduces the sample-confidence
    # concept cleanly, without the dead System-1 baggage it used to carry),
    # and adds a small nudge for HR "dueness" (hr_due_ratio, built earlier
    # this session) as a tiebreaker context signal, not a dominant factor.
    _cs_scores = [h.hr_score, h.hrr_score, h.hit_score, h.contact_score]
    _cs_base = 0.30*h.hr_score + 0.25*h.hrr_score + 0.25*h.hit_score + 0.20*h.contact_score
    _cs_spread = max(_cs_scores) - min(_cs_scores)
    _cs_balance_factor = 1.0 - minmax_norm(_cs_spread, 12, 50) * 0.15
    _cs_sample_confidence = (
        0.55 * minmax_norm(safe_float(h.season_pa, 0.0), 40, 220) +
        0.45 * minmax_norm(safe_float(h.recent_350_den, 0.0), 6, 26)
    )
    _cs_confidence_mult = 0.85 + 0.15 * _cs_sample_confidence
    _cs_due_ratio = safe_float(getattr(h, "hr_due_ratio", 1.0), 1.0)
    _cs_due_nudge = 0.0
    if 1.3 <= _cs_due_ratio <= 2.5:
        _cs_due_nudge = minmax_norm(_cs_due_ratio, 1.3, 2.5) * 3.0
    elif 2.5 < _cs_due_ratio <= 4.0:
        _cs_due_nudge = 3.0 * (1.0 - minmax_norm(_cs_due_ratio, 2.5, 4.0))
    h.consistency_score = round(min(100.0, _cs_base * _cs_balance_factor * _cs_confidence_mult + _cs_due_nudge), 2)
    # ────────────────────────────────────────────────────────────────────────

    h.hr_reason = h.simple_reason_1
    h.hit_reason = "Low K + split BA + recent hits" if h.season_k_rate <= 0.25 else "Recent hits + traffic matchup"
    h.hrr_reason = "Lineup context + recent runs/RBI + pitcher traffic"
    h.contact_reason = "XBH form + EV/hard-hit + 375 contact"
    h.top_pick_reason = h.beginner_label
    # 2026-08-12: same fix as simple_reason_1-3/risk_reason above — this is
    # displayed verbatim on the Games page's ALT chip (every card, per
    # GameStrip's own comment: "ALT ON EVERY CARD"), and h.alt_hr_score is
    # already the exact number deciding which branch fires, just never shown.
    if h.alt_hr_score >= 58 and h.hr_score_v2 < 42:
        h.alt_reason = f"Missed main HR cut but has pitch mix/contact/bullpen upside (alt score {h.alt_hr_score:.0f})"
    elif h.alt_hr_score >= 50:
        h.alt_reason = f"Secondary HR look (alt score {h.alt_hr_score:.0f})"
    else:
        h.alt_reason = f"Low alt HR priority (alt score {h.alt_hr_score:.0f})"

    # V31 final decision layer: keeps risky power bats alive as Power Watch and makes Avoid HR strict.
    apply_decision_engine_v31(h)

    # Let Damage Conversion influence board rank after the final role is known.
    dc = safe_float(getattr(h, "damage_conversion_score", 0.0), 0.0)
    if getattr(h, "true_avoid_hr", False):
        h.top_board_score_v2 = round(max(0.0, h.top_board_score_v2 - 18.0), 2)
        h.top_board_bucket = "True Avoid HR"
    elif getattr(h, "power_watch_flag", False):
        h.top_board_score_v2 = round(_hr2_clip(0.78*h.top_board_score_v2 + 0.22*dc + 4.0), 2)
        h.top_board_bucket = "Power Watch"
    elif dc >= 68 and h.top_board_bucket in {"Trap Risk", "Pure HR"}:
        h.top_board_score_v2 = round(_hr2_clip(0.82*h.top_board_score_v2 + 0.18*dc + 2.0), 2)
        h.top_board_bucket = "Damage Conversion"

    h.top_board_tags = top_board_tags_for(h)
    if h.damage_conversion_score >= 66 and "Damage Fit" not in h.top_board_tags:
        h.top_board_tags.insert(0, "Damage Fit")
    if h.power_watch_flag and "Power Watch" not in h.top_board_tags:
        h.top_board_tags.insert(0, "Power Watch")
    if h.true_avoid_hr and "True Avoid" not in h.top_board_tags:
        h.top_board_tags.insert(0, "True Avoid")
    # 🧩 Aligned Signals: new badge for the strongest validated combo found in the
    # 7-day backtest -- weak-spot lineup match + pitch-type match + real recent
    # contact quality (BBE-sample HR or elite max EV) stacking together measured
    # noticeably better than any of the three alone. Worth its own marker so it's
    # visible at a glance instead of buried across three separate flags.
    _bbe_for_combo = getattr(h, "bbe_profile", {}) or {}
    _good_contact_for_combo = bool(safe_int(_bbe_for_combo.get("hr"), 0) >= 2 or safe_float(_bbe_for_combo.get("max_ev"), 0.0) >= 108)
    if h.weak_spot_flag and getattr(h, "pitch_type_match_flag", False) and _good_contact_for_combo:
        h.top_board_tags.insert(0, "🧩 Aligned Signals")
    h.top_board_tags = list(dict.fromkeys(h.top_board_tags))[:6]
    h.top_board_rank_reason = ", ".join(h.top_board_tags[:4]) if h.top_board_tags else h.beginner_label
    h.top_pick_reason = h.final_hr_role
    return h


def apply_matchup_center_fields(h: HitterRecord) -> HitterRecord:
    """Dashboard-ready matchup read for the Matchup Attack Center.

    This does not replace the main model scores. It gives the website a clean label/reason layer
    using pitcher weakness, true V2 lineup spot damage, split fit, low-K context, and hitter power/contact.
    """
    split_avg = h.avg_vs_lhp if h.pitcher_throws == "L" else h.avg_vs_rhp
    split_iso = h.iso_vs_lhp if h.pitcher_throws == "L" else h.iso_vs_rhp
    d350 = h.recent_350_num / max(1, h.recent_350_den)
    k_rate = safe_float(getattr(h, "pitcher_k_rate", 0.0), 0.0)
    k9 = safe_float(getattr(h, "pitcher_k9", 0.0), 0.0)
    low_k = (0 < k_rate <= 0.205) or (0 < k9 <= 7.50)
    very_low_k = (0 < k_rate <= 0.165) or (0 < k9 <= 6.50)
    _sw = h.bats
    if (h.bats or "").upper()[:1] == "S":
        _sw = "R" if (h.pitcher_throws or "").upper()[:1] == "L" else "L"
    side_weak = (_sw == "L" and h.pitcher_weak_side == "LHB") or (_sw == "R" and h.pitcher_weak_side == "RHB")
    spot_hot = h.pitcher_spot_damage_score >= 62 or h.pitcher_zone_damage_score >= 64
    spot_watch = h.pitcher_spot_damage_score >= 52 or h.pitcher_zone_damage_score >= 54
    weak_pitcher = (
        h.pitcher_hr9 >= 1.20 or
        h.pitcher_whip >= 1.30 or
        h.pitcher_attack_score >= 40 or
        h.pitcher_hardhit_allowed >= 0.42 or
        h.pitcher_barrel_allowed >= 0.085 or
        spot_hot
    )
    safe_pitcher = (
        h.pitcher_hr9 < 1.00 and
        h.pitcher_whip < 1.22 and
        not low_k and
        h.pitcher_spot_damage_score < 38 and
        h.pitcher_zone_damage_score < 42
    )
    power_fit = (
        h.hr_score >= 35 or h.hrw_score >= 58 or h.recent_ideal_hr_contact >= 0.11 or
        d350 >= 0.16 or h.recent_375_num >= 2 or split_iso >= 0.185
    )
    contact_fit = h.hit_score >= 72 or h.season_avg >= 0.270 or split_avg >= 0.280 or h.last5_hits >= 5
    score = (
        0.26 * min(100.0, h.hr_score) +
        0.17 * min(100.0, h.hrw_score) +
        0.11 * min(100.0, h.hrr_score) +
        0.08 * min(100.0, h.hit_score) +
        min(16.0, h.recent_ideal_hr_contact * 115.0) +
        min(8.0, d350 * 42.0) +
        min(7.0, split_avg * 18.0) +
        min(7.0, split_iso * 20.0) +
        (10.0 if spot_hot else 6.0 if spot_watch else 0.0) +
        (5.0 if side_weak else 0.0) +
        (5.0 if weak_pitcher else 0.0) +
        (5.0 if very_low_k else 3.0 if low_k else 0.0) -
        (7.0 if safe_pitcher else 0.0)
    )
    h.matchup_score = round(max(0.0, min(100.0, score)), 1)
    if h.matchup_score >= 72 and power_fit and weak_pitcher:
        h.matchup_tier = "Attack"
        h.matchup_label = "HR Attack"
    elif contact_fit and low_k and h.matchup_score >= 62:
        h.matchup_tier = "Attack"
        h.matchup_label = "Contact Attack"
    elif h.matchup_score >= 62 and (power_fit or weak_pitcher or spot_watch):
        h.matchup_tier = "Watch"
        h.matchup_label = "Matchup Watch"
    elif safe_pitcher and h.matchup_score < 54:
        h.matchup_tier = "Safe"
        h.matchup_label = "Pitcher Safer"
    else:
        h.matchup_tier = "Neutral"
        h.matchup_label = "Neutral"
    h.pitcher_low_k_flag = bool(low_k)
    h.weak_pitcher_flag = bool(weak_pitcher)
    h.pitcher_safe_flag = bool(safe_pitcher)
    reasons = []
    if spot_hot:
        reasons.append(f"V2 spot damage {h.pitcher_spot_damage_score:.1f}")
    elif spot_watch:
        reasons.append(f"spot watch {h.pitcher_spot_damage_score:.1f}")
    if side_weak:
        reasons.append(f"{h.bats}HB attacks pitcher weak side")
    if low_k:
        reasons.append(f"low-K pitcher {round(k_rate*100)}%" if k_rate else f"low-K pitcher K/9 {k9:.1f}")
    if weak_pitcher:
        reasons.append(f"pitcher HR/9 {h.pitcher_hr9:.2f} / WHIP {h.pitcher_whip:.2f}")
    if power_fit:
        reasons.append(f"HRW {h.hrw_score:.0f} / IHR {h.recent_ideal_hr_contact:.2f} / 350+ {round(d350*100)}%")
    if contact_fit and not power_fit:
        reasons.append(f"contact path HIT {h.hit_score:.1f} / split AVG {split_avg:.3f}")
    h.matchup_reason = " · ".join(reasons[:5]) or "No clear matchup edge"
    return h


def score_hitter(h: HitterRecord) -> HitterRecord:
    split_avg = h.avg_vs_lhp if h.pitcher_throws == "L" else h.avg_vs_rhp
    split_iso = h.iso_vs_lhp if h.pitcher_throws == "L" else h.iso_vs_rhp
    season_pa = max(1, safe_int(h.season_pa, 1))
    h.hr_per_pa = round(safe_float(h.season_hr, 0.0) / season_pa, 4)
    h.hr_pa_score = round(100 * minmax_norm(h.hr_per_pa, 0.015, 0.085), 1)
    # Same switch-hitter correction as above: "S" used to fall through the
    # else branch and silently take the vs-RHB numbers even when batting left.
    _eb = h.bats
    if (h.bats or "").upper()[:1] == "S":
        _eb = "R" if (h.pitcher_throws or "").upper()[:1] == "L" else "L"
    side_hr9 = h.pitcher_hr9_vs_lhb if _eb == "L" else h.pitcher_hr9_vs_rhb
    side_whip = h.pitcher_whip_vs_lhb if _eb == "L" else h.pitcher_whip_vs_rhb
    side_match = 1.0 if ((_eb == "L" and h.pitcher_weak_side == "LHB") or (_eb == "R" and h.pitcher_weak_side == "RHB")) else 0.4

    # V2 lineup-spot damage from pitcher history. This powers the Matchups tab color/hover layer.
    spot_key = str(safe_int(h.lineup_spot, 0))
    spot_damage = (h.pitcher_lineup_spot_damage or {}).get(spot_key, {}) if isinstance(h.pitcher_lineup_spot_damage, dict) else {}
    zone_key = _spot_zone_for_lineup_spot(h.lineup_spot)
    zone_damage = (h.pitcher_lineup_zone_damage or {}).get(zone_key, {}) if isinstance(h.pitcher_lineup_zone_damage, dict) else {}
    h.pitcher_spot_damage_score = safe_float(spot_damage.get("damage_score"), 0.0)
    h.pitcher_spot_damage_label = str(spot_damage.get("label") or _spot_damage_label(h.pitcher_spot_damage_score))
    h.pitcher_spot_damage_reason = str(spot_damage.get("reason") or "No exact lineup-spot pitcher history")
    h.pitcher_zone_damage_score = safe_float(zone_damage.get("damage_score"), 0.0)
    h.pitcher_zone_damage_label = str(zone_damage.get("label") or _spot_damage_label(h.pitcher_zone_damage_score))
    h.pitcher_zone_damage_reason = str(zone_damage.get("reason") or "No lineup-zone pitcher history")

    recent_bbe = max(1, h.recent_350_den)
    tracked_dist = max(0, h.recent_distance_tracked)
    season_pa = max(1, h.season_pa)

    def shrink_rate(raw: float, n: int, prior: float, stabilizer: int) -> float:
        return ((raw * n) + (prior * stabilizer)) / (n + stabilizer)

    damage350_raw = h.recent_350_num / max(1, tracked_dist)
    damage375_raw = h.recent_375_num / max(1, tracked_dist)
    damage350 = shrink_rate(damage350_raw, tracked_dist, 0.18, 12)
    damage375 = shrink_rate(damage375_raw, tracked_dist, 0.08, 16)
    # 400ft+ damage rate. dist_400_plus already exists in bbe_profile (used
    # elsewhere in the file, e.g. compute_spray_park_fit) but recent_power_proxy
    # never read it before -- added per audit (2026-06-27). Lower prior than
    # 375+ since 400ft+ contact is rarer.
    _bbe_profile = h.bbe_profile if isinstance(h.bbe_profile, dict) else {}
    damage400_raw = safe_int(_bbe_profile.get("dist_400_plus"), 0) / max(1, tracked_dist)
    damage400 = shrink_rate(damage400_raw, tracked_dist, 0.04, 18)
    hard_hit = shrink_rate(h.recent_hard_hit_rate, recent_bbe, 0.38, 18)
    sweet_spot = shrink_rate(h.recent_sweet_spot_rate, recent_bbe, 0.33, 18)
    ideal_hr = shrink_rate(h.recent_ideal_hr_contact, recent_bbe, 0.10, 18)
    barrel = shrink_rate(h.recent_barrel_rate, recent_bbe, 0.08, 18)
    xwoba = shrink_rate(h.recent_xwoba, recent_bbe, 0.320, 18)
    pull_rate = shrink_rate(h.recent_pull_rate, recent_bbe, 0.38, 18)

    # REMOVED per audit (2026-06-27): recent_fb, l5_barrel/l10_barrel/
    # l5_hard/l10_hard/l5_xwoba/l10_xwoba/l5_pull/l10_pull, and
    # season_hr_rate/season_hr_rate_shrunk were all confirmed dead -- they
    # exclusively fed the primary_score/check_score cluster (and its
    # hot_quality_trend/hot_multiplier sub-piece) already removed above.
    # No other code in this function or downstream reads any of them.

    primary_weak = h.lineup_spot in (2, 3, 4)
    secondary_weak = h.lineup_spot in (1, 5)
    power_gate = (
        split_iso >= 0.170
        or ideal_hr >= 0.10
        or damage350 >= 0.18
        or h.last5_xbh >= 2
        or h.last10_xbh >= 3
    )
    true_spot_hot = h.pitcher_spot_damage_score >= 62 or h.pitcher_zone_damage_score >= 64
    true_spot_warm = h.pitcher_spot_damage_score >= 52 or h.pitcher_zone_damage_score >= 54
    star_flag = (
        (true_spot_hot and power_gate) or
        (side_match >= 1.0 and ((primary_weak and power_gate) or (secondary_weak and (split_iso >= 0.200 or ideal_hr >= 0.12 or damage375 >= 0.08))))
    )
    h.weak_spot_flag = star_flag

    # ── ⭐ FLAG AND ⭐ REASON ARE NOW WRITTEN IN THE SAME BREATH (2026-08-16) ──
    #
    # WHAT WAS WRONG. On a real slate (mock/fix_slate.json, 266 rows) 42 rows
    # carried weak_spot_flag, 57 carried a non-empty weak_spot_reason, and only
    # 27 carried both — two fields describing the same star disagreed on 45 rows,
    # and the site had to render that disagreement honestly, which read as
    # nonsense. Traced: both are set together at HitterRecord construction in
    # build_hitter_records (`weak_spot_flag=spot in pitcher.weak_spots`,
    # `weak_spot_reason=_weak_spot_reason_for(pitcher, spot)`), and at THAT point
    # they agree perfectly — _weak_spot_reason_for returns "" for exactly the
    # spots that are not in pitcher.weak_spots. Then score_hitter (this function)
    # overwrites the flag five lines up with `star_flag`, a completely different
    # and much better test, and never touches the reason. The reason has been
    # stale-by-construction ever since star_flag was introduced: it answers a
    # question (is this PITCHER weak at the #N spot?) that stopped being the
    # question the flag asks.
    #
    # THE TWO REALLY DO MEASURE DIFFERENT THINGS, which is why "just recompute
    # the reason from pitcher.weak_spots" would not have fixed it:
    #   · pitcher.weak_spots is pitcher-side only — spots with damage_score >= 58
    #     and pa >= 4, and if a pitcher has none it FALLS BACK to his three worst
    #     spots by damage score regardless of quality. That fallback is why 30
    #     rows carried a reason with no star.
    #   · star_flag is a hitter x pitcher star: real spot/zone damage AND power,
    #     OR platoon edge AND a top-of-order slot AND power. That second disjunct
    #     contains no pitcher-weakness claim at all, which is why 15 rows carried
    #     a star with no reason — including, on that slate, Gunnar Henderson at a
    #     spot the pitcher scores 4.9/100 damage allowed at. The local names
    #     `primary_weak` / `secondary_weak` do not mean the pitcher is weak; they
    #     mean the HITTER bats 2-3-4 / 1-5. Documented here rather than renamed
    #     because they feed the weak_spot_bonus ladder directly below.
    #
    # WHICH ONE IS AUTHORITATIVE: the flag, without argument. Every consumer in
    # this file reads the flag (the ⭐ in five report renderers, the HRW gate, the
    # pair scorer, the stack-alert line, the overall-score interaction) and none
    # reads the reason — a whole-repo grep found the reason referenced only here,
    # at its construction site, and in live_results_tracker.SLOT_FIELDS. And the
    # flag is the measured one: 18.0% HR against a 13.9% baseline in the graded
    # archive. So the reason derives from the flag, never the other way round.
    #
    # THE INVARIANT, asserted in tests/test_hr_gate_label.py:
    #     bool(weak_spot_flag) == bool(weak_spot_reason.strip())
    #
    # NOTHING IS LOST by blanking the reason on the 30 unstarred rows. The
    # pitcher-side sentence it held was a strict SUBSET of pitcher_spot_damage_reason,
    # which is populated on 266/266 rows of that same slate and carries more of
    # the same numbers ("spot #1: 38 PA, 0.500 SLG, 0.219 ISO, HR rate 5.3%, XBH
    # rate 7.9%, HH 33.3%" against the old "Pitcher has allowed 2 HR to the #1
    # spot in 38 PA this season (.500 SLG)."). And when the star DOES fire, the
    # pitcher-side sentence is kept as the lead of the new reason rather than
    # discarded — the star's own clause is appended after it, so the row now says
    # both what the pitcher gives up there and what made this hitter the one to
    # take it. No score, no flag and no pick moves; only the sentence does.
    _ws_pitcher_note = str(getattr(h, "weak_spot_reason", "") or "").strip()
    if star_flag:
        _ws_bits = [_ws_pitcher_note] if _ws_pitcher_note else []
        if true_spot_hot and power_gate:
            _ws_bits.append(
                f"#{h.lineup_spot} spot is getting damaged (spot {h.pitcher_spot_damage_score:.0f}, "
                f"zone {h.pitcher_zone_damage_score:.0f} of 100) and he has the power to use it."
            )
        elif primary_weak and power_gate:
            _ws_bits.append(
                f"Bats #{h.lineup_spot} with the platoon edge and a live power profile "
                f"— the star is the matchup, not a hole in the pitcher "
                f"(spot damage only {h.pitcher_spot_damage_score:.0f} of 100)."
            )
        else:
            _ws_bits.append(
                f"Bats #{h.lineup_spot} with the platoon edge and top-end power "
                f"(ISO {display_avg(split_iso)} vs this hand) — the star is the matchup, "
                f"not a hole in the pitcher (spot damage {h.pitcher_spot_damage_score:.0f} of 100)."
            )
        h.weak_spot_reason = " ".join(_ws_bits)
    else:
        h.weak_spot_reason = ""

    if true_spot_hot and power_gate:
        h.weak_spot_bonus = 0.034
    elif true_spot_warm and power_gate:
        h.weak_spot_bonus = 0.020
    elif primary_weak and side_match >= 1.0:
        h.weak_spot_bonus = 0.028
    elif secondary_weak and side_match >= 1.0:
        h.weak_spot_bonus = 0.016
    elif h.lineup_spot in (2, 3, 4, 5):
        h.weak_spot_bonus = 0.010
    else:
        h.weak_spot_bonus = 0.0

    # REMOVED per audit (2026-06-27): sample_confidence, experience_penalty,
    # season_hr_layer, recent_power_proxy/recent_results_layer/recent_hr_layer,
    # pitcher_layer, weather_layer, lineup_layer, and raw_hr_primary were all
    # traced and confirmed to exclusively feed the primary_score/check_score
    # cluster removed just above -- none of them are read by anything else
    # in this function or beyond it. Removed as one verified-dead unit
    # rather than left computing real numbers nobody reads.

    # REMOVED per audit (2026-06-27): quality_gate, primary_score, due_layer,
    # raw_hr_check, check_score, agreement, h.self_check_hr_score, and
    # h.hr_confidence were computed here -- ALL confirmed dead
    # (self_check_hr_score/hr_confidence verified via whole-file grep to
    # never be read anywhere downstream).
    #
    # h.hr_score itself is ALSO dead -- apply_model_v2_layers() overwrites it
    # immediately after score_hitter() returns -- but several further lines
    # below this point still mutate h.hr_score in place (fake-power guard,
    # elite-pitcher penalty, etc). Rather than also remove every one of
    # those (each correctly computing other real, possibly-still-relevant
    # local context alongside the dead mutation, so cutting them individually
    # risked collateral damage), h.hr_score is given a neutral placeholder
    # here so those downstream lines have a defined value to operate on.
    # The actual number that results is never read by anything -- it's
    # fully overwritten before this function returns.
    h.hr_score = 50.0

    # Fake-power guard: trim HR ceiling if contact quality is weak.
    # Triggers when 2 or more of 4 soft-contact signals are present.
    # (Old version required all 4 simultaneously — too easy to slip through.)
    fake_power_flags = sum([
        barrel < 0.055,
        hard_hit < 0.32,
        ideal_hr < 0.08,
        xwoba < 0.310,
    ])
    if fake_power_flags >= 2:
        h.hr_score = round(h.hr_score * 0.84, 2)
    # Fix 2: Elite pitcher penalty — strong aces suppress HR upside
    elite_pitcher_penalty = 0.0
    if h.pitcher_era <= 2.50 and h.pitcher_whip <= 1.05:
        elite_pitcher_penalty = 0.06  # true ace — significant penalty
    elif h.pitcher_era <= 3.20 and h.pitcher_whip <= 1.15:
        elite_pitcher_penalty = 0.03  # solid starter — moderate penalty
    if elite_pitcher_penalty > 0:
        h.hr_score = round(h.hr_score * (1.0 - elite_pitcher_penalty), 2)

    # Elite HR shape: barrel + ideal launch + pulled contact deserves a small push.
    if barrel >= 0.10 and ideal_hr >= 0.12 and pull_rate >= 0.40:
        h.hr_score = round(h.hr_score * 1.07, 2)

    # Pitch Mix Fit: small additive adjustment from batter strengths vs pitcher arsenal.
    pmix_adj = 0.0
    if h.pitch_mix_score >= 80:
        pmix_adj = 4.0
    elif h.pitch_mix_score >= 70:
        pmix_adj = 2.5
    elif h.pitch_mix_score >= 60:
        pmix_adj = 1.0
    elif h.pitch_mix_score < 45:
        pmix_adj = -2.0
    if pmix_adj:
        h.hr_score = round(max(0.0, h.hr_score + pmix_adj), 2)

    # REMOVED per audit (2026-06-27): hit_base/raw_hit_score/h.hit_score,
    # h.hrr_score, and h.contact_score were all computed here and then
    # immediately overwritten by apply_model_v2_layers() (h.hit_score =
    # h.hit_score_v2, etc) right after this function returns -- confirmed
    # dead, same as h.hr_score/h.overall_score above. This block also
    # referenced weather_layer, which had already been removed as part of
    # the earlier dead-code cleanup in this same function -- that left a
    # real NameError bug sitting here (caught during a final verification
    # pass, never actually shipped) since nothing had exercised this dead
    # path to surface it. Removed entirely rather than patched, since every
    # output it produced was already confirmed unused.


    # HRW (Home Run Window): timing indicator used for output and pairs.
    # Uses L20 PA + L7 internally. L5 is a light confirmation layer so obvious hot bats don't show ice cold.
    l20pa_bbe = max(1, h.l20pa_bbe)
    l20pa_350_rate = h.l20pa_350_num / max(1, h.l20pa_350_den)
    l20pa_375_rate = h.l20pa_375_num / max(1, h.l20pa_350_den)

    # BUGFIX (2026-06-28): ev_shrunk was deleted during the System 1 dead-
    # code cleanup (it lived inside recent_power_proxy, scoped to
    # recent_350_den) but this line, scoped to the L20PA window, still
    # referenced it -- a real NameError crash on every run, confirmed via
    # an actual traceback. Recomputed here scoped correctly to l20pa_bbe
    # (this block's actual sample size) rather than reviving the deleted,
    # differently-scoped version.
    l20pa_ev_shrunk = shrink_rate(safe_float(getattr(h, "recent_ev", 88.5), 88.5), l20pa_bbe, 88.5, 20)

    # EV added to HRW l20_form — measures true power quality in recent PA window
    l20_form = (
        0.20 * minmax_norm(h.l20pa_ideal_hr_contact, 0.02, 0.18) +
        0.14 * minmax_norm(h.l20pa_barrel_rate, 0.00, 0.14) +
        0.15 * minmax_norm(l20pa_350_rate, 0.04, 0.34) +
        0.09 * minmax_norm(l20pa_375_rate, 0.01, 0.18) +
        0.10 * minmax_norm(h.l20pa_hard_hit_rate, 0.22, 0.56) +
        0.10 * minmax_norm(l20pa_ev_shrunk, 85.0, 94.0) +     # EV in HRW window
        0.07 * minmax_norm(h.l20pa_fb_rate, 0.22, 0.48) +
        0.06 * minmax_norm(h.l20pa_pull_rate, 0.25, 0.58) +
        0.09 * minmax_norm(h.l20pa_xwoba, 0.260, 0.430)
    )

    l7_results = (
        0.34 * minmax_norm(h.last7_hr, 0, 3) +
        0.22 * minmax_norm(h.last7_xbh, 0, 5) +
        0.14 * minmax_norm(h.last7_hits, 0, 11) +
        0.12 * minmax_norm(h.last7_avg, 0.170, 0.420) +
        0.10 * minmax_norm(h.last7_runs + h.last7_rbi, 0, 12) +
        0.08 * minmax_norm(h.l20pa_hr, 0, 2)
    )

    l5_confirm = (
        0.42 * minmax_norm(h.last5_hr, 0, 3) +
        0.24 * minmax_norm(h.last5_xbh, 0, 5) +
        0.18 * minmax_norm(h.last5_hits, 0, 9) +
        0.16 * minmax_norm(h.last5_runs + h.last5_rbi, 0, 12)
    )

    recent_hr_bump = 0.0
    if h.last7_hr >= 3:
        recent_hr_bump += 0.08
    elif h.last7_hr == 2:
        recent_hr_bump += 0.055
    elif h.last7_hr == 1:
        recent_hr_bump += 0.025

    if h.last5_hr >= 3:
        recent_hr_bump += 0.07
    elif h.last5_hr == 2:
        recent_hr_bump += 0.045
    elif h.last5_hr == 1:
        recent_hr_bump += 0.018

    pa_conf = minmax_norm(h.l20pa_pa, 6, 20)
    bbe_conf = minmax_norm(l20pa_bbe, 3, 10)
    window_conf = 0.55 * pa_conf + 0.45 * bbe_conf

    # Fix 4: Shift HRW to trust recent game results more over potentially stale Statcast
    hrw = (0.42 * l20_form + 0.38 * l7_results + 0.20 * l5_confirm + recent_hr_bump) * (0.88 + 0.12 * window_conf)

    # Soft floors: recent multi-HR form should not display as ice unless contact is completely empty.
    if h.last5_hr >= 3 or h.last7_hr >= 3:
        hrw = max(hrw, 0.62)
    elif h.last5_hr >= 2 or h.last7_hr >= 2:
        hrw = max(hrw, 0.52)
    elif h.last5_hr == 1 or h.last7_hr == 1:
        hrw = max(hrw, 0.42)

    h.hrw_score = round(max(0.0, min(88.0, hrw * 100)), 1)

    # Light lineup boost: top-half bats get more chances to see the bullpen.
    if h.lineup_spot in (1, 2, 3, 4, 5):
        h.hr_score = round(h.hr_score * 1.03, 2)
    elif h.lineup_spot >= 7:
        h.hr_score = round(h.hr_score * 0.97, 2)

    # SAFETY CLIP: hr_score has no upper bound enforced through the chain of
    # multipliers above it (hot_multiplier alone can reach 1.16x, before the
    # elite-shape 1.07x and this 1.03x lineup boost can stack on top). Checked
    # a full month of real data (1,833 rows) and the max ever observed was
    # exactly 100.0, so this hasn't actually misfired -- but nothing stops it
    # from doing so, and overall_score below inherits whatever comes out of
    # here uncapped. Cheap insurance with no downside since every other score
    # in the file is already clipped to 0-100 via _hr2_clip.
    h.hr_score = round(max(0.0, min(100.0, h.hr_score)), 2)

    # ─── THE OPPORTUNITY FOLD (2026-08-09) ─────────────────────────────────
    # Donovan: "fold the hrr and hit score into hr for use."
    #
    # WHERE THIS CAME FROM. Auditing designations across 4,620 graded picks
    # turned up something embarrassing: the bucket named HR is not the best
    # bucket at home runs.
    #
    #     TOP       671 picks   21.9% homered   95% CI 18.9-25.2
    #     HR        672 picks   15.9% homered   95% CI 13.4-18.9
    #     all picks 4620        14.9%
    #
    # The intervals do not overlap, so it is not noise. And the HR bucket beats
    # the all-picks baseline by one point — the designation whose entire job is
    # finding home runs is barely distinguishable from picking any designated
    # hitter at all.
    #
    # WHY TOP WINS. TOP is drawn from overall_score, which is hr_score blended
    # with the other three markets. So the thing beating the HR score at home
    # runs is THE HR SCORE MIXED WITH HRR AND HIT. Tested directly on 27
    # held-out nights, every blend beat hr_score alone and the direction never
    # once flipped:
    #
    #     top 20 by...                       HR%      vs hr_score alone
    #     hr_score alone                    20.7%     baseline
    #     0.90 hr + 0.10 hrr                21.9%     10W-4L
    #     0.80 hr + 0.20 hrr                22.4%     11W-4L
    #     0.70 hr + 0.20 hrr + 0.10 hit     22.6%     13W-4L  p=0.049
    #     0.70 hr + 0.30 hrr                22.8%     13W-5L  p=0.096
    #
    # WHAT THE MODEL WAS MISSING, in one word: OPPORTUNITY. hrr_score knows
    # how many times he will bat, whether the lineup around him turns over, and
    # whether this is a game where runs happen at all. hr_score was scoring the
    # SWING and ignoring how many swings he gets. You cannot homer in the
    # dugout, and no amount of barrel rate fixes batting eighth behind a lineup
    # that goes down in order.
    #
    # 0.70/0.20/0.10 is the blend that reached p<0.05. Deliberately not 0.70 hr
    # + 0.30 hrr even though it scored a hair higher — hrr at 0.30 starts
    # ranking run-scorers rather than home-run hitters, and the two are only
    # correlated up to a point. contact_score is left out on purpose: it is the
    # one score that does not beat a random shuffle (42.6% vs 40.7%), so
    # folding it in would be adding noise on evidence.
    #
    # hr_score_pure keeps the unblended model output so the fold can always be
    # measured against what it replaced, and so a future backtest can tell the
    # two apart in the archive.
    h.hr_score_pure = h.hr_score
    _fold_hrr = safe_float(getattr(h, "hrr_score", 0.0), 0.0)
    _fold_hit = safe_float(getattr(h, "hit_score", 0.0), 0.0)
    if _fold_hrr > 0.0 or _fold_hit > 0.0:
        # A missing sibling score falls back to hr_score rather than to zero,
        # so a row with no HRR published is ranked on what it does have instead
        # of being pushed 30% of the way to the bottom of the board.
        _fold_hrr = _fold_hrr if _fold_hrr > 0.0 else h.hr_score
        _fold_hit = _fold_hit if _fold_hit > 0.0 else h.hr_score
        h.hr_score = round(_hr2_clip(
            0.70 * h.hr_score + 0.20 * _fold_hrr + 0.10 * _fold_hit
        ), 2)

    # overall_score is computed inside apply_model_v2_layers (lines ~6161 and ~6209,
    # the second pass using recency-adjusted components). Computing it here would
    # be immediately overwritten -- removed per audit (2026-06-29).
    apply_model_v2_layers(h)
    apply_matchup_center_fields(h)
    return h

def build_hitter_records(client: MLBClient, db: CacheDB, game: Dict[str, Any], slate_date: dt.date) -> List[HitterRecord]:
    game_pk = safe_int(game.get("gamePk"), 0)
    live = client.live_game(game_pk)
    game_data = live.get("gameData", {})
    boxscore = live.get("liveData", {}).get("boxscore", {})
    teams_box = boxscore.get("teams", {})
    away_team = game_data.get("teams", {}).get("away", {})
    home_team = game_data.get("teams", {}).get("home", {})
    away_abbr = normalize_team_abbr(away_team.get("abbreviation", ""))
    home_abbr = normalize_team_abbr(home_team.get("abbreviation", ""))
    venue = game_data.get("venue", {})
    venue_name = venue.get("name", "Unknown venue")
    venue_blob = client.venue(safe_int(venue.get("id"), 0)) if venue.get("id") else {}
    lat, lon = get_venue_coords(venue_blob)
    roof = infer_roof(venue_blob, home_abbr)
    # Weather caching added (per audit, 2026-06-27): fetch_weather previously
    # had ZERO caching -- every single game hit Open-Meteo fresh, every run,
    # with no fallback to a recent cached value if the provider was slow or
    # rate-limiting. This is the most likely real cause of reported weather
    # timeouts (cumulative slowness across 15 games, not a single call
    # exceeding the 30s TIMEOUT). Keyed on venue + rounded coords + game date
    # (not exact time) since a forecast doesn't need minute-level precision
    # and rounding improves cache-hit rate across nearly-identical lookups.
    if lat is not None and lon is not None:
        _weather_cache_key = f"weather_v1:{venue_name}:{round(lat, 2)}:{round(lon, 2)}:{str(game.get('gameDate', ''))[:10]}:{roof}"
        _cached_weather = db.get(_weather_cache_key, max_age_days=1)
        if isinstance(_cached_weather, dict) and _cached_weather.get("weather_source") not in (None, "none"):
            weather = WeatherSummary(**_cached_weather)
        else:
            weather = fetch_weather(lat, lon, game.get("gameDate", ""), roof, home_abbr)
            try:
                db.set(_weather_cache_key, dataclasses.asdict(weather))
            except Exception:
                pass
    else:
        weather = WeatherSummary(roof=roof)

    probable = game_data.get("probablePitchers", {}) or {}
    probable_home_id = safe_int((probable.get("home") or {}).get("id"), 0)
    probable_away_id = safe_int((probable.get("away") or {}).get("id"), 0)
    # TBD? Chase it before settling (live feed, then rotation inference).
    _tmp_home_tid = safe_int((game_data.get("teams", {}).get("home", {}) or {}).get("id"), 0)
    _tmp_away_tid = safe_int((game_data.get("teams", {}).get("away", {}) or {}).get("id"), 0)
    probable_home_id, _home_proj = resolve_probable_pitcher(game_pk, probable_home_id, _tmp_home_tid, slate_date)
    probable_away_id, _away_proj = resolve_probable_pitcher(game_pk, probable_away_id, _tmp_away_tid, slate_date)
    data_end = statcast_data_end_date(slate_date)
    pitchers = {
        "home": build_pitcher_profile(client, db, probable_home_id, home_abbr, data_end),
        "away": build_pitcher_profile(client, db, probable_away_id, away_abbr, data_end),
    }
    team_ids = {
        "home": safe_int((game_data.get("teams", {}).get("home", {}) or {}).get("id"), 0),
        "away": safe_int((game_data.get("teams", {}).get("away", {}) or {}).get("id"), 0),
    }
    bullpens = {
        "home": build_team_bullpen_profile(client, db, team_ids["home"], home_abbr, probable_home_id, data_end),
        "away": build_team_bullpen_profile(client, db, team_ids["away"], away_abbr, probable_away_id, data_end),
    }
    # ── THE OTHER HALF OF A STEAL (2026-08-23) ────────────────────────────
    # Both feeds are LEAGUE-WIDE and cached for the whole run, so this is two
    # HTTP round trips for the entire slate rather than two per game. If either
    # is down, its status says so and every row it would have filled says
    # "missing" rather than quietly taking a league-average value — an unknown
    # catcher scored as average is exactly how a matchup against the best
    # thrower in baseball reads neutral.
    _savant_season = slate_date.year
    _catchers, _catcher_status = ({}, "missing")
    _team_def, _team_def_status = ({}, "missing")
    if SAVANT_FEEDS is not None:
        try:
            _catchers, _catcher_status = SAVANT_FEEDS.catcher_throwing(_savant_season)
            _team_def, _team_def_status = SAVANT_FEEDS.team_defense(_savant_season)
        except Exception as _exc:                                 # noqa: BLE001
            _catcher_status = _team_def_status = f"error:{type(_exc).__name__}"
    else:
        _catcher_status = _team_def_status = "module_missing"
    catchers_by_side = {}
    for _side in ("home", "away"):
        cid, cname, csrc = find_catcher(teams_box.get(_side, {}) or {})
        catchers_by_side[_side] = {"id": cid, "name": cname, "source": csrc}

    rows: List[HitterRecord] = []
    for side in ("away", "home"):
        team_box = teams_box.get(side, {})
        lineup = extract_lineup(team_box)
        confirmed = bool(lineup)
        team_id = safe_int((game_data.get("teams", {}).get(side, {}) or {}).get("id"), 0)
        if not lineup:
            lineup = build_projected_lineup(client, team_box, team_id)
        if not lineup:
            continue

        opp_key = "home" if side == "away" else "away"
        pitcher = pitchers[opp_key]
        # The catcher THIS lineup is running against is the OPPOSING team's.
        _c = catchers_by_side.get(opp_key, {}) or {}
        _cprof = _catchers.get(_c.get("id") or 0) or {}
        _tdef = _team_def.get(team_ids.get(opp_key, 0)) or {}
        opp_bullpen = bullpens.get(opp_key, {})
        # Pull pitch mix scoped by hitter handedness — pitchers throw very different
        # mixes vs LHB vs RHB. Each call hits the same underlying Statcast pull (cached).
        pitch_mix_all = build_pitcher_pitch_mix(db, pitcher.player_id, data_end)
        pitch_mix_vs_L = build_pitcher_pitch_mix(db, pitcher.player_id, data_end, batter_side="L")
        pitch_mix_vs_R = build_pitcher_pitch_mix(db, pitcher.player_id, data_end, batter_side="R")
        team_abbr = away_abbr if side == "away" else home_abbr
        opp_abbr = home_abbr if side == "away" else away_abbr
        park_factor = float(PARK_FACTORS.get(home_abbr, 100))
        park_pf = get_park_factors(home_abbr)
        team_players = team_box.get("players", {}) or {}

        team_side_rows: List[HitterRecord] = []
        for pid, spot in lineup:
            pbox = team_players.get(f"ID{pid}", {})
            name = (pbox.get("person") or {}).get("fullName", f"Player {pid}")
            bats = ((pbox.get("batSide") or {}).get("code")) or "?"
            # Fallback: boxscore won't have batSide if lineup isn't confirmed yet.
            #
            # BUGFIX: this previously called client.person(pid) live on every run
            # with no caching, and any failure was swallowed by a bare
            # `except Exception: pass` -- so a single flaky/rate-limited call
            # silently left bats="?" for that player with zero trace in logs.
            # Confirmed via a month of real graded_results data: bats was "?"
            # for 100% of 1,833 rows, meaning every weak-side matchup check in
            # the scoring engine (9 separate call sites) has never actually
            # fired. Now cached (7-day TTL, same pattern as get_player_split_stats)
            # so a successful lookup persists across runs, and failures are
            # logged with the real exception instead of disappearing.
            if bats == "?":
                cache_key = f"bat_side:{pid}"
                cached_entry = db.get(cache_key, max_age_days=7)
                if cached_entry and cached_entry.get("bats"):
                    bats = cached_entry["bats"]
                else:
                    try:
                        _pb = client.person(pid)
                        _person = (_pb.get("people") or [{}])[0]
                        bats = (_person.get("batSide") or {}).get("code") or "?"
                        if bats != "?":
                            db.set(cache_key, {"bats": bats})
                        else:
                            print(f"WARNING: batSide missing from /people/{pid} response for {name}", file=sys.stderr)
                    except Exception as exc:
                        print(f"WARNING: batSide lookup failed for {name} (pid={pid}): {type(exc).__name__}: {exc}", file=sys.stderr)
            jersey = safe_int(pbox.get("jerseyNumber"), 0) or None

            try:
                sblob = client.person_stats(pid, group="hitting", stat_type="season")
                stats_list = sblob.get("stats") or []
                first = stats_list[0] if stats_list else {}
                splits = first.get("splits") or []
                stat = (splits[0].get("stat") if splits else {}) or {}
            except Exception:
                stat = {}
            season = flatten_season_hitting(stat)
            # Instrumentation (2026-08-13): unlike the person_stats call just
            # above (which already catches and falls back to stat={}), this
            # call had no try/except and compute_window_from_gamelog() has no
            # status field of its own -- a failed or empty pull here silently
            # produces {"avg": 0.250, "hits": 0, ...} for last5/7/10 with
            # nothing on the record to distinguish that from a real 0-for-5.
            # last5_runs/last5_rbi specifically have sat un-populating in the
            # graded archive for weeks with no way to tell why -- this is the
            # first point that can actually say so.
            try:
                glog = client.person_game_log(pid)
                last5_status = "ok" if (glog.get("stats") or []) else "empty"
            except Exception as exc:
                glog = {}
                last5_status = f"error:{type(exc).__name__}"
            l5 = compute_window_from_gamelog(glog, 5)
            l7 = compute_window_from_gamelog(glog, 7)
            l10 = compute_window_from_gamelog(glog, 10)
            gs_since_hr = games_since_last_hr(glog, max_lookback=60)
            blank_prof = compute_blank_profile(glog)
            split = get_player_split_stats(client, db, pid)
            sc = build_batter_statcast_profile(db, pid, statcast_data_end_date(slate_date))
            _xhr_register_batter(pid, sc)
            batter_pitch_profile = build_batter_pitch_type_profile(db, pid, statcast_data_end_date(slate_date))
            # Select handed pitch mix for this hitter. Fall back to all-batters if
            # the handed slice is empty or low-sample (<150 pitches).
            _eside = effective_side(bats, getattr(pitcher, "throws", None))
            handed_mix = pitch_mix_vs_L if _eside == "L" else pitch_mix_vs_R if _eside == "R" else pitch_mix_all
            if safe_int(handed_mix.get("sample_pitches"), 0) < 150:
                pitch_mix_data = pitch_mix_all
            else:
                pitch_mix_data = handed_mix
            # Mirror the same handed-split logic on the BATTER side: use his
            # vs_lhp/vs_rhp per-pitch-type profile (matching today's pitcher's
            # throwing hand) instead of the all-time blend across both hands.
            # That data already existed (built for PitchBreakdown.js) but was
            # never read here -- added per audit (2026-06-27). Falls back to
            # the all-time blend if the handed slice's total sample is too
            # thin to trust, same pattern as the pitcher-side fallback above.
            _batter_handed_key = "vs_lhp" if pitcher.throws == "L" else "vs_rhp" if pitcher.throws == "R" else None
            _batter_handed_slice = batter_pitch_profile.get(_batter_handed_key, {}) if _batter_handed_key else {}
            _batter_handed_by_pitch = _batter_handed_slice.get("by_pitch", {}) if isinstance(_batter_handed_slice, dict) else {}
            _batter_handed_sample = sum(safe_int(v.get("seen"), 0) for v in _batter_handed_by_pitch.values()) if _batter_handed_by_pitch else 0
            if _batter_handed_by_pitch and _batter_handed_sample >= 40:
                batter_by_pitch_for_fit = _batter_handed_by_pitch
            else:
                batter_by_pitch_for_fit = batter_pitch_profile.get("by_pitch", {})
            pmix_fit = calculate_pitch_mix_fit(
                pitch_mix_data.get("mix", {}),
                batter_by_pitch_for_fit,
                pitch_mix_data.get("per_pitch_damage", {}),
                batter_pitch_profile.get("recent10_games", {}).get("by_pitch", {}),
            )
            bvp = build_batter_vs_pitcher_profile(db, pid, pitcher.player_id, statcast_data_end_date(slate_date))

            park_dims = get_park_dimensions(venue_name)
            park_fit = compute_spray_park_fit(venue_name, bats, sc.get("bbe_profile", {}), sc.get("spray_chart", []), weather)

            rec = HitterRecord(
                game_pk=game_pk,
                game_time=game.get("gameDate", ""),
                team=team_abbr,
                opponent=opp_abbr,
                venue_name=venue_name,
                lineup_confirmed=confirmed,
                player_id=pid,
                name=name,
                bats=bats,
                lineup_spot=spot,
                jersey_number=jersey,
                # BUGFIX (2026-06-27): park_fit was computed above but never
                # stored on the record -- compute_damage_conversion_v31's
                # park_weather/air_pull modifiers and the park_weather term
                # feeding hr_raw both read h.park_fit via getattr, which
                # always returned the empty-dict default. Now that weather
                # data itself is fixed (venue coords bug), this completes
                # the wiring so those modifiers can actually fire.
                park_fit=park_fit,
                season_avg=season["season_avg"],
                season_obp=season["season_obp"],
                season_ops=season["season_ops"],
                season_slg=season["season_slg"],
                season_iso=season["season_iso"],
                season_tb=season.get("season_tb", 0),
                season_ab=season.get("season_ab", 0),
                season_sb=season.get("season_sb", 0),
                season_cs=season.get("season_cs", 0),
                season_sb_attempt_rate=season.get("season_sb_attempt_rate", 0.0),
                season_doubles=season.get("season_doubles", 0),
                season_triples=season.get("season_triples", 0),
                season_babip=season.get("season_babip", 0.0),
                season_hr=season["season_hr"],
                season_pa=season["season_pa"],
                season_bb_rate=season["season_bb_rate"],
                season_k_rate=season["season_k_rate"],
                season_rbi=season["season_rbi"],
                season_runs=season["season_runs"],
                season_rbi_per_pa=season["season_rbi_per_pa"],
                season_runs_per_pa=season["season_runs_per_pa"],
                last5_avg=l5["avg"],
                last5_hits=l5["hits"],
                last5_hr=l5["hr"],
                last5_xbh=l5["xbh"],
                last5_runs=l5["runs"],
                last5_rbi=l5["rbi"],
                last5_status=last5_status,
                last7_avg=l7["avg"],
                last7_hits=l7["hits"],
                last7_hr=l7["hr"],
                last7_xbh=l7["xbh"],
                last7_runs=l7["runs"],
                last7_rbi=l7["rbi"],
                last10_avg=l10["avg"],
                last10_hits=l10["hits"],
                last10_hr=l10["hr"],
                last10_xbh=l10["xbh"],
                avg_vs_rhp=split["avg_vs_rhp"],
                avg_vs_lhp=split["avg_vs_lhp"],
                iso_vs_rhp=split["iso_vs_rhp"],
                iso_vs_lhp=split["iso_vs_lhp"],
                recent_350_num=sc["recent_350_num"],
                recent_350_den=max(1, sc["recent_350_den"]),
                recent_distance_tracked=safe_int(sc.get("recent_distance_tracked"), 0),
                recent_375_num=sc["recent_375_num"],
                recent_400_num=safe_int(sc.get("recent_400_num"), 0),
                recent_max_distance=safe_float(sc.get("recent_max_distance"), 0.0),
                recent_avg_distance=safe_float(sc.get("recent_avg_distance"), 0.0),
                recent_avg_hr_distance=safe_float(sc.get("recent_avg_hr_distance"), 0.0),
                recent_pull_air_rate=safe_float(sc.get("recent_pull_air_rate"), 0.0),
                recent_squared_up_rate=sc.get("recent_squared_up_rate"),
                recent_squared_up_sample=safe_int(sc.get("recent_squared_up_sample"), 0),
                recent_blast_rate=sc.get("recent_blast_rate"),
                recent_bat_tracking_status=str(sc.get("recent_bat_tracking_status") or "missing"),
                recent_bat_tracking_window=str(sc.get("recent_bat_tracking_window") or ""),
                season_max_distance=safe_float(sc.get("season_max_distance"), 0.0),
                recent_ev=safe_float(sc.get("recent_ev"), 88.5),
                recent_hard_hit_rate=sc["recent_hard_hit_rate"],
                recent_sweet_spot_rate=sc["recent_sweet_spot_rate"],
                recent_ideal_hr_contact=sc["recent_ideal_hr_contact"],
                recent_fb_rate=sc["recent_fb_rate"],
                recent_ld_rate=safe_float(sc.get("recent_ld_rate"), 0.0),
                recent_gb_rate=safe_float(sc.get("recent_gb_rate"), 0.0),
                recent_popup_rate=safe_float(sc.get("recent_popup_rate"), 0.0),
                recent_barrel_rate=sc["recent_barrel_rate"],
                recent_xwoba=safe_float(sc.get("recent_xwoba"), 0.320),
                recent_pull_rate=safe_float(sc.get("recent_pull_rate"), 0.38),
                l5_barrel_rate=safe_float(sc.get("l5_barrel_rate"), 0.0),
                l10_barrel_rate=safe_float(sc.get("l10_barrel_rate"), 0.0),
                l5_hard_hit_rate=safe_float(sc.get("l5_hard_hit_rate"), 0.0),
                l10_hard_hit_rate=safe_float(sc.get("l10_hard_hit_rate"), 0.0),
                l5_xwoba=safe_float(sc.get("l5_xwoba"), 0.320),
                l10_xwoba=safe_float(sc.get("l10_xwoba"), 0.320),
                l5_pull_rate=safe_float(sc.get("l5_pull_rate"), 0.38),
                l10_pull_rate=safe_float(sc.get("l10_pull_rate"), 0.38),
                l20pa_pa=safe_int(sc.get("l20pa_pa"), 0),
                l20pa_bbe=safe_int(sc.get("l20pa_bbe"), 0),
                l20pa_hr=safe_int(sc.get("l20pa_hr"), 0),
                l20pa_xbh=safe_int(sc.get("l20pa_xbh"), 0),
                l20pa_350_num=safe_int(sc.get("l20pa_350_num"), 0),
                l20pa_350_den=max(1, safe_int(sc.get("l20pa_350_den"), 1)),
                l20pa_375_num=safe_int(sc.get("l20pa_375_num"), 0),
                l20pa_hard_hit_rate=safe_float(sc.get("l20pa_hard_hit_rate"), 0.0),
                l20pa_ideal_hr_contact=safe_float(sc.get("l20pa_ideal_hr_contact"), 0.0),
                l20pa_fb_rate=safe_float(sc.get("l20pa_fb_rate"), 0.0),
                l20pa_barrel_rate=safe_float(sc.get("l20pa_barrel_rate"), 0.0),
                l20pa_xwoba=safe_float(sc.get("l20pa_xwoba"), 0.320),
                l20pa_pull_rate=safe_float(sc.get("l20pa_pull_rate"), 0.38),
                l25pa_pa=safe_int(sc.get("l25pa_pa"), 0),
                l25pa_bbe=safe_int(sc.get("l25pa_bbe"), 0),
                l25pa_avg_ev=safe_float(sc.get("l25pa_avg_ev"), 88.5),
                l25pa_avg_la=safe_float(sc.get("l25pa_avg_la"), 0.0),
                l25pa_hard_hit_rate=safe_float(sc.get("l25pa_hard_hit_rate"), 0.0),
                l25pa_barrel_rate=safe_float(sc.get("l25pa_barrel_rate"), 0.0),
                l25pa_sweet_spot_rate=safe_float(sc.get("l25pa_sweet_spot_rate"), 0.0),
                l25pa_ld_rate=safe_float(sc.get("l25pa_ld_rate"), 0.0),
                l25pa_gb_rate=safe_float(sc.get("l25pa_gb_rate"), 0.0),
                l25pa_fb_rate=safe_float(sc.get("l25pa_fb_rate"), 0.0),
                l25pa_popup_rate=safe_float(sc.get("l25pa_popup_rate"), 0.0),
                l25pa_air_rate=safe_float(sc.get("l25pa_air_rate"), 0.0),
                l25pa_300_plus=safe_int(sc.get("l25pa_300_plus"), 0),
                l25pa_375_plus=safe_int(sc.get("l25pa_375_plus"), 0),
                l25pa_avg_bat_speed=sc.get("l25pa_avg_bat_speed"),
                l25pa_avg=safe_float(sc.get("l25pa_avg"), 0.0),
                babip=sc["babip"],
                pitcher_id=pitcher.player_id,
                pitcher_name=pitcher.name,
                pitcher_team=pitcher.team_abbr,
                pitcher_throws=pitcher.throws,
                pitcher_era=pitcher.era,
                pitcher_whip=pitcher.whip,
                pitcher_season_stats_status=getattr(pitcher, "season_stats_status", "missing"),
                pitcher_hr9=pitcher.hr9,
                pitcher_bb9=getattr(pitcher, "bb9", 3.20),
                pitcher_hr_allowed=pitcher.hr_allowed,
                pitcher_k_rate=getattr(pitcher, "k_rate", 0.0),
                pitcher_k9=getattr(pitcher, "k9", 0.0),
                pitcher_babip=pitcher.babip,
                pitcher_fb_rate=pitcher.fb_rate,
                pitcher_statcast_bbe=pitcher.statcast_bbe,
                pitcher_statcast_games=getattr(pitcher, "statcast_games", 0),
                pitcher_statcast_base_bbe=getattr(pitcher, "statcast_base_bbe", 0),
                pitcher_statcast_base_games=getattr(pitcher, "statcast_base_games", 0),
                pitcher_statcast_status=pitcher.statcast_status,
                pitcher_ev_allowed=pitcher.ev_allowed,
                pitcher_fb_velo_delta=pitcher.fb_velo_delta,
                pitcher_fb_velo_status=pitcher.fb_velo_status,
                pitcher_hardhit_allowed=pitcher.hardhit_allowed,
                pitcher_barrel_allowed=pitcher.barrel_allowed,
                pitcher_statcast_fb_rate=pitcher.statcast_fb_rate,
                pitcher_gb_rate=getattr(pitcher, "gb_allowed", 0.42),
                pitcher_ld_rate=getattr(pitcher, "ld_allowed", 0.21),
                pitcher_popup_rate=getattr(pitcher, "popup_allowed", 0.05),
                pitcher_375_allowed=pitcher.dist375_allowed,
                pitcher_400_allowed=pitcher.dist400_allowed,
                pitcher_attack_score=pitcher.pitcher_attack_score,
                pitcher_attack_tag=pitcher.pitcher_attack_tag,
                pitcher_hr9_vs_lhb=pitcher.hr9_vs_lhb,
                pitcher_hr9_vs_rhb=pitcher.hr9_vs_rhb,
                pitcher_whip_vs_lhb=pitcher.whip_vs_lhb,
                pitcher_whip_vs_rhb=pitcher.whip_vs_rhb,
                pitcher_hr_vs_lhb=pitcher.hr_vs_lhb,
                pitcher_hr_vs_rhb=pitcher.hr_vs_rhb,
                pitcher_xbh_vs_lhb=pitcher.xbh_vs_lhb,
                pitcher_xbh_vs_rhb=pitcher.xbh_vs_rhb,
                pitcher_l3_era=pitcher.l3_era,
                pitcher_l3_whip=pitcher.l3_whip,
                pitcher_l3_hr9=pitcher.l3_hr9,
                pitcher_l3_starts_found=pitcher.l3_starts_found,
                pitcher_weak_side=pitcher.weak_side,
                pitcher_weak_side_score=(pitcher.weak_side_score_lhb if effective_side(bats, pitcher.throws) == "L" else pitcher.weak_side_score_rhb),
                pitcher_weak_side_gap=pitcher.weak_side_gap,
                pitcher_side_slug=(pitcher.slug_vs_lhb if effective_side(bats, pitcher.throws) == "L" else pitcher.slug_vs_rhb),
                pitcher_side_ops=(pitcher.ops_vs_lhb if effective_side(bats, pitcher.throws) == "L" else pitcher.ops_vs_rhb),
                pitcher_lineup_spot_damage=getattr(pitcher, "lineup_spot_damage", {}) or {},
                pitcher_situational_splits=getattr(pitcher, "situational_splits", {}) or {},
                pitcher_lineup_zone_damage=getattr(pitcher, "lineup_zone_damage", {}) or {},
                pitch_mix_score=safe_float(pmix_fit.get("score"), 50.0),
                pitch_mix_note=str(pmix_fit.get("note", "PMix N/A")),
                pitcher_primary_mix=str(pitch_mix_data.get("primary_mix", "Mix N/A")),
                pitch_mix_sample=safe_int(pmix_fit.get("sample"), 0),
                pitch_type_match_flag=bool(pmix_fit.get("pitch_type_match_flag", False)),
                pitch_type_match_code=str(pmix_fit.get("pitch_type_match_code", "")),
                pitch_type_match_note=str(pmix_fit.get("pitch_type_match_note", "")),
                pitch_type_match_score=safe_float(pmix_fit.get("pitch_type_match_score"), 0.0),
                pitcher_pitch_arsenal_detail=pitch_mix_data.get("pitch_type_summary", []) or [],
                games_since_last_hr=gs_since_hr,
                last_game_date=blank_prof["last_game_date"],
                last_game_ab=blank_prof["last_game_ab"],
                last_game_pa=blank_prof["last_game_pa"],
                last_game_hits=blank_prof["last_game_hits"],
                last_game_hr=blank_prof["last_game_hr"],
                last_game_tb=blank_prof["last_game_tb"],
                last_game_rbi=blank_prof["last_game_rbi"],
                last_game_runs=blank_prof["last_game_runs"],
                last_game_hrr=blank_prof["last_game_hrr"],
                blank_streak=blank_prof["blank_streak"],
                after_blank_n=blank_prof["after_blank_n"],
                after_blank_hit=blank_prof["after_blank_hit"],
                after_blank_hrr1=blank_prof["after_blank_hrr1"],
                after_blank_hrr2=blank_prof["after_blank_hrr2"],
                after_blank_tb2=blank_prof["after_blank_tb2"],
                overall_n=blank_prof["overall_n"],
                overall_hit=blank_prof["overall_hit"],
                overall_hrr1=blank_prof["overall_hrr1"],
                overall_hrr2=blank_prof["overall_hrr2"],
                overall_tb2=blank_prof["overall_tb2"],
                after_hit_n=blank_prof["after_hit_n"],
                after_hit_hit=blank_prof["after_hit_hit"],
                after_hit_hrr1=blank_prof["after_hit_hrr1"],
                after_hit_hrr2=blank_prof["after_hit_hrr2"],
                after_hit_tb2=blank_prof["after_hit_tb2"],
                blank_profile_status=blank_prof["blank_profile_status"],
                bbe_profile=sc.get("bbe_profile", {}),
                spray_chart=sc.get("spray_chart", []),
                contact_log=sc.get("contact_log", sc.get("spray_chart", [])),
                batted_ball_log=sc.get("batted_ball_log", sc.get("spray_chart", [])),
                hr_shape_profile=sc.get("hr_shape_profile", {}) or {},
                personal_shape_match=safe_float(sc.get("personal_shape_match"), 0.0),
                personal_shape_recent_rate=safe_float(sc.get("personal_shape_recent_rate"), 0.0),
                personal_shape_season_rate=safe_float(sc.get("personal_shape_season_rate"), 0.0),
                personal_shape_status=str(sc.get("personal_shape_status") or "missing"),
                pitcher_pitch_mix=pitch_mix_data,
                pitcher_pitch_mix_vs_lhb=pitch_mix_vs_L,
                pitcher_pitch_mix_vs_rhb=pitch_mix_vs_R,
                pitcher_pitch_type_summary_vs_lhb=pitch_mix_vs_L.get("pitcher_pitch_type_summary", []) or pitch_mix_vs_L.get("pitch_type_summary", []) or [],
                pitcher_pitch_type_summary_vs_rhb=pitch_mix_vs_R.get("pitcher_pitch_type_summary", []) or pitch_mix_vs_R.get("pitch_type_summary", []) or [],
                pitcher_primary_mix_vs_lhb=str(pitch_mix_vs_L.get("primary_mix", "") or ""),
                pitcher_primary_mix_vs_rhb=str(pitch_mix_vs_R.get("primary_mix", "") or ""),
                batter_pitch_type_profile=batter_pitch_profile,
                pitch_mix_matchup={
                    "score": safe_float(pmix_fit.get("score"), 50.0),
                    "note": pmix_fit.get("note", "PMix N/A"),
                    "sample": safe_int(pmix_fit.get("sample"), 0),
                    "pitch_trap": bool(pmix_fit.get("pitch_trap", False)),
                    "trap_reason": str(pmix_fit.get("trap_reason", "")),
                    "best_damage_pitch": str(pmix_fit.get("best_damage_pitch", "")),
                    "best_damage_usage": safe_float(pmix_fit.get("best_damage_usage"), 0.0),
                    "strong_pitch_signal": bool(pmix_fit.get("strong_pitch_signal", False)),
                    "details": pmix_fit.get("details", []),
                    "pitcher_mix": pitch_mix_data.get("mix", {}),
                    "pitcher_primary_mix": pitch_mix_data.get("primary_mix", "Mix N/A"),
                    "batter_by_pitch": batter_pitch_profile.get("by_pitch", {}),
                    "crush_pitches": [x for x in str(pmix_fit.get("note", "")).replace("Crush ", "").split("/") if x and "Risk" not in x and "Neutral" not in x and "N/A" not in x][:3],
                },
                pitch_type_summary=batter_pitch_profile.get("pitch_type_summary", []),
                game_log=[],
                statcast_pull_status=sc.get("statcast_pull_status", "unknown"),
                bullpen_era=safe_float(opp_bullpen.get("era"), 4.20),
                bullpen_hr9=safe_float(opp_bullpen.get("hr9"), 1.10),
                bullpen_whip=safe_float(opp_bullpen.get("whip"), 1.30),
                bullpen_quality=str(opp_bullpen.get("quality", "average")),
                bullpen_attack_score=safe_float(opp_bullpen.get("attack_score"), 50.0),
                bullpen_pitch_fit=(
                    safe_float(
                        calculate_pitch_mix_fit(
                            opp_bullpen.get("team_mix", {}),
                            batter_by_pitch_for_fit,
                        ).get("score"),
                        50.0,
                    )
                    if opp_bullpen.get("team_mix_status") == "ok"
                    else 50.0
                ),
                bvp_pa=safe_int(bvp.get("pa"), 0),
                bvp_ab=safe_int(bvp.get("ab"), 0),
                bvp_hits=safe_int(bvp.get("hits"), 0),
                bvp_hr=safe_int(bvp.get("hr"), 0),
                bvp_xbh=safe_int(bvp.get("xbh"), 0),
                bvp_avg=safe_float(bvp.get("avg"), 0.0),
                bvp_ops=safe_float(bvp.get("ops"), 0.0),
                bvp_note=str(bvp.get("note", "No BvP sample")),
                bvp_babip=safe_float(bvp.get("babip"), 0.300),
                bvp_woba=safe_float(bvp.get("woba"), 0.320),
                bvp_iso=safe_float(bvp.get("iso"), 0.150),
                bvp_obp=safe_float(bvp.get("obp"), 0.320),
                bvp_k_pct=safe_float(bvp.get("k_pct"), 0.220),
                bvp_bb_pct=safe_float(bvp.get("bb_pct"), 0.080),
                bvp_barrels=safe_int(bvp.get("barrels"), 0),
                bvp_hard_hit=safe_int(bvp.get("hard_hit"), 0),
                weather_temp_f=weather.temp_f,
                weather_wind_mph=weather.wind_mph,
                weather_wind_deg=weather.wind_deg,
                roof=weather.roof,
                park_factor=park_factor,
                weather_humidity=weather.humidity,
                weather_feels_like_f=weather.feels_like_f,
                weather_precip_chance=weather.precip_chance,
                weather_wind_direction_label=weather.wind_direction_label,
                weather_wind_boost=weather.wind_boost,
                weather_source=weather.weather_source,
                weak_spot_flag=spot in pitcher.weak_spots,
                weak_spot_bonus=0.0,  # computed by score_hitter from tiered criteria
                weak_spot_reason=_weak_spot_reason_for(pitcher, spot),
                # Pitcher advanced stats — passed through from PitcherSummary.
                # ── the catcher and the defence behind tonight's arm ──────
                opp_catcher_id=_c.get("id", 0),
                opp_catcher_name=_c.get("name", ""),
                opp_catcher_source=_c.get("source", ""),
                opp_catcher_cs_rate=_cprof.get("cs_rate"),
                opp_catcher_cs_rate_expected=_cprof.get("cs_rate_expected"),
                opp_catcher_pop_time=_cprof.get("pop_time"),
                opp_catcher_arm_strength=_cprof.get("arm_strength"),
                opp_catcher_sb_attempts=_cprof.get("sb_attempts", 0),
                # Three ways this can be unknown and they are NOT the same:
                # the feed failed, the feed is fine but this catcher is not
                # qualified for it, or we never worked out who is catching.
                # A reader who cannot tell them apart cannot tell a hard
                # matchup from an unmeasured one.
                opp_catcher_status=(
                    _catcher_status if _catcher_status != "ok"
                    else "ok" if _cprof
                    else "no_catcher" if not _c.get("id")
                    else "unqualified"
                ),
                opp_def_oaa=_tdef.get("oaa"),
                opp_def_oaa_vs_hand=(
                    _tdef.get("oaa_vs_lhb")
                    if effective_side(bats, getattr(pitcher, "throws", "")) == "L"
                    else _tdef.get("oaa_vs_rhb")
                ) if _tdef else None,
                opp_def_success_rate=_tdef.get("success_rate"),
                opp_def_status=(_team_def_status if _team_def_status != "ok"
                                else "ok" if _tdef else "no_team"),
                pitcher_wild_pitches=getattr(pitcher, "wild_pitches", 0),
                pitcher_pickoffs=getattr(pitcher, "pickoffs", 0),
                pitcher_balks=getattr(pitcher, "balks", 0),
                pitcher_sb_against=getattr(pitcher, "sb_against", 0),
                pitcher_cs_against=getattr(pitcher, "cs_against", 0),
                pitcher_sb_attempts_against=getattr(pitcher, "sb_attempts_against", 0),
                pitcher_cs_rate_against=getattr(pitcher, "cs_rate_against", None),
                pitcher_wp9=getattr(pitcher, "wp9", None),
                pitcher_pickoff_rate=getattr(pitcher, "pickoff_rate", None),
                pitcher_running_game_status=getattr(pitcher, "running_game_status", "missing"),
                pitcher_meatball_pct=getattr(pitcher, "meatball_pct", 0.070),
                pitcher_meatball_pct_vs_lhb=getattr(pitcher, "meatball_pct_vs_lhb", 0.070),
                pitcher_meatball_pct_vs_rhb=getattr(pitcher, "meatball_pct_vs_rhb", 0.070),
                pitcher_meatball_pitches_vs_lhb=getattr(pitcher, "meatball_pitches_vs_lhb", 0),
                pitcher_meatball_pitches_vs_rhb=getattr(pitcher, "meatball_pitches_vs_rhb", 0),
                pitcher_meatball_side_status=getattr(pitcher, "meatball_side_status", "missing"),
                pitcher_putaway_pct=getattr(pitcher, "putaway_pct", 0.180),
                pitcher_swstr_pct=getattr(pitcher, "swstr_pct", 0.110),
                pitcher_first_pitch_strike_pct=getattr(pitcher, "first_pitch_strike_pct", 0.600),
                pitcher_whiff_pct=getattr(pitcher, "whiff_pct", 0.240),
                pitcher_pullair_allowed_pct=getattr(pitcher, "pullair_allowed_pct", 0.220),
                pitcher_advanced_stats_sample=getattr(pitcher, "advanced_stats_sample", 0),
                pitcher_advanced_stats_status=getattr(pitcher, "advanced_stats_status", "missing"),
                # Extended pitcher stats — passed through from PitcherSummary.
                pitcher_fip=getattr(pitcher, "fip", 4.00),
                pitcher_avg_against=getattr(pitcher, "avg_against", 0.250),
                pitcher_obp_against=getattr(pitcher, "obp_against", 0.320),
                pitcher_slg_against=getattr(pitcher, "slg_against", 0.400),
                pitcher_ops_against=getattr(pitcher, "ops_against", 0.720),
                pitcher_iso_against=getattr(pitcher, "iso_against", 0.150),
                pitcher_woba_against=getattr(pitcher, "woba_against", 0.320),
                pitcher_tb_allowed=getattr(pitcher, "tb_allowed", 0),
                pitcher_bb_allowed=getattr(pitcher, "bb_allowed", 0),
                pitcher_bb_pct=getattr(pitcher, "bb_pct", 0.080),
                pitcher_barrels_allowed_count=getattr(pitcher, "barrels_allowed_count", 0),
                pitcher_hr_fb_pct=getattr(pitcher, "hr_fb_pct", 0.100),
                pitcher_extended_stats_status=getattr(pitcher, "extended_stats_status", "missing"),
                pitcher_trend_direction=getattr(pitcher, "trend_direction", "unknown"),
                pitcher_trend_reason=getattr(pitcher, "trend_reason", ""),
                # Per-stat park factors — same number used everywhere in scoring.
                park_hr_factor=park_pf["hr"],
                park_hits_factor=park_pf["hits"],
                park_barrel_factor=park_pf["barrel"],
                park_hardhit_factor=park_pf["hardhit"],
                park_k_factor=park_pf["k"],
                park_dist_factor=park_pf["dist"],
            )
            team_side_rows.append(score_hitter(rec))
        _apply_lineup_context_to_team_rows(team_side_rows)
        rows.extend(team_side_rows)
    return rows



def fmt_top15_line(rec: HitterRecord, idx: int) -> str:
    star = " ⭐" if rec.weak_spot_flag else ""
    split_avg = rec.avg_vs_lhp if rec.pitcher_throws == "L" else rec.avg_vs_rhp
    split_side = "LHP" if rec.pitcher_throws == "L" else "RHP"
    return (
        f"{idx}. {rec.name}{star} | {rec.team} | IdealHR {rec.recent_ideal_hr_contact:.2f} | HR {rec.hr_score:.1f} | FB {round(rec.recent_fb_rate * 100):.0f}% | spot {rec.lineup_spot}\n"
        f"   BA(S) {display_avg(rec.season_avg)} | BA vs {split_side} {display_avg(split_avg)} | "
        f"350+ {rec.recent_350_num}/{max(1, rec.recent_350_den)} | "
        f"375+ {rec.recent_375_num}/{max(1, rec.recent_350_den)} | "
        f"L5 {rec.last5_hits}H/{rec.last5_hr}HR/{rec.last5_xbh}XBH"
    )





def hrw_emoji(score: float) -> str:
    # Aligned to hrw_zone_label()'s actual bands (55/70/80), which this used to
    # disagree with (this used flat 50/60/70 cutoffs while hrw_zone_label uses
    # 55/70/80) -- the two were parallel systems that could show different
    # emoji for the same score. Also splits the 70-80 vs 80+ bands the same
    # way PlayerCard.js does, since hrw_zone_score_value() deliberately
    # dampens 80+ as less reliable than 70-80, not the same tier.
    s = safe_float(score, 0.0)
    if s > 80.0:
        return "🌋"
    if s > 70.0:
        return "🚀"
    if s >= 55.0:
        return "⚡"
    if s >= 45.0:
        # Changed 👀→🌤️: 👀 was double-booked with the final_hr_role "Power Watch"
        # tag, which now uses 🔭 instead. 🌤️ reads as "building/uncertain conditions",
        # fitting the borderline-timing meaning of this band.
        return "🌤️"
    return "🧊"


def hrw_text(rec: HitterRecord) -> str:
    return f"HRW {hrw_emoji(getattr(rec, 'hrw_score', 0.0))} ({getattr(rec, 'hrw_score', 0.0):.0f})"


def ihr_text(rec: HitterRecord) -> str:
    return f"IHR {rec.recent_ideal_hr_contact:.2f}"


def pmix_text(rec: HitterRecord) -> str:
    sample = getattr(rec, "pitch_mix_sample", 0)
    sample_txt = "" if sample >= 20 else " ⚠️"
    return f"PMix {getattr(rec, 'pitch_mix_score', 50.0):.0f}{sample_txt} | {getattr(rec, 'pitch_mix_note', 'PMix N/A')}"


def split_ba_text(rec: HitterRecord) -> str:
    if rec.pitcher_throws == "L":
        return f"BA vs LHP {display_avg(rec.avg_vs_lhp)}"
    return f"BA vs RHP {display_avg(rec.avg_vs_rhp)}"


def section_bar() -> str:
    return "═══════════════════════════════════════"

def hr_pace_text(rec: HitterRecord) -> str:
    """Signed HR count vs his expected pace over his recent PA window, instead
    of a raw HR/PA decimal that's hard to read at a glance.
    +N = ahead of pace (hot) | -N = behind pace (due) | ~0 = on pace.
    """
    season_pa = max(1, safe_int(getattr(rec, "season_pa", 0), 0))
    hr_per_pa = safe_float(getattr(rec, "hr_per_pa", None), None)
    if not hr_per_pa:
        hr_per_pa = safe_float(getattr(rec, "season_hr", 0), 0) / season_pa
    recent_pa = safe_int(getattr(rec, "l20pa_pa", 0), 0) or max(0, safe_int(getattr(rec, "recent_350_den", 0), 0))
    recent_hr = safe_int(getattr(rec, "l20pa_hr", 0), 0) or safe_int(getattr(rec, "last5_hr", 0), 0)
    expected = recent_pa * hr_per_pa
    diff = recent_hr - expected
    if diff >= 0.4:
        tag = "hot"
    elif diff <= -0.4:
        tag = "due"
    else:
        tag = "on pace"
    return f"HR/PA {diff:+.1f} vs pace ({tag})"


def last_hr_text(rec: HitterRecord) -> str:
    """Games since his last HR, from the already-computed games_since_last_hr
    field (capped at 60 by games_since_last_hr() in the game-log walk).
    Just the number + unit, no "Last HR:"/"ago" wording.
    """
    g = safe_int(getattr(rec, "games_since_last_hr", 60), 60)
    if g >= 60:
        return "60+g"
    return f"{g}g"


def aligned_emoji(rec: HitterRecord) -> str:
    return " 🧩" if "🧩 Aligned Signals" in getattr(rec, "top_board_tags", []) else ""


def fmt_board_line(rec: HitterRecord, idx: int, label: str = "", sample_txt: str = "") -> str:
    # Condensed to 3 lines (was 7 + a separately-appended Sample line = 8).
    # Same fields, just packed tighter so the board scrolls in ~1/3 the space.
    star = " ⭐" if rec.weak_spot_flag else ""
    label_txt = f" | {label}" if label else ""
    tracked = max(1, rec.recent_350_den)
    sample_suffix = f" | {sample_txt}" if sample_txt else ""
    return (
        f"{idx}. {rec.name}{star}{aligned_emoji(rec)} ({rec.team}){label_txt} — {getattr(rec, 'final_hr_role', getattr(rec, 'beginner_label', 'HR profile'))}{sample_suffix}\n"
        f"HR {rec.hr_score:.1f} | Board {getattr(rec, 'top_board_score_v2', rec.hr_score):.1f} | DC {getattr(rec, 'damage_conversion_score', 0.0):.1f} | {hr_pace_text(rec)} | {last_hr_text(rec)} | {hrw_text(rec)} | {ihr_text(rec)} | spot {rec.lineup_spot}\n"
        f"{pmix_text(rec)} | 350+ {rec.recent_350_num}/{tracked} 375+ {rec.recent_375_num}/{tracked} | FB {round(rec.recent_fb_rate * 100):.0f}% Pull {round(rec.recent_pull_rate * 100):.0f}% | L5 {rec.last5_hits}H/{rec.last5_hr}HR/{rec.last5_xbh}XBH"
    )


def projected_hr_total(rows: List[HitterRecord]) -> Tuple[int, int, str, int, int]:
    """Estimate slate HR total from model profiles, power contact, pitcher weakness, and game count."""
    if not rows:
        return 0, 0, "No Slate", 0, 0

    games = max(1, len({r.game_pk for r in rows}))
    ranked = sorted(rows, key=lambda r: r.hr_score, reverse=True)
    top_profiles = [r for r in ranked if r.hr_score >= 34 and r.season_pa >= 15]
    elite_profiles = [r for r in ranked if r.hr_score >= 42 and r.season_pa >= 15]

    def power_quality(r: HitterRecord) -> float:
        tracked = max(1, r.recent_350_den)
        return (
            0.30 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.24) +
            0.22 * minmax_norm(r.recent_350_num / tracked, 0.08, 0.42) +
            0.18 * minmax_norm(r.recent_375_num / tracked, 0.02, 0.24) +
            0.13 * minmax_norm(r.recent_fb_rate, 0.25, 0.52) +
            0.10 * minmax_norm(r.recent_pull_rate, 0.28, 0.62) +
            0.07 * minmax_norm(r.hrw_score, 35, 75)
        )

    top_power = sorted(rows, key=lambda r: (r.hr_score * 0.65 + 35 * power_quality(r)), reverse=True)[:max(20, games * 4)]
    hitter_component = sum(minmax_norm(r.hr_score, 22, 58) * (0.72 + 0.28 * power_quality(r)) for r in top_power)

    pitcher_weak_games = set()
    for r in rows:
        _es = effective_side(r.bats, r.pitcher_throws)
        side_hr9 = r.pitcher_hr9_vs_lhb if _es == "L" else r.pitcher_hr9_vs_rhb
        side_match = (
            (_es == "L" and r.pitcher_weak_side == "LHB")
            or (_es == "R" and r.pitcher_weak_side == "RHB")
        )
        if r.pitcher_hr9 >= 1.20 or side_hr9 >= 1.20 or r.pitcher_fb_rate >= 0.39 or side_match:
            pitcher_weak_games.add(r.game_pk)

    weakness_component = len(pitcher_weak_games) * 0.42
    park_component = sum(minmax_norm(r.park_factor, 96, 110) for r in top_power[:games * 2]) * 0.18

    base = games * 1.05
    projected_mid = base + 0.34 * hitter_component + weakness_component + park_component
    low = max(0, round(projected_mid - max(3.0, games * 0.28)))
    high = max(low + 1, round(projected_mid + max(3.0, games * 0.28)))

    if projected_mid >= games * 2.0:
        grade = "Strong"
    elif projected_mid >= games * 1.45:
        grade = "Medium"
    else:
        grade = "Light"

    return low, high, grade, len(top_profiles), len(pitcher_weak_games)



def longest_hr_distance_score(rec: HitterRecord) -> float:
    tracked = max(1, rec.recent_350_den)
    split_iso = rec.iso_vs_lhp if rec.pitcher_throws == "L" else rec.iso_vs_rhp

    # Longest HR is a ceiling market. Prioritize true distance markers over regular HR score.
    # Longest HR = distance ceiling. EV is the #1 predictor of raw distance.
    # A ball hit at 110 mph travels 430+ ft. At 100 mph: ~390 ft. At 95 mph: ~360 ft.
    # EV gets the highest single weight here — more than 375+, more than barrel rate.
    batter_ceiling = (
        0.30 * minmax_norm(rec.recent_ev, 85.0, 96.0) +         # EV — #1 predictor of distance
        0.22 * minmax_norm(rec.recent_375_num / tracked, 0.02, 0.30) +  # proven distance contact
        0.14 * minmax_norm(rec.recent_ideal_hr_contact, 0.04, 0.24) +   # ideal launch conditions
        0.10 * minmax_norm(rec.recent_350_num / tracked, 0.08, 0.45) +
        0.09 * minmax_norm(rec.recent_barrel_rate, 0.03, 0.18) +
        0.06 * minmax_norm(rec.recent_hard_hit_rate, 0.28, 0.64) +
        0.05 * minmax_norm(rec.recent_fb_rate, 0.25, 0.54) +
        0.03 * minmax_norm(rec.recent_pull_rate, 0.28, 0.62) +
        0.01 * minmax_norm(max(rec.season_iso, split_iso), 0.08, 0.38)
    )

    if rec.pitcher_statcast_bbe > 0 and rec.pitcher_statcast_status == "ok":
        p_bbe = max(1, rec.pitcher_statcast_bbe)
        pitcher_bomb_env = (
            0.28 * minmax_norm(rec.pitcher_barrel_allowed, 0.03, 0.13) +
            0.23 * minmax_norm(rec.pitcher_hardhit_allowed, 0.30, 0.52) +
            0.20 * minmax_norm(rec.pitcher_ev_allowed, 86.0, 92.5) +
            0.16 * minmax_norm(rec.pitcher_fb_rate, 0.28, 0.48) +
            0.09 * minmax_norm(rec.pitcher_375_allowed / p_bbe, 0.02, 0.18) +
            0.04 * minmax_norm(rec.pitcher_400_allowed / p_bbe, 0.00, 0.07)
        )
    else:
        # Missing pitcher Statcast should not create fake strength. Use a small neutral layer from HR/9/FB only.
        pitcher_bomb_env = (
            0.55 * minmax_norm(rec.pitcher_hr9, 0.70, 2.00) +
            0.45 * minmax_norm(rec.pitcher_fb_rate, 0.28, 0.48)
        ) * 0.55

    environment = (
        0.45 * minmax_norm(rec.park_factor, 95, 113) +
        0.35 * minmax_norm((rec.weather_temp_f or 70), 55, 95) +
        0.20 * minmax_norm((rec.weather_wind_mph or 0), 0, 20)
    )
    score = 100 * (0.66 * batter_ceiling + 0.22 * pitcher_bomb_env + 0.12 * environment)

    # Soft contact guard — don't show guys with no true distance profile
    if rec.season_iso < 0.150 and rec.recent_ideal_hr_contact < 0.08 and (rec.recent_375_num / tracked) < 0.05:
        score *= 0.82

    # Recent 375+ streak bonus — rewrd guys who have been hitting it out recently
    if rec.recent_375_num >= 3:
        score *= 1.08  # multiple 375+ balls recently — real distance ceiling
    elif rec.recent_375_num >= 2:
        score *= 1.04

    # HRW timing alignment — if power window is open AND distance profile is strong, small boost
    if rec.hrw_score >= 70 and (rec.recent_375_num / tracked) >= 0.12:
        score *= 1.04

    return round(min(score, 100.0), 1)

def build_the_four(rows: List[HitterRecord]) -> str:
    """THE FOUR (added 2026-07-14): the slimmed daily output — exactly one
    pick per category: HR, HIT, HRR, CONTACT/ALT. Highest score in each
    category, no player repeats, True Avoids excluded, trusted samples only."""
    if not rows:
        return ""
    used: set = set()

    def take(score_attr: str, label: str, emoji: str):
        pool = [
            r for r in rows
            if r.player_id not in used
            and not getattr(r, "true_avoid_hr", False)
            and r.season_pa >= 40
        ]
        if not pool:
            return None
        r = max(pool, key=lambda x: safe_float(getattr(x, score_attr, 0.0), 0.0))
        used.add(r.player_id)
        return (label, emoji, r, safe_float(getattr(r, score_attr, 0.0), 0.0))

    picks = [
        take("hr_score", "HR", "🧨"),
        take("hit_score", "HIT", "💠"),
        take("hrr_score", "HRR", "🏁"),
        take("contact_score", "CONTACT", "⚾"),
    ]
    lines = ["🎯 THE FOUR " + "─" * 34, ""]
    for p in picks:
        if not p:
            continue
        label, emoji, r, sc = p
        star = " ⭐" if getattr(r, "weak_spot_flag", False) else ""
        head = f" {emoji} {label:<8} {r.name} ({r.team}){star}"
        lines.append(f"{head:<44}{sc:>5.1f}")
        lines.append(f"    {'':<8} L5 {r.last5_hits}H/{r.last5_hr}HR/{r.last5_xbh}XBH · vs {r.pitcher_name}")
        lines.append("")
    lines.append("─" * 46)
    return "\n".join(lines)


def build_longest_hr_targets(rows: List[HitterRecord], n: int = 3) -> str:
    if not rows:
        return ""
    hr_pool = sorted(rows, key=lambda r: r.hr_score, reverse=True)[:max(20, len({r.game_pk for r in rows}) * 3)]
    # Eligibility reworked per request (2026-06-28): this section is
    # specifically about who has ACTUALLY been hitting the ball deep
    # recently -- requires real 375+ or 400+ history directly, rather than
    # letting season ISO or contact-quality proxies substitute for someone
    # who's never actually hit one that far.
    eligible = [
        r for r in hr_pool
        if r.season_pa >= 15
        and (r.recent_375_num >= 1 or safe_int((r.bbe_profile or {}).get("dist_400_plus"), 0) >= 1)
    ]
    if len(eligible) < n:
        eligible = hr_pool
    # Ranking upgraded 2026-07-13: blend the matchup-aware distance score
    # (pitcher bomb environment included) with the new pure batter-distance
    # longest_hr_score (400ft+ rate, avg EV, 350ft+ rate, avg distance).
    def _combined_longest(r: HitterRecord) -> float:
        return 0.55 * longest_hr_distance_score(r) + 0.45 * safe_float(getattr(r, "longest_hr_score", 0.0), 0.0)

    ranked = sorted(eligible, key=_combined_longest, reverse=True)[:n]
    lines = ["💣 LONGEST BOMB WATCH " + "─" * 24, ""]
    for idx, r in enumerate(ranked, start=1):
        tracked = max(1, r.recent_350_den)
        d400 = safe_int((r.bbe_profile or {}).get("dist_400_plus"), 0)
        p_line = (
            f"P allowed: Barrel {round(r.pitcher_barrel_allowed * 100):.1f}% | HH {round(r.pitcher_hardhit_allowed * 100):.0f}% | EV {r.pitcher_ev_allowed:.1f} | 375+ {r.pitcher_375_allowed} | BABIP {r.pitcher_babip:.3f}"
            if r.pitcher_statcast_bbe > 0 and r.pitcher_statcast_status == "ok"
            else f"P Statcast: N/A ({r.pitcher_statcast_status}) | BABIP {r.pitcher_babip:.3f}"
        )
        lines.append(
            # Condensed to 2 lines (was 4) — same fields, tighter packing.
            f"{idx}. {r.name} ({r.team}) — DIST {longest_hr_distance_score(r):.1f} | RAW {safe_float(getattr(r, 'longest_hr_score', 0.0), 0.0):.1f} | HR {r.hr_score:.1f} | {hr_pace_text(r)} | {last_hr_text(r)} | IHR {r.recent_ideal_hr_contact:.2f} | 350+ {r.recent_350_num}/{tracked} 375+ {r.recent_375_num}/{tracked} 400+ {d400}\n"
            f"FB {round(r.recent_fb_rate * 100):.0f}% Pull {round(r.recent_pull_rate * 100):.0f}% | Pitcher {r.pitcher_attack_tag} ATK {r.pitcher_attack_score:.0f} | {p_line}"
        )
        if idx < len(ranked):
            lines.append("")
    return "\n".join(lines)


def _due_bomber_eligible(rows: List[HitterRecord]) -> List[HitterRecord]:
    """Shared filter/sort for DUE BOMBER BOARD and DUE BOMBER PAIRS (added
    2026-07-25 -- restored from an older report format that this file had
    lost; rebuilt from the exact thresholds/labels shown in that older
    output rather than from original source, since no prior version of this
    function exists in this file. Definition, matching the legend text: HR
    drought 5+ games, batter sits in a documented pitcher weak-spot
    (weak_spot_flag / the ⭐ star), and HR score 45+. HRW is deliberately
    NOT used as a filter or sort key -- the whole point of this board is
    surfacing power bats whose HR-timing score is currently burying them
    because of the drought, so gating on HRW would defeat the purpose."""
    eligible = [
        r for r in rows
        if not getattr(r, "true_avoid_hr", False)
        and safe_int(getattr(r, "games_since_last_hr", 0), 0) >= 5
        and getattr(r, "weak_spot_flag", False)
        and safe_float(getattr(r, "hr_score", 0.0), 0.0) >= 45.0
    ]
    return sorted(eligible, key=lambda r: safe_float(r.hr_score, 0.0), reverse=True)


def build_due_bomber_board(rows: List[HitterRecord], n: int = 15) -> str:
    ranked = _due_bomber_eligible(rows)[:n]
    if not ranked:
        return ""
    lines = [
        "💣 DUE BOMBER BOARD " + "─" * 26,
        " drought 5g+ · ⭐ weak-spot · HR score 45+ · HRW ignored on purpose",
    ]
    for idx, r in enumerate(ranked, start=1):
        g = safe_int(getattr(r, "games_since_last_hr", 0), 0)
        lines.append(
            f"{idx}) {r.name} ({r.team}) — {g}g since HR | HR {r.hr_score:.1f} | HRW {r.hrw_score:.0f} | vs {r.pitcher_name} HR/9 {r.pitcher_hr9:.2f}"
        )
    return "\n".join(lines)


def _hot_power_eligible(rows: List[HitterRecord]) -> List[HitterRecord]:
    """Hitters who are BOTH currently hot and elite-rate power.

    Two conditions, matching the tags exactly as build_top_board_tags sets
    them, so the pair reason and the board tags can never disagree:

      * ``L5 HR 2+``      -> last5_hr >= 2   (hot right now)
      * ``HR/PA Elite``   -> hr_per_pa >= 0.060 (elite rate, not a hot streak
                             off a weak baseline)

    Why this replaced DUE BOMBER PAIRS (2026-07-26): the due-bomber rule
    pairs the two longest HR droughts on the slate and deliberately ignores
    HRW, on the theory that a drought is a coiled spring. Reviewed against a
    real slate it did not hold up -- Schwarber (6g) + Contreras (5g) and
    Brandon Lowe (12g) + Buxton (12g) both went 0-for-4 on homers, while
    every hitter who actually went deep that day carried the opposite
    profile. Two of them homered TWICE (Murakami, Wood) and both carried
    this exact tag pair.

    Drought is not evidence of anything. Recent homers plus a career-rate
    that says the recent homers aren't a fluke is at least a signal. The
    DUE BOMBER *board* is untouched -- it stays as a watchlist. It's only
    the automatic pairing that's been swapped.
    """
    eligible = [
        r for r in rows
        if not getattr(r, "true_avoid_hr", False)
        and safe_int(getattr(r, "last5_hr", 0), 0) >= 2
        and safe_float(getattr(r, "hr_per_pa", 0.0), 0.0) >= 0.060
    ]
    # Sorted by HR score so the strongest matchup leads, not the biggest
    # raw power number -- the rate filter has already done that job.
    return sorted(eligible, key=lambda r: safe_float(r.hr_score, 0.0), reverse=True)


def build_hot_power_pairs(rows: List[HitterRecord], n_pairs: int = 2) -> str:
    ranked = _hot_power_eligible(rows)
    if len(ranked) < 2:
        return ""
    lines = ["🔥 HOT POWER PAIRS", "L5 2+ HR and elite HR/PA — hot bats with the rate to back it"]
    idx = 1
    i = 0
    while i + 1 < len(ranked) and idx <= n_pairs:
        a, b = ranked[i], ranked[i + 1]
        hr_a = safe_int(getattr(a, "last5_hr", 0), 0)
        hr_b = safe_int(getattr(b, "last5_hr", 0), 0)
        pa_a = safe_float(getattr(a, "hr_per_pa", 0.0), 0.0)
        pa_b = safe_float(getattr(b, "hr_per_pa", 0.0), 0.0)
        lines.append(f"{idx}) {a.name} ({a.team}) + {b.name} ({b.team})")
        lines.append(
            f"   Reason: L5 {hr_a}HR+{hr_b}HR | HR/PA {pa_a:.3f}+{pa_b:.3f} "
            f"| HR {a.hr_score:.0f}+{b.hr_score:.0f}"
        )
        idx += 1
        i += 2
    return "\n".join(lines)


def build_data_quality_banner(rows: List[HitterRecord]) -> str:
    """Top-of-report alert line so a bad feed day is visible before you scroll
    past 30+ player blocks to find out. Only prints when something's actually
    missing -- a clean run produces no banner at all.
    """
    if not rows:
        return ""
    total = len(rows)
    no_weather = sum(1 for r in rows if str(getattr(r, "weather_source", "none")) == "none")
    no_pitch_mix = sum(1 for r in rows if safe_int(getattr(r, "pitch_mix_sample", 0), 0) < 20)
    no_pitcher_statcast = sum(1 for r in rows if str(getattr(r, "pitcher_statcast_status", "missing")) != "ok")
    unconfirmed_lineups = sum(1 for r in rows if not getattr(r, "lineup_confirmed", True))

    warnings: List[str] = []
    if no_weather:
        warnings.append(f"⚠️ {no_weather}/{total} players missing weather data")
    if no_pitch_mix:
        warnings.append(f"⚠️ {no_pitch_mix}/{total} players have a low/no pitch-mix sample")
    if no_pitcher_statcast:
        warnings.append(f"⚠️ {no_pitcher_statcast}/{total} players' pitcher has no usable Statcast")
    if unconfirmed_lineups:
        warnings.append(f"⚠️ {unconfirmed_lineups}/{total} players on unconfirmed lineups")

    if not warnings:
        return ""
    return section_bar() + "\n" + "\n".join(warnings) + "\n" + section_bar() + "\n"


def build_model_health_report(rows: List[HitterRecord]) -> str:
    """Footer summary: how many players landed in each final role, plus the
    same data-quality counts as the top banner, so you can sanity-check the
    whole slate's model output at a glance without re-reading every block.
    """
    if not rows:
        return ""
    total = len(rows)
    games = len({r.game_pk for r in rows})

    role_counts: Dict[str, int] = {}
    for r in rows:
        role = str(getattr(r, "final_hr_role", "Unknown"))
        role_counts[role] = role_counts.get(role, 0) + 1
    role_lines = [f"  {role}: {count}" for role, count in sorted(role_counts.items(), key=lambda kv: -kv[1])]

    weather_ok = sum(1 for r in rows if str(getattr(r, "weather_source", "none")) != "none")
    pitch_mix_ok = sum(1 for r in rows if safe_int(getattr(r, "pitch_mix_sample", 0), 0) >= 20)
    pitcher_statcast_ok = sum(1 for r in rows if str(getattr(r, "pitcher_statcast_status", "missing")) == "ok")

    lines = [
        "📋 MODEL HEALTH " + "─" * 30,
        "",
        f"Players scored: {total} | Games: {games}",
        "Role breakdown:",
    ]
    lines.extend(role_lines)
    lines.append(
        f"Coverage: weather {weather_ok}/{total} | pitch-mix {pitch_mix_ok}/{total} | pitcher Statcast {pitcher_statcast_ok}/{total}"
    )
    return "\n".join(lines)


def build_slate_prediction(rows: List[HitterRecord]) -> str:
    """PROJECTED section (split out 2026-07-25 from build_top10_alt_board so
    it can be placed at the very top of the report, ahead of THE FOUR, per
    request -- it no longer needs to travel bundled with TOP 30 / ALT LOOKS)."""
    proj_low, proj_high, slate_grade, top_profile_count, weak_pitcher_count = projected_hr_total(rows)
    parts: List[str] = []
    parts.append("📊 SLATE " + "─" * 37 + "\n\n")
    parts.append(f" projected HRs {proj_low}–{proj_high} · power grade {slate_grade}\n")
    parts.append(f" top HR profiles {top_profile_count} · weak pitcher spots {weak_pitcher_count}\n\n")
    return "".join(parts)


def build_top10_alt_board(rows: List[HitterRecord]) -> str:
    global LAST_ALT_USED_IDS, LAST_ALT_TAGS
    LAST_ALT_USED_IDS = set()
    LAST_ALT_TAGS = {}
    MIN_TOP10_PA = 40
    MIN_TOP10_BBE = 10

    def trusted_sample(r: HitterRecord) -> bool:
        return r.season_pa >= MIN_TOP10_PA and r.recent_350_den >= MIN_TOP10_BBE

    def elite_limited_exception(r: HitterRecord) -> bool:
        tracked = max(1, r.recent_350_den)
        return (
            r.hr_score >= 42.0
            and r.season_pa >= 15
            and (
                r.recent_ideal_hr_contact >= 0.14
                or (r.recent_350_num / tracked) >= 0.28
                or (r.recent_375_num / tracked) >= 0.14
                or r.recent_barrel_rate >= 0.10
            )
        )

    def top10_rank_score(r: HitterRecord) -> float:
        # V31: Top Board now uses Damage Conversion + role, not pure raw HR score.
        base = safe_float(getattr(r, "top_board_score_v2", 0.0), safe_float(getattr(r, "hr_score", 0.0), 0.0))
        dc = safe_float(getattr(r, "damage_conversion_score", 0.0), 0.0)
        role_boost = 0.0
        if getattr(r, "true_avoid_hr", False):
            role_boost -= 35.0
        elif getattr(r, "power_watch_flag", False):
            role_boost += 5.0
        elif "HR Bet" in str(getattr(r, "final_hr_role", "")):
            role_boost += 7.0
        elif "HRR" in str(getattr(r, "final_hr_role", "")):
            role_boost -= 3.0
        return 0.68*base + 0.32*dc + role_boost

    # Shadow A/B ranking (2026-07-13): rank the same slate by the
    # power-anchored, no-recency shadow score so grading can compare
    # shadow ranks 1-15 vs 16-30 against the live board's buckets.
    for _srank, _srec in enumerate(
        sorted(rows, key=lambda r: safe_float(getattr(r, "hr_score_shadow", 0.0), 0.0), reverse=True), 1
    ):
        _srec.shadow_board_rank = _srank

    # Longest-HR leaderboard (2026-07-13): rank the slate by raw distance
    # ceiling so the board can surface a "longest bomb tonight" pick.
    for _lrank, _lrec in enumerate(
        sorted(rows, key=lambda r: safe_float(getattr(r, "longest_hr_score", 0.0), 0.0), reverse=True), 1
    ):
        _lrec.longest_hr_rank = _lrank

    ranked_all = sorted(rows, key=top10_rank_score, reverse=True)
    ranked_trusted = [r for r in ranked_all if trusted_sample(r) and not getattr(r, "true_avoid_hr", False)]
    limited_variance = [r for r in ranked_all if not getattr(r, "true_avoid_hr", False) and not trusted_sample(r) and elite_limited_exception(r)]

    top10: List[HitterRecord] = []
    team_exposure: Dict[str, int] = {}
    for rec in ranked_trusted:
        if team_exposure.get(rec.team, 0) >= 3:
            continue
        top10.append(rec)
        team_exposure[rec.team] = team_exposure.get(rec.team, 0) + 1
        if len(top10) >= 30:
            break
    if len(top10) < 30:
        seen = {r.player_id for r in top10}
        for rec in ranked_trusted:
            if rec.player_id in seen:
                continue
            top10.append(rec)
            seen.add(rec.player_id)
            if len(top10) >= 30:
                break

    top10_ids = {r.player_id for r in top10}
    global LAST_TOP30_BOARD
    LAST_TOP30_BOARD = list(top10)
    # ALT LOOKS should be quality names not already shown in the main game-slot categories.
    game_slot_ids = set(game_pick_type_map(rows).keys()) if rows else set()

    def pick_unique(candidates: List[HitterRecord], taken_ids: set[int], n: int) -> List[HitterRecord]:
        out: List[HitterRecord] = []
        local_games: set[int] = set()
        for r in candidates:
            if r.player_id in taken_ids:
                continue
            if r.game_pk in local_games:
                continue
            out.append(r)
            taken_ids.add(r.player_id)
            local_games.add(r.game_pk)
            if len(out) >= n:
                break
        return out

    taken_ids = set(top10_ids) | set(game_slot_ids)
    # due_score() removed from this weight (2026-08-24, see hr_pace_flag) --
    # its continuous weight folds into hot_score, and the honest part of it
    # (the EV gap, matched with a currently HR-prone pitcher) survives as a
    # small flat bonus rather than a re-blended signal.
    hot_due_candidates = sorted(
        [r for r in rows if trusted_sample(r)],
        key=lambda r: (0.90 * hot_score(r) + 0.08 * minmax_norm(r.hr_score, 18, 60)
                       + (0.10 if r.hr_pace_flag else 0.0)),
        reverse=True,
    )
    hot_due = pick_unique(hot_due_candidates, taken_ids, 5)

    matchup_candidates = sorted(
        [r for r in rows if trusted_sample(r)],
        key=lambda r: (matchup_score(r) + (0.10 if r.weak_spot_flag else 0.0) + 0.06 * minmax_norm(r.hr_score, 18, 60)),
        reverse=True,
    )
    matchup = pick_unique(matchup_candidates, taken_ids, 5)

    variance_candidates = sorted(
        limited_variance + [r for r in rows if trusted_sample(r)],
        key=lambda r: (
            0.34 * minmax_norm(r.recent_375_num / max(1, r.recent_350_den), 0.02, 0.30)
            + 0.24 * minmax_norm(r.recent_ideal_hr_contact, 0.00, 0.18)
            + 0.16 * minmax_norm(r.recent_barrel_rate, 0.00, 0.15)
            + 0.12 * minmax_norm(r.season_iso, 0.08, 0.32)
            + 0.08 * minmax_norm(r.last5_hr, 0, 3)
            + (0.10 if not trusted_sample(r) else 0.0)
        ),
        reverse=True,
    )
    variance = pick_unique(variance_candidates, taken_ids, 3)

    alt_rows = (
        [(r, "HOT/DUE") for r in hot_due]
        + [(r, "MATCHUP") for r in matchup]
        + [(r, "VARIANCE ⚠️ LIMITED") for r in variance]
    )
    if len(alt_rows) < 15:
        filler_candidates = sorted(
            [r for r in rows if r.player_id not in taken_ids and not getattr(r, "true_avoid_hr", False)],
            key=lambda r: (0.34 * matchup_score(r) + 0.58 * hot_score(r)
                           + (0.08 if r.hr_pace_flag else 0.0)),
            reverse=True,
        )
        fillers = pick_unique(filler_candidates, taken_ids, 15 - len(alt_rows))
        alt_rows.extend((r, "ALT") for r in fillers)

    # Lock ALT LOOKS out of later pools/pairs/builders so ALT stays truly unique on the sheet.
    LAST_ALT_USED_IDS = {r.player_id for r, _ in alt_rows[:15]}
    # Docket #12: stamp WHICH alt lane each hitter landed in, so ALT LOOKS
    # can finally be graded like every other designation.
    LAST_ALT_TAGS.update({r.player_id: tag.split(" ")[0] for r, tag in alt_rows[:15]})

    parts: List[str] = []
    parts.append("⚾ THE BOARDS " + "─" * 33 + "\n\n")
    parts.append("🏆 TOP 30 " + "─" * 36 + "\n")
    parts.append(f" min {MIN_TOP10_PA} PA · {MIN_TOP10_BBE} BBE · avoid = TRUE avoid only\n\n")
    if top10:
        parts.append("\n\n".join(
            fmt_board_line(r, i, sample_txt=f"PA {r.season_pa} BBE {r.recent_350_den}")
            for i, r in enumerate(top10, start=1)
        ))
    else:
        parts.append("No trusted-sample Top 30 candidates yet. Check ALT LOOKS.\n")

    parts.append("\n\n🔄 ALT LOOKS · small sample / variance " + "─" * 7 + "\n\n")
    parts.append("\n\n".join(
        fmt_board_line(
            r, i, tag,
            sample_txt=f"{'✅' if trusted_sample(r) else '⚠️'} PA {r.season_pa} BBE {r.recent_350_den}",
        )
        for i, (r, tag) in enumerate(alt_rows[:15], start=1)
    ))
    return "".join(parts)

def render_pick_line(label: str, rec: HitterRecord, score_label: str, score_value: float) -> str:
    star = " ⭐" if rec.weak_spot_flag else ""
    tracked = max(1, rec.recent_350_den)
    clean_label = label.replace("—", "").strip()

    # Condensed to 2 lines per pick (was 4-5). Same fields, tighter packing.
    if score_label == "OVR":
        return (
            f"{clean_label} {rec.name}{star}{aligned_emoji(rec)} ({rec.team}) — HR {rec.hr_score:.1f} | OVR {score_value:.1f} | {hr_pace_text(rec)} | {last_hr_text(rec)} | {hrw_text(rec)} | {ihr_text(rec)}\n"
            f"{pmix_text(rec)} | 350+ {rec.recent_350_num}/{tracked} 375+ {rec.recent_375_num}/{tracked} | FB {round(rec.recent_fb_rate * 100):.0f}% Pull {round(rec.recent_pull_rate * 100):.0f}% | L5 {rec.last5_hits}H/{rec.last5_hr}HR/{rec.last5_xbh}XBH R{rec.last5_runs} RBI{rec.last5_rbi}"
        )

    if score_label == "HR":
        return (
            f"{clean_label} {rec.name}{star}{aligned_emoji(rec)} ({rec.team}) — HR {score_value:.1f} | {hr_pace_text(rec)} | {last_hr_text(rec)} | {hrw_text(rec)} | {ihr_text(rec)}\n"
            f"{pmix_text(rec)} | 350+ {rec.recent_350_num}/{tracked} 375+ {rec.recent_375_num}/{tracked} | FB {round(rec.recent_fb_rate * 100):.0f}% Pull {round(rec.recent_pull_rate * 100):.0f}% | L5 {rec.last5_hits}H/{rec.last5_hr}HR/{rec.last5_xbh}XBH"
        )

    if score_label == "HIT":
        return (
            f"{clean_label} {rec.name}{star}{aligned_emoji(rec)} ({rec.team}) — HIT {score_value:.1f} | {hr_pace_text(rec)} | {last_hr_text(rec)} | {ihr_text(rec)}\n"
            f"BA {display_avg(rec.season_avg)} | {split_ba_text(rec)} | K {round(rec.season_k_rate * 100):.0f}% | L5 {rec.last5_hits}H/{rec.last5_hr}HR/{rec.last5_xbh}XBH"
        )

    if score_label == "HRR":
        return (
            f"{clean_label} {rec.name}{star}{aligned_emoji(rec)} ({rec.team}) — HRR {score_value:.1f} | {hr_pace_text(rec)} | {last_hr_text(rec)} | {hrw_text(rec)} | {ihr_text(rec)}\n"
            f"BA {display_avg(rec.season_avg)} | BABIP {rec.babip:.3f} | PreOB {rec.lineup_pre_onbase:.3f} Post {rec.lineup_post_convert:.3f} | L5 {rec.last5_hits}H/{rec.last5_runs}R/{rec.last5_rbi}RBI"
        )

    return (
        f"{clean_label} {rec.name}{star}{aligned_emoji(rec)} ({rec.team}) — XBH Anchor | {hr_pace_text(rec)} | {last_hr_text(rec)} | {hrw_text(rec)} | {ihr_text(rec)}\n"
        f"BA {display_avg(rec.season_avg)} | BABIP {rec.babip:.3f} | L5 {rec.last5_hits}H/{rec.last5_xbh}XBH/{rec.last5_hr}HR"
    )

def pick_top(records: List[HitterRecord], attr: str, n: int, used: Optional[Iterable[int]] = None) -> List[HitterRecord]:
    used_set = set(used or [])
    return sorted([r for r in records if r.player_id not in used_set], key=lambda x: getattr(x, attr), reverse=True)[:n]


def game_team_context(hitters: List[HitterRecord]) -> Tuple[str, str, float]:
    teams = sorted({r.team for r in hitters})
    if len(teams) < 2:
        return (teams[0] if teams else "", teams[0] if teams else "", 0.0)
    team_scores: Dict[str, float] = {}
    for team in teams:
        team_rows = [r for r in hitters if r.team == team]
        top3 = sorted(team_rows, key=lambda r: r.overall_score, reverse=True)[:3]
        if top3:
            team_scores[team] = sum(r.overall_score for r in top3) / len(top3)
        else:
            team_scores[team] = 0.0
    sorted_teams = sorted(team_scores.items(), key=lambda kv: kv[1], reverse=True)
    diff = sorted_teams[0][1] - sorted_teams[1][1]
    return sorted_teams[0][0], sorted_teams[1][0], diff


def choose_pick_with_balance(hitters: List[HitterRecord], attr: str, used_players: set[int], preferred_team: Optional[str] = None, avoid_team: Optional[str] = None, gap: float = 3.5) -> Optional[HitterRecord]:
    candidates = sorted([r for r in hitters if r.player_id not in used_players], key=lambda x: getattr(x, attr), reverse=True)
    if not candidates:
        return None
    top = candidates[0]
    for cand in candidates[1:]:
        if preferred_team and cand.team != preferred_team:
            continue
        if avoid_team and cand.team == avoid_team:
            continue
        if getattr(top, attr) - getattr(cand, attr) <= gap:
            return cand
    if preferred_team:
        team_matches = [c for c in candidates if c.team == preferred_team]
        if team_matches and getattr(top, attr) - getattr(team_matches[0], attr) <= gap:
            return team_matches[0]
    if avoid_team:
        alt = [c for c in candidates if c.team != avoid_team]
        if alt and getattr(top, attr) - getattr(alt[0], attr) <= gap:
            return alt[0]
    return top

# NOTE: a byte-for-byte duplicate pick_top() definition used to sit here.
# Removed -- it silently shadowed the one above with identical code, so
# nothing behavioral changes, but it was dead weight worth cleaning up.


def same_day_homer_score(rec: HitterRecord) -> float:
    tracked = max(1, rec.recent_350_den)
    lineup_bonus = 1.0 if rec.lineup_spot in (1, 2, 3, 4, 5) else 0.55
    return (
        0.24 * minmax_norm(rec.last5_hr, 0, 3) +
        0.18 * minmax_norm(rec.last5_xbh, 0, 4) +
        0.14 * minmax_norm(rec.last10_hr, 0, 5) +
        0.14 * minmax_norm(rec.recent_375_num / tracked, 0.03, 0.24) +
        0.12 * minmax_norm(rec.recent_ideal_hr_contact, 0.05, 0.22) +
        0.10 * minmax_norm(rec.hr_score, 22, 60) +
        0.08 * lineup_bonus
    )


def same_day_pair_score(a: HitterRecord, b: HitterRecord) -> float:
    return 0.50 * same_day_homer_score(a) + 0.50 * same_day_homer_score(b)


def hot_score(rec: HitterRecord) -> float:
    return (
        0.40 * minmax_norm(rec.last5_hr, 0, 3) +
        0.25 * minmax_norm(rec.last5_xbh, 0, 4) +
        0.20 * minmax_norm(rec.last10_hr, 0, 5) +
        0.15 * minmax_norm(rec.last5_hits, 0, 8)
    )


# REMOVED (2026-08-24): due_score() blended six recent-shape/contact-quality
# terms (0.20 recent_350, 0.15 recent_375, 0.15 ideal-contact, 0.12 barrel,
# 0.09 hard-hit, 0.07 season ISO) with a 0.07 "last5_hr==0" term and a 0.15
# expected-value gap term into one continuous number. The archive leak
# investigation (bots/leak_scan.py, 2026-08-23) found that last5_hr in
# graded rows is refreshed AFTER the game, making that 0.07 term -- and by
# extension the whole blend's apparent "predictiveness" on archived data --
# largely an artifact of the archive remembering the outcome, not a real
# signal (measured at 78% contact-quality hotness / 15% honest EV-gap
# dueness / 7% recency once decomposed). Only the EV-gap piece was honest
# on its own terms, so it survives -- as hr_pace_flag/hr_pace_gap above,
# gated to fire only when matched with a pitcher who is CURRENTLY (last 3
# starts) giving up home runs at an elevated rate, not blended back into a
# continuous score. Every call site above now uses rec.hr_pace_flag
# directly (a flat bonus or a boolean tag) instead of this function.


def matchup_score(rec: HitterRecord) -> float:
    split_avg = rec.avg_vs_lhp if rec.pitcher_throws == "L" else rec.avg_vs_rhp
    split_iso = rec.iso_vs_lhp if rec.pitcher_throws == "L" else rec.iso_vs_rhp
    _esm = effective_side(rec.bats, rec.pitcher_throws)
    side_match = 1.0 if ((_esm == "L" and rec.pitcher_weak_side == "LHB") or (_esm == "R" and rec.pitcher_weak_side == "RHB")) else 0.45
    weak_spot = 1.0 if rec.weak_spot_flag else 0.4
    side_hr9 = rec.pitcher_hr9_vs_lhb if _esm == "L" else rec.pitcher_hr9_vs_rhb
    return (
        0.26 * minmax_norm(split_avg, 0.180, 0.360) +
        0.24 * minmax_norm(split_iso, 0.08, 0.38) +
        0.20 * minmax_norm(side_hr9, 0.7, 2.2) +
        0.15 * minmax_norm(rec.pitcher_hr_allowed, 5, 30) +
        0.10 * side_match +
        0.05 * weak_spot
    )


def pair_allowed(a: HitterRecord, b: HitterRecord) -> bool:
    return a.player_id != b.player_id and a.game_pk != b.game_pk


# REMOVED per audit (2026-06-27): best_hr_pair_score and hot_due_pair_score
# were both confirmed dead -- never called anywhere. Both were superseded by
# the dict-based _pb_pair_score() system (lanes: "Best HR Pair", "Hot + Due
# Pair", etc) that actually powers the real pair-builder output today.
# matchup_score and hot_score themselves are NOT dead -- both are still
# genuinely used elsewhere in the file -- only these two pair-combination
# wrappers were orphaned leftovers from an earlier system. (due_score
# itself was removed 2026-08-24 -- see the note above where it used to
# live, and hr_pace_flag/hr_pace_gap earlier in this file.)


# REMOVED per audit (2026-06-27): numerology_pair_score was confirmed dead
# -- never called anywhere in the file, doesn't power any of the 6 real
# pair-builder lanes. It weighted numerology_score (literal date/jersey-
# number digit math, no statistical basis) at 60% combined. Removed along
# with numerology_score itself below.


def pair_text(a: HitterRecord, b: HitterRecord, extra: str = "") -> str:
    tag = f" | {extra}" if extra else ""
    return f"{a.name} ({a.team}) + {b.name} ({b.team}){tag}"


def select_diverse_pairs(scored_pairs, max_pairs=2, max_player_exposure=1):
    selected = []
    exposure = {}
    for a, b, score, label in scored_pairs:
        if exposure.get(a.player_id, 0) >= max_player_exposure:
            continue
        if exposure.get(b.player_id, 0) >= max_player_exposure:
            continue
        selected.append((a, b, score, label))
        exposure[a.player_id] = exposure.get(a.player_id, 0) + 1
        exposure[b.player_id] = exposure.get(b.player_id, 0) + 1
        if len(selected) >= max_pairs:
            break
    return selected



def build_pool(rows: List[HitterRecord], size: int, variant: str, used_players=None):
    used_players = set(used_players or [])
    scored = []
    # Updated pool weights — data shows HRR picks produce most HRs (29% vs 10% for HR picks)
    if variant == "4":
        for r in rows:
            score = (
                0.30 * r.hr_score +      # reduced — HR score alone not predictive enough
                0.25 * r.hrr_score +     # added — HRR guys homer most per graded data
                0.30 * hot_score(r) +    # L5/L7 form — absorbed due_score's old 0.15 weight
                0.10 * matchup_score(r)
                + (0.05 if r.hr_pace_flag else 0.0)  # honest EV gap x hot recent pitcher
            )
            scored.append((r, score))
    else:
        for r in rows:
            score = (
                0.28 * r.hr_score +
                0.24 * r.hrr_score +     # HRR weighted higher for 6-man pools too
                0.28 * hot_score(r) +    # absorbed due_score's old 0.15 weight
                0.10 * matchup_score(r) +
                0.05 * (r.overall_score / 100.0)
                + (0.05 if r.hr_pace_flag else 0.0)  # honest EV gap x hot recent pitcher
            )
            scored.append((r, score))
    scored.sort(key=lambda x: x[1], reverse=True)

    selected = []
    used_games = set()
    for rec, score in scored:
        if rec.player_id in used_players:
            continue
        if rec.game_pk in used_games:
            continue
        selected.append((rec, score))
        used_players.add(rec.player_id)
        used_games.add(rec.game_pk)
        if len(selected) >= size:
            break
    return selected, used_players




def build_game_pick_role_map(rows: List[HitterRecord]) -> Dict[Tuple[int, int], str]:
    by_game: Dict[int, List[HitterRecord]] = {}
    for r in rows:
        by_game.setdefault(r.game_pk, []).append(r)
    role_map: Dict[Tuple[int, int], List[str]] = {}
    for game_pk, hitters in by_game.items():
        used: set[int] = set()
        # MINI-BOT AUDIT (2026-08-08). The 38-day replay was blunt: as a
        # one-pick-per-game ranker, overall_score homered 19.4% while plain
        # season_iso hit 22.1% and iso+last5 form hit 22.9%; and hr_score
        # itself added NOTHING once ISO was known (the 3×3 conditional table
        # was flat across hr_score within every ISO band, with hr70+/thin-ISO
        # picks at 11.4% — below base). TOP and HR now rank on an explicit
        # power score, ISO-led with the model as a tiebreaker-weight. The
        # trap_flag filter is gone from the chain: it graded 15.5% vs 15.3%
        # (708/1344) — pure noise as a selector (kept as a display caution).
        # Replay of this exact ladder: TOP 22.9%, HR 18.4%, combined 20.6%
        # vs 17.9% shipped.
        # SHRINKAGE (2026-08-23): season_iso enters both the rank and the
        # floor as its shrunk-toward-league value (see shrink_to_league --
        # the Veen bug). A 15-PA "ISO .500" reads ~.191 here and no longer
        # outranks a 435-PA .249; the PA >= 15 gate below stays as a hard
        # eligibility cut but the shrinkage now does the real work.
        def _shrunk_iso(h) -> float:
            return shrink_to_league(safe_float(getattr(h, "season_iso", 0.0), 0.0),
                                    safe_float(getattr(h, "season_pa", 0.0), 0.0),
                                    LEAGUE_ISO)

        def _power_rank(h) -> float:
            return (100.0 * _shrunk_iso(h)
                    + 10.0 * safe_float(getattr(h, "last5_hr", 0.0), 0.0)
                    + 0.35 * safe_float(getattr(h, "hr_score", 0.0), 0.0))

        def _power_slot(exclude: set) -> HitterRecord:
            pool = [h for h in hitters if h.player_id not in exclude and getattr(h, "season_pa", 0) >= 15]
            if not pool:
                pool = [h for h in hitters if h.player_id not in exclude] or hitters
            # ISO floor first — the one filter the archive validated
            for cond in (
                lambda h: _shrunk_iso(h) >= 0.180,
                lambda h: True,
            ):
                tier = [h for h in pool if cond(h)]
                if tier:
                    return sorted(tier, key=_power_rank, reverse=True)[0]
            return pool[0]

        top_pick = _power_slot(used) if hitters else None
        used.add(top_pick.player_id)
        role_map.setdefault((game_pk, top_pick.player_id), []).append("TOP")

        # TOP/HR DOUBLE-UP (2026-08-12). Was: HR = _power_slot(used), which
        # excluded TOP's own player and re-ranked the REMAINDER by the same
        # ISO-led power rank TOP uses -- so on any night TOP's pick was also
        # the game's best hr_score bat (measured at 63.9% of games, n=790),
        # HR was handed to a forced second choice instead. Backtested the
        # cost on the 129 games since 2026-07-27 where that forced swap
        # happened: the excluded true-best-hr_score player homered 29.5% of
        # the time vs the actual badge-holder's 13.2% (McNemar p<0.01; see
        # BOT-DATA-REQUESTS.md, "Pick-slot overlap," 2026-08-12). TOP is now
        # the only role allowed to also hold HR: HR ranks by raw hr_score
        # over the same PA/ISO-eligible pool TOP draws from, without
        # excluding TOP's player. If they're genuinely the same hitter,
        # role_map carries "TOP/HR" for that one player_id (the existing
        # "/".join(v) return already supported this; it just never fired
        # before). HIT/HRR/CONTACT below are unchanged -- they still exclude
        # both TOP and HR, same as always.
        # HR = THE COVERAGE SLOT (2026-08-23, Donovan's design: "maybe one
        # can be one and the other be the other"). TOP is the precision slot
        # -- the bat you'd bet, ISO-led rank above. HR is now the game's
        # best REMAINING power bat, required to be a DIFFERENT player than
        # TOP, so the two badges are two shots at the game's homer instead
        # of one man wearing both. This deliberately reverts the 2026-08-12
        # same-man double-up: that change was right when both slots meant
        # precision (forcing a worse second choice was pure loss -- and its
        # 29.5%-vs-13.2% backtest was leak-measured anyway); with HR
        # redefined as coverage, distinctness IS the feature. Measured
        # before this change: TOP and HR were one man in 82% of games, TOP
        # alone covered 32% of HR games, TOP + best distinct second bat
        # covered 58% (leak-ceiling). Target on record: a TOP/HR pick
        # connects in >=50% of games that have a homer.
        # Ranked on hr_score with the shrunk-ISO floor -- same tiering as
        # TOP, different rank key, excluding TOP's player.
        def _hr_slot(exclude: set) -> HitterRecord:
            pool = [h for h in hitters if h.player_id not in exclude and getattr(h, "season_pa", 0) >= 15]
            if not pool:
                pool = [h for h in hitters if h.player_id not in exclude] or list(hitters)
            for cond in (
                lambda h: _shrunk_iso(h) >= 0.180,
                lambda h: True,
            ):
                tier = [h for h in pool if cond(h)]
                if tier:
                    return sorted(tier, key=lambda h: safe_float(getattr(h, "hr_score", 0.0), 0.0), reverse=True)[0]
            return pool[0]

        hr_pick = _hr_slot(used) if hitters else top_pick
        used.add(hr_pick.player_id)
        role_map.setdefault((game_pk, hr_pick.player_id), []).append("HR")

        # ONE pick per category per game (2026-08-06). This took 2 for HIT
        # and 2 for HRR, which put two robots on the same game's board rows
        # and made "the bot's HIT pick" ambiguous everywhere downstream —
        # the five-slot design, the graded results and the site all treat these
        # as single designations. The second-best name still surfaces through
        # Alt Looks / cross-check; it just doesn't wear the pick badge.
        hit_picks = pick_top(hitters, "hit_score", 1, used)
        used.update(h.player_id for h in hit_picks)
        for hp in hit_picks:
            role_map.setdefault((game_pk, hp.player_id), []).append("HIT")

        hrr_picks = pick_top(hitters, "hrr_score", 1, used)
        used.update(h.player_id for h in hrr_picks)
        for hp in hrr_picks:
            role_map.setdefault((game_pk, hp.player_id), []).append("HRR")

        # CONTACT DOUBLE-UP (2026-08-12 pt.2). Same test as TOP/HR, run
        # against CONTACT: on the 98 games since 2026-07-27 where CONTACT's
        # exclusion forced a substitution, the excluded true-best
        # contact_score player hit 2+ total bases 52.0% of the time vs the
        # actual badge-holder's 28.6% (chi2~9.98, p<0.01) -- the largest gap
        # of the four non-TOP roles, bigger than HR's own (BOT-DATA-REQUESTS.md,
        # "Pick-slot overlap," 2026-08-12). Unlike HR, CONTACT never had one
        # clean partner role stealing its best candidate -- it sits at the
        # end of the chain, excluded by whichever of TOP/HR/HIT/HRR got there
        # first. So instead of excluding one specific role, CONTACT now
        # excludes no one: it ranks by raw contact_score over every hitter in
        # the game, same scoring as always, just without the used-set filter
        # (pick_top's own PA-less, unfiltered pool -- CONTACT never had a PA
        # gate, so none is added here either). If the true best already
        # holds another role, role_map's "/".join(v) carries the combined
        # tag, same mechanism as TOP/HR. HIT and HRR are untouched -- their
        # backtest gap didn't clear significance (+6.2pp, +6.6pp, both n.s.).
        anchor = pick_top(hitters, "contact_score", 1)[0]
        role_map.setdefault((game_pk, anchor.player_id), []).append("CONTACT")

        # WATCH TIER (2026-08-23). Donovan's floor: "90% or close -- any
        # designated pick homers, per game, at minimum." Measured leak-free
        # (3 clean nights, 26 HR games): six picks ranked by the current
        # score cover ~69% of HR games and even nine reach 85%, because 12
        # of 47 homerers sat ranked 10th or worse pre-game. Six badges
        # cannot reach 90% with any ranker -- the covered tier has to
        # widen. WATCH is the next THREE bats by hr_score not already
        # holding TOP or HR (a HIT/HRR/CONTACT holder may add WATCH -- the
        # "/".join below already carries combined tags). It is a coverage
        # marker, not a pick: it changes no slot, no grading category, and
        # no score; it exists so the coverage report can count it and the
        # site can show it. Six badges + WATCH sits near the K=9 line
        # (~85% today), rising as ordering improves.
        _watch_excl = {top_pick.player_id, hr_pick.player_id}
        _watch_pool = sorted(
            [h for h in hitters if h.player_id not in _watch_excl],
            key=lambda h: safe_float(getattr(h, "hr_score", 0.0), 0.0),
            reverse=True,
        )
        for w in _watch_pool[:3]:
            role_map.setdefault((game_pk, w.player_id), []).append("WATCH")

    return {k: "/".join(v) for k, v in role_map.items()}


# ── ⛔ vs 🥇 — ONE RUN, ONE PIECE OF ADVICE (2026-08-16) ─────────────────────
#
# Donovan asked this twice: "if the top pick is homerun why give someone a skip
# hr if that is the bench mark." TOP and HR are graded on a HOME RUN, so
# `best_bet_type = "Avoid for HR"` on a man this same run just designated TOP is
# the bot issuing two opposite instructions about one hitter.
#
# WHY THE TWO FIELDS CAN DISAGREE AT ALL — the ordering, traced 2026-08-16.
# `best_bet_type` is a PER-HITTER field. It is written inside
# apply_model_v2_layers() (`_hr2_best_bet_and_label`, and then overwritten by
# apply_decision_engine_v31() a few hundred lines later in the same function),
# which runs from score_hitter() while ONE game's rows are being built. The
# designation is a SLATE-level, one-pick-per-game ranking: build_game_pick_role_map()
# above cannot run until every game's rows exist, which is a whole pipeline stage
# later. So best_bet_type is not "wrong early" — at the moment it is computed the
# information it contradicts does not exist yet. It is physically impossible for
# the hitter-level label to know it is about to be designated, and the earliest
# honest place to reconcile the two is right here, the instant the role map lands.
# That is why this is a function called once at that point and not a fix pushed
# back into the scorer: pushing it back would require running the slate ranker
# per hitter, which is the same ranker, run 266 times, for the same answer.
#
# THE FIX IS THE LABEL, NOT THE SELECTOR, and that is a measured decision rather
# than a taste one. Over 52 graded nights, TOP picks carrying this flag homered
# 18/55 (32.7%) against 124/631 (19.7%) for the ones without it — two-proportion
# z = 2.30, significant at 95%. Across every tracked hitter the flag barely
# separates at all (133/951 = 14.0% against a 245/1584 = 15.5% no-tag baseline).
# It keys on HR-SHAPE gates, not on power; when an elite power bat trips one, the
# tag fires and the power is still there, and the ISO-led TOP selector is finding
# exactly those bats. Filtering them out — the intuitive fix — would have deleted
# the best-performing TOP picks in the archive. So nobody is dropped, no score
# moves; the contradictory ADVICE is what moves.
#
# `true_avoid_hr`, `avoid_hr_reasons`, `final_hr_role` and `beginner_label` are
# all left untouched, so the gate stays fully auditable downstream and the site
# can keep showing it with its record attached. Only the recommendation changes.
def reconcile_best_bet_with_designation(rows_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stop `best_bet_type` saying "avoid" about a hitter this same run designated
    TOP or HR. Mutates and returns rows_payload.

    Call this ONCE, immediately after `game_pick_role` has been stamped onto the
    payload rows (after the pick lock has had its say — the frozen first-pitch map
    is the designation that counts, so reconciling against the fresh map would
    label the wrong player once a game is underway).

    Invariant established, and asserted in tests/test_hr_gate_label.py:

        a row whose game_pick_role contains TOP or HR never publishes a
        best_bet_type beginning with "avoid" — and every row that was changed
        carries the original verdict in best_bet_type_raw plus hr_gate_flagged.

    Nothing is deleted: best_bet_type_raw is the untouched original string, and
    `true_avoid_hr` / `avoid_hr_reasons` still carry the gate's full reasoning on
    the same row. hr_gate_flagged / best_bet_type_raw are written ONLY on rows
    that actually changed, which is the shape the site's hrGateVerdict() already
    reads (absent == not flagged); rows that never contradicted are left alone so
    "no key" keeps meaning "nothing to explain".
    """
    for row in rows_payload or []:
        roles = {x.strip().upper() for x in str(row.get('game_pick_role') or '').split('/') if x.strip()}
        if not (roles & {'TOP', 'HR'}):
            continue
        if not str(row.get('best_bet_type') or '').strip().lower().startswith('avoid'):
            continue
        row['best_bet_type_raw'] = row.get('best_bet_type')
        # "HR" for TOP-only rows too, NOT the role name (fixed 2026-08-16, same
        # session that shipped the first cut of this repair). The first version
        # wrote `'HR' if 'HR' in roles else 'TOP'`, which put the string "TOP"
        # into best_bet_type — and "TOP" is not a best_bet_type. The field's
        # entire value space is what _hr2_best_bet_and_label() and
        # apply_decision_engine_v31() can return: HR / HR or HRR / HRR + HR
        # Sprinkle / HRR / XBH / HRR / Hits / Avoid for HR / Avoid HR. "TOP" is a
        # game_pick_role value that had leaked one field over. That matters
        # beyond tidiness: live_results_tracker.SLOT_FIELDS archives
        # best_bet_type, and the archive is sliced BY that string (HR 153/665 =
        # 23.0%, no tag 245/1584 = 15.5%, HRR + HR Sprinkle 125/839 = 14.9%,
        # Avoid for HR 133/951 = 14.0%, HR or HRR 53/399 = 13.3%). Writing a role
        # name in there invents a phantom category in every future cut of that
        # table. "HR" is also the honest answer on the merits: TOP and HR are
        # both graded on a home run — that is the whole premise of this repair —
        # and these particular bats homered 18/55 = 32.7%, the best rate in the
        # table above, so "HR" understates them rather than overstating them.
        row['best_bet_type'] = 'HR'
        row['hr_gate_flagged'] = True
    return rows_payload


def _pool_eligibility(rows: List[HitterRecord]) -> Tuple[List[HitterRecord], List[HitterRecord], List[HitterRecord]]:
    ranked = sorted(rows, key=lambda r: r.hr_score, reverse=True)
    core = ranked[:15]
    mid = [r for r in ranked if 30 <= r.hr_score <= 40]
    wtf = [
        r for r in ranked
        if 25 <= r.hr_score <= 35
        and (
            (r.recent_350_num / max(1, r.recent_350_den) >= 0.12)
            or r.recent_ideal_hr_contact >= 0.10
            or r.season_iso >= 0.180
            or r.recent_barrel_rate >= 0.06
        )
    ]
    return core, mid, wtf


def _pick_unique(candidates: List[HitterRecord], used_ids: set[int], used_games: set[int]) -> Optional[HitterRecord]:
    for r in candidates:
        if r.player_id in used_ids:
            continue
        if r.game_pk in used_games:
            continue
        return r
    for r in candidates:
        if r.player_id not in used_ids:
            return r
    return None


def build_signature_pools(rows: List[HitterRecord], size: int = 4) -> Dict[str, List[Tuple[HitterRecord, float]]]:
    ranked = sorted(rows, key=lambda r: r.hr_score, reverse=True)
    core, mid, wtf = _pool_eligibility(rows)

    def entry(rec: Optional[HitterRecord]) -> Optional[Tuple[HitterRecord, float]]:
        if rec is None:
            return None
        score = (
            0.45 * rec.hr_score +
            0.35 * hot_score(rec) +    # absorbed due_score's old 0.20 weight
            0.15 * matchup_score(rec)
            + (0.05 if rec.hr_pace_flag else 0.0)  # honest EV gap x hot recent pitcher
        )
        return (rec, score)

    pools: Dict[str, List[Tuple[HitterRecord, float]]] = {}

    if size == 4:
        a = [(r, r.hr_score) for r in ranked[:4]]
        pools["A"] = a

        used_ids = {r.player_id for r in ranked[:3]}
        used_games = {r.game_pk for r in ranked[:3]}
        b_last = _pick_unique(wtf, used_ids, used_games) or _pick_unique(ranked[3:], used_ids, used_games)
        pools["B"] = [(r, r.hr_score) for r in ranked[:3]] + ([entry(b_last)] if b_last else [])

        used_ids = {r.player_id for r in ranked[:3]}
        used_games = {r.game_pk for r in ranked[:3]}
        c_last = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[3:], used_ids, used_games)
        pools["C"] = [(r, r.hr_score) for r in ranked[:3]] + ([entry(c_last)] if c_last else [])

        used_ids = {r.player_id for r in ranked[:2]}
        used_games = {r.game_pk for r in ranked[:2]}
        d_mid = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[2:], used_ids, used_games)
        if d_mid:
            used_ids.add(d_mid.player_id); used_games.add(d_mid.game_pk)
        d_wtf = _pick_unique(wtf, used_ids, used_games) or _pick_unique(ranked[2:], used_ids, used_games)
        d = [(r, r.hr_score) for r in ranked[:2]]
        if d_mid:
            d.append(entry(d_mid))
        if d_wtf:
            d.append(entry(d_wtf))
        pools["D"] = d[:4]
        return pools

    # 6-man A/B/C/D
    pools["A"] = [(r, r.hr_score) for r in ranked[:6]]

    used_ids = {r.player_id for r in ranked[:4]}
    used_games = {r.game_pk for r in ranked[:4]}
    b_mid = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[4:], used_ids, used_games)
    if b_mid:
        used_ids.add(b_mid.player_id); used_games.add(b_mid.game_pk)
    b_wtf = _pick_unique(wtf, used_ids, used_games) or _pick_unique(ranked[4:], used_ids, used_games)
    b = [(r, r.hr_score) for r in ranked[:4]]
    if b_mid:
        b.append(entry(b_mid))
    if b_wtf:
        b.append(entry(b_wtf))
    pools["B"] = b[:6]

    used_ids = {r.player_id for r in ranked[:3]}
    used_games = {r.game_pk for r in ranked[:3]}
    c_mid1 = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[3:], used_ids, used_games)
    if c_mid1:
        used_ids.add(c_mid1.player_id); used_games.add(c_mid1.game_pk)
    c_mid2 = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[3:], used_ids, used_games)
    if c_mid2:
        used_ids.add(c_mid2.player_id); used_games.add(c_mid2.game_pk)
    c_wtf = _pick_unique(wtf, used_ids, used_games) or _pick_unique(ranked[3:], used_ids, used_games)
    c = [(r, r.hr_score) for r in ranked[:3]]
    for rec in (c_mid1, c_mid2, c_wtf):
        if rec:
            c.append(entry(rec))
    pools["C"] = c[:6]

    used_ids = {r.player_id for r in ranked[:2]}
    used_games = {r.game_pk for r in ranked[:2]}
    d_mid1 = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[2:], used_ids, used_games)
    if d_mid1:
        used_ids.add(d_mid1.player_id); used_games.add(d_mid1.game_pk)
    d_mid2 = _pick_unique(mid, used_ids, used_games) or _pick_unique(ranked[2:], used_ids, used_games)
    if d_mid2:
        used_ids.add(d_mid2.player_id); used_games.add(d_mid2.game_pk)
    d_wtf1 = _pick_unique(wtf, used_ids, used_games) or _pick_unique(ranked[2:], used_ids, used_games)
    if d_wtf1:
        used_ids.add(d_wtf1.player_id); used_games.add(d_wtf1.game_pk)
    d_wtf2 = _pick_unique(wtf, used_ids, used_games) or _pick_unique(ranked[2:], used_ids, used_games)
    d = [(r, r.hr_score) for r in ranked[:2]]
    for rec in (d_mid1, d_mid2, d_wtf1, d_wtf2):
        if rec:
            d.append(entry(rec))
    pools["D"] = d[:6]
    return pools



def build_signature_pairs(rows: List[HitterRecord]) -> Dict[str, Tuple[Optional[HitterRecord], Optional[HitterRecord]]]:
    ranked = sorted(rows, key=lambda r: r.hr_score, reverse=True)
    core, mid, wtf = _pool_eligibility(rows)
    if len(ranked) < 2:
        return {"A": (ranked[0], None)} if ranked else {"A": (None, None)}

    first = ranked[0]
    second = next((r for r in ranked[1:] if pair_allowed(first, r)), ranked[1] if len(ranked) > 1 else None)
    third = next((r for r in ranked[2:] if pair_allowed(first, r)), second)
    mid_pick = next((r for r in mid if pair_allowed(first, r) and r.player_id != first.player_id), third)
    wtf_pick = next((r for r in wtf if pair_allowed(first, r) and r.player_id != first.player_id), mid_pick)

    return {
        "A": (first, second),
        "B": (first, third),
        "C": (first, mid_pick),
        "D": (first, wtf_pick),
    }



def _format_pool_cell(item: Optional[Tuple[HitterRecord, float]], role_map: Dict[Tuple[int, int], str], col_width: int = 40) -> str:
    if not item:
        return "".ljust(col_width)
    rec, score = item
    role = role_map.get((rec.game_pk, rec.player_id), "")
    role_txt = f" | {role}" if role else ""
    txt = f"- {rec.name} ({rec.team}) | {score:.1f}{role_txt}"
    return txt[:col_width].ljust(col_width)


def _format_horizontal_pool_row(items: List[Optional[Tuple[HitterRecord, float]]], role_map: Dict[Tuple[int, int], str], col_width: int = 40) -> str:
    return "  ".join(_format_pool_cell(item, role_map, col_width) for item in items).rstrip()




def dedupe_players(players: List[HitterRecord]) -> List[HitterRecord]:
    """Deduplicate players — for double headers keep the better matchup game.
    Better matchup = weaker pitcher (higher pitcher_hr9 + lower ERA + higher FB rate).
    """
    best: Dict[int, HitterRecord] = {}
    for p in players:
        if p.player_id not in best:
            best[p.player_id] = p
        else:
            # Keep whichever game has the better (weaker) pitcher matchup
            current = best[p.player_id]
            current_score = (current.pitcher_hr9 or 0) * 0.5 + (1 / max(0.01, current.pitcher_era or 4.5)) * (-0.3) + (current.pitcher_fb_rate or 0.34) * 0.2
            new_score = (p.pitcher_hr9 or 0) * 0.5 + (1 / max(0.01, p.pitcher_era or 4.5)) * (-0.3) + (p.pitcher_fb_rate or 0.34) * 0.2
            if new_score > current_score:
                best[p.player_id] = p
    # Preserve original ordering based on first seen position
    seen_order = {}
    for i, p in enumerate(players):
        if p.player_id not in seen_order:
            seen_order[p.player_id] = i
    return sorted(best.values(), key=lambda p: seen_order.get(p.player_id, 999))




def _recent_rate(rec: HitterRecord, num: int) -> float:
    return num / max(1, rec.recent_350_den)


def _season_hr_game_probability(rec: HitterRecord) -> float:
    """Transparent HR chance estimate derived only from the hitter's season.

    This is intentionally not called a calibrated model probability. It turns
    HR/PA into a one-game chance with a conservative lineup-slot PA estimate,
    which gives pairs and pools a common, interpretable objective while the
    prediction log accumulates enough locked outcomes for true calibration.
    """
    pa = max(0, safe_int(getattr(rec, "season_pa", 0), 0))
    hrs = max(0, safe_int(getattr(rec, "season_hr", 0), 0))
    rate = safe_float(getattr(rec, "hr_per_pa", 0.0), 0.0)
    if rate <= 0.0 and pa > 0:
        rate = hrs / pa
    # Small-sample shrinkage toward an 11% per-game league-ish baseline. The
    # 120-PA prior stops a 2-HR cup of coffee from outranking established bats.
    raw_game = 1.0 - (1.0 - max(0.0, min(rate, 0.15))) ** 4.15
    weight = pa / (pa + 120.0)
    return round(max(0.025, min(0.40, weight * raw_game + (1.0 - weight) * 0.11)), 4)


def _ticket_probability_at_least(players: List[HitterRecord], need: int) -> float:
    """Independent-leg estimate for a pair/pool grade ladder."""
    probs = [_season_hr_game_probability(r) for r in players]
    if not probs or need <= 0:
        return 1.0 if need <= 0 else 0.0
    dist = [1.0] + [0.0] * len(probs)
    for prob in probs:
        nxt = [0.0] * len(dist)
        for hits, mass in enumerate(dist):
            nxt[hits] += mass * (1.0 - prob)
            if hits + 1 < len(nxt):
                nxt[hits + 1] += mass * prob
        dist = nxt
    return round(sum(dist[need:]), 4)


def _pool_leg_score(rec: HitterRecord) -> float:
    """Rank a leg for a 2+ pool: season chance first, audited context second."""
    probability_rank = minmax_norm(_season_hr_game_probability(rec), 0.06, 0.25)
    context_rank = minmax_norm(_hr_alignment_score(rec), 35.0, 85.0)
    return round(100.0 * (0.65 * probability_rank + 0.35 * context_rank), 3)


def _split_iso_for(rec: HitterRecord) -> float:
    return rec.iso_vs_lhp if rec.pitcher_throws == "L" else rec.iso_vs_rhp


def _power_signal(rec: HitterRecord) -> bool:
    """Power gate so HRR does not turn into singles-only pool picks."""
    return (
        rec.hr_score >= 28.0
        or rec.season_iso >= 0.170
        or _split_iso_for(rec) >= 0.170
        or _recent_rate(rec, rec.recent_350_num) >= 0.16
        or _recent_rate(rec, rec.recent_375_num) >= 0.07
        or rec.recent_ideal_hr_contact >= 0.095
        or rec.recent_barrel_rate >= 0.065
        or rec.last5_xbh >= 2
        or rec.last7_hr >= 1
    )


def _hr_alignment_score(rec: HitterRecord) -> float:
    """Pool/pair leg ranker. MINI-BOT AUDIT (2026-08-08): its heaviest input
    was its weakest signal — hr_score AUC 0.540 vs season_iso 0.620 and
    last5_hr 0.599; the pools it built ran 13.8% per leg vs 23.0% for a
    naive top-4-by-score sort. Rebuilt ISO/form-led with the model scores
    demoted to tiebreakers."""
    score = (
        0.40 * (100.0 * minmax_norm(safe_float(getattr(rec, "season_iso", 0.0), 0.0), 0.10, 0.30))
        + 0.20 * (100.0 * minmax_norm(safe_float(getattr(rec, "last5_hr", 0.0), 0.0), 0.0, 3.0))
        + 0.15 * rec.hr_score + 0.15 * rec.hrw_score + 0.10 * rec.hrr_score
    )
    if rec.hrw_score < 45:
        score *= 0.90
    if rec.hrw_score < 35:
        score *= 0.82
    if rec.hrr_score < 45:
        score *= 0.93
    if not _power_signal(rec):
        score *= 0.86
    # Small push for true overlap bats.
    if rec.hr_score >= 30 and rec.hrr_score >= 55 and rec.hrw_score >= 50:
        score += 4.0
    if rec.hr_score >= 34 and rec.hrw_score >= 55:
        score += 2.0
    # due_score removed here (2026-08-24) -- see hr_pace_flag above. This
    # ranking function drives top_pool_candidates and most bucket sorting
    # in System 2's pairs/pools, and dueness is directly timing-relevant to
    # "is this HR confirmed/due now," which is this function's whole
    # purpose, so it keeps a real bonus -- just a flat one, not a re-blended
    # continuous score built on a contaminated signal.
    score += 4.0 if rec.hr_pace_flag else 0.0
    score += minmax_norm(rec.consistency_score, 25, 65) * 4.0
    return round(score, 3)


def _hrr_power_score(rec: HitterRecord) -> float:
    """Ranks HRR bats for HR pools: form first, but with power/timing required."""
    score = 0.42 * rec.hrr_score + 0.30 * rec.hrw_score + 0.28 * rec.hr_score
    if _power_signal(rec):
        score += 3.0
    if rec.hr_score >= 30:
        score += 2.0
    if rec.hrw_score >= 60:
        score += 2.0
    if rec.hrr_score >= 60 and rec.hr_score < 20:
        score -= 5.0
    # due_score removed here (2026-08-24) -- see hr_pace_flag above.
    # consistency_score stays weighted higher since this function is
    # specifically about trustworthy, reliable production bats (its whole
    # purpose is catching HRR-high/HR-low mismatches above) --
    # consistency_score's balance/confidence read fits that intent
    # directly; dueness gets a smaller flat bonus here than in
    # _hr_alignment_score for the same reason.
    score += minmax_norm(rec.consistency_score, 25, 65) * 6.0
    score += 2.5 if rec.hr_pace_flag else 0.0
    return round(score, 3)


def top_pool_candidates(rows: List[HitterRecord], limit: int = 62) -> List[HitterRecord]:
    # Do not use raw HR score only here. Results showed HRR/HRW are timing confirmation,
    # so pools/pairs should start from aligned HR candidates.
    return sorted(dedupe_players(rows), key=_pool_leg_score, reverse=True)[:limit]


def classify_pool_buckets(rows: List[HitterRecord]) -> Dict[str, List[HitterRecord]]:
    ranked = dedupe_players(top_pool_candidates(rows, 62))

    hot_hrr = sorted([
        r for r in ranked
        if r.hrr_score >= 50.0 and r.hrw_score >= 45.0 and _power_signal(r)
    ], key=_pool_leg_score, reverse=True)

    hybrid = sorted([
        r for r in ranked
        if r.hr_score >= 28.0 and r.hrr_score >= 48.0 and r.hrw_score >= 45.0 and _power_signal(r)
    ], key=_pool_leg_score, reverse=True)

    core = sorted([
        r for r in ranked
        if r.hr_score >= 32.0 and _power_signal(r) and (r.hrw_score >= 42.0 or r.hrr_score >= 55.0)
    ], key=_pool_leg_score, reverse=True)
    if len(core) < 10:
        # fallback keeps old HR ceiling alive, but still sorted by alignment
        extra = [r for r in ranked if r not in core and r.hr_score >= 30.0 and _power_signal(r)]
        core = dedupe_players(core + sorted(extra, key=_pool_leg_score, reverse=True))[:14]

    mid = sorted([
        r for r in ranked
        if r.player_id not in {p.player_id for p in core}
        and r.player_id not in {p.player_id for p in hot_hrr[:10]}
        and (r.hr_score >= 24.0 or r.hrw_score >= 50.0 or r.hrr_score >= 54.0)
        and _power_signal(r)
    ], key=_pool_leg_score, reverse=True)

    wtf = sorted([
        r for r in ranked
        if r.player_id not in {p.player_id for p in core}
        and r.player_id not in {p.player_id for p in mid}
        and (
            20.0 <= r.hr_score <= 36.0
            or _recent_rate(r, r.recent_350_num) >= 0.14
            or r.recent_ideal_hr_contact >= 0.09
            or r.recent_barrel_rate >= 0.06
            or r.season_iso >= 0.165
            or r.last5_hr >= 1
            or r.last5_xbh >= 2
        )
    ], key=_pool_leg_score, reverse=True)

    return {
        "all": ranked,
        "hrr": dedupe_players(hot_hrr),
        "hybrid": dedupe_players(hybrid),
        "core": dedupe_players(core),
        "mid": dedupe_players(mid),
        "wtf": dedupe_players(wtf),
    }


def game_pick_type_map(rows: List[HitterRecord]) -> Dict[int, str]:
    by_game: Dict[int, List[HitterRecord]] = {}
    for r in rows:
        by_game.setdefault(r.game_pk, []).append(r)

    tag_map: Dict[int, str] = {}
    for _, hitters in by_game.items():
        used: set[int] = set()
        top_pick = pick_top(hitters, "overall_score", 1)[0]
        used.add(top_pick.player_id)
        # 2026-08-12, on request ("distinguishable, different emojis and
        # wording"): this pick_type tag set used to be near-identical to the
        # site's game_pick_role badges (🏆/🧨/🏁/💠/⚾) -- same categories,
        # almost the same icons, easy to mistake one list for the other even
        # though they can disagree (pick_type has no TOP/HR double-up, picks
        # 2 for HIT/HRR where game_pick_role picks 1+). New set shares zero
        # icons with game_pick_role OR final_hr_role (best_bet_type).
        tag_map[top_pick.player_id] = "🥇TOP"

        # Docket #14 + #17 (2026-08-05). Measured on 1,377 graded HR-type
        # picks: sub-.18-ISO picks homered 11.5% vs 19.5% above; overall_score
        # out-predicts hr_score on homers (+7.3 vs +4.7 quartile spread); and
        # 24 hitters on one recent slate were simultaneously the HR pick and
        # trap-flagged — a self-contradiction. So the HR slot now ranks by
        # overall_score behind an ISO floor, skipping trapped bats while any
        # alternative exists. The fallback chain guarantees a pick in every
        # game however thin the slate.
        def _hr_slot() -> HitterRecord:
            pool = [h for h in hitters if h.player_id not in used and getattr(h, "season_pa", 0) >= 15]
            if not pool:
                pool = [h for h in hitters if h.player_id not in used] or [top_pick]
            for cond in (
                lambda h: getattr(h, "season_iso", 0.0) >= 0.180 and not getattr(h, "trap_flag", False),
                lambda h: getattr(h, "season_iso", 0.0) >= 0.180,
                lambda h: not getattr(h, "trap_flag", False),
                lambda h: True,
            ):
                tier = [h for h in pool if cond(h)]
                if tier:
                    return sorted(tier, key=lambda h: getattr(h, "overall_score", 0.0), reverse=True)[0]
            return top_pick
        hr_pick = _hr_slot() if len(hitters) > 1 else top_pick
        used.add(hr_pick.player_id)
        tag_map[hr_pick.player_id] = "🎆HR"

        hit_picks = pick_top(hitters, "hit_score", 2, used)
        used.update(h.player_id for h in hit_picks)
        for hp in hit_picks:
            tag_map.setdefault(hp.player_id, "➕HIT")

        hrr_picks = pick_top(hitters, "hrr_score", 2, used)
        used.update(h.player_id for h in hrr_picks)
        for hp in hrr_picks:
            tag_map.setdefault(hp.player_id, "🔺HRR")

        anchor = pick_top(hitters, "contact_score", 1, used)[0] if len(hitters) > len(used) else pick_top(hitters, "contact_score", 1)[0]
        tag_map.setdefault(anchor.player_id, "🟢CON")
    return tag_map


def player_max_exposure(rec: HitterRecord, buckets: Dict[str, List[HitterRecord]]) -> int:
    # Clean-output rule: one player should not appear across multiple pair/pool plays.
    return 1


def can_use_player(
    rec: HitterRecord,
    buckets: Dict[str, List[HitterRecord]],
    global_exposure: Dict[int, int],
    local_ids: set[int],
    local_games: set[int],
    blocked_ids: Optional[set[int]] = None,
    blocked_top_ids: Optional[set[int]] = None,
) -> bool:
    if rec.player_id in local_ids:
        return False
    if rec.game_pk in local_games:
        return False
    if blocked_ids and rec.player_id in blocked_ids:
        return False
    if blocked_top_ids and rec.player_id in blocked_top_ids:
        return False
    return global_exposure.get(rec.player_id, 0) < player_max_exposure(rec, buckets)


def select_from_bucket(
    bucket: List[HitterRecord],
    count: int,
    buckets: Dict[str, List[HitterRecord]],
    global_exposure: Dict[int, int],
    local_ids: set[int],
    local_games: set[int],
    blocked_ids: Optional[set[int]] = None,
    blocked_top_ids: Optional[set[int]] = None,
) -> List[HitterRecord]:
    out: List[HitterRecord] = []
    for r in bucket:
        if not can_use_player(r, buckets, global_exposure, local_ids, local_games, blocked_ids, blocked_top_ids):
            continue
        out.append(r)
        local_ids.add(r.player_id)
        local_games.add(r.game_pk)
        global_exposure[r.player_id] = global_exposure.get(r.player_id, 0) + 1
        if len(out) >= count:
            break
    return out


def fallback_fill(
    candidate_rows: List[HitterRecord],
    need: int,
    buckets: Dict[str, List[HitterRecord]],
    global_exposure: Dict[int, int],
    local_ids: set[int],
    local_games: set[int],
    blocked_ids: Optional[set[int]] = None,
    blocked_top_ids: Optional[set[int]] = None,
) -> List[HitterRecord]:
    ranked = sorted(candidate_rows, key=_pool_leg_score, reverse=True)
    out: List[HitterRecord] = []
    for r in ranked:
        if not can_use_player(r, buckets, global_exposure, local_ids, local_games, blocked_ids, blocked_top_ids):
            continue
        out.append(r)
        local_ids.add(r.player_id)
        local_games.add(r.game_pk)
        global_exposure[r.player_id] = global_exposure.get(r.player_id, 0) + 1
        if len(out) >= need:
            break
    return out


def build_structured_pool(
    candidate_rows: List[HitterRecord],
    recipe: Dict[str, int],
    buckets: Dict[str, List[HitterRecord]],
    global_exposure: Dict[int, int],
    blocked_ids: Optional[set[int]] = None,
    blocked_top_ids: Optional[set[int]] = None,
) -> List[HitterRecord]:
    local_ids: set[int] = set()
    local_games: set[int] = set()
    out: List[HitterRecord] = []
    # New order leans HRR/timing first, then HR ceiling.
    for bucket_name in ("hrr", "hybrid", "core", "mid", "wtf"):
        want = recipe.get(bucket_name, 0)
        if want <= 0:
            continue
        out.extend(
            select_from_bucket(
                buckets.get(bucket_name, []), want, buckets, global_exposure, local_ids, local_games, blocked_ids, blocked_top_ids
            )
        )
    total_need = sum(recipe.values())
    if len(out) < total_need:
        out.extend(
            fallback_fill(
                candidate_rows, total_need - len(out), buckets, global_exposure, local_ids, local_games, blocked_ids, blocked_top_ids
            )
        )
    return out



LAST_HR_SECTION_USED_IDS: set[int] = set()
LAST_ALT_USED_IDS: set[int] = set()
LAST_ALT_TAGS: Dict[int, str] = {}
LAST_TOP30_BOARD: List[HitterRecord] = []

def clean_pick_tag(tag: str) -> str:
    """Convert internal category tags to emoji-only display for pairs/pools."""
    if not tag:
        return ""
    if "🥇" in tag:
        return "🥇"
    if "🎆" in tag:
        return "🎆"
    if "🔺" in tag:
        return "🔺"
    if "➕" in tag:
        return "➕"
    if "🟢" in tag:
        return "🟢"
    return ""


def inferred_display_emoji(rec: HitterRecord, current: str = "") -> str:
    """Fallback emoji for unique/cross-check names that are not game-slot picks."""
    if current in {"🥇", "🎆", "🔺", "🟢"}:
        return current
    if current == "➕" and (_power_signal(rec) or rec.hr_score >= 28 or rec.recent_ideal_hr_contact >= 0.12):
        return current
    if rec.hr_score >= 38 and rec.hrr_score >= 55:
        return "🥇"
    if rec.hr_score >= 33 and _power_signal(rec):
        return "🎆"
    if rec.hrr_score >= 60 and _power_signal(rec):
        return "🔺"
    if rec.contact_score >= 62 and (rec.last5_xbh >= 2 or rec.recent_ideal_hr_contact >= 0.12):
        return "🟢"
    if rec.hit_score >= 72 and (_power_signal(rec) or rec.last5_xbh >= 2):
        return "➕"
    return ""


def clean_player_name(rec: HitterRecord, pick_tag_map: Dict[int, str], short: bool = False, show_emoji: bool = True) -> str:
    base_emoji = clean_pick_tag(pick_tag_map.get(rec.player_id, "")) if show_emoji else ""
    emoji = inferred_display_emoji(rec, base_emoji) if show_emoji else ""
    name = rec.name
    if short:
        name = name.replace("Munetaka Murakami", "Murakami")
        name = name.replace("Spencer Torkelson", "Torkelson")
        name = name.replace("Luis Campusano", "Campusano")
        name = name.replace("Giancarlo Stanton", "Stanton")
    return f"{name} ({rec.team})" + (f" {emoji}" if emoji else "")


def format_pool_row(rec: HitterRecord, pick_tag_map: Dict[int, str]) -> str:
    return clean_player_name(rec, pick_tag_map, short=True, show_emoji=True)


def _pool_title(title: str) -> str:
    title = title.replace("|", "—")
    title = title.replace("2 HRR+HYBRID", "STRONGEST")
    title = title.replace("3 HRR CORE", "STRONGEST")
    title = title.replace("HRR+HR", "BALANCED")
    title = title.replace("HYBRID/VAR", "MID / VAR")
    return title.upper()


def format_pool_columns(
    title_a: str, pool_a: List[HitterRecord],
    title_b: str, pool_b: List[HitterRecord],
    title_c: str, pool_c: List[HitterRecord],
    title_d: str, pool_d: List[HitterRecord],
    pick_tag_map: Dict[int, str],
    width: int = 30,
) -> List[str]:
    """Clean horizontal pools: team abbr + category emoji only, no score clutter."""
    pools = [(title_a, pool_a), (title_b, pool_b), (title_c, pool_c), (title_d, pool_d)]
    col_width = max(26, min(width, 34))
    lines: List[str] = []
    lines.append("  ".join(_pool_title(t)[:col_width].ljust(col_width) for t, _ in pools).rstrip())
    lines.append("")
    max_len = max((len(p) for _, p in pools), default=0)
    for i in range(max_len):
        cells: List[str] = []
        for _, pool in pools:
            txt = format_pool_row(pool[i], pick_tag_map)[:col_width] if i < len(pool) else ""
            cells.append(txt.ljust(col_width))
        lines.append("  ".join(cells).rstrip())
    return lines

def choose_unique_pairs(
    scored_pairs: List[Tuple[HitterRecord, HitterRecord, float]],
    used_ids: set[int],
    count: int = 2,
    max_exposure: int = 1,
    same_game_counter: Optional[Dict[int, int]] = None,
    same_game_cap: int = 3,
) -> List[Tuple[HitterRecord, HitterRecord, float]]:
    out: List[Tuple[HitterRecord, HitterRecord, float]] = []
    exposure: Dict[int, int] = {}
    for pid in used_ids:
        exposure[pid] = exposure.get(pid, 0) + 1
    for a, b, score in scored_pairs:
        if exposure.get(a.player_id, 0) >= max_exposure or exposure.get(b.player_id, 0) >= max_exposure:
            continue
        if same_game_counter is not None and a.game_pk == b.game_pk and same_game_counter.get(a.game_pk, 0) >= same_game_cap:
            continue
        exposure[a.player_id] = exposure.get(a.player_id, 0) + 1
        exposure[b.player_id] = exposure.get(b.player_id, 0) + 1
        used_ids.add(a.player_id); used_ids.add(b.player_id)
        if same_game_counter is not None and a.game_pk == b.game_pk:
            same_game_counter[a.game_pk] = same_game_counter.get(a.game_pk, 0) + 1
        out.append((a, b, score))
        if len(out) >= count:
            break
    return out


def build_structured_pairs(
    candidate_rows: List[HitterRecord],
    buckets: Dict[str, List[HitterRecord]],
    global_exposure: Dict[int, int],
) -> Dict[str, List[Tuple[HitterRecord, HitterRecord, float, str]]]:
    ranked = sorted(candidate_rows, key=_pool_leg_score, reverse=True)

    def pitcher_weakness_points(rec: HitterRecord) -> float:
        pts = 0.0
        _es2 = effective_side(rec.bats, rec.pitcher_throws)
        side_hr9 = rec.pitcher_hr9_vs_lhb if _es2 == "L" else rec.pitcher_hr9_vs_rhb
        side_whip = rec.pitcher_whip_vs_lhb if _es2 == "L" else rec.pitcher_whip_vs_rhb
        side_match = (
            (_es2 == "L" and rec.pitcher_weak_side == "LHB")
            or (_es2 == "R" and rec.pitcher_weak_side == "RHB")
        )
        if rec.pitcher_hr9 >= 1.30: pts += 2.0
        elif rec.pitcher_hr9 >= 1.05: pts += 1.0
        if rec.pitcher_fb_rate >= 0.40: pts += 1.25
        elif rec.pitcher_fb_rate >= 0.36: pts += 0.60
        if side_hr9 >= 1.25: pts += 2.0
        elif side_hr9 >= 1.05: pts += 1.0
        if side_whip >= 1.35: pts += 0.75
        if side_match: pts += 1.0
        return pts

    def active_form_score(r: HitterRecord) -> float:
        """Based on actual homer data: L5 hits 6+, L5 XBH 2+, L5 HR 1+ are the key signals."""
        tracked = max(1, r.recent_350_den)
        return (
            0.28 * minmax_norm(r.last5_hits, 3, 10) +      # actively hitting
            0.24 * minmax_norm(r.last5_xbh, 0, 5) +        # hard contact not soft singles
            0.20 * minmax_norm(r.last7_hr, 0, 4) +         # recently homered
            0.15 * minmax_norm(r.last5_hr, 0, 3) +         # in HR form right now
            0.13 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.28)  # contact quality
        )

    def power_ceiling_score(r: HitterRecord) -> float:
        """Raw power ceiling — EV, IHR, 350+, barrel."""
        tracked = max(1, r.recent_350_den)
        ev = getattr(r, 'recent_ev', 88.5)
        return (
            0.30 * minmax_norm(ev, 85.0, 95.0) +
            0.25 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.26) +
            0.22 * minmax_norm(r.recent_375_num / tracked, 0.02, 0.28) +
            0.13 * minmax_norm(r.recent_barrel_rate, 0.03, 0.18) +
            0.10 * minmax_norm(r.season_iso, 0.08, 0.38)
        )

    # shared_day_bonus() removed (2026-08-28). It was fully built, fully
    # documented with the real 186k-pair study, and never called anywhere in
    # this file — pair_score() below already has its own comment explaining
    # why: "Shared-context bonuses are intentionally not added... Adding them
    # again double-counts them." Kept as dead code for a while as a record of
    # the work; deleting it now that the same finding is independently
    # confirmed and permanently documented in lib/pairEvidence.js on the site
    # repo, which is the canonical writeup going forward. Removing this also
    # closes out the matching site-side bug: PairBuilder.js's live Fit score
    # was still weighting sameGame/days/since at 25%/10%/10% on the belief
    # this docstring itself disproves — fixed same day, see that file's own
    # 2026-08-28 comment on the `fit` calculation.

    def pair_score(a: HitterRecord, b: HitterRecord, mode: str) -> float:
        # Base: joint power ceiling + active form + timing
        form_a = active_form_score(a)
        form_b = active_form_score(b)
        power_a = power_ceiling_score(a)
        power_b = power_ceiling_score(b)

        score = (
            0.26 * (form_a + form_b) +          # active form — biggest signal from data
            0.24 * (power_a + power_b) +         # power ceiling
            0.22 * ((a.hrw_score + b.hrw_score) / 2.0) * 0.01 +  # timing alignment
            0.11 * pitcher_weakness_points(a) +  # matchup
            0.11 * pitcher_weakness_points(b) +
            0.09 * (a.hr_score + b.hr_score) * 0.01 +  # HR score anchor
            # due_score and consistency_score added per audit (2026-06-27) --
            # this system had real depth (form, power, matchup, shared-day
            # bonus) but was missing both signals entirely, same gap found
            # in the separate JSON pair-builder system and already fixed
            # there. Modest weight since this formula already has 6 solid
            # components; these add a complementary dueness/reliability
            # read rather than dominating.
            0.04 * ((1.0 if a.hr_pace_flag else 0.0) + (1.0 if b.hr_pace_flag else 0.0)) +
            0.04 * (minmax_norm(a.consistency_score, 25, 65) + minmax_norm(b.consistency_score, 25, 65))
        )

        # Shared-context bonuses are intentionally not added. The archive
        # showed same game/team/park at chance, while the useful overlap
        # signals (ISO and recent HR) are already carried by each leg's own
        # probability/context inputs. Adding them again double-counts them.

        # Lineup spot bonus
        if a.lineup_spot in (1,2,3,4,5): score += 0.8
        if b.lineup_spot in (1,2,3,4,5): score += 0.8

        # Mode-specific boosts
        if mode == "hot" and form_a >= 0.55 and form_b >= 0.55: score += 4.0
        if mode == "trigger" and (a.hrw_score >= 65 or b.hrw_score >= 65): score += 3.0
        if mode == "hybrid" and power_a >= 0.50 and power_b >= 0.50: score += 3.0
        if mode == "value" and (_power_signal(a) and _power_signal(b)): score += 2.0

        # ─── LEG QUALITY (2026-08-09) ──────────────────────────────────────
        # Donovan: "pair scoring needs to be rebuilt, yes."
        #
        # It did. Graded against the archive, the pair score ranked BACKWARDS:
        # its top third of pairs cleared 3.4% and its bottom third cleared
        # 5.3%. Whatever it was ordering, it was not the thing that happens.
        #
        # The legs themselves are fine — a name inside a pair or pool homers
        # 19.0% of the time against a 14.9% slate baseline, so the SELECTION
        # is working and the RANKING was not. Splitting 1,588 graded legs at
        # each field's median showed what actually separates a leg that goes
        # deep from one that doesn't:
        #
        #     season_iso            23.0% vs 14.9%   +8.2   intervals separate
        #     pitcher_whip          22.8%    14.8%   +7.9   intervals separate
        #     season_k_rate         22.9%    15.7%   +7.2   intervals separate
        #     last5_hr              22.5%    15.4%   +7.2   intervals separate
        #     weak_spot_flag        23.3%    17.3%   +6.0
        #     recent_barrel_rate    17.3%    20.7%   -3.4   (backwards)
        #
        # THE BUG, and it is the whole ballgame: season_k_rate is one of the
        # three strongest POSITIVE leg signals, and this function was docking
        # 6 points for it. High-strikeout hitters are power hitters — that is
        # the same swing viewed from two sides — and the penalty was
        # systematically demoting exactly the bats that convert. Two hitters at
        # 30% K each lost 6 points and another 2 on top, which is enough to
        # invert an ordering all on its own.
        #
        # The penalty is replaced by a bonus at the same magnitude, and a
        # positive term for the other three measured signals. Barrel rate is
        # left alone rather than inverted — a negative reading at -3.4 with
        # overlapping intervals is not evidence of anything, and inverting it
        # would be fitting the noise.
        _k = (safe_float(a.season_k_rate, 0.22) + safe_float(b.season_k_rate, 0.22)) / 2.0
        score += 6.0 * minmax_norm(_k, 0.18, 0.32)
        score += 5.0 * (minmax_norm(a.season_iso, 0.120, 0.320)
                        + minmax_norm(b.season_iso, 0.120, 0.320))
        score += 4.0 * (minmax_norm(safe_float(a.pitcher_whip, 1.25), 1.05, 1.60)
                        + minmax_norm(safe_float(b.pitcher_whip, 1.25), 1.05, 1.60))
        score += 3.0 * (minmax_norm(a.last5_hr, 0, 3) + minmax_norm(b.last5_hr, 0, 3))
        if getattr(a, "weak_spot_flag", False): score += 2.5
        if getattr(b, "weak_spot_flag", False): score += 2.5

        # Hard penalties
        if not _power_signal(a) or not _power_signal(b): score -= 5.0
        if (a.hrw_score or 0) < 40: score -= 6.0
        if (b.hrw_score or 0) < 40: score -= 6.0
        if (a.pitcher_era or 0) <= 2.50 and (a.pitcher_whip or 0) <= 1.05: score -= 4.0
        if (b.pitcher_era or 0) <= 2.50 and (b.pitcher_whip or 0) <= 1.05: score -= 4.0
        if (a.lineup_spot or 9) >= 8: score -= 3.0
        if (b.lineup_spot or 9) >= 8: score -= 3.0

        # The ticket only wins when both legs homer. Make the geometric mean
        # of their transparent season estimates the primary rank and keep the
        # audited context formula as a smaller tiebreaker. This prevents a
        # large pile of shared-day bonuses from outranking two better bats.
        joint_quality = 100.0 * math.sqrt(
            _season_hr_game_probability(a) * _season_hr_game_probability(b)
        )
        return round(0.80 * joint_quality + 0.20 * score, 2)

    hrr_hot = buckets.get("hrr", [])[:18]
    core = buckets.get("core", [])[:18]
    hybrid = buckets.get("hybrid", [])[:18]
    mid = buckets.get("mid", [])[:20]

    hot_pairs=[]; trigger_pairs=[]; hybrid_pairs=[]; value_pairs=[]
    usable = dedupe_players(hrr_hot + hybrid + core + mid + ranked[:25])
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            a, b = usable[i], usable[j]
            if a.player_id == b.player_id: continue
            # Final pair filter: avoid dead timing combos.
            if ((a.hrw_score + b.hrw_score) / 2.0) < 50.0:  # lowered from 60 — 60 was too strict on lighter slates
                continue
            a_hot = a in hrr_hot or (a.hrr_score >= 55 and a.hrw_score >= 45 and _power_signal(a))
            b_hot = b in hrr_hot or (b.hrr_score >= 55 and b.hrw_score >= 45 and _power_signal(b))
            a_core = a in core or a.hr_score >= 32
            b_core = b in core or b.hr_score >= 32
            a_hybrid = a in hybrid or (a.hr_score >= 28 and a.hrr_score >= 48 and a.hrw_score >= 45)
            b_hybrid = b in hybrid or (b.hr_score >= 28 and b.hrr_score >= 48 and b.hrw_score >= 45)

            if a_hot and b_hot:
                hot_pairs.append((a, b, pair_score(a, b, "hot")))
            if (a_hot and b_core) or (b_hot and a_core) or (a.hrw_score >= 60 and b_core) or (b.hrw_score >= 60 and a_core):
                trigger_pairs.append((a, b, pair_score(a, b, "trigger")))
            if a_hybrid and b_hybrid:
                hybrid_pairs.append((a, b, pair_score(a, b, "hybrid")))
            if (_power_signal(a) and _power_signal(b)) and (a.hrw_score >= 45 or b.hrw_score >= 45 or a.hrr_score >= 52 or b.hrr_score >= 52):
                value_pairs.append((a, b, pair_score(a, b, "value")))

    for lst in (hot_pairs, trigger_pairs, hybrid_pairs, value_pairs):
        lst.sort(key=lambda x: x[2], reverse=True)

    # MINI-BOT AUDIT (2026-08-08, B1): global_exposure was a parameter this
    # function never read — ALT LOOKS / TOP-30 blocks silently did nothing
    # for pair lanes. Seed used_ids from it so the blocks actually block.
    used_ids: set[int] = {pid for pid, v in (global_exposure or {}).items() if v >= 1}
    same_game_counter:Dict[int,int]={}; out={}
    hot=choose_unique_pairs(hot_pairs,used_ids,2,1,same_game_counter)
    trigger=choose_unique_pairs(trigger_pairs,used_ids,2,1,same_game_counter)
    hybrid_sel=choose_unique_pairs(hybrid_pairs,used_ids,2,1,same_game_counter)
    value=choose_unique_pairs(value_pairs,used_ids,2,1,same_game_counter)
    if hot: out["A"]=[(*p,"🏁 HRR Hot Stack") for p in hot]
    if trigger: out["B"]=[(*p,"⚡ Trigger + Power") for p in trigger]
    if hybrid_sel: out["C"]=[(*p,"🎯 Hybrid Core") for p in hybrid_sel]
    if value: out["D"]=[(*p,"🎲 Variance Power") for p in value]
    return out



def top_up_pool(
    pool: List[HitterRecord],
    target_size: int,
    candidate_rows: List[HitterRecord],
    section_blocked: set[int],
    pick_tag_map: Dict[int, str],
    avoid_top_ids: Optional[set[int]] = None,
    prefer_variance: bool = False,
) -> List[HitterRecord]:
    """Make sure every displayed pool column has enough names.

    For 6-man C/D, prefer underlisted model-score players so those columns add variance
    instead of repeating the obvious game-block picks.
    """
    out = list(pool)
    used = {p.player_id for p in out} | set(section_blocked) | set(LAST_ALT_USED_IDS)
    avoid_top_ids = avoid_top_ids or set()

    def variance_score(r: HitterRecord) -> float:
        return (
            0.32 * minmax_norm(r.hr_score, 18, 55) +
            0.24 * minmax_norm(r.hrw_score, 35, 85) +
            0.18 * minmax_norm(r.hrr_score, 45, 85) +
            0.16 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.30) +
            0.10 * minmax_norm(r.recent_375_num / max(1, r.recent_350_den), 0.02, 0.25)
        )

    def add_from(candidates: List[HitterRecord], allow_top: bool = False) -> None:
        nonlocal out, used
        for r in candidates:
            if len(out) >= target_size:
                break
            if r.player_id in used:
                continue
            if (not allow_top) and r.player_id in avoid_top_ids:
                continue
            out.append(r)
            used.add(r.player_id)

    ranked = sorted(candidate_rows, key=_pool_leg_score, reverse=True)
    if prefer_variance:
        untagged_quality = sorted([
            r for r in ranked
            if not pick_tag_map.get(r.player_id)
            and r.player_id not in avoid_top_ids
            and (r.hr_score >= 24 or r.hrw_score >= 50 or r.hrr_score >= 55 or r.recent_ideal_hr_contact >= 0.12)
            and (_power_signal(r) or r.last5_xbh >= 2 or r.hrw_score >= 55)
        ], key=variance_score, reverse=True)
        underlisted = sorted([
            r for r in ranked
            if pick_tag_map.get(r.player_id) not in {"🥇TOP", "🎆HR"}
            and r.player_id not in avoid_top_ids
            and (_power_signal(r) or r.hrw_score >= 55 or r.hrr_score >= 58)
        ], key=variance_score, reverse=True)
        add_from(untagged_quality, allow_top=False)
        add_from(underlisted, allow_top=False)

    tagged = [r for r in ranked if pick_tag_map.get(r.player_id)]
    powerish = [r for r in ranked if _power_signal(r)]
    if not prefer_variance:
        add_from(tagged, allow_top=False)
    add_from(powerish, allow_top=False)
    add_from(ranked, allow_top=True)
    return out[:target_size]

def _s2_pair_reason(a: HitterRecord, b: HitterRecord) -> str:
    # Extracted per audit (2026-06-27) so both the .txt report and the new
    # JSON export use the EXACT same reason logic -- guarantees they can
    # never drift out of sync with each other over time.
    reasons = []
    ev_a = getattr(a, 'recent_ev', 0)
    ev_b = getattr(b, 'recent_ev', 0)
    if ev_a >= 91 and ev_b >= 91:
        reasons.append(f"EV {ev_a:.1f}+{ev_b:.1f}")
    if (a.hrw_score or 0) >= 60 and (b.hrw_score or 0) >= 60:
        reasons.append(f"HRW {round(a.hrw_score or 0)}+{round(b.hrw_score or 0)}")
    if (a.pitcher_hr9 or 0) >= 1.2 and (b.pitcher_hr9 or 0) >= 1.2:
        reasons.append(f"HR/9 {a.pitcher_hr9}+{b.pitcher_hr9}")
    if 'HARD' in (a.pitcher_attack_tag or '') and 'HARD' in (b.pitcher_attack_tag or ''):
        reasons.append("both HARD CONTACT")
    elif 'HARD' in (a.pitcher_attack_tag or '') or 'HARD' in (b.pitcher_attack_tag or ''):
        reasons.append("1 HARD CONTACT")
    a_side = (a.bats == 'L' and a.pitcher_weak_side == 'LHB') or (a.bats == 'R' and a.pitcher_weak_side == 'RHB')
    b_side = (b.bats == 'L' and b.pitcher_weak_side == 'LHB') or (b.bats == 'R' and b.pitcher_weak_side == 'RHB')
    if a_side and b_side:
        reasons.append("both ⭐ weak side")
    if a.last5_hits >= 6 and b.last5_hits >= 6:
        reasons.append(f"L5 {a.last5_hits}H+{b.last5_hits}H hot")
    if (a.last7_hr or 0) >= 1 and (b.last7_hr or 0) >= 1:
        reasons.append(f"L7 {a.last7_hr}HR+{b.last7_hr}HR")
    if (a.last5_xbh or 0) >= 3 and (b.last5_xbh or 0) >= 3:
        reasons.append(f"L5 XBH {a.last5_xbh}+{b.last5_xbh}")
    if a.hr_pace_flag or b.hr_pace_flag:
        reasons.append("due-rate pressure")
    if a.consistency_score >= 50 and b.consistency_score >= 50:
        reasons.append("both balanced/reliable")
    if not reasons:
        reasons.append("HR + power profile")
    return " | ".join(reasons[:3])


def _s2_pair_tags(a: HitterRecord, b: HitterRecord) -> List[str]:
    # Tags added per audit (2026-06-27) -- System 2 pairs had reason text
    # but no tags list at all, unlike System 1. Mirrors the reason logic's
    # same signal checks into short tag form.
    tags: List[str] = []
    if (a.hrw_score or 0) >= 60 or (b.hrw_score or 0) >= 60: tags.append("HRW")
    if (a.pitcher_hr9 or 0) >= 1.2 or (b.pitcher_hr9 or 0) >= 1.2: tags.append("Pitcher target")
    # Richer pitcher-vulnerability signals added (per audit, 2026-06-28),
    # using the real composite pitcher_attack_score/tag system (flyball rate,
    # barrel rate, hard-hit rate, EV, HR/9 allowed -- not just the single
    # crude HR/9 threshold above). Kept as separate tags per request, rather
    # than replacing "Pitcher target" with this richer version.
    a_tag, b_tag = (a.pitcher_attack_tag or ''), (b.pitcher_attack_tag or '')
    if 'BLOWUP' in a_tag or 'BLOWUP' in b_tag: tags.append("Blowup incoming")
    elif 'HR ENVIRONMENT' in a_tag or 'HR ENVIRONMENT' in b_tag: tags.append("HR environment")
    elif (a.pitcher_attack_score or 0) >= 65 or (b.pitcher_attack_score or 0) >= 65: tags.append("High attack score")
    if 'HARD' in a_tag or 'HARD' in b_tag: tags.append("Hard contact")
    a_side = (a.bats == 'L' and a.pitcher_weak_side == 'LHB') or (a.bats == 'R' and a.pitcher_weak_side == 'RHB')
    b_side = (b.bats == 'L' and b.pitcher_weak_side == 'LHB') or (b.bats == 'R' and b.pitcher_weak_side == 'RHB')
    if a_side or b_side: tags.append("Weak side")
    if (a.last7_hr or 0) >= 1 or (b.last7_hr or 0) >= 1: tags.append("Recent HR")
    if a.hr_pace_flag or b.hr_pace_flag: tags.append("Due")
    if a.consistency_score >= 50 and b.consistency_score >= 50: tags.append("Reliable")
    return tags[:7]


def _s2_risk(score: float) -> str:
    # MINI-BOT AUDIT (2026-08-08, B12): the old 50/30 thresholds were System
    # 1's scale (scores in the hundreds). System 2 pair scores live ~5–18,
    # so every pair printed "Lower" while pools (fed alignment averages
    # ~50–75) all printed "High". Rescaled to System 2's actual range.
    if score >= 12:
        return "High"
    if score >= 7:
        return "Medium"
    return "Lower"


def _s2_player_dict(r: HitterRecord) -> Dict[str, Any]:
    # Added per audit (2026-06-27) to give System 2's pairs/pools a JSON
    # export, mirroring System 1's _pb_player key selection so the frontend
    # can read either system's output the same way.
    return {
        "player_id": r.player_id, "name": r.name, "team": r.team, "opponent": r.opponent,
        "game_pk": r.game_pk, "lineup_spot": r.lineup_spot, "bats": r.bats,
        "hr_score": r.hr_score, "overall_score": r.overall_score, "hrw_score": r.hrw_score,
        "hr_score_shadow": r.hr_score_shadow, "shadow_board_rank": r.shadow_board_rank,
        "longest_hr_score": r.longest_hr_score, "longest_hr_rank": r.longest_hr_rank,
        "hrr_score": r.hrr_score, "hit_score": r.hit_score, "contact_score": r.contact_score,
        "consistency_score": r.consistency_score,
        "recent_ideal_hr_contact": r.recent_ideal_hr_contact, "recent_350_num": r.recent_350_num,
        "recent_350_den": r.recent_350_den, "recent_375_num": r.recent_375_num,
        "recent_400_num": getattr(r, "recent_400_num", 0),
        "recent_max_distance": getattr(r, "recent_max_distance", 0.0),
        "recent_avg_distance": getattr(r, "recent_avg_distance", 0.0),
        "recent_avg_hr_distance": getattr(r, "recent_avg_hr_distance", 0.0),
        "recent_pull_air_rate": getattr(r, "recent_pull_air_rate", 0.0),
        "recent_squared_up_rate": getattr(r, "recent_squared_up_rate", None),
        "recent_squared_up_sample": getattr(r, "recent_squared_up_sample", 0),
        "recent_blast_rate": getattr(r, "recent_blast_rate", None),
        "season_max_distance": getattr(r, "season_max_distance", 0.0),
        "recent_ev": getattr(r, "recent_ev", None), "last5_hits": r.last5_hits,
        "last5_hr": r.last5_hr, "last5_xbh": r.last5_xbh, "last7_hr": r.last7_hr,
        "season_hr": r.season_hr, "season_pa": r.season_pa, "hr_per_pa": r.hr_per_pa,
        "season_hr_game_probability": _season_hr_game_probability(r),
        "probability_source": "season HR/PA, shrunk; not calibrated model probability",
        "pitcher_name": r.pitcher_name, "pitcher_team": getattr(r, "pitcher_team", ""),
        "pitcher_throws": r.pitcher_throws, "pitcher_hr9": r.pitcher_hr9, "pitcher_whip": r.pitcher_whip,
        "pitcher_attack_tag": getattr(r, "pitcher_attack_tag", ""),
        "pitch_mix_score": getattr(r, "pitch_mix_score", None),
        "weather_label": getattr(r, "weather_label", ""),
    }


def build_top30_pairs(top30: List[HitterRecord]) -> Tuple[str, List[Dict[str, Any]]]:
    """2 pairs built directly from the Top 30 board itself (not the separate
    62-player pair/pool candidate pool used by the other 4 lanes), so the
    sheet always has one pairing lane that's a straight 1:1 mirror of the
    board you're already looking at. Kept simple on purpose: rank by a blend
    of HR score + damage conversion, greedily take the top 2 non-overlapping
    pairs (different players, different games).
    """
    eligible = [r for r in top30 if not getattr(r, "true_avoid_hr", False)]
    if len(eligible) < 2:
        return "", []

    candidates: List[Tuple[HitterRecord, HitterRecord, float]] = []
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            if not pair_allowed(a, b):
                continue
            chance_quality = 100.0 * math.sqrt(
                _season_hr_game_probability(a) * _season_hr_game_probability(b)
            )
            context_quality = 0.25 * (_hr_alignment_score(a) + _hr_alignment_score(b))
            score = round(0.80 * chance_quality + 0.20 * context_quality, 2)
            candidates.append((a, b, score))
    candidates.sort(key=lambda x: x[2], reverse=True)

    used_ids: set[int] = set()
    selected: List[Tuple[HitterRecord, HitterRecord, float]] = []
    for a, b, score in candidates:
        if a.player_id in used_ids or b.player_id in used_ids:
            continue
        selected.append((a, b, score))
        used_ids.add(a.player_id)
        used_ids.add(b.player_id)
        if len(selected) >= 2:
            break

    if not selected:
        return "", []

    lines = ["🔒 TOP 30 PAIRS", "Straight from the Top 30 board, max 1 appearance"]
    json_pairs: List[Dict[str, Any]] = []
    for idx, (a, b, score) in enumerate(selected, start=1):
        lines.append(f"{idx}) {a.name} ({a.team}) + {b.name} ({b.team})")
        reason_txt = _s2_pair_reason(a, b)
        lines.append(f"   Reason: {reason_txt}")
        json_pairs.append({
            "type": "Top 30 Pairs",
            "lane_key": "TOP30",
            "pair_key": "|".join(sorted([str(a.player_id), str(b.player_id)])),
            "pair_score": score,
            "estimated_both_hr_probability": _ticket_probability_at_least([a, b], 2),
            "risk": _s2_risk(score),
            "tags": _s2_pair_tags(a, b),
            "reason": reason_txt,
            "players": [_s2_player_dict(a), _s2_player_dict(b)],
        })
    return "\n".join(lines), json_pairs


def build_pair_sections(rows: List[HitterRecord]) -> Tuple[str, Dict[str, Any]]:
    """
    Pairs and pools. WRAPPED, because a failure here must not cost the slate.

    2026-08-09, Donovan: "the site's bot crashed today." This function is the
    biggest thing I changed — retiring the 6-man pool, restructuring the
    recipes, rewriting pair_score — and it sits in the middle of a run that
    also produces the HR board, the four markets and every published payload.
    A pool recipe that comes back empty, a bucket that does not exist on a
    thin slate, an index into a list that is shorter than it was yesterday:
    any of those raise, and before this wrapper any of them took the WHOLE
    NIGHT'S SLATE with them.

    That trade is indefensible. Pairs and pools are a side dish; the board is
    the product. So the section now degrades to empty and the run continues,
    and the error is printed loudly enough to find in the Actions log rather
    than swallowed.
    """
    try:
        return _build_pair_sections(rows)
    except Exception as e:
        import traceback
        print("!! pairs/pools section failed — slate continues without it")
        print(f"!! {type(e).__name__}: {e}")
        traceback.print_exc()
        return "", {"recommended_pairs": [], "pools_4man": [], "pools_6man": [], "pools_3man": []}


def _build_pair_sections(rows: List[HitterRecord]) -> Tuple[str, Dict[str, Any]]:
    global LAST_HR_SECTION_USED_IDS
    LAST_HR_SECTION_USED_IDS = set()
    if len(rows) < 6: return "", {"recommended_pairs": [], "pools_4man": [], "pools_6man": [], "pools_3man": []}
    candidate_rows = top_pool_candidates(rows, 62)
    ranked = sorted(candidate_rows, key=_pool_leg_score, reverse=True)
    pick_tag_map = game_pick_type_map(rows)
    buckets = classify_pool_buckets(rows)
    global_exposure: Dict[int, int] = {}
    # Block ALT LOOKS from later pair/pool sections. ALT should stay unique.
    for pid in LAST_ALT_USED_IDS:
        global_exposure[pid] = 99

    # Top 30 Pairs: built straight from the Top 30 board (not the 62-player
    # pair/pool candidate pool the other 4 lanes use), so there's always a
    # pairing lane that's a direct mirror of the board. Computed first so its
    # 4 players are blocked out of the other lanes below, same as ALT LOOKS.
    top30_pair_text, top30_json_pairs = build_top30_pairs(LAST_TOP30_BOARD)
    top30_used_ids: set[int] = set()
    for p in top30_json_pairs:
        for pd in p.get("players", []):
            top30_used_ids.add(pd.get("player_id"))
    for pid in top30_used_ids:
        global_exposure[pid] = 99

    structured_pairs = build_structured_pairs(candidate_rows, buckets, global_exposure)
    pair_pool_used_ids: set[int] = set(LAST_ALT_USED_IDS) | top30_used_ids
    for entries in structured_pairs.values():
        for a, b, *_ in entries:
            pair_pool_used_ids.add(a.player_id)
            pair_pool_used_ids.add(b.player_id)
            # Block pair players from pools so the output does not recycle the same names.
            global_exposure[a.player_id] = player_max_exposure(a, buckets)
            global_exposure[b.player_id] = player_max_exposure(b, buckets)
    lines=["🧩 PAIRINGS", "Rules: max 1 appearance in pairs | reasons compact"]
    if top30_pair_text:
        lines.append("")
        lines.append(top30_pair_text)
    pair_titles={"A":"🔒 CORE HR PAIRS","B":"🧬 STATCAST HR PAIRS","C":"🎯 FLEX HR PAIRS","D":"🎲 VARIANCE POWER PAIRS"}
    json_pairs: List[Dict[str, Any]] = list(top30_json_pairs)
    for key in ("A","B","C","D"):
        if key not in structured_pairs:
            continue
        if len(lines) > 2:
            lines.append("")
        lines.append(pair_titles[key])
        for idx,(a,b,score,label) in enumerate(structured_pairs[key], start=1):
            lines.append(f"{idx}) {clean_player_name(a, pick_tag_map)}  + {clean_player_name(b, pick_tag_map)}")
            reason_txt = _s2_pair_reason(a, b)
            lines.append(f"   Reason: {reason_txt}")
            json_pairs.append({
                "type": pair_titles[key].split(" ", 1)[1] if " " in pair_titles[key] else pair_titles[key],
                "lane_key": key,
                "pair_key": "|".join(sorted([str(a.player_id), str(b.player_id)])),
                "pair_score": score,
                "estimated_both_hr_probability": _ticket_probability_at_least([a, b], 2),
                "risk": _s2_risk(score),
                "tags": _s2_pair_tags(a, b),
                "reason": reason_txt,
                "players": [_s2_player_dict(a), _s2_player_dict(b)],
            })
    top5_ids={r.player_id for r in ranked[:5]}
    pool4_blocked:set[int]=set()
    # Restructured recipes: each pool now maps cleanly to its own label
    # instead of all four leaning on the same hrr-heavy mix (the old A/B/C/D
    # all had hrr:1-2 in common, which made "Strongest" and "HRR+Power" read
    # almost identically). Now: A is pure top-HR-ceiling, B is pure
    # production+combo, C draws one from every bucket for a genuine
    # balanced mix, D is mostly variance/underdog.
    # Added 1 wtf (variance) slot into each of A/B/C -- previously only D had
    # any variance-bucket presence, so A/B/C were entirely made of the same
    # high-confidence core/hybrid/hrr names with no real spread between them.
    # REBALANCED (2026-07-25, pass 2): slot-level backtest -- matching each
    # pool player's position to the recipe's bucket order and checking who
    # actually homered, not just pool-level totals -- showed hybrid is the
    # real winner (14.0% aggregate across every pool it appears in, 13-19.7%
    # every time), while core is the consistent laggard everywhere (10.1%
    # aggregate, never above 10.5% in any pool). hrr's reputation was mostly
    # one pool's context (15.7% in B) -- it ran 5.2% in C and 7.8% in 6-Man B,
    # so it is not the universal winner pass 1 assumed. Cut the remaining
    # core slot from A for a 2nd hybrid, and swapped C's hrr slot (5.2% in
    # this exact pool) for a 2nd hybrid (19.7% in this exact pool). B and D
    # still unchanged -- still the pools that are winning.
    pool4_a=build_structured_pool(candidate_rows,{"hrr":1,"hybrid":2,"wtf":1},buckets,global_exposure,blocked_ids=pool4_blocked); pool4_blocked.update(r.player_id for r in pool4_a); pair_pool_used_ids.update(r.player_id for r in pool4_a)
    pool4_b=build_structured_pool(candidate_rows,{"hrr":2,"hybrid":1,"wtf":1},buckets,global_exposure,blocked_ids=pool4_blocked); pool4_blocked.update(r.player_id for r in pool4_b); pair_pool_used_ids.update(r.player_id for r in pool4_b)
    pool4_c=build_structured_pool(candidate_rows,{"hybrid":2,"wtf":2},buckets,global_exposure,blocked_ids=pool4_blocked); pool4_blocked.update(r.player_id for r in pool4_c); pair_pool_used_ids.update(r.player_id for r in pool4_c)
    pool4_d=build_structured_pool(candidate_rows,{"mid":1,"wtf":3},buckets,global_exposure,blocked_ids=pool4_blocked,blocked_top_ids=top5_ids)
    pair_pool_used_ids.update(r.player_id for r in pool4_d)
    lines.append(""); lines.append("🏊 4-MAN HR POOLS")
    lines.extend(format_pool_columns("POOL A — Strongest", pool4_a, "POOL B — HRR+Power", pool4_b, "POOL C — Balanced", pool4_c, "POOL D — Variance", pool4_d, pick_tag_map, width=34))
    # 6-man pools use their own exposure budget so C/D do not disappear after pairs + 4-man pools.
    # MINI-BOT AUDIT (2026-08-08, B4): this budget was seeded only with ALT
    # ids, so pair/TOP30/4-man players could reappear in 6-man pools. Seed
    # from the full exposure map — one appearance means one appearance.
    pool6_exposure: Dict[int, int] = {pid: 99 for pid in LAST_ALT_USED_IDS}
    for _pid, _v in (global_exposure or {}).items():
        if _v >= 1:
            pool6_exposure[_pid] = 99
    for _pid in pair_pool_used_ids:
        pool6_exposure[_pid] = 99
    pool6_blocked:set[int]=set()
    # REBALANCED (2026-07-25, pass 2): same slot-level backtest as the 4-man
    # pools above -- core ran 10.4% in both 6-Man A and B (consistent
    # laggard), while hybrid ran 13.0-13.6% in the same two pools and hrr
    # ran only 7.8% in B specifically. Trimmed one more core slot from A for
    # a 3rd hybrid, and swapped B's hrr slot (7.8% in this exact pool) for a
    # 3rd hybrid. C and D unchanged -- D's mid slot in particular is running
    # 15.1% and rising, no reason to touch it.
    # ─── THE 6-MAN IS RETIRED (2026-08-09) ─────────────────────────────────
    # Donovan: "retire the six man. I was using it as another pool of players
    # to use since a decent portion did go every night, but yes it is clearly
    # missing the mark."
    #
    # He had it exactly right, on both halves. Joining every pool leg to its
    # graded row across 40 nights:
    #
    #     4-MAN   0 went 51.2% | 1 went 35.0% | 2 went 13.1% | 3 went 0.6%
    #     6-MAN   0 went 46.2% | 1 went 37.5% | 2 went 11.9% | 3 went 3.8% | 4 went 0.6%
    #
    # So a decent portion DOES go every night — 1+ lands 48.8% of the time on
    # the 4-man and 53.8% on the 6-man, and a pool leg homers 17.5% against a
    # 14.9% slate baseline. The selection was never the problem.
    #
    # ALL SIX is the problem. Six independent 17.5% shots is one in ~34,000;
    # the archive has now watched 160 six-man pools and seen a maximum of four.
    # A product that cannot hit is not a product, however good its legs are.
    #
    # So the six splits into TWO THREES. Same players, same buckets, same
    # exposure budget — the only thing that changes is that the unit you are
    # asked to believe in got small enough to be reachable. Three legs at 17.5%
    # gives roughly a 44% chance of at least one and 8% of at least two, and
    # unlike "all six" those are outcomes the archive actually contains.
    #
    # Recipes come straight off the retired six-man ones, split down the
    # middle: A keeps the core+hybrid spine, its partner takes the hrr+wtf
    # side; B splits its four hybrids two and two.
    pool3_a=build_structured_pool(candidate_rows,{"core":1,"hybrid":2},buckets,pool6_exposure,blocked_ids=pool6_blocked); pool3_a=top_up_pool(pool3_a,3,candidate_rows,pool6_blocked,pick_tag_map); pool6_blocked.update(r.player_id for r in pool3_a); pair_pool_used_ids.update(r.player_id for r in pool3_a)
    pool3_b=build_structured_pool(candidate_rows,{"hrr":1,"hybrid":1,"wtf":1},buckets,pool6_exposure,blocked_ids=pool6_blocked); pool3_b=top_up_pool(pool3_b,3,candidate_rows,pool6_blocked,pick_tag_map); pool6_blocked.update(r.player_id for r in pool3_b); pair_pool_used_ids.update(r.player_id for r in pool3_b)
    pool3_c=build_structured_pool(candidate_rows,{"hybrid":2,"mid":1},buckets,pool6_exposure,blocked_ids=pool6_blocked); pool3_c=top_up_pool(pool3_c,3,candidate_rows,pool6_blocked,pick_tag_map); pool6_blocked.update(r.player_id for r in pool3_c); pair_pool_used_ids.update(r.player_id for r in pool3_c)
    pool3_d=build_structured_pool(candidate_rows,{"wtf":2,"mid":1},buckets,pool6_exposure,blocked_ids=pool6_blocked,blocked_top_ids=top5_ids); pool3_d=top_up_pool(pool3_d,3,candidate_rows,pool6_blocked,pick_tag_map,avoid_top_ids=top5_ids,prefer_variance=True)
    pair_pool_used_ids.update(r.player_id for r in pool3_d)
    lines.append(""); lines.append("🏊 3-MAN HR POOLS  (replaces the 6-man — see the note in the code)")
    lines.append("   Grade ladder: 2+ is a hit, 3/3 is perfect. One homer is not a win.")
    lines.extend(format_pool_columns("POOL A — Strongest", pool3_a, "POOL B — HRR+Var", pool3_b, "POOL C — Balanced", pool3_c, "POOL D — Variance", pool3_d, pick_tag_map, width=34))
    # Kept under the old names so downstream consumers that read pool6_* keep
    # working; they now hold three names each rather than six.
    pool6_a, pool6_b, pool6_c, pool6_d = pool3_a, pool3_b, pool3_c, pool3_d
    LAST_HR_SECTION_USED_IDS = set(pair_pool_used_ids)

    # JSON pools added per audit (2026-06-27) -- mirrors System 1's pool
    # output shape (name, size, pool_score, risk, tags, reason, players),
    # built from System 2's actual selected players so both the .txt report
    # and this JSON payload always reflect the same underlying selection.
    def _s2_pool_json(name: str, selected: List[HitterRecord], size: int) -> Dict[str, Any]:
        pool_score = round(sum(_pool_leg_score(r) for r in selected) / max(1, len(selected)), 1)
        pool_tags: List[str] = []
        for r in selected:
            if (r.hrw_score or 0) >= 60 and "HRW" not in pool_tags: pool_tags.append("HRW")
            if (r.pitcher_hr9 or 0) >= 1.2 and "Pitcher target" not in pool_tags: pool_tags.append("Pitcher target")
            # Richer pitcher-attack signals added (per audit, 2026-06-28),
            # matching what pairs already got -- pools previously had no
            # pitcher-vulnerability tag of any kind.
            r_tag = r.pitcher_attack_tag or ''
            if 'BLOWUP' in r_tag and "Blowup incoming" not in pool_tags: pool_tags.append("Blowup incoming")
            elif 'HR ENVIRONMENT' in r_tag and "HR environment" not in pool_tags: pool_tags.append("HR environment")
            if r.hr_pace_flag and "Due" not in pool_tags: pool_tags.append("Due")
            if r.consistency_score >= 50 and "Reliable" not in pool_tags: pool_tags.append("Reliable")
            if (r.last7_hr or 0) >= 1 and "Recent HR" not in pool_tags: pool_tags.append("Recent HR")
        same_game = len(set(r.game_pk for r in selected)) < len(selected)
        reason = f"{name} build · optimized for 2+" + (" · includes same-game legs" if same_game else "") + f" · {len(selected)}/{size} players filled"
        return {
            "name": name, "size": size, "pool_score": pool_score,
            # pool_score is an alignment AVERAGE (~50–75), not a pair score —
            # rescale into pair units before labeling (audit B12: pools all
            # printed "High" while pairs all printed "Lower")
            "risk": _s2_risk((pool_score - 45.0) / 2.5), "tags": pool_tags[:9], "reason": reason,
            "primary_bar": 2,
            "estimated_grade_probability": {
                "2plus": _ticket_probability_at_least(selected, 2),
                "3plus": _ticket_probability_at_least(selected, 3),
                "perfect": _ticket_probability_at_least(selected, len(selected)),
                "source": "independent season HR/PA estimates; screening estimate, not calibrated forecast",
            },
            "players": [_s2_player_dict(r) for r in selected],
        }

    json_pools_4man = [
        _s2_pool_json("Pool A — Strongest", pool4_a, len(pool4_a)),
        _s2_pool_json("Pool B — HRR+Power", pool4_b, len(pool4_b)),
        _s2_pool_json("Pool C — Balanced", pool4_c, len(pool4_c)),
        _s2_pool_json("Pool D — Variance", pool4_d, len(pool4_d)),
    ]
    # PUBLISHED KEY FIX (2026-08-12): pool6_a-d have held the retired-6-man's
    # 3-man replacement since 2026-08-09 ("kept under the old names so
    # downstream consumers keep working," above) — but this JSON kept
    # shipping them under the OLD "pools_6man" key, while Pools.js (updated
    # the SAME day) was already reading a "pools_3man" key this file never
    # sent. Two halves of one fix, done on the same day, that never actually
    # met: real 3-man pools have been arriving under "pools_6man" and getting
    # labelled "(retired)" on the site ever since. Ships under the name the
    # site already expects; labels match the text output above (Strongest /
    # HRR+Var / Balanced / Variance) instead of the stale pre-retirement
    # set. pools_6man goes genuinely empty rather than carrying today's data
    # under yesterday's name.
    json_pools_3man = [
        _s2_pool_json("Pool A — Strongest", pool6_a, len(pool6_a)),
        _s2_pool_json("Pool B — HRR+Var", pool6_b, len(pool6_b)),
        _s2_pool_json("Pool C — Balanced", pool6_c, len(pool6_c)),
        _s2_pool_json("Pool D — Variance", pool6_d, len(pool6_d)),
    ]
    json_payload = {
        # available_pool added per audit (2026-06-27) -- the frontend
        # (Pairs.js) uses this as its PRIMARY data source for client-side
        # pair/pool building (buildVariantPairs, buildVariantPools); without
        # it, most of the page falls back to whatever raw `players` prop it
        # was given, which may not match this function's candidate pool.
        "available_pool": [_s2_player_dict(r) for r in candidate_rows],
        "recommended_pairs": json_pairs,
        # left empty: System 2 has no native 3-man combo logic of its own
        # (a separate 3/5-man combo system already exists inside
        # build_hrr_builder for a different purpose) -- frontend already
        # handles this key being empty/missing gracefully.
        "recommended_3mans": [],
        "pools_4man": json_pools_4man,
        "pools_3man": json_pools_3man,
        "pools_6man": [],
    }
    return "\n".join(lines), json_payload


def build_model_cross_check_plays(rows: List[HitterRecord], pick_tag_map: Dict[int, str], used_ids: set[int]) -> List[str]:
    """Extra names: high across categories or HR upside that may be underlisted."""
    if not rows:
        return []

    def multi_score(r: HitterRecord) -> float:
        return (
            0.30 * minmax_norm(r.hr_score, 18, 60) +
            0.24 * minmax_norm(r.hrw_score, 35, 85) +
            0.22 * minmax_norm(r.hrr_score, 45, 85) +
            0.14 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.30) +
            0.10 * minmax_norm(r.recent_375_num / max(1, r.recent_350_den), 0.02, 0.25)
        )

    def upside_score(r: HitterRecord) -> float:
        return (
            0.34 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.30) +
            0.24 * minmax_norm(r.recent_375_num / max(1, r.recent_350_den), 0.02, 0.25) +
            0.18 * minmax_norm(r.hr_score, 20, 55) +
            0.14 * minmax_norm(r.hrw_score, 35, 85) +
            0.10 * minmax_norm(r.last5_xbh, 0, 5)
        )

    def pick(cands: List[HitterRecord], n: int, local_used: set[int]) -> List[HitterRecord]:
        out: List[HitterRecord] = []
        for r in cands:
            if r.player_id in local_used:
                continue
            out.append(r)
            local_used.add(r.player_id)
            if len(out) >= n:
                break
        return out

    local_used: set[int] = set()
    fresh = [r for r in rows if r.player_id not in used_ids]
    base = fresh if len(fresh) >= 8 else list(rows)
    high = pick(sorted(base, key=multi_score, reverse=True), 5, local_used)
    upside_base = [r for r in base if r.player_id not in {x.player_id for x in high}]
    upside = pick(sorted(upside_base, key=upside_score, reverse=True), 4, local_used)

    lines = ["🔎 MODEL CROSS-CHECK PLAYS", "", "HIGH ACROSS CATEGORIES"]
    for r in high:
        lines.append(clean_player_name(r, pick_tag_map))
    lines.append("")
    lines.append("HR UPSIDE / UNDERLISTED")
    for r in upside:
        lines.append(clean_player_name(r, pick_tag_map))
    return lines

def pick_top_min_pa(records: List[HitterRecord], attr: str, n: int, min_pa: int, used: Optional[Iterable[int]] = None) -> List[HitterRecord]:
    used_set = set(used or [])
    eligible = [r for r in records if r.player_id not in used_set and r.season_pa >= min_pa]
    if len(eligible) >= n:
        return sorted(eligible, key=lambda x: getattr(x, attr), reverse=True)[:n]
    fallback = sorted([r for r in records if r.player_id not in used_set], key=lambda x: getattr(x, attr), reverse=True)
    return fallback[:n]


def build_hrr_builder(rows: List[HitterRecord]) -> str:
    if not rows:
        return ""

    # HRR Builder: game-slot HRR picks first, then cross-check upgrades when stronger.
    # Final logic: controlled repeats, max 2 appearances per player, exact combos cannot repeat.
    # Includes high HIT-score players when they also grade well through HRR.
    MIN_HRR_PA = 15

    eligible_all = [r for r in rows if r.season_pa >= MIN_HRR_PA]
    tag_map = game_pick_type_map(rows)

    def hrr_builder_viable(r: HitterRecord) -> bool:
        return (
            r.hrr_score >= 52
            and (r.hrw_score >= 45 or r.hr_score >= 30 or r.recent_ideal_hr_contact >= 0.14 or r.hit_score >= 72)
            and not (r.hrw_score < 30 and r.recent_ideal_hr_contact < 0.10 and r.hr_score < 28 and r.hit_score < 72)
        )

    game_slot_hrr = [r for r in eligible_all if "🔺" in tag_map.get(r.player_id, "") and hrr_builder_viable(r)]
    hit_pick_upgrades = [
        r for r in eligible_all
        if "➕" in tag_map.get(r.player_id, "")
        and r.player_id not in {x.player_id for x in game_slot_hrr}
        and r.hit_score >= 70
        and r.hrr_score >= 54
        and hrr_builder_viable(r)
    ]
    cross_upgrades = [
        r for r in eligible_all
        if r.player_id not in {x.player_id for x in game_slot_hrr + hit_pick_upgrades}
        and r.hrr_score >= 58
        and hrr_builder_viable(r)
    ]

    eligible = game_slot_hrr + hit_pick_upgrades + cross_upgrades
    clean_eligible = [r for r in eligible if r.player_id not in LAST_HR_SECTION_USED_IDS and r.player_id not in LAST_ALT_USED_IDS]
    if len(clean_eligible) >= 8:
        eligible = clean_eligible
    if len(eligible) < 5:
        eligible = [r for r in eligible_all if hrr_builder_viable(r)] or eligible_all or list(rows)

    def hrr_builder_score(r: HitterRecord) -> float:
        production = minmax_norm(r.last5_hits + r.last5_runs + r.last5_rbi, 0, 18)
        context = minmax_norm(r.lineup_pre_onbase, 0.280, 0.430) + minmax_norm(r.lineup_post_convert, 0.280, 0.500)
        contact = minmax_norm(r.season_avg, 0.200, 0.330) + minmax_norm(r.babip, 0.250, 0.380)
        k_floor = 1.0 - minmax_norm(r.season_k_rate, 0.12, 0.34)
        hit_floor = minmax_norm(r.hit_score, 50, 85)
        # due_score/consistency_score added per audit (2026-06-27) -- same
        # gap found in every other scoring system this session. Modest
        # weights since this formula already has 6 solid components.
        return (
            0.44 * r.hrr_score
            + 8.0 * production
            + 5.0 * context
            + 4.0 * contact
            + 3.0 * k_floor
            + 3.0 * hit_floor
            + 2.0 * minmax_norm(r.recent_ideal_hr_contact, 0.04, 0.18)
            + (1.5 if r.hr_pace_flag else 0.0)
            + 2.0 * minmax_norm(r.consistency_score, 25, 65)
        )

    ranked = sorted(eligible, key=hrr_builder_score, reverse=True)
    if len(ranked) < 2:
        return ""

    def player_txt(r: HitterRecord) -> str:
        return f"{r.name} ({r.team})"

    def combo_key(combo: List[HitterRecord]) -> Tuple[int, ...]:
        return tuple(sorted(p.player_id for p in combo))

    def combo_score(combo: List[HitterRecord]) -> float:
        if not combo:
            return 0.0
        avg = sum(hrr_builder_score(p) for p in combo) / len(combo)
        teams = len({p.team for p in combo})
        hit_bonus = sum(minmax_norm(p.hit_score, 58, 85) for p in combo) / len(combo)
        return avg + 1.4 * teams + 2.0 * hit_bonus

    used_combo_keys: set[Tuple[int, ...]] = set()
    usage: Dict[int, int] = {}
    seen_players: set[int] = set()
    MAX_HRR_BUILDER_EXPOSURE = 2

    def combo_adds_fresh_player(combo: List[HitterRecord]) -> bool:
        return any(p.player_id not in seen_players for p in combo)

    def register_combo(combo: List[HitterRecord]) -> None:
        used_combo_keys.add(combo_key(combo))
        for p in combo:
            usage[p.player_id] = usage.get(p.player_id, 0) + 1
            seen_players.add(p.player_id)

    def make_combos(size: int, count: int) -> List[List[HitterRecord]]:
        from itertools import combinations
        pool = ranked[:max(size + 8, min(len(ranked), 22))]
        candidates = [list(c) for c in combinations(pool, size)]
        candidates.sort(key=combo_score, reverse=True)
        out: List[List[HitterRecord]] = []

        # Pass 1: strict exposure + each combo adds at least one fresh name when possible.
        for combo in candidates:
            key = combo_key(combo)
            if key in used_combo_keys:
                continue
            if any(usage.get(p.player_id, 0) >= MAX_HRR_BUILDER_EXPOSURE for p in combo):
                continue
            if seen_players and not combo_adds_fresh_player(combo):
                continue
            out.append(combo)
            register_combo(combo)
            if len(out) >= count:
                return out

        # Pass 2: keep exposure cap, but allow no-fresh combos if slate is thin.
        for combo in candidates:
            key = combo_key(combo)
            if key in used_combo_keys:
                continue
            if any(usage.get(p.player_id, 0) >= MAX_HRR_BUILDER_EXPOSURE for p in combo):
                continue
            out.append(combo)
            register_combo(combo)
            if len(out) >= count:
                return out

        # Pass 3: emergency fill only; exact duplicate combos still blocked.
        for combo in candidates:
            key = combo_key(combo)
            if key in used_combo_keys:
                continue
            out.append(combo)
            register_combo(combo)
            if len(out) >= count:
                return out
        return out

    core_plays = make_combos(2, 2)
    balanced_plays = make_combos(3, 2)
    value_plays = make_combos(5, 2)

    def reason(combo: List[HitterRecord]) -> str:
        # Dynamic reason added per audit (2026-06-27), replacing the old
        # static positional text ("first combo = X, second = Y" regardless
        # of what was actually in it) -- mirrors the same approach already
        # used for System 2's pairs/pools.
        parts = []
        avg_hrr = sum(p.hrr_score for p in combo) / len(combo)
        if avg_hrr >= 65:
            parts.append("elite HRR floor")
        elif avg_hrr >= 55:
            parts.append("solid HRR floor")
        avg_hit = sum(p.hit_score for p in combo) / len(combo)
        if avg_hit >= 70:
            parts.append("strong contact")
        due_hits = sum(1 for p in combo if p.hr_pace_flag)
        if due_hits >= 1:
            parts.append(f"{due_hits} due-rate play{'s' if due_hits > 1 else ''}")
        reliable_hits = sum(1 for p in combo if p.consistency_score >= 50)
        if reliable_hits >= len(combo) // 2 + 1:
            parts.append("balanced/reliable group")
        if len({p.team for p in combo}) == len(combo):
            parts.append("full team spread")
        if not parts:
            parts.append("production + depth")
        return " · ".join(parts[:3])

    def player_stat_txt(r: HitterRecord) -> str:
        star = " ⭐" if getattr(r, "weak_spot_flag", False) else ""
        return f"{r.name}{star} ({r.team}) [HRR {r.hrr_score:.0f}/HIT {r.hit_score:.0f}]"

    # REDESIGNED (2026-07-25, per request): was a single dense run-on line
    # per combo (names + reason, no stat context, no spacing between combo
    # groups). Now: each combo gets a labeled size tag, per-player HRR/HIT
    # scores inline so you can judge quality without cross-referencing the
    # board, a combo-average line, and a blank line between plays so the
    # section doesn't read as one solid wall of text.
    def add_group(lines: List[str], title: str, combos: List[List[HitterRecord]], size_label: str) -> None:
        if not combos:
            return
        lines.append("")
        lines.append(f"{title}  ({size_label})")
        for i, combo in enumerate(combos, start=1):
            avg_hrr = sum(p.hrr_score for p in combo) / len(combo)
            avg_hit = sum(p.hit_score for p in combo) / len(combo)
            lines.append(f"{i}) " + "  +  ".join(player_stat_txt(p) for p in combo))
            lines.append(f"   avg HRR {avg_hrr:.1f} | avg HIT {avg_hit:.1f} · {reason(combo)}")

    lines: List[str] = []
    lines.append(section_bar())
    lines.append("🏁 HRR BUILDER")
    lines.append(section_bar())
    lines.append("Rules: max 2/player · no duplicate combos · hit picks can qualify")
    add_group(lines, "💰 CORE PLAY", core_plays, "2-man")
    add_group(lines, "💰 BALANCED PLAY", balanced_plays, "3-man")
    add_group(lines, "💰 VALUE PLAY", value_plays, "5-man")
    return "\n".join(lines).rstrip()


# Global plate-appearance filter used across every output section.
# Smart strict rule: prefer recent PA when present, otherwise require a real season sample.
#
# BUGFIX: this previously read getattr(rec, "last30_pa", 0), but no field by
# that name exists anywhere on HitterRecord -- it always silently returned the
# default 0, so the "prefer recent PA" branch never fired for any player and
# this collapsed to season_pa-only filtering. l20pa_pa (a real, populated
# 20-PA window field used elsewhere in scoring) is the closest available
# substitute. Threshold scaled proportionally: 30/20 * 20 = 20 (i.e. the full
# 20-PA window counts as "recent enough"), rather than reusing 30 against a
# 20-PA field, which would make the recent-PA branch impossible to satisfy.
GLOBAL_MIN_RECENT_PA = 20
GLOBAL_MIN_SEASON_PA = 80

def passes_global_pa_filter(rec: HitterRecord) -> bool:
    recent_pa = safe_int(getattr(rec, "l20pa_pa", 0), 0)
    if recent_pa >= GLOBAL_MIN_RECENT_PA:
        return True
    return safe_int(getattr(rec, "season_pa", 0), 0) >= GLOBAL_MIN_SEASON_PA

def apply_global_pa_filter(rows: List[HitterRecord]) -> List[HitterRecord]:
    return [r for r in rows if passes_global_pa_filter(r)]

def render_game_block(game: Dict[str, Any], hitters: List[HitterRecord]) -> str:

    if not hitters:
        away = normalize_team_abbr(game.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "AWAY"))
        home = normalize_team_abbr(game.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "HOME"))
        return f"{away} @ {home} ({game.get('gameDate', '')})\nNo lineup data yet.\n"

    used: set[int] = set()
    top_pick = pick_top(hitters, "overall_score", 1)[0]
    used.add(top_pick.player_id)

    hr_pick = pick_top_min_pa(hitters, "hr_score", 1, 15, used)[0] if len(hitters) > 1 else top_pick
    used.add(hr_pick.player_id)

    # Trimmed from 2-per-category to exactly 1 each (2026-07-25, per request):
    # every game block should show exactly one Top/HR/Hit/HRR/Base pick, not
    # two Hit and two HRR picks.
    hit_picks = pick_top(hitters, "hit_score", 1, used)
    used.update(h.player_id for h in hit_picks)

    hrr_picks = pick_top(hitters, "hrr_score", 1, used)
    used.update(h.player_id for h in hrr_picks)

    anchor = pick_top(hitters, "contact_score", 1, used)[0] if len(hitters) > len(used) else pick_top(hitters, "contact_score", 1)[0]

    try:
        # Phoenix (America/Phoenix) doesn't observe DST, so it's a stable
        # fixed UTC-7 = MST year-round -- matches the TODAY/slate-date logic
        # already used elsewhere in the bot, rather than a raw UTC stamp.
        game_dt_utc = dt.datetime.fromisoformat(game.get("gameDate", "").replace("Z", "+00:00"))
        if ZoneInfo is not None:
            stamp = game_dt_utc.astimezone(ZoneInfo("America/Phoenix")).strftime("%I:%M %p MST")
        else:
            stamp = game_dt_utc.strftime("%I:%M %p UTC")
    except Exception:
        stamp = game.get("gameDate", "")

    t = top_pick

    def _two_weak_pitches(rec: HitterRecord) -> str:
        # Top 2 pitches to attack: highest HR-allowed / hard-hit-allowed among
        # pitches he actually throws enough to matter (>=5% usage, >=5 BBE).
        arsenal = getattr(rec, "pitcher_pitch_arsenal_detail", []) or []
        meaningful = [p for p in arsenal if safe_float(p.get("usage_pct"), 0) >= 5.0 and safe_int(p.get("bbe_allowed"), 0) >= 5]
        if not meaningful:
            return "N/A"
        worst2 = sorted(
            meaningful,
            key=lambda p: (safe_int(p.get("hr_allowed"), 0), safe_float(p.get("hard_hit_rate_allowed"), 0)),
            reverse=True,
        )[:2]
        return " , ".join(
            f"{p.get('pitch_type', '?')} ({safe_int(p.get('hr_allowed'), 0)}HR {safe_float(p.get('hard_hit_rate_allowed'), 0) * 100:.0f}%HH)"
            for p in worst2
        )

    def _pitcher_lines(rec: HitterRecord) -> List[str]:
        # rec is a hitter FACING this pitcher, so rec.pitcher_* describes him.
        weak_side_txt = f" | {rec.pitcher_weak_side} Weak" if rec.pitcher_weak_side else ""
        hh_txt = ""
        if rec.pitcher_statcast_bbe > 0 and rec.pitcher_statcast_status == "ok":
            hh_txt = f" | HH {round(rec.pitcher_hardhit_allowed * 100)}%"
        trend = getattr(rec, "pitcher_trend_direction", "unknown")
        trend_txt = ""
        if trend == "worsening":
            trend_txt = " | 📉 Trending worse (target)"
        elif trend == "improving":
            trend_txt = " | 📈 Trending better"
        lines = [
            f"🎯 {rec.pitcher_team} SP {rec.pitcher_name} ({rec.pitcher_throws}){weak_side_txt} — {rec.pitcher_attack_tag} | HR/9 {rec.pitcher_hr9:.2f}{hh_txt}{trend_txt}",
            f"Weak pitches: {_two_weak_pitches(rec)}",
        ]
        if trend_txt and getattr(rec, "pitcher_trend_reason", ""):
            lines.append(f"  {rec.pitcher_trend_reason}")
        return lines

    # Show BOTH starting pitchers: away-team hitters face the home starter,
    # home-team hitters face the away starter -- so one rep from each side
    # of the lineup gives us both pitchers' full stat lines.
    away_team = hitters[0].team
    home_team = hitters[0].opponent
    away_side_hitters = [h for h in hitters if h.team == away_team]
    home_side_hitters = [h for h in hitters if h.team == home_team]
    away_rep = pick_top(away_side_hitters, "overall_score", 1)[0] if away_side_hitters else t
    home_rep = pick_top(home_side_hitters, "overall_score", 1)[0] if home_side_hitters else t

    target_lines: List[str] = []
    target_lines.extend(_pitcher_lines(home_rep))  # home-side hitters face the AWAY pitcher
    target_lines.extend(_pitcher_lines(away_rep))   # away-side hitters face the HOME pitcher

    # Weather: temp + wind direction only (per request), no usage-speed gate
    # on wind so direction still shows even on a calm day.
    weather_parts = []
    if t.weather_temp_f is not None:
        weather_parts.append(f"{round(t.weather_temp_f):.0f}F")
    if t.weather_wind_mph is not None:
        dir_label = getattr(t, "weather_wind_direction_label", "")
        dir_str = f" {dir_label}" if dir_label else ""
        weather_parts.append(f"Wind {round(t.weather_wind_mph):.0f}mph{dir_str}")
    if weather_parts:
        target_lines.append(" | ".join(weather_parts))

    header = f"⚾ {hitters[0].team} @ {hitters[0].opponent} ({stamp})"
    if not hitters[0].lineup_confirmed:
        header += "\n(UNCONFIRMED LINEUP)"

    blocks = [section_bar(), header, "\n".join(target_lines), section_bar(), ""]
    blocks.append(render_pick_line("🥇 Top Pick |", top_pick, "OVR", top_pick.overall_score))
    blocks.append("")
    blocks.append(render_pick_line("🎆 HR Pick |", hr_pick, "HR", hr_pick.hr_score))
    for hp in hit_picks:
        blocks.append("")
        blocks.append(render_pick_line("➕ Hit Pick |", hp, "HIT", hp.hit_score))
    for hp in hrr_picks:
        blocks.append("")
        blocks.append(render_pick_line("🔺 HRR Pick |", hp, "HRR", hp.hrr_score))
    blocks.append("")
    blocks.append(render_pick_line("🟢 Base Pick |", anchor, "CON", anchor.contact_score))
    return "\n".join(blocks)


def game_has_started(game: Dict[str, Any]) -> bool:
    """Return True only after first pitch/live/final so picks stay frozen for integrity."""
    status = game.get("status", {}) or {}
    abstract = str(status.get("abstractGameState", "")).lower()
    detailed = str(status.get("detailedState", "")).lower()
    code = str(status.get("statusCode", "")).upper()

    if abstract in {"live", "final"}:
        return True
    if code in {"I", "F", "O"}:
        return True
    if any(word in detailed for word in ("in progress", "final", "game over", "completed")):
        return True
    return False



def refresh_locked_lineup_status(client: MLBClient, game: Dict[str, Any], rows: List[HitterRecord]) -> List[HitterRecord]:
    """Keep locked picks, but refresh lineup status for the website display.

    Game lock should protect the actual selected players/picks after a game starts.
    It should NOT keep showing Pending once MLB has the real lineup or the game is final.
    This only updates row.lineup_confirmed; it does not rebuild or replace any player picks.
    """
    if not rows:
        return rows

    try:
        live = client.live_game(safe_int(game.get("gamePk"), 0))
        game_data = live.get("gameData", {}) or {}
        boxscore = live.get("liveData", {}).get("boxscore", {}) or {}
        teams_box = boxscore.get("teams", {}) or {}

        away_abbr = normalize_team_abbr(((game_data.get("teams", {}) or {}).get("away", {}) or {}).get("abbreviation", ""))
        home_abbr = normalize_team_abbr(((game_data.get("teams", {}) or {}).get("home", {}) or {}).get("abbreviation", ""))

        away_confirmed = bool(extract_lineup(teams_box.get("away", {}) or {}))
        home_confirmed = bool(extract_lineup(teams_box.get("home", {}) or {}))

        status = game_data.get("status", {}) or {}
        abstract = str(status.get("abstractGameState", "")).lower()
        detailed = str(status.get("detailedState", "")).lower()
        game_final = abstract == "final" or "final" in detailed

        confirmed_by_team = {
            away_abbr: away_confirmed or game_final,
            home_abbr: home_confirmed or game_final,
        }

        updated = 0
        for r in rows:
            if confirmed_by_team.get(normalize_team_abbr(str(r.team)), False) and not r.lineup_confirmed:
                r.lineup_confirmed = True
                updated += 1

        if updated:
            print(f"🔓 Updated locked lineup status: {updated} rows now confirmed for display.", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"⚠️ Could not refresh locked lineup status: {exc}", file=sys.stderr, flush=True)

    return rows



# ── WEBSITE WEATHER PAYLOAD NORMALIZER ──────────────────────────────────────
def _weather_num(v, default=None):
    try:
        if v is None or v == "" or v == "--":
            return default
        return float(v)
    except Exception:
        return default


def _weather_env_pct(row: Dict[str, Any]) -> int:
    """Small website-facing HR carry effect, in whole percent points."""
    temp = _weather_num(row.get("weather_temp_f"), None)
    wind = _weather_num(row.get("weather_wind_mph"), 0.0) or 0.0
    wind_boost = _weather_num(row.get("weather_wind_boost"), 0.0) or 0.0
    roof = str(row.get("roof") or "open").lower()
    if roof in {"closed", "dome"}:
        return 0
    env = 0.0
    if temp is not None:
        if temp >= 85:
            env += 0.06
        elif temp >= 75:
            env += 0.03
        elif temp <= 50:
            env -= 0.03
    # directional wind already scaled in fetch_weather; if unavailable, use light generic wind bump only
    env += wind_boost
    if wind_boost == 0 and wind >= 12:
        env += 0.015
    return int(round(env * 100))


def _weather_display(row: Dict[str, Any]) -> str:
    roof = str(row.get("roof") or "open").strip() or "open"
    temp = _weather_num(row.get("weather_temp_f"), None)
    wind = _weather_num(row.get("weather_wind_mph"), None)
    direction = str(row.get("weather_wind_direction_label") or "").strip()
    pct = _weather_env_pct(row)
    parts = [roof.capitalize()]
    if temp is not None:
        parts.append(f"{round(temp):.0f}°F")
    if wind is not None:
        wind_txt = f"{round(wind):.0f} mph"
        if direction:
            wind_txt += f" {direction}"
        parts.append(wind_txt)
    if temp is None and wind is None:
        parts.append("weather data missing")
    sign = "+" if pct > 0 else ""
    parts.append(f"{sign}{pct}% HR")
    return " · ".join(parts)


def mark_hidden_hr_value(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flag the hitters the v2 rewrite promoted hardest — the actual "hidden value".

    WHY THIS MOVED OUT OF THE PER-HITTER PATH (2026-08-11).

    hidden_hr_value was False on ALL 2,534 archived rows carrying it and on
    178 of 178 rows of a live slate. The badge is rendered in three site
    components and had never once appeared.

    The gate was:

        old_hr_score < 42 and hr_score_new >= 48 and not trap_flag and (...)

    which asks for a hitter the OLD model scored under 42 and the NEW model
    scores 48+. That was written when the two scores shared a scale. They no
    longer do: across a real slate hr_score_v2 runs a median 22.8 points BELOW
    hr_score_old. So the gate wants a jump upward across a seam that the
    rewrite moved the other way, and the window is empty — 0 of 178 rows.

    Rescaling the constants does not fix it either, because hr_score_old is
    itself degenerate: seven different hitters on one slate share exactly
    55.6, so any absolute cut on it lumps Ohtani and Harper in with the
    genuinely overlooked bats and defeats the point of the flag.

    RANK IS SCALE-FREE, which is why this is now a slate pass instead of a
    per-hitter test. "Hidden" means the new model ranks him much better than
    the old one did AND he now ranks well enough to actually back:

        moved up >= 30 places, and sits in the top 30 of the v2 ranking

    On a 178-hitter slate that is ~4.5% and it lands on Coby Mayo, Hunter
    Goodman, Jake McCarthy, Max Muncy, Mickey Moniak — overlooked names with
    strong ISO, which is exactly what the badge was for, rather than the stars
    every absolute threshold surfaced.

    Runs after all rows are scored; leaves any row missing either score alone.
    """
    scored = [r for r in rows
              if isinstance(r.get("hr_score_old"), (int, float))
              and isinstance(r.get("hr_score_v2"), (int, float))]
    if len(scored) < 20:            # too thin a slate to rank meaningfully
        return rows
    old_rank = {id(r): i + 1 for i, r in enumerate(sorted(scored, key=lambda r: -r["hr_score_old"]))}
    new_rank = {id(r): i + 1 for i, r in enumerate(sorted(scored, key=lambda r: -r["hr_score_v2"]))}
    for r in scored:
        moved = old_rank[id(r)] - new_rank[id(r)]
        hidden = bool(moved >= 30 and new_rank[id(r)] <= 30 and not r.get("trap_flag"))
        if hidden:
            r["hidden_hr_value"] = True
            r["hidden_value_reason"] = (
                f"Underrated play — the current model ranks him #{new_rank[id(r)]} tonight, "
                f"{moved} places above where the legacy score had him"
            )
            tags = r.get("top_board_tags")
            if isinstance(tags, list) and "Hidden HR Value" not in tags:
                tags.append("Hidden HR Value")
    return rows


def enrich_weather_payload_for_website(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add flat aliases + nested weather object so every dashboard section can read weather.

    The original weather_* fields stay untouched. This just adds website-friendly keys:
      temp_f, wind_mph, wind_deg, wind_direction_label, environment_boost,
      weather_label, weather_hr_effect_pct, weather.has_data, weather.display
    """
    enriched = []
    for row in rows:
        r = dict(row)
        temp = _weather_num(r.get("weather_temp_f"), None)
        wind = _weather_num(r.get("weather_wind_mph"), None)
        wind_deg = _weather_num(r.get("weather_wind_deg"), None)
        wind_boost = _weather_num(r.get("weather_wind_boost"), 0.0) or 0.0
        pct = _weather_env_pct(r)
        display = _weather_display(r)
        source = str(r.get("weather_source") or "none").strip().lower()
        roof_lc = str(r.get("roof") or "open").strip().lower()

        # IMPORTANT: roof=open/retractable by itself is not weather data.
        # Mark true only when an actual weather source returned temp/wind,
        # or when the park is a closed/dome environment that does not need wind.
        has_real_weather = (source not in {"", "none"}) and (temp is not None or wind is not None)
        has_roof_only_weather = roof_lc in {"closed", "dome"}
        has_data = bool(has_real_weather or has_roof_only_weather)

        # Flat aliases for dashboard fallback readers
        r["temp_f"] = temp
        r["wind_mph"] = wind
        r["wind_deg"] = wind_deg
        r["wind_direction_label"] = r.get("weather_wind_direction_label") or ""

        # ── 0 IS NOT "NO WEATHER" (2026-08-11) ────────────────────────────
        #
        # Found by trying to test whether the model's home runs are
        # weather-fragile and getting an empty table back:
        # weather_hr_effect_pct sat on 2,369 archived rows and was ZERO on
        # every single one, while a live slate carries -2% to +8%.
        #
        # The cause is not the grader's whitelist (fixed separately) — it is
        # here. wind_boost and weather_wind_boost are dataclass fields
        # defaulting to 0.0, and _weather_env_pct returns 0 when there is
        # nothing to compute from. So on every night the weather fetch did not
        # land, this wrote a confident 0 that means "we never knew" and reads
        # identically to "calm night, neutral conditions". weather_temp_f is
        # populated on only 1,191 of 5,766 archived rows, so that is most
        # nights.
        #
        # has_data already exists to make the distinction, but a consumer that
        # reads the NUMBER without checking the FLAG — which is every
        # consumer, because a number is the obvious thing to read — silently
        # treats unknown as neutral. That is the same trap as Number(null)
        # being 0 rather than NaN, and it cost this archive its entire weather
        # history.
        #
        # None serializes to JSON null, which no analysis will mistake for a
        # measurement. Callers that want a number still have has_data to gate
        # on, and the site's own reader already checks weather_has_data first.
        if has_data:
            r["wind_boost"] = wind_boost
            r["environment_boost"] = round(pct / 100.0, 4)
            r["weather_hr_effect_pct"] = pct
        else:
            r["wind_boost"] = None
            r["environment_boost"] = None
            r["weather_hr_effect_pct"] = None
        r["weather_label"] = display
        r["weather_has_data"] = has_data

        # V22/V28: flat pitcher arsenal aliases for the dashboard.
        # Values are percentages 0-100. Metadata keys are blocked.
        pitch_mix_blob = r.get("pitcher_pitch_mix") if isinstance(r.get("pitcher_pitch_mix"), dict) else {}

        raw_usage = (
            r.get("pitcher_pitch_usage")
            or r.get("pitcher_pitch_usage_pct")
            or r.get("pitcher_arsenal")
            or pitch_mix_blob.get("pitcher_pitch_usage")
            or pitch_mix_blob.get("pitcher_pitch_usage_pct")
            or pitch_mix_blob.get("pitcher_arsenal")
            or pitch_mix_blob.get("usage")
            or pitch_mix_blob.get("mix")
            or {}
        )

        allowed_pitch_codes = {
            "FF", "SI", "FC", "SL", "ST", "CU", "KC", "CH", "FS",
            "FO", "KN", "EP", "CS", "SC", "SV", "FA"
        }

        # x100 BUG FIX (2026-08-22). This decided fraction-vs-percentage ONE
        # VALUE AT A TIME: `if 0 < val <= 1: val *= 100`. A pitch thrown 1% or
        # less of the time is also <= 1, so it was multiplied by a hundred and
        # became the biggest number in the arsenal. 14 of 59 published starters
        # were wrong and 7 summed past 150%: Tanner Gordon's SI 1.0% read as
        # 100.0 while his own primary_mix string listed no SI at all, and the
        # site's Arsenal panel showed it. A usage dict is fractions or
        # percentages AS A WHOLE - decide once from the total. Percentages sum
        # to ~100, fractions to ~1, three orders of magnitude apart.
        pitcher_usage = {}
        _cand = {}
        if isinstance(raw_usage, dict):
            for k, v in raw_usage.items():
                code = str(k).upper().strip()
                if code not in allowed_pitch_codes:
                    continue
                try:
                    val = float(v)
                except Exception:
                    continue
                if val > 0:
                    _cand[code] = val

            _total = sum(_cand.values())
            _scale = 100.0 if 0 < _total <= 1.5 else 1.0
            for code, val in _cand.items():
                out_val = val * _scale
                if 0 < out_val <= 100:
                    pitcher_usage[code] = round(out_val, 1)

        pitcher_usage = dict(sorted(pitcher_usage.items(), key=lambda x: x[1], reverse=True))

        summary = (
            pitch_mix_blob.get("pitcher_arsenal_summary")
            or pitch_mix_blob.get("primary_mix")
            or r.get("pitcher_primary_mix")
            or (" | ".join([f"{k} {v:.0f}%" for k, v in list(pitcher_usage.items())[:4]]) if pitcher_usage else "Mix N/A")
        )

        status = pitch_mix_blob.get("pitcher_pitch_mix_status") or pitch_mix_blob.get("status") or ("ok" if pitcher_usage else "missing")
        sample = safe_int(
            pitch_mix_blob.get("pitcher_pitch_mix_sample")
            or pitch_mix_blob.get("sample_pitches")
            or r.get("pitcher_pitch_mix_sample"),
            0
        )

        r["pitcher_pitch_usage"] = pitcher_usage
        r["pitcher_pitch_usage_pct"] = pitcher_usage
        r["pitcher_arsenal"] = pitcher_usage
        r["pitcher_arsenal_summary"] = summary
        r["pitcher_primary_mix"] = summary
        r["pitcher_pitch_mix_status"] = status
        r["pitcher_pitch_mix_sample"] = sample
        r["pitcher_pitch_mix_debug"] = pitch_mix_blob.get("debug_message", "")
        r["pitcher_pitch_mix_source_window"] = pitch_mix_blob.get("source_window", "")
        r["pitcher_pitch_type_summary"] = [
            {"pitch_type": k, "pitch_code": k, "usage_pct": v, "usage": v, "count": None}
            for k, v in pitcher_usage.items()
        ]

        # Nested object for future components
        r["weather"] = {
            "temp_f": temp,
            "wind_mph": wind,
            "wind_deg": wind_deg,
            "roof": r.get("roof") or "open",
            "humidity": r.get("weather_humidity"),
            "feels_like_f": r.get("weather_feels_like_f"),
            "precip_chance": r.get("weather_precip_chance"),
            "wind_direction_label": r.get("weather_wind_direction_label") or "",
            "wind_boost": wind_boost,
            "environment_boost": round(pct / 100.0, 4),
            "hr_effect_pct": pct,
            "source": r.get("weather_source") or "none",
            "display": display,
            "has_data": r["weather_has_data"],
        }
        enriched.append(r)
    return enriched
# ─────────────────────────────────────────────────────────────────────────────




def write_pitch_usage_debug_file(rows_payload: List[Dict[str, Any]], slate_date: dt.date, slate_label: str) -> None:
    """Write public/data/pitch_usage_debug.json so we can verify cloud pitcher usage."""
    try:
        by_pitcher = {}
        for row in rows_payload:
            pitcher = str(row.get("pitcher_name") or "Unknown")
            pid_key = f"{row.get('pitcher_team', '')}:{pitcher}"

            usage = row.get("pitcher_pitch_usage") if isinstance(row.get("pitcher_pitch_usage"), dict) else {}
            status = row.get("pitcher_pitch_mix_status") or "missing"
            sample = safe_int(row.get("pitcher_pitch_mix_sample"), 0)
            summary = row.get("pitcher_arsenal_summary") or row.get("pitcher_primary_mix") or "Mix N/A"

            if pid_key not in by_pitcher:
                by_pitcher[pid_key] = {
                    "pitcher": pitcher,
                    "team": row.get("pitcher_team"),
                    "throws": row.get("pitcher_throws"),
                    "status": status,
                    "sample": sample,
                    "summary": summary,
                    "usage": usage,
                    "debug": row.get("pitcher_pitch_mix_debug", ""),
                    "source_window": row.get("pitcher_pitch_mix_source_window", ""),
                    "rows_using_pitcher": 0,
                }

            by_pitcher[pid_key]["rows_using_pitcher"] += 1

        pitchers = list(by_pitcher.values())
        with_usage = [p for p in pitchers if p.get("usage")]
        missing = [p for p in pitchers if not p.get("usage")]

        payload = {
            "schema": "pitch_usage_debug_v1",
            "date": slate_date.isoformat(),
            "role": slate_label,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "pybaseball_available": bool(statcast_pitcher is not None),
            "checked_pitchers": len(pitchers),
            "with_usage": len(with_usage),
            "missing_usage": len(missing),
            "pitchers": pitchers,
        }

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "pitch_usage_debug.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        data_dir = DASHBOARD_REPO / "public" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "pitch_usage_debug.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        print(
            f"✅ Pitch usage debug: {len(with_usage)}/{len(pitchers)} pitchers loaded usage",
            file=sys.stderr
        )
    except Exception as exc:
        print(f"⚠️ Pitch usage debug write failed: {exc}", file=sys.stderr)


# ── HR/PA + PA/HR V30 ENRICHMENT ────────────────────────────────────────────
def _v30_safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "--", "—"):
            return default
        return float(value)
    except Exception:
        return default


def _v30_safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "--", "—"):
            return default
        return int(float(value))
    except Exception:
        return default


def _hr_pa_tier(hr_per_pa: float) -> str:
    if hr_per_pa >= 0.045:
        return "Elite"
    if hr_per_pa >= 0.035:
        return "Strong"
    if hr_per_pa >= 0.025:
        return "Playable"
    if hr_per_pa > 0:
        return "Low"
    return "Unknown"


def _hr_due_tag(row: Dict[str, Any]) -> str:
    """PropFinder-style due label based on power rate + recent HR drought proxy.

    We usually do not have exact PA since last HR inside the daily slate file, so this
    uses L5/L20PA fields as a stable daily-bot proxy until the pair-history cache
    builder starts exporting exact last-HR dates.
    """
    hr_per_pa = _v30_safe_float(row.get("hr_per_pa"), 0.0)
    l20pa_pa = _v30_safe_int(row.get("l20pa_pa"), 0)
    l20pa_hr = _v30_safe_int(row.get("l20pa_hr"), 0)
    l5_hr = _v30_safe_int(row.get("last5_hr"), 0)
    ihr = _v30_safe_float(row.get("recent_ideal_hr_contact"), 0.0)
    hrw = _v30_safe_float(row.get("hrw_score"), 0.0)
    if hr_per_pa >= 0.045 and l20pa_pa >= 12 and l20pa_hr == 0 and ihr >= 0.10:
        return "Due Elite Power"
    if hr_per_pa >= 0.035 and l5_hr == 0 and hrw >= 55:
        return "Due Power"
    if l5_hr >= 2:
        return "Hot HR Form"
    if l5_hr >= 1:
        return "Recent HR"
    return "Neutral"


def enrich_signal_pills_and_best_non_hr(rows_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Adds two new fields to every player row:
      signal_pills  — up to 3 short display tags always including HRW, plus 2 best context signals
      best_non_hr_category — for Avoid HR players: which tab they actually score well in
                             values: 'hits' | 'hrr' | 'contact' | 'none'
    """
    def _sf(row, key, default=0.0):
        try:
            v = row.get(key, default)
            return float(v) if v not in (None, "", "--") else default
        except Exception:
            return default

    def _si(row, key, default=0):
        try:
            v = row.get(key, default)
            return int(v) if v not in (None, "", "--") else default
        except Exception:
            return default

    out: List[Dict[str, Any]] = []
    for row in rows_payload or []:
        if not isinstance(row, dict):
            continue
        x = dict(row)

        # ── SIGNAL PILLS ──────────────────────────────────────────────────
        pills: List[str] = []

        # Pill 1: HRW — always shown, zone-aware label
        hrw = _sf(x, "hrw_score", 0.0)
        hrw_zone = x.get("hrw_zone", "")
        if hrw_zone == "strong_capped":
            pills.append(f"HRW {hrw:.0f} \U0001f525")
        elif hrw_zone == "volatile_hot":
            pills.append(f"HRW {hrw:.0f} \u26a1")
        elif hrw_zone == "sweet_spot":
            pills.append(f"HRW {hrw:.0f} \u2714\ufe0f")
        elif hrw_zone == "cold":
            pills.append(f"HRW {hrw:.0f} \u2744\ufe0f")
        else:
            pills.append(f"HRW {hrw:.0f}")

        # Pill 2: best form signal (L5 HR > recent barrel > IHR)
        l5_hr = _si(x, "last5_hr", 0)
        l7_hr = _si(x, "last7_hr", 0)
        recent_barrel = _sf(x, "recent_barrel_rate", 0.0)
        ihr = _sf(x, "recent_ideal_hr_contact", 0.0)
        bbe_power = _sf(x, "batted_ball_power_score", 0.0)

        if l5_hr >= 3:
            pills.append(f"L5: {l5_hr}HR")
        elif l5_hr >= 2:
            pills.append(f"L5: {l5_hr}HR")
        elif l7_hr >= 2:
            pills.append(f"L7: {l7_hr}HR")
        elif recent_barrel >= 0.14:
            pills.append(f"Brl {recent_barrel*100:.0f}%")
        elif ihr >= 0.18:
            pills.append(f"IHR {ihr*100:.0f}%")
        elif bbe_power >= 80:
            pills.append("BBE+")

        # Pill 3: matchup / pitcher signal
        pmix_note = (x.get("pitch_mix_note") or "").strip()
        pmix_score = _sf(x, "pitch_mix_score", 50.0)
        pitcher_hr9 = _sf(x, "pitcher_hr9", 1.0)
        weak_pitcher = bool(x.get("weak_pitcher_flag"))
        mistake_match = bool(x.get("pitcher_mistake_match"))
        lineup_spot = _si(x, "lineup_spot", 9)
        wk_side_score = _sf(x, "pitcher_weak_side_score", 0.0)
        bats = x.get("bats", "")
        weak_side = x.get("pitcher_weak_side", "")
        _esd = effective_side(bats, x.get("pitcher_throws", ""))
        on_weak_side = (weak_side == "LHB" and _esd == "L") or (weak_side == "RHB" and _esd == "R")

        if on_weak_side and wk_side_score >= 55:
            side_lbl = "LHB" if _esd == "L" else "RHB"
            pills.append(f"Weak vs {side_lbl}")
        elif mistake_match and pmix_score >= 75:
            crush = pmix_note.replace("Crush ", "").split()[0] if pmix_note.startswith("Crush") else ""
            pills.append(f"PMix {crush}" if crush else "PMix✓")
        elif pitcher_hr9 >= 1.8:
            pills.append(f"P-HR9 {pitcher_hr9:.1f}")
        elif weak_pitcher and lineup_spot <= 2:
            pills.append(f"#{lineup_spot} Weak P")
        elif pmix_score >= 85:
            pills.append("PMix Elite")
        elif lineup_spot <= 2:
            pills.append(f"#{lineup_spot} Lineup")

        x["signal_pills"] = pills[:3]

        # ── BEST NON-HR CATEGORY (Avoid HR players only) ─────────────────
        true_avoid = bool(x.get("true_avoid_hr", False))
        if true_avoid:
            hit_score = _sf(x, "hit_score", 0.0)
            hrr_score = _sf(x, "hrr_score", 0.0)
            contact_score = _sf(x, "contact_score", 0.0)
            candidates = [
                ("hits", hit_score),
                ("hrr", hrr_score),
                ("contact", contact_score),
            ]
            best_cat, best_val = max(candidates, key=lambda t: t[1])
            if best_val >= 55:
                x["best_non_hr_category"] = best_cat
            else:
                x["best_non_hr_category"] = "none"
        else:
            x["best_non_hr_category"] = ""

        out.append(x)
    return out


def enrich_hr_pa_payload(rows_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add PropFinder-style HR/PA and PA/HR fields to every player row."""
    out: List[Dict[str, Any]] = []
    for row in rows_payload or []:
        if not isinstance(row, dict):
            continue
        x = dict(row)
        pa = _v30_safe_float(x.get("season_pa") or x.get("pa") or x.get("PA"), 0.0)
        hr = _v30_safe_float(x.get("season_hr") or x.get("hr") or x.get("HR"), 0.0)
        hr_per_pa = (hr / pa) if pa > 0 else 0.0
        pa_per_hr = (pa / hr) if hr > 0 else None
        x["hr_per_pa"] = round(hr_per_pa, 4)
        x["pa_per_hr"] = round(pa_per_hr, 1) if pa_per_hr else None
        raw_game = 1.0 - (1.0 - max(0.0, min(hr_per_pa, 0.15))) ** 4.15
        sample_weight = pa / (pa + 120.0)
        season_game_prob = max(0.025, min(0.40, sample_weight * raw_game + (1.0 - sample_weight) * 0.11))
        x["season_hr_game_probability"] = round(season_game_prob, 4)
        x["hr_probability_source"] = "season HR/PA, shrunk; not calibrated model probability"
        x["hr_pa_tier"] = _hr_pa_tier(hr_per_pa)
        # A current-form proxy until exact PA-since-last-HR is produced by the history cache builder.
        recent_pa = _v30_safe_int(x.get("l20pa_pa"), 0) or max(0, _v30_safe_int(x.get("recent_350_den"), 0))
        recent_hr = _v30_safe_int(x.get("l20pa_hr"), 0) or _v30_safe_int(x.get("last5_hr"), 0)
        x["recent_pa_window"] = recent_pa
        x["recent_hr_window"] = recent_hr
        x["expected_hrs_recent_window"] = round(recent_pa * hr_per_pa, 2) if recent_pa else 0.0
        x["hr_due_score"] = round(max(0.0, (recent_pa * hr_per_pa) - recent_hr) * 25.0, 1) if recent_pa else 0.0
        x["hr_due_tag"] = _hr_due_tag(x)
        out.append(x)
    return out

# REMOVED per audit (2026-06-27): System 1 (the old _pb_* JSON pair-
# builder formulas) deleted entirely. Pair-builder JSON output is now
# produced by System 2 (build_pair_sections / build_structured_pairs /
# build_structured_pool), the same logic that already powered the .txt
# report -- see build_pair_sections's json_payload return value and the
# write site at the end of main().



def sync_pair_builder_v2_to_website_repo(slate_date: dt.date, dated_path: Path, latest_path: Path) -> None:
    try:
        data_dir = DASHBOARD_REPO / "public" / "data"
        if not data_dir.parent.exists():
            print(f"⚠️ Pair Builder sync skipped; website repo not found at {DASHBOARD_REPO}", file=sys.stderr)
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        current_dir = data_dir / "current"
        current_dir.mkdir(parents=True, exist_ok=True)
        for src, dest in [
            (dated_path, data_dir / dated_path.name),
            (latest_path, data_dir / "pair_builder_latest.json"),
            (latest_path, current_dir / "pair_builder_latest.json"),
        ]:
            shutil.copy2(src, dest)
            print(f"📁 Pair Builder copy: {src.name} → {dest}", file=sys.stderr)
        idx_path = data_dir / "index.json"
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {}
        except Exception:
            idx = {}
        if not isinstance(idx, dict): idx = {}
        pair_files = idx.get("pair_builder", [])
        if not isinstance(pair_files, list): pair_files = []
        entry = {"date": slate_date.isoformat(), "path": f"/data/{dated_path.name}", "latest": "/data/pair_builder_latest.json"}
        pair_files = [entry] + [x for x in pair_files if not (isinstance(x, dict) and x.get("date") == slate_date.isoformat())]
        idx["pair_builder"] = pair_files[:40]
        idx["pair_builder_latest"] = "/data/pair_builder_latest.json"
        idx_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")
        print("✅ Pair Builder V2 synced to website data folder.", file=sys.stderr)
    except Exception as exc:
        print(f"⚠️ Pair Builder sync failed: {exc}", file=sys.stderr)
# ─────────────────────────────────────────────────────────────────────────────


def load_locked_rows_by_game(json_path: Path) -> Dict[int, List[HitterRecord]]:
    """Load previously saved rows so started games can reuse the original saved picks."""
    if not json_path.exists():
        return {}
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    allowed = set(HitterRecord.__dataclass_fields__.keys())
    defaults = {
        name: field.default
        for name, field in HitterRecord.__dataclass_fields__.items()
        if field.default is not dataclasses.MISSING
    }

    by_game: Dict[int, List[HitterRecord]] = {}
    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean = {k: row[k] for k in allowed if k in row}
        for k, v in defaults.items():
            clean.setdefault(k, v)
        try:
            rec = HitterRecord(**clean)
        except Exception:
            continue
        by_game.setdefault(int(rec.game_pk), []).append(rec)
    return by_game


# ── MODEL FOUNDATION: run metadata (Task 2) ──────────────────────────────────
#
# One run_id per bot EXECUTION, generated once here at the top of main() --
# not per hitter, not per game. Thirteen scheduled executions a day (see
# .github/workflows/today.yml) each get exactly one run_id, and every row
# that execution scores carries it (see the stamping loop in main()).

def _current_git_sha() -> str:
    """Commit identity for this run. Must mean exactly one thing: the source
    commit that executed mlb_dashboard.py and produced this prediction log.

    PROVENANCE FIX (2026-08-21): this used to prefer the GITHUB_SHA env var
    over `git rev-parse HEAD`, on the assumption that GITHUB_SHA always
    names the commit actually checked out. It doesn't, reliably. today.yml's
    checkout step passes an explicit branch name (`ref: main`), and
    actions/checkout resolves an explicit branch ref by fetching and
    checking out whatever that branch's CURRENT tip is at the moment the
    checkout step runs -- not GITHUB_SHA. GITHUB_SHA for a schedule or
    workflow_dispatch run is fixed at event-trigger time, before the runner
    has even started. today.yml fires up to ~13x/day, so if a push lands
    main in the (usually short, but non-zero -- runner queue/provisioning)
    gap between "run triggered" and "checkout step executes", the two
    diverge: GITHUB_SHA still names the OLDER pre-push commit while the
    working tree -- and everything that actually ran -- is the NEWER one.
    That is a real mismatch in the direction that matters: it would make
    run_meta.git_sha understate what code produced the row.
    `git rev-parse HEAD`, run in the working tree that is executing this
    process right now, cannot have that race -- it can only ever answer
    "what is actually checked out here," which is what this field must
    mean. GITHUB_SHA is kept only as a fallback for a context where this
    isn't a real git checkout at all (e.g. a stripped deployment). Never
    raises -- provenance is nice-to-have, not worth failing a slate."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha:
        return sha
    return "unknown"


# The exact set of functions whose AST structure determines the HR
# fingerprint's "formula_structure_sha256" layer -- see config_fingerprint.py
# for why this can't be MODEL_WEIGHTS alone. apply_model_v2_layers is where
# hr_raw/hr_score_new/the corrected/live hr_score are actually computed
# (including every inline trap multiplier, gate bonus/penalty, and the
# 2026-07-31 shadow re-anchor); minmax_norm/_hr2_clip/_spot_damage_for_batter
# are the shared scaling/clip/pitcher-damage helpers it calls whose own
# internals could change hr_score without changing apply_model_v2_layers's
# own text at all. Literal list, fixed order -- not derived/discovered, so a
# future function added to the HR path must be added here by hand (same
# "declared, not derived" honesty tradeoff model_registry.py already makes
# about MODEL_VERSIONS; see that module's own docstring).
#
# COVERAGE FIX (2026-08-21, Sol audit #2 finding #2): calculate_pitch_mix_fit
# and score_hitter were missing from this list even though both directly move
# hr_score -- calculate_pitch_mix_fit populates h.pitch_mix_score and
# h.pitch_type_match_score, which apply_model_v2_layers weights at 0.06+0.05
# (11% of hr_blend) into hr_raw; score_hitter computes h.weak_spot_bonus (a
# six-tier ladder up to +3.4 points, also added into hr_raw). Before this fix,
# tuning either function's thresholds changed every row's hr_score while
# hr_config_hash() stayed byte-identical -- exactly the silent-drift failure
# mode this module's own docstring says it exists to catch.
#
# score_hitter is ~400 lines and computes plenty besides weak_spot_bonus
# (spot/zone damage labels, HRW inputs, etc.) -- adding the whole function is
# deliberately over-inclusive, the same documented tradeoff already accepted
# for apply_model_v2_layers's sibling markets (see this file's own "KNOWN,
# DOCUMENTED SCOPE LIMIT" section in config_fingerprint.py): an occasional
# false "config changed" flag on an unrelated score_hitter edit is a two-
# minute check; missing a real weak_spot_bonus tuning change is a wrong
# model-evaluation decision. Surgically extracting just the six-tier ladder
# would be a scoring-code refactor, explicitly out of scope for a
# provenance-only change.
#
# COVERAGE FIX ROUND 2 (2026-08-21, quick review of the finding-#2 fix itself):
# compute_damage_conversion_v31 and apply_decision_engine_v31 had the exact
# same gap. compute_damage_conversion_v31 blends five weighted terms
# (0.30/0.24/0.22/0.14/0.10) plus several literal penalty branches into
# h.damage_conversion_score, which apply_model_v2_layers reads into hr_raw at
# a weight comparable to the two functions above (see MODEL_WEIGHTS'
# damage_conversion_score entry). apply_model_v2_layers's own source only
# contains the *call site* `compute_damage_conversion_v31(h)` / the decision
# engine's call site -- function_structure_hash() hashes each named function's
# own ast.dump(), not its callees' bodies, so a callee's internals can change
# forever without moving apply_model_v2_layers's hash. Tuning either
# function's weights/thresholds silently changed hr_score while
# hr_config_hash() stayed identical -- the same failure mode finding #2 was
# supposed to close, just one layer deeper.
_HR_CONFIG_FORMULA_FUNCS = (
    apply_model_v2_layers,
    minmax_norm,
    _hr2_clip,
    _spot_damage_for_batter,
    calculate_pitch_mix_fit,
    score_hitter,
    compute_damage_conversion_v31,
    apply_decision_engine_v31,
    # 2026-08-22: the pitch-match missing-data sentinel policy lives here --
    # same only-called-not-hashed gap the quick review closed for
    # compute_damage_conversion_v31/apply_decision_engine_v31; a future edit
    # to this helper must move the hash.
    _pitch_match_term,
    # 2026-08-23: small-sample shrinkage policy (the Veen bug). K/league
    # constants live in MODEL_WEIGHTS-adjacent module scope but the POLICY is
    # this function's body -- hash it so an edit moves config_hash.
    shrink_to_league,
    # 2026-08-23: the meatball hand-split resolver. Its BODY is the policy --
    # the 150-pitch per-side floor, and the rule that a thin side falls back to
    # the arm's overall rate rather than publishing a rate built on ten
    # pitches. Both feed the 0.12 slice of pitcher_damage and the decision
    # engine's pitcher_meatball_high gate, so an edit here changes produced
    # numbers and must move the hash. Same only-called-not-hashed gap the two
    # entries above closed.
    meatball_vs_hand,
)
# The centralized HR tuning surface from MODEL_WEIGHTS (see that dict's own
# header comment). Order fixed for readability only -- canonical_json sorts
# keys before hashing, so dict declaration order here is not load-bearing.
_HR_CONFIG_WEIGHT_KEYS = ("hr_blend", "hr_gate_thresholds", "recency_multiplier")


def hr_config_hash() -> Optional[str]:
    """Deterministic fingerprint of the exact scoring configuration
    currently producing mlb_hr_v4's HR Score -- see config_fingerprint.py
    and docs/MODELS.md. None if the fingerprint module failed to import, or
    if hashing itself raises for any reason (e.g. a function in
    _HR_CONFIG_FORMULA_FUNCS can't be source-located) -- a provenance
    fingerprint failing must never block a scoring run; an honest missing
    hash is far better than a run crash or a fabricated one."""
    if CONFIG_FINGERPRINT is None:
        return None
    try:
        weights_subset = {k: MODEL_WEIGHTS[k] for k in _HR_CONFIG_WEIGHT_KEYS}
        return CONFIG_FINGERPRINT.hr_config_hash(weights_subset, _HR_CONFIG_FORMULA_FUNCS)
    except Exception as exc:
        print(f"⚠️ hr_config_hash() failed ({exc}); config_hash will be empty this run.", file=sys.stderr)
        return None


def _run_env_metadata() -> Dict[str, str]:
    """Environment fingerprint for this run -- closes the "which pybaseball
    version scored tonight" blind spot named in the roadmap review. Nothing
    here is required for the bot to run; a missing package version reads as
    "unknown" rather than raising."""
    env: Dict[str, str] = {"python": platform.python_version()}
    try:
        import importlib.metadata as _ilm
        env["pybaseball"] = _ilm.version("pybaseball")
    except Exception:
        env["pybaseball"] = "unknown"
    return env


def build_run_meta(slate_date: dt.date, slate_label: str, args: "argparse.Namespace") -> Dict[str, Any]:
    """One run identity per bot execution. See docs/MODELS.md for the
    model_version/schema_version policy this embeds.

    run_id shape: "{slate_date}.{HHMMSSZ}.{source}" -- seconds granularity
    (not minutes) because a manual re-run and a scheduled run landing in the
    same minute would otherwise collide; GitHub's own run id is included as
    the source so a run_id is traceable back to the exact Actions run (or
    "local-<8 hex>" off the runner for a Mac execution).
    """
    now = dt.datetime.now(dt.timezone.utc)
    gha_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    source = f"gha-{gha_run_id}" if gha_run_id else f"local-{uuid.uuid4().hex[:8]}"
    run_id = f"{slate_date.isoformat()}.{now.strftime('%H%M%S')}Z.{source}"

    if MODEL_REGISTRY is not None:
        model_family = MODEL_REGISTRY.MODEL_FAMILY
        model_versions = MODEL_REGISTRY.model_versions_snapshot()
        schema_version = MODEL_REGISTRY.SCHEMA_VERSION
    else:
        # Registry failed to import (see the try/except at module import
        # above). Keep the run_meta shape intact with honest placeholders
        # rather than crashing the run over a metadata-only failure.
        model_family = "unknown"
        model_versions = {}
        schema_version = 0

    # PROVENANCE (2026-08-21): config_hashes answers "what scoring
    # configuration was ACTUALLY in effect," independent of and in addition
    # to model_versions' declared "what we meant to run" -- see
    # config_fingerprint.py's module docstring. Only "hr" is computed for
    # now (Task 6+ scope note, not a design limit: extending to sibling
    # markets is straightforward repetition of the same pattern once
    # someone wants it costed against those markets' own inline-constant
    # surfaces). Explicit `{"hr": None}` rather than an absent key when
    # hashing fails, so "we tried and it failed" is distinguishable from
    # "this run predates config_hash existing" at analysis time.
    config_hashes = {"hr": hr_config_hash()}

    return {
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "slate_date": slate_date.isoformat(),
        # Not GITHUB_WORKFLOW's filename (Actions doesn't expose that) --
        # the workflow's display NAME, which is the closest honest signal
        # available; "local" off a Mac run, matching the roadmap's
        # today.yml | manual | local intent without overclaiming precision.
        "trigger": os.environ.get("GITHUB_WORKFLOW", "").strip() or "local",
        "git_sha": _current_git_sha(),
        "model_family": model_family,
        "model_versions": model_versions,
        "config_hashes": config_hashes,
        "schema_version": schema_version,
        "env": _run_env_metadata(),
    }


# ── MODEL FOUNDATION: prediction log (Task 4) ────────────────────────────────
#
# One JSONL file per bot execution: first line is this run's run_meta, then
# one compact record per scored hitter. Intentional schema (five blocks),
# not a dump of the 300+-field HitterRecord -- see docs/MODELS.md and
# DASH_ROADMAP.md §4 for the field roster this mirrors.
#
# Built from `rows_payload` (the fully-enriched dict form used to write
# today.json/tomorrow.json), not from the raw HitterRecord dataclasses,
# because several of the fields below (game_pick_role, hr_gate_flagged,
# best_bet_type) are only set as post-construction dict mutations later in
# main() -- they do not exist on the dataclass itself. Calling this after
# every rows_payload enrichment step has run is what makes those available.

def sync_model_foundation_outputs_to_website_repo(slate_label: str, run_meta: Dict[str, Any], prediction_log_path: Optional[Path]) -> None:
    """Publish this run's metadata alongside the slate.

    DEVIATION FROM DASH_ROADMAP.md §3 (documented, not silent): the roadmap
    describes embedding run_meta "in the today_slim.json envelope." That
    envelope does not exist -- inspection of this file shows today.json /
    today_slim.json are a bare JSON *list* of hitter rows (see
    write_json_and_aliases() and make_slim.py's slim_rows()/_rows_of(),
    both of which branch on `isinstance(payload, list)` as the primary
    case), not a dict with metadata keys alongside a rows array. Adding a
    top-level "run_meta" key would turn that list into a dict and break
    every consumer that assumes a list: make_slim.py's slate_is_real() /
    slim_file(), load_locked_rows_by_game() here, and the Streamlit app.
    That is exactly the kind of site/consumer risk this task is scoped to
    avoid.

    The additive-safe equivalent: write run_meta to its own small companion
    file, current/{today,tomorrow}_run_meta.json, next to the slate it
    describes -- new file, zero existing keys touched, zero risk to any
    current reader. (Every row in the slate itself also carries run_id +
    model_version directly, per Task 3, so a consumer that already has a
    row never needs this file to know what produced it; this file is for
    "what run made the CURRENT slate" at a glance.)
    """
    try:
        data_dir = DASHBOARD_REPO / "public" / "data"
        # BUG FIX (2026-08-21, found via a live production run that published
        # today_slim.json/today.txt -- proving the row-level stamping and
        # sync_breakdown_to_website_repo_v2() both worked -- but never
        # produced today_run_meta.json or a prediction_log file). The
        # original guard checked `data_dir.parent.exists()` i.e. whether
        # public/ already exists. In CI, public/data/current/ is created by
        # sync_breakdown_to_website_repo_v2() at the very end of main() --
        # AFTER this function runs -- so public/ never exists yet at this
        # point in a fresh checkout, and this guard silently returned early
        # on every single CI run without ever raising. Matching the sibling
        # sync_breakdown_to_website_repo_v2()'s guard instead: check for the
        # repo marker (_is_dashboard_repo), not for a directory this same
        # process tree is responsible for creating.
        if not _is_dashboard_repo(DASHBOARD_REPO):
            print(f"⚠️ Model-foundation sync skipped; website repo not found at {DASHBOARD_REPO}", file=sys.stderr)
            return
        current_dir = data_dir / "current"
        current_dir.mkdir(parents=True, exist_ok=True)

        run_meta_path = current_dir / f"{slate_label}_run_meta.json"
        run_meta_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        print(f"📁 Run meta: {run_meta_path.name} (run_id={run_meta.get('run_id')})", file=sys.stderr)

        if prediction_log_path is not None and prediction_log_path.exists():
            dest = current_dir / prediction_log_path.name
            shutil.copy2(prediction_log_path, dest)
            print(f"📁 Prediction log: {prediction_log_path.name} → {dest}", file=sys.stderr)
    except Exception as exc:
        print(f"⚠️ Model-foundation sync failed: {exc}", file=sys.stderr)


# Every SLOT_FIELDS key whose value comes from a live, no-as-of-date API pull
# and therefore moves as the night's games finish — the leak surface Sol
# confirmed at code level (claude/moonshot-sol-verdict-graded-archive-leak.md).
# Snapshotted per-row into prediction_log's slot_snapshot at generation time;
# grading overlays them from the LOCKED run. Scores are covered separately by
# the `scores` dict. Static/identity keys are deliberately not here.
PREGAME_SNAPSHOT_FIELDS = (
    "season_iso", "season_avg", "season_hr", "season_xbh", "season_pa",
    "season_k_rate", "season_bb_rate", "season_tb", "season_ab",
    "season_sb", "season_cs", "season_sb_attempt_rate",
    "last5_hits", "last5_hr", "last5_xbh", "last10_xbh", "last10_hr",
    "l20pa_hr", "l20pa_xbh", "games_since_last_hr",
    "avg_vs_lhp", "avg_vs_rhp", "iso_vs_lhp", "iso_vs_rhp",
    "recent_350_num", "recent_350_den", "recent_375_num",
    "recent_barrel_rate", "recent_fb_rate", "recent_ld_rate",
    "recent_gb_rate", "recent_popup_rate", "recent_ideal_hr_contact",
    "recent_pull_rate",
    "pitcher_hr_allowed", "pitcher_fb_rate", "pitcher_hr9",
    "pitcher_bb_pct", "pitcher_bb9",
    "weak_spot_flag", "weak_spot_reason", "trap_flag", "alt_look_tag",
    "best_bet_type", "best_bet_type_raw", "hr_gate_flagged", "true_avoid_hr",
    "best_non_hr_category", "final_hr_role", "top_board_tags",
    "pitch_type_match_flag", "pitch_type_match_score", "pitch_mix_score",
    "hidden_hr_value", "high_confidence_hr_flag",
    "multi_hit_score", "multi_hit_flag", "multi_hit_reason",
    # ── pitcher surface ──
    # THE PITCHER SURFACE (2026-08-23) — 98 pitcher fields ship on every slate
    # row and five reached the archive: hr_allowed, fb_rate, hr9, bb_pct, bb9.
    # Meatball %, barrel and EV allowed, HR/FB%, xHR/BBE, the handedness splits
    # and the attack score were computed nightly and thrown away, so no analysis
    # of the arm has ever been possible past two columns. Capture only — no
    # score reads these yet.
    "pitcher_meatball_pct", "pitcher_barrel_allowed", "pitcher_ev_allowed",
    "pitcher_hardhit_allowed", "pitcher_hr_fb_pct", "pitcher_xhr_bbe",
    "pitcher_xhr_allowed", "pitcher_hr9_vs_lhb", "pitcher_hr9_vs_rhb",
    "pitcher_attack_score", "pitcher_attack_tag", "pitcher_era",
    "pitcher_fip", "pitcher_whip", "pitcher_k9", "pitcher_k_rate",
    "pitcher_gb_rate", "pitcher_ld_rate", "pitcher_popup_rate",
    "pitcher_iso_against", "pitcher_slg_against", "pitcher_woba_against",
    "pitcher_babip", "pitcher_l3_era", "pitcher_l3_hr9", "pitcher_l3_whip",
    "pitcher_hr_luck", "pitcher_weak_side_score", "pitcher_weak_side_gap",
    "pitcher_zone_damage_score", "pitcher_spot_damage_score",
    "pitcher_pullair_allowed_pct", "pitcher_375_allowed",
    "pitcher_400_allowed", "pitcher_first_pitch_strike_pct",
    "pitcher_swstr_pct", "pitcher_whiff_pct", "pitcher_putaway_pct",
    "pitcher_statcast_bbe", "pitcher_fb_velo_delta", "pitcher_hr_vs_lhb",
    "pitcher_hr_vs_rhb", "pitcher_whip_vs_lhb", "pitcher_whip_vs_rhb",
    "pitcher_xbh_vs_lhb", "pitcher_xbh_vs_rhb", "pitcher_side_ops",
    "pitcher_side_slug", "pitcher_obp_against", "pitcher_ops_against",
    "pitcher_avg_against", "pitcher_low_k_flag", "pitcher_safe_flag",
    "pitcher_trend_direction", "pitcher_mistake_match",
    # ── batted-ball surface ──
    # THE BATTED-BALL SURFACE (2026-08-23) — of the 52 fields the site's filter
    # menu exposes, Max EV, Avg dist, Max dist, Air %, Pull %, Sweet-spot %, BBE
    # and Hard-hit % had ZERO archived rows and Avg EV had 13 nights of 28.
    # Launch angle is the batted-ball signal that separates homers (FB% z=+4.02
    # on 2,275 rows) and exit velocity is not (barrel z=-0.01 on 1,790); neither
    # statement can be properly checked until these are kept.
    "recent_ev", "recent_hard_hit_rate", "recent_sweet_spot_rate",
    "recent_avg_hr_distance", "recent_max_distance",
    "recent_distance_tracked", "season_max_distance", "recent_xwoba",
    "l20pa_fb_rate", "l20pa_barrel_rate", "l20pa_hard_hit_rate",
    "l20pa_ideal_hr_contact", "l20pa_bbe", "l20pa_pull_rate",
    "l25pa_air_rate", "l25pa_sweet_spot_rate", "l25pa_barrel_rate",
    "l25pa_gb_rate", "l25pa_ld_rate", "l25pa_popup_rate", "l25pa_fb_rate",
    "l25pa_avg_ev", "l25pa_bbe", "l25pa_hard_hit_rate", "l5_barrel_rate",
    "l5_hard_hit_rate", "l5_pull_rate", "l10_barrel_rate",
    "l10_hard_hit_rate", "l10_pull_rate", "xhr_bbe",
    # ── season rates ──
    # SEASON RATE FIELDS the filter menu offers and the archive never kept —
    # SLG, BABIP and HR per PA all had zero archived rows.
    "season_slg", "season_babip", "babip", "hr_per_pa",
    # ── park / air / bvp ──
    # PARK, AIR AND BATTER-VS-PITCHER. park_hr_factor was archived on 19 of 28
    # nights and its siblings on none. A field present on only part of the
    # archive is worse than a missing one: sorted across all nights, a 12-night
    # field's top quintile can read 88% purely because its quintiles are a date
    # filter.
    "park_dist_factor", "park_barrel_factor", "park_hardhit_factor",
    "park_hits_factor", "park_k_factor", "weather_hr_effect_pct",
    "weather_wind_boost", "wind_boost", "bvp_iso", "bvp_barrels",
    "bvp_hard_hit", "bvp_babip",
    # ── published sub-scores ──
    # PUBLISHED SCORES THE ARCHIVE DROPPED. Shown on the site, never gradeable.
    "longest_hr_score", "longest_hr_rank", "hr_due_score",
    "batted_ball_power_score", "damage_conversion_score",
    "matchup_power_score",
)


def build_prediction_log_lines(run_meta: Dict[str, Any], rows_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        from .hr_tiers import build_hr_overlay
    except ImportError:
        from hr_tiers import build_hr_overlay

    lines: List[Dict[str, Any]] = [run_meta]
    prediction_date = run_meta.get("slate_date", "")
    for row in rows_payload:
        if not isinstance(row, dict):
            continue
        # hr_shape_components is the dict apply_model_v2_layers already writes
        # onto the record (see its assignment in that function). Four of the
        # nine additions below live in there rather than as flat fields, so it
        # is read once here instead of four times inline.
        _shape = row.get("hr_shape_components") or {}
        if not isinstance(_shape, dict):
            _shape = {}
        lines.append({
            # identity / join keys
            "prediction_date": prediction_date,
            "player_id": row.get("player_id"),
            "player": row.get("name"),
            "game_pk": row.get("game_pk"),
            "team": row.get("team"),
            "opp": row.get("opponent"),
            "opp_pitcher_id": row.get("pitcher_id"),
            "prediction_type": "slate_row",
            "game_pick_role": row.get("game_pick_role", ""),
            # run / version metadata
            "run_id": run_meta.get("run_id"),
            "generated_at": run_meta.get("generated_at"),
            "model_versions": run_meta.get("model_versions"),
            # PROVENANCE: mirrors run_id/generated_at's own "copy the run's
            # value onto every row" pattern -- a hash/reference, not the
            # config blob itself. See config_fingerprint.py, docs/MODELS.md.
            "config_hash": (run_meta.get("config_hashes") or {}).get("hr"),
            # main scores
            "scores": {
                "hr": row.get("hr_score"),
                "hit": row.get("hit_score"),
                "hrr": row.get("hrr_score"),
                "contact": row.get("contact_score"),
                "overall": row.get("overall_score"),
                "top_board": row.get("top_board_score_v2"),
                "hrw": row.get("hrw_score"),
                "multi_hit": row.get("multi_hit_score"),
            },
            # candidate / shadow -- not production signals, kept so a future
            # shadow-vs-production comparison has same-night, same-hitter
            # data to compare rather than needing to start from zero.
            "candidate": {
                "hr_score_shadow": row.get("hr_score_shadow"),
                "best_blend_score": row.get("best_blend_score"),
                "alt_hr_score": row.get("alt_hr_score"),
            },
            # important components -- values that today die uncaptured past
            # the live slate (see DASH_MODEL_INVENTORY.md §3). Field names
            # below map to the canonical HitterRecord field that already
            # computes each one; nothing here is a new calculation.
            "components": {
                "pmix": row.get("pitch_mix_score"),
                "pitch_type_match": row.get("pitch_type_match_score"),
                "ihr": row.get("recent_ideal_hr_contact"),
                "zone_damage": row.get("pitcher_zone_damage_score"),
                "spot_damage": row.get("pitcher_spot_damage_score"),
                "pull_rate": row.get("recent_pull_rate"),
                "fb_rate": row.get("recent_fb_rate"),
                "barrel_rate": row.get("recent_barrel_rate"),
                "damage_conversion": row.get("damage_conversion_score"),
                # "weak side" component is a numeric composite score, not
                # the string label -- pitcher_weak_side (the label, e.g.
                # "RHB") is available in the full slate row already.
                "weak_side": row.get("pitcher_weak_side_score"),
                "pitcher_hr9": row.get("pitcher_hr9"),
                # No dedicated gate-signal COUNT field exists in the current
                # pipeline -- mapped to the existing hr_gate_flagged boolean
                # (best_bet_type gets rewritten to "HR" when this fires; see
                # reconcile_best_bet_with_designation) rather than inventing
                # a new calculation, per the "map the existing canonical
                # value" instruction.
                "hr_gate_flagged": row.get("hr_gate_flagged", False),

                # ── ADDED 2026-08-22, roadmap step 9b task 3 ──────────────
                # The values step 9's component research needed and could not
                # get. Ten of the twelve signals the roadmap names were
                # dropped from the archived rows between June and August (329
                # fields per row down to 120), so the two open questions the
                # handoff posed -- is batted_shape weaker than its own raw
                # inputs, and should pitcher_side_ops/_slug be wired in --
                # were unanswerable on current data. Full findings:
                # claude/moonshot-opus-component-research-findings.md.
                #
                # Every key below maps to a value the pipeline ALREADY
                # computes. Nothing here is a new calculation, and nothing
                # here sits inside a function in _HR_CONFIG_FORMULA_FUNCS, so
                # this moves no score and no config_hash.

                # batted_shape and its two raw sub-inputs. Measured on clean
                # pre-July data: the composite -3.1 (q=0.211) while max_ev
                # alone is +4.8 (q=0.036), and double-stratified the composite
                # is -5.3 once max_ev is held fixed. That is the strongest
                # structural finding step 9 produced, and it was only findable
                # because the June rows still carried the parts.
                "batted_shape": _shape.get("batted_ball_damage"),
                "shape_max_ev": _shape.get("max_ev"),
                "shape_raw_pull_rate": _shape.get("raw_pull_rate"),

                # season_power's actual blend input. Largest weight in the
                # table (0.24) and, until now, unlogged -- which is why the
                # 2026-08-09 raise from 0.12 could not be checked against
                # anything.
                "season_power": _shape.get("season_power_baseline"),

                # "computed, stored on the record, and then used by nothing"
                # (hr_blend's own comment). Still unresolved: they reproduce
                # +5-7pp on leaked data but collapse to n=187, p=0.31 when
                # measured cleanly. They cannot be resolved until they are
                # logged again.
                "pitcher_side_ops": row.get("pitcher_side_ops"),
                "pitcher_side_slug": row.get("pitcher_side_slug"),

                # The post-blend additive (up to +3.4 points). Needed to
                # measure the weak-spot double-count: weak_spot_flag and
                # weak_spot_bonus are built from the same five ingredients
                # and both reach hr_score.
                "weak_spot_bonus": row.get("weak_spot_bonus"),

                # The pa_per_hr blend term reads h.hr_pa_score, so that is
                # what gets logged under that name -- with the raw season
                # rate beside it, since the term's own comment asks to
                # "revisit sizing after another month of data" and the raw
                # rate is what that revisit needs.
                "pa_per_hr": row.get("hr_pa_score"),
                "hr_per_pa": row.get("hr_per_pa"),

                # recent_form is a LOCAL inside apply_model_v2_layers, not a
                # record field. Stashing it on the record would edit a
                # config_hash'd function and move the hash without moving a
                # single score, so its five raw inputs are logged instead.
                # That is strictly better for research anyway -- the
                # batted_shape finding above came from having the parts, not
                # the composite, and recent_form is the term whose weight was
                # justified on a leaked measurement (-9.2, q=0.001 when
                # measured cleanly, across four separate pathways into the
                # score).
                "recent_form_last5_hr": row.get("last5_hr"),
                "recent_form_last10_hr": row.get("last10_hr"),
                "recent_form_l20pa_hr": row.get("l20pa_hr"),
                "recent_form_last5_xbh": row.get("last5_xbh"),
                "recent_form_l20pa_xbh": row.get("l20pa_xbh"),

                # ── ADDED 2026-08-22, roadmap step 9b task 1 ──────────────
                # games_since_last_hr is the OTHER field Claim A's evidence
                # named (Sol brief / Opus findings doc): 0 for 95.7% of
                # players who homered and 1.1% of those who didn't, on the
                # leaked archive. recent_form_last5_hr above covers last5_hr;
                # this was the one still missing. Same pattern as everything
                # above -- an existing computed value, not a new calculation,
                # not inside _HR_CONFIG_FORMULA_FUNCS.
                "games_since_last_hr": row.get("games_since_last_hr"),
                # Inputs used by the immutable HR overlay. These are copied
                # individually too so ordinary feature research does not have
                # to understand the tier schema.
                "season_iso": row.get("season_iso"),
                "recent_ev": row.get("recent_ev"),
                "l25pa_air_rate": row.get("l25pa_air_rate"),
                "recent_hard_hit_rate": row.get("recent_hard_hit_rate"),
                "recent_pull_air_rate": row.get("recent_pull_air_rate"),
                "recent_avg_distance": row.get("recent_avg_distance"),
                "recent_squared_up_rate": row.get("recent_squared_up_rate"),
                "recent_squared_up_sample": row.get("recent_squared_up_sample"),
                "recent_blast_rate": row.get("recent_blast_rate"),
                "season_hr_game_probability": row.get("season_hr_game_probability"),
            },
            # ── THE FULL PRE-GAME SNAPSHOT (2026-08-23) ─────────────────────
            # 11cec10's own scope note, closed: "a SLOT_FIELDS key not yet in
            # _LOCKED_SCORE_MAP/_LOCKED_COMPONENT_MAP stays exposed to the
            # leak until it's added". Instead of widening two maps one field
            # at a time, every leak-prone SLOT_FIELDS key is snapshotted here
            # wholesale at generation time, and apply_locked_features overlays
            # whatever this dict carries. Identity/static keys (ids, names,
            # venue, park factors) are deliberately absent — they cannot leak
            # an outcome and overlaying them would fight the live row.
            "slot_snapshot": {k: row.get(k) for k in PREGAME_SNAPSHOT_FIELDS if k in row},
            "hr_overlay": build_hr_overlay(row),
            # Moonshot scores are 0-100 indices, not probabilities. Never a
            # bare number here -- only a real calibrated probability would
            # ever populate this field, and none exists yet.
            "probability": None,
        })
    return lines


def write_prediction_log(run_meta: Dict[str, Any], rows_payload: List[Dict[str, Any]]) -> Optional[Path]:
    """Write bots/outputs/prediction_log_<slate_date>.<runstamp>.jsonl for
    this run. Returns the path written, or None if the registry import
    failed (see MODEL_REGISTRY at module scope) -- a metadata/logging
    failure must never block the slate the rest of this function already
    produced."""
    if MODEL_REGISTRY is None:
        print("prediction log skipped: model_registry did not import", file=sys.stderr)
        return None
    try:
        run_id = run_meta["run_id"]
        slate_date_str = run_meta["slate_date"]
        # run_id is "{slate_date}.{HHMMSSZ}.{source}" -- the runstamp is
        # everything after the leading slate_date segment, so the filename
        # reconstructs the exact run_id it belongs to.
        runstamp = run_id.split(".", 1)[1] if "." in run_id else run_id
        path = OUT_DIR / f"prediction_log_{slate_date_str}.{runstamp}.jsonl"
        lines = build_prediction_log_lines(run_meta, rows_payload)
        with path.open("w", encoding="utf-8") as f:
            for obj in lines:
                f.write(json.dumps(obj, default=str))
                f.write("\n")
        return path
    except Exception as exc:
        print(f"prediction log write failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="MLB Daily Breakdown Bot — Today Pitch Mix Version v1 MAY")
    parser.add_argument("--date", default="today", help="Slate date YYYY-MM-DD, today, or tomorrow. Default is today.")
    parser.add_argument("--today", action="store_true", help="Force today's slate")
    parser.add_argument("--tomorrow", action="store_true", help="Run tomorrow's slate without waiting for midnight")
    parser.add_argument("--days-ahead", type=int, default=0, help="Run a future slate N days from today")
    parser.add_argument("--no-pairs", action="store_true", help="Skip pairing section")
    # FLIPPED (2026-07-25): this had defaulted to False since 2026-07-14,
    # which silently dropped PAIRINGS, POOLS, HRR BUILDER, cross-check, game
    # blocks, and the legend from the daily .txt report unless the caller
    # remembered to pass --full explicitly. Whatever invokes the daily run
    # wasn't passing it, so the report had been running "slim" without
    # anyone asking for that. Defaulting to full report now; pass
    # --slim to opt into the old THE FOUR-only behavior instead.
    parser.add_argument("--slim", dest="full", action="store_false", help="Slim report (THE FOUR + board + longest HR + model health only). Default is the full report (pairs, pools, HRR builder, cross-check, game blocks, legend).")
    # Kept as a no-op alias (rather than removed outright) so any existing
    # cron job/launcher that already passes --full explicitly doesn't start
    # erroring out with "unrecognized argument" -- it just confirms the new
    # default instead of changing anything.
    parser.add_argument("--full", dest="full", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(full=True)
    parser.add_argument("--force-refresh", action="store_true", help="Ignore saved game locks and rebuild every game block")
    args = parser.parse_args()

    slate_date = TODAY if args.today else resolve_slate_date(args.date, tomorrow=args.tomorrow, days_ahead=args.days_ahead)
    print(f"RUNNING SLATE DATE: {slate_date.isoformat()} | Phoenix today: {TODAY.isoformat()}")
    client = MLBClient()
    db = CacheDB(DB_PATH)
    try:
        games = client.schedule(slate_date)
        # Sort chronologically by first pitch. MLB's schedule endpoint doesn't
        # guarantee time order, and the .txt report reads top-to-bottom, so an
        # unsorted list meant early-afternoon games could show up after
        # night games. gameDate is an ISO8601 UTC string ("...Z"), which
        # sorts correctly as plain text without needing to parse it first.
        games = sorted(games, key=lambda g: str(g.get("gameDate", "")))
        if not games:
            print(f"No MLB games found for {slate_date.isoformat()}.")
            return 0

        print(f"Pulling slate for {slate_date.isoformat()}...", file=sys.stderr)

        slate_label = slate_output_label(slate_date)
        clean_prefix = f"mlb_breakdown_{slate_label}_{slate_date.isoformat()}"
        txt_path = OUT_DIR / f"{clean_prefix}.txt"
        json_path = OUT_DIR / f"{clean_prefix}.json"
        # V29 clean output: no duplicate legacy aliases.
        txt_alias_paths = []
        json_alias_paths = []

        # MODEL FOUNDATION (Task 2): one run identity for this entire bot
        # execution, generated once here -- never per hitter, never per game.
        run_meta = build_run_meta(slate_date, slate_label, args)
        print(f"RUN_ID: {run_meta['run_id']}", file=sys.stderr)

        locked_rows_by_game = {} if args.force_refresh else load_locked_rows_by_game(json_path)

        raw_rows: List[HitterRecord] = []
        game_rows: List[Tuple[Dict[str, Any], List[HitterRecord]]] = []

        for idx, game in enumerate(games, start=1):
            away = normalize_team_abbr(game.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "AWAY"))
            home = normalize_team_abbr(game.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "HOME"))
            game_pk = safe_int(game.get("gamePk"), 0)

            if game_has_started(game) and game_pk in locked_rows_by_game:
                rows = locked_rows_by_game[game_pk]
                rows = refresh_locked_lineup_status(client, game, rows)
                filtered_rows = rows
                print(f"[{idx}/{len(games)}] LOCKED {away} @ {home} — using saved game block picks.", file=sys.stderr, flush=True)
            else:
                print(f"[{idx}/{len(games)}] Building {away} @ {home}...", file=sys.stderr, flush=True)
                rows = build_hitter_records(client, db, game, slate_date)
                filtered_rows = apply_global_pa_filter(rows)
                # MODEL FOUNDATION (Task 3): stamp freshly-scored rows with
                # the HR market's current version + this run's run_id.
                # Deliberately NOT applied to the LOCKED branch above: a
                # locked game reuses a row that was actually scored by an
                # earlier run, and overwriting its run_id here would
                # misattribute it to a run that never touched it. A locked
                # row keeps whatever stamp it was saved with (defaulted to
                # "" for any row saved before this change existed).
                if MODEL_REGISTRY is not None:
                    _hr_version = MODEL_REGISTRY.MODEL_VERSIONS.get("hr", "")
                    # PROVENANCE: same run_meta this run already built its
                    # run_id from, read once per game loop rather than
                    # recomputed per row -- config_hashes["hr"] is fixed for
                    # the whole run (see hr_config_hash()/build_run_meta()).
                    _hr_hash = (run_meta.get("config_hashes") or {}).get("hr") or ""
                    for _r in rows:
                        _r.model_version = _hr_version
                        _r.run_id = run_meta["run_id"]
                        _r.config_hash = _hr_hash

            raw_rows.extend(rows)
            game_rows.append((game, filtered_rows))

        all_rows: List[HitterRecord] = [r for _, rows in game_rows for r in rows]
        removed_count = len(raw_rows) - len(all_rows)
        if removed_count > 0:
            print(f"Global PA filter removed {removed_count} low-sample players (requires recent PA 20+ when available or season PA 80+).", file=sys.stderr)
        game_blocks = [render_game_block(game, rows) for game, rows in game_rows if rows]

        data_end = statcast_data_end_date(slate_date)
        quality_banner = build_data_quality_banner(all_rows)
        _slate_str = slate_date.strftime("%A · %B %d, %Y").upper()
        # REORDERED (2026-07-25, per request): PROJECTED -> THE FOUR -> GAME
        # BY GAME -> PAIRS -> POOLS -> BOARDS -> the rest. header_text +
        # slate_text form the new top-of-report PROJECTED block; boards_text
        # (TOP 30 + ALT LOOKS) now renders later, after pairs/pools. NOTE:
        # build_top10_alt_board() must still be CALLED here, before
        # build_pair_sections() runs below -- it sets the LAST_ALT_USED_IDS
        # and LAST_TOP30_BOARD globals that pools/pairs read to avoid
        # reusing ALT LOOKS / Top 30 names. Only the *display position* of
        # its returned text has moved, not the call order.
        header_text = (
            "┏" + "━" * 46 + "\n"
            f"┃ ⚾ THE BOARD — {_slate_str}\n"
            f"┃ data locked {data_end.isoformat()} · lineups live/confirmed\n"
            f"┃ filter: recent 20+ PA (or 80+ season PA)\n"
            "┗" + "━" * 46 + "\n\n"
            + (quality_banner + "\n" if quality_banner else "")
        )
        slate_text = build_slate_prediction(all_rows)
        boards_text = build_top10_alt_board(all_rows)
        legend = """LEGEND

Score Keys:
HR = Home run score / power ceiling
OVR = overall Top Pick score; HR + HRR + HIT blend
HRW = Home Run Window; timing score for today's HR chance
IHR = Ideal HR contact rate
350+/375+ = recent tracked balls hit 350/375+ feet
FB = recent fly-ball rate
Pull = recent pull rate
HR/9 = pitcher home runs allowed per 9 innings
ATK = Pitcher Attack Score from recent pitcher damage allowed
Projected HR Total = model estimate for full-slate home runs
Longest HR Targets = distance-ceiling section, not safest HR picks

Emoji / Category Keys — tonight's game-slot picks (pick_type; this report only, not the site):
🥇 TOP = best overall/model blend from that game or board
🎆 HR = pure HR/power pick
🔺 HRR = hits + runs + RBI production profile; also used for hot production bats
➕ HIT = base-hit profile / hit-floor pick
🟢 CON = Base Pick; contact/XBH anchor, gap-power or total-base profile
⭐ = batter lines up in a pitcher weak spot
🎯 = pitcher throws a pitch type this batter crushes (pitch-type match)
🧩 = Aligned Signals: weak-spot + pitch-match + real recent contact quality all stacking together (strongest validated combo)
🚀 HRW 70+ = strong HR timing
⚡ HRW 60–69 = playable HR timing
🌤️ HRW 50–59 = borderline/building timing
🧊 HRW under 50 = weak HR timing
⚠️ LIMITED = sample/variance warning (soft caution, score not forced down)
⛔ = hard score suppression in effect (e.g. True Avoid HR -- score actively capped, not just flagged)

Role Tags (final_hr_role -- a DIFFERENT system from the game-slot picks above:
his general profile for the season, not tonight's designation. Also on the
site, on every card. Corrected here 2026-08-12 -- this legend had drifted
behind the actual code, still teaching the emoji final_hr_role moved off of):
💎 HR Bet = model's top-confidence home run play
📈 HR Lean = good home run shot, strong but not airtight
🧲 HRR / XBH = good bet for extra-base hits even if HR isn't a lock
🔭 Power Watch = real raw power, more matchup uncertainty
🧭 Contact / Monitor = not projecting a home run; may suit contact bets
⛔ True Avoid HR = model expects no home run; score actively capped

Player Output Keys:
BA = batting average
BA vs RHP/LHP = batting average split vs starting pitcher side
BABIP = batting average on balls in play
PreOB = on-base strength of hitters before him
Post = run-production strength of hitters after him
L5 = last 5 games shown on output
K = strikeout rate
XBH = extra-base hits
2+ TB = two or more total bases

Sections:
Top 30 = trusted sample only; 40+ PA and 10+ tracked BBE
Global PA Filter = applies to every output section; recent PA 30+ when available, otherwise season PA 80+
ALT LOOKS = unique quality looks not already used in main board/pools/pairs when possible
PAIRINGS = HR-focused two-man slips with compact reasons
DUE BOMBER = drought 5g+ + weak-spot star + HR score 45+; HRW ignored on purpose (timing score buries droughts)
POOLS = HR coverage groups; C/D can include variance and underlisted model plays
HRR BUILDER = production slips; 2-man, 3-man, and 5-man combos; max 2 appearances per player
MODEL CROSS-CHECK = high-scoring players across multiple categories or underlisted HR upside

Helpful Read:
Use HR picks for home run slips.
Use HRR Builder for hit/run/RBI style slips.
Use Base Picks for XBH or 2+ total-base ideas.
Use ALT LOOKS as quality variance, not primary plays.
"""
        longest_hr_text = build_longest_hr_targets(all_rows, 3)
        # RESTORED (2026-07-25): DUE BOMBER BOARD/PAIRS had gone missing from
        # this file entirely (not gated behind --full or any flag -- the
        # functions simply weren't here). Rebuilt from the exact format/
        # thresholds shown in the 2026-07-24 reference report.
        due_bomber_board_text = build_due_bomber_board(all_rows, 15)
        # SWAPPED (2026-07-26): was build_due_bomber_pairs. Drought-based
        # pairing was reviewed against a real slate and went 0-for-4 on
        # homers, while the L5-2+/HR-rate-elite profile went 4-for-4
        # including two multi-HR games. The due bomber BOARD is untouched --
        # only the automatic pairing changed.
        hot_power_pairs_text = build_hot_power_pairs(all_rows, 2)
        the_four_text = build_the_four(all_rows)
        game_by_game_text = ("🗓️ GAME BY GAME " + "─" * 30 + "\n\n" + "\n\n".join(game_blocks)) if game_blocks else ""

        # REORDERED (2026-07-25, per request): PROJECTED -> THE FOUR -> GAME
        # BY GAME -> PAIRS -> POOLS -> BOARDS (Top 30 + ALT LOOKS) -> the
        # rest (Longest Bomb Watch, Due Bomber Board, HRR Builder, Model
        # Cross-Check, Legend, Model Health). Pairs/pools/HRR
        # builder/cross-check/game-by-game/legend still gate on --full (now
        # defaulted True); pass --slim for the old THE FOUR-only report.
        report_text = header_text + slate_text
        if the_four_text:
            report_text += "\n\n" + the_four_text
        if args.full and game_by_game_text:
            report_text += "\n\n" + game_by_game_text
        pair_sections_json: Dict[str, Any] = {"recommended_pairs": [], "pools_4man": [], "pools_6man": [], "pools_3man": []}
        if not args.no_pairs:
            pair_text, pair_sections_json = build_pair_sections(all_rows)
            if pair_text and hot_power_pairs_text:
                pair_text = pair_text + "\n\n" + hot_power_pairs_text
            if pair_text and args.full:
                report_text += "\n\n" + pair_text
        if boards_text:
            report_text += "\n\n" + boards_text
        if longest_hr_text:
            report_text += "\n\n" + longest_hr_text
        if due_bomber_board_text:
            report_text += "\n\n" + due_bomber_board_text
        if args.full:
            hrr_builder_text = build_hrr_builder(all_rows)
            if hrr_builder_text:
                report_text += "\n\n" + hrr_builder_text
            cross_lines = build_model_cross_check_plays(
                all_rows,
                game_pick_type_map(all_rows),
                set(LAST_HR_SECTION_USED_IDS) | set(LAST_ALT_USED_IDS),
            )
            if cross_lines:
                report_text += "\n\n" + "\n".join(cross_lines)
            report_text += "\n\n" + legend + "\n"
        model_health_text = build_model_health_report(all_rows)
        if model_health_text:
            report_text += "\n" + model_health_text + "\n"

        # Docket #20: finalize this run's league (EV,LA) table and stamp the
        # xHR / luck fields on every row before serialization.
        try:
            finalize_xhr_fields(all_rows, db)
        except Exception as _xexc:
            print(f"xHR finalize skipped: {_xexc}", file=sys.stderr)
        rows_payload = enrich_weather_payload_for_website([dataclasses.asdict(r) for r in all_rows])
        # Slate-level, so it can rank. See mark_hidden_hr_value for why the
        # per-hitter version could never fire.
        rows_payload = mark_hidden_hr_value(rows_payload)
        rows_payload = enrich_hr_pa_payload(rows_payload)
        rows_payload = enrich_signal_pills_and_best_non_hr(rows_payload)

        # Stamp game pick role (TOP/HR/HIT/HRR/CONTACT) onto each player row.
        #
        # PICK LOCK (2026-08-06). The slate rebuilds hourly and re-picked every
        # run — including for games already in progress, which meant the "HR
        # pick" could quietly become a different hitter in the 4th inning and
        # the graded record would grade the REVISION. Picks are promises;
        # rewriting them after first pitch is grading a bet nobody could have
        # made. Rule: the last role map stamped BEFORE a game's first pitch is
        # that game's map forever. Persisted in the cache per (date, game);
        # games with no lock yet (not started, or TBD time) keep updating.
        _role_map = build_game_pick_role_map(all_rows)
        try:
            _lock_key = f"pick_lock:{slate_date.isoformat()}"
            _lock = db.get(_lock_key) or {}
            _now = dt.datetime.now(dt.timezone.utc)
            _gtime = {}
            for r in all_rows:
                gt = getattr(r, 'game_time', None)
                if r.game_pk and gt and r.game_pk not in _gtime:
                    _gtime[r.game_pk] = str(gt)
            def _started(pk):
                t = _gtime.get(pk)
                if not t:
                    return False
                try:
                    ts = dt.datetime.fromisoformat(str(t).replace('Z', '+00:00'))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=dt.timezone.utc)
                    return _now >= ts
                except Exception:
                    return False
            _locked_map = {}
            _fresh_by_game = {}
            for (pk, pid), role in _role_map.items():
                _fresh_by_game.setdefault(pk, {})[str(pid)] = role
            for pk in set(list(_fresh_by_game.keys()) + [int(k) for k in _lock.keys() if str(k).isdigit()]):
                if _started(pk) and str(pk) in _lock:
                    # frozen: first-pitch map wins, this run's opinion discarded
                    for pid_s, role in _lock[str(pk)].items():
                        _locked_map[(pk, int(pid_s))] = role
                else:
                    for pid_s, role in _fresh_by_game.get(pk, {}).items():
                        _locked_map[(pk, int(pid_s))] = role
                    if not _started(pk) and pk in _fresh_by_game:
                        _lock[str(pk)] = _fresh_by_game[pk]
            db.set(_lock_key, _lock)
            _role_map = _locked_map
        except Exception as _lock_exc:
            print(f"pick lock skipped (fresh map used): {_lock_exc}", file=sys.stderr)
        for row in rows_payload:
            key = (row.get('game_pk'), row.get('player_id'))
            row['game_pick_role'] = _role_map.get(key, '')
            row['alt_look_tag'] = LAST_ALT_TAGS.get(row.get('player_id'), '')
            row['pitcher_projected'] = safe_int(row.get('pitcher_id'), 0) in PROJECTED_PITCHERS

        # ⛔ vs 🥇: the designation has just landed (post pick-lock), so this is
        # the first moment in the whole run at which a hitter-level best_bet_type
        # can be checked against the slate-level role it contradicts. One call,
        # one mechanism — see reconcile_best_bet_with_designation() for the
        # ordering trace and the 18/55 vs 124/631 measurement behind it. This
        # USED to be an anonymous inline block right here inside the loop above;
        # it was moved out on 2026-08-16 so the rule has a name, a docstring
        # stating its invariant, and a test that can call it without standing up
        # the pipeline (tests/test_hr_gate_label.py).
        rows_payload = reconcile_best_bet_with_designation(rows_payload)

        # ── DISCORD SLATE BOARD (2026-08-06). Every today-bot run that CHANGES
        # the picks posts tonight's board to the webhook: top names per
        # category by that category's own score, plus what moved since the
        # last post. Identical republishes stay silent (signature check in the
        # cache), so hourly runs don't spam the same board — and once games
        # start, the pick lock means changes stop on their own. Silent no-op
        # without the DISCORD_WEBHOOK env. ──
        try:
            # Multi-room (2026-08-08): the secret may carry SEVERAL webhook
            # URLs separated by commas/whitespace — the board posts to all.
            _dws = [u.strip() for u in os.environ.get("DISCORD_WEBHOOK", "").replace(",", "\n").split() if u.strip().startswith("http")]
            if _dws:
                import hashlib as _hl
                import urllib.request as _ur
                _CAT_SC = {
                    'TOP': lambda r: safe_float(r.get('top_board_score_v2') or r.get('overall_score'), 0.0),
                    'HR': lambda r: safe_float(r.get('hr_score'), 0.0),
                    'HIT': lambda r: safe_float(r.get('hit_score'), 0.0),
                    'HRR': lambda r: safe_float(r.get('hrr_score'), 0.0),
                    'CONTACT': lambda r: safe_float(r.get('contact_score'), 0.0),
                }
                _picks_by_cat = {}
                _pick_map = {}
                for row in rows_payload:
                    _pr = str(row.get('game_pick_role') or '').split('/')[0].strip().upper()
                    if _pr in _CAT_SC:
                        _picks_by_cat.setdefault(_pr, []).append(row)
                        _pick_map[str(row.get('player_id'))] = {"role": _pr, "name": str(row.get('name') or '')}
                _sig = _hl.sha1(json.dumps(sorted((k, v["role"]) for k, v in _pick_map.items())).encode()).hexdigest()
                _sig_key = f"discord_slate_sig:{slate_date.isoformat()}"
                _prev = db.get(_sig_key) or {}
                if _pick_map and _sig != _prev.get("sig"):
                    # SHORT AND SWEET (2026-08-06): the board reads like a tip
                    # sheet, not a roster dump — ONE edge per category, one
                    # line each, changes as a count not a ledger.
                    _EMO = {'TOP': '🥇', 'HR': '💣', 'HIT': '🎯', 'HRR': '🏁', 'CONTACT': '⚾'}
                    # DEPTH ROTATION (2026-08-07 v2, "still showing the same
                    # people"): the top name per category IS the edge and can't
                    # honestly rotate — but the DEPTH can. Each post expands a
                    # different category to its top three, so consecutive
                    # boards read differently even when every #1 held.
                    _seq0 = safe_int((db.get(f"discord_slate_seq:{slate_date.isoformat()}") or {}).get("n"), 0)
                    _exp_cat = ('TOP', 'HR', 'HIT', 'HRR', 'CONTACT')[_seq0 % 5]
                    _ls = []
                    for _cat in ('TOP', 'HR', 'HIT', 'HRR', 'CONTACT'):
                        _rows = sorted(_picks_by_cat.get(_cat, []), key=_CAT_SC[_cat], reverse=True)
                        if not _rows:
                            continue
                        r = _rows[0]
                        _conf = '' if r.get('lineup_confirmed') else ' ◻'
                        _arm = str(r.get('pitcher_name') or 'TBD').split()[-1]
                        _ls.append(f"{_EMO.get(_cat, '')} **{r.get('name')}** · {_cat} {_CAT_SC[_cat](r):.0f} · vs {_arm}{_conf}")
                        if _cat == _exp_cat and len(_rows) > 1:
                            _depth = ' · '.join(
                                f"{r2.get('name')} {_CAT_SC[_cat](r2):.0f}" for r2 in _rows[1:3])
                            _ls.append(f"    ↳ next in line: {_depth}")
                    # ── 🎬 ROTATING SPOTLIGHT (2026-08-07, Donovan: "make sure
                    # tonight's edge shows something different every
                    # notification"). The five category lines are the board's
                    # identity and stay; what rotates is one spotlight section
                    # per post, cycling through five angles so consecutive
                    # boards never read identical even when the picks held.
                    # Headline player gets a real MLB Film Room search link —
                    # the "maybe some videos" ask, honestly: a link to actual
                    # highlights, not an embedded clip we don't have rights to
                    # rehost. Angle index persists per date in the cache.
                    import urllib.parse as _up
                    _seq_key = f"discord_slate_seq:{slate_date.isoformat()}"
                    _seq = safe_int((db.get(_seq_key) or {}).get("n"), 0)
                    # LINK POLICY (2026-08-08, Donovan: "sending it to savant
                    # is kinda backwards — only links to videos and the site").
                    # Savant links sent readers to someone else's product.
                    # Every player link now deep-links to OUR site — the hash
                    # router (#tab=power&p={id}) opens his card right on the
                    # board. Film Room search is the video link, name-only
                    # (mlb.com /video player pages hit an access wall, but the
                    # Film Room SEARCH page is public).
                    _SITE = "https://moonshot-mlb.vercel.app"
                    def _film(nm, pid=None):
                        if pid:
                            return f"[📊 his card]({_SITE}/#tab=power&p={safe_int(pid, 0)})"
                        return f"[🎬 film](https://www.mlb.com/video/search?q={_up.quote(str(nm))})"
                    def _spot(angle):
                        if angle == 0:  # best park tonight
                            _gp = {}
                            for r2 in rows_payload:
                                _gp.setdefault(r2.get('game_pk'), []).append(r2)
                            best, bedge = None, -99.0
                            for _rows2 in _gp.values():
                                r0 = _rows2[0]
                                pf = safe_float(r0.get('park_hr_factor') or r0.get('park_dist_factor'), 0.0)
                                wx = r0.get('weather_hr_effect_pct') or r0.get('hr_weather_effect_pct')
                                t2 = safe_float(r0.get('weather_temp_f') or r0.get('temp_f'), 0.0)
                                w2 = safe_float(r0.get('weather_wind_mph') or r0.get('wind_mph'), 0.0)
                                wl = str(r0.get('wind_direction_label') or '')
                                wout = w2 if 'out' in wl.lower() else (-w2 if 'in' in wl.lower() else 0.0)
                                edge = ((pf - 1) * 100 if pf > 0 else 0.0) + (safe_float(wx, 0.0) if wx is not None else wout + ((t2 - 70) / 7 if t2 > 0 else 0.0))
                                if edge > bedge:
                                    bedge, best = edge, _rows2
                            if not best:
                                return None
                            r0 = best[0]
                            thr = max(best, key=lambda r2: safe_float(r2.get('longest_hr_score'), 0.0))
                            return (f"\n🌋 **Park of the night**: {r0.get('venue_name') or 'TBD'} ({bedge:+.0f}% vs neutral) — "
                                    f"biggest threat in the building: **{thr.get('name')}** · {_film(thr.get('name'), thr.get('player_id'))}")
                        if angle == 1:  # back-to-back watch
                            b2 = sorted([r2 for r2 in rows_payload if safe_int(r2.get('games_since_last_hr'), 99) == 0],
                                        key=lambda r2: safe_float(r2.get('hr_score'), 0.0), reverse=True)[:3]
                            if not b2:
                                return None
                            names = ' · '.join(f"**{r2.get('name')}**" for r2 in b2)
                            return f"\n🔁 **B2B watch** — went deep last game: {names} · {_film(b2[0].get('name'), b2[0].get('player_id'))}"
                        if angle == 2:  # luck buy (calibrated xHR)
                            lk = sorted([r2 for r2 in rows_payload
                                         if safe_int(r2.get('xhr_bbe'), 0) >= 50 and safe_float(r2.get('season_hr_luck'), 0.0) <= -1.5],
                                        key=lambda r2: safe_float(r2.get('season_hr_luck'), 0.0))[:1]
                            if not lk:
                                return None
                            r2 = lk[0]
                            return (f"\n🍀 **Luck buy**: **{r2.get('name')}** — {safe_int(r2.get('season_hr'),0)} HR on contact worth "
                                    f"{safe_float(r2.get('season_xhr'),0.0):.1f} · the regression bet is with him · {_film(r2.get('name'), r2.get('player_id'))}")
                        if angle == 3:  # alt look of the night
                            al = sorted([r2 for r2 in rows_payload if str(r2.get('alt_look_tag') or '').strip()],
                                        key=lambda r2: safe_float(r2.get('alt_hr_score') or r2.get('hr_score'), 0.0), reverse=True)[:1]
                            if not al:
                                return None
                            r2 = al[0]
                            return f"\n🅰 **Alt look of the night**: **{r2.get('name')}** ({str(r2.get('alt_look_tag')).strip()}) — off the main board, quality variance lane"
                        # angle 4: weak-spot stack
                        _gp = {}
                        for r2 in rows_payload:
                            _gp.setdefault(r2.get('game_pk'), []).append(r2)
                        best = max(_gp.values(), key=lambda rs: sum(1 for r2 in rs if r2.get('weak_spot_flag')), default=None)
                        if not best:
                            return None
                        wk = [r2 for r2 in best if r2.get('weak_spot_flag')]
                        if len(wk) < 2:
                            return None
                        r0 = best[0]
                        nm = ' · '.join(f"**{r2.get('name')}**" for r2 in wk[:3])
                        return f"\n⭐ **Stack alert**: {r0.get('team')} vs {r0.get('opponent_team')} carries {len(wk)} weak pitcher spots — {nm}"
                    _spot_line = None
                    for _try in range(5):
                        _spot_line = _spot((_seq + _try) % 5)
                        if _spot_line:
                            break
                    if _spot_line:
                        _ls.append(_spot_line)
                    db.set(_seq_key, {"n": _seq + 1})

                    _old_map = _prev.get("map") or {}
                    _n_chg = 0
                    if _old_map:
                        _n_chg = sum(1 for pid, v in _pick_map.items()
                                     if _old_map.get(pid, {}).get("role") != v["role"])
                        _n_chg += sum(1 for pid in _old_map if pid not in _pick_map)
                    if _n_chg:
                        _ls.append(f"\n🔁 {_n_chg} change{'s' if _n_chg != 1 else ''} since the last board — full story on the site")
                    _desc = "\n".join(_ls)[:4000]
                    _payload = {"embeds": [{
                        "title": f"🗒 Tonight's edge — {slate_date.isoformat()}",
                        "description": _desc,
                        "color": 0xF97316,
                        "footer": {"text": "one per category, its own scale · ◻ lineup not confirmed · picks lock at first pitch · stats & analysis, not financial or betting advice"},
                        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }]}
                    for _dw in _dws:
                        try:
                            _req = _ur.Request(_dw, data=json.dumps(_payload).encode("utf-8"),
                                               headers={"Content-Type": "application/json", "User-Agent": "moonshot-bot"})
                            _ur.urlopen(_req, timeout=10)
                        except Exception as _pexc:
                            print(f"discord slate post failed: {_pexc}", file=sys.stderr)
                    db.set(_sig_key, {"sig": _sig, "map": _pick_map})

                    # ── 📓 PICK CHANGELOG (2026-08-08, wishlist #2): when a
                    # pick changes between runs, name the INPUT that moved.
                    # Context per pick is snapshotted in the cache each run;
                    # the diff against last run's snapshot becomes
                    # current/pick_changes.json — one plain-language line per
                    # change, which the site's Since panel can quote instead
                    # of leaving readers to guess. ──
                    try:
                        _ctx_key = f"pick_ctx:{slate_date.isoformat()}"
                        _prev_ctx = (db.get(_ctx_key) or {}).get("ctx") or {}
                        _new_ctx = {}
                        _by_pid = {str(row.get('player_id')): row for row in rows_payload}
                        for _pid, _v in _pick_map.items():
                            _row = _by_pid.get(_pid) or {}
                            _new_ctx[_pid] = {
                                "name": _v.get("name", ""), "role": _v.get("role", ""),
                                "conf": bool(_row.get("lineup_confirmed")),
                                "pit": safe_int(_row.get("pitcher_id"), 0),
                                "pit_name": str(_row.get("pitcher_name") or ""),
                                "sc": round(safe_float(_row.get("hr_score"), 0.0), 1),
                            }
                        _changes = []
                        for _pid, _nc in _new_ctx.items():
                            _oc = _prev_ctx.get(_pid)
                            if _oc is None:
                                # new pick — say what likely earned it
                                _why = []
                                if _nc["conf"]:
                                    _why.append("lineup confirmed him")
                                _who_out = next((o["name"] for op, o in _prev_ctx.items()
                                                 if op not in _new_ctx and o.get("role") == _nc["role"]), None)
                                _changes.append({
                                    "kind": "in", "name": _nc["name"], "role": _nc["role"],
                                    "reason": (" · ".join(_why) or "score moved to the top of the lane")
                                              + (f" — replaces {_who_out}" if _who_out else ""),
                                })
                            elif _oc.get("role") != _nc["role"]:
                                _changes.append({
                                    "kind": "moved", "name": _nc["name"], "role": _nc["role"],
                                    "reason": f"category changed {_oc.get('role')}→{_nc['role']}",
                                })
                        for _pid, _oc in _prev_ctx.items():
                            if _pid in _new_ctx:
                                continue
                            _row_now = _by_pid.get(_pid) or {}
                            if not _row_now:
                                _reason = "off the slate (lineup scratch or postponement)"
                            elif safe_int(_row_now.get("pitcher_id"), 0) != _oc.get("pit", 0) and _oc.get("pit"):
                                _reason = f"pitcher changed ({_oc.get('pit_name') or 'listed arm'} out)"
                            elif not _row_now.get("lineup_confirmed") and _oc.get("conf"):
                                _reason = "dropped from the posted lineup"
                            else:
                                _sc_now = round(safe_float(_row_now.get("hr_score"), 0.0), 1)
                                _d = _sc_now - safe_float(_oc.get("sc"), 0.0)
                                _reason = f"score moved {_d:+.1f} — someone else took the lane"
                            _changes.append({"kind": "out", "name": _oc.get("name", ""),
                                             "role": _oc.get("role", ""), "reason": _reason})
                        db.set(_ctx_key, {"ctx": _new_ctx})
                        if _changes or _prev_ctx:
                            _clog = {
                                "date": slate_date.isoformat(),
                                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "changes": _changes[:40],
                            }
                            _targets = [OUT_DIR / "pick_changes.json"]
                            try:
                                for _ap in (json_alias_paths or []):
                                    _targets.append(Path(_ap).parent / "pick_changes.json")
                            except Exception:
                                pass
                            for _cp in _targets:
                                try:
                                    _cp.parent.mkdir(parents=True, exist_ok=True)
                                    _cp.write_text(json.dumps(_clog, indent=1), encoding="utf-8")
                                except Exception:
                                    pass
                    except Exception as _clexc:
                        print(f"pick changelog skipped: {_clexc}", file=sys.stderr)
        except Exception as _dexc:
            print(f"discord slate board skipped: {_dexc}", file=sys.stderr)
        # HR Score 2.0 clean output: only the main TXT + JSON are written locally.

        # Print report first so the final console lines clearly show save/sync status.
        print(report_text)

        write_text_and_aliases(txt_path, report_text, txt_alias_paths)
        write_json_and_aliases(json_path, rows_payload, json_alias_paths)

        # MODEL FOUNDATION (Tasks 2 & 4): this run's metadata + the
        # append-only prediction log, written from the fully-enriched
        # rows_payload (game_pick_role, hr_gate_flagged, etc. are only
        # present here, not on the raw HitterRecord objects). Both are
        # best-effort -- a failure here must never cost the slate that was
        # already written above.
        try:
            _pred_log_path = write_prediction_log(run_meta, rows_payload)
            sync_model_foundation_outputs_to_website_repo(slate_label, run_meta, _pred_log_path)
        except Exception as _mf_exc:
            print(f"⚠️ Model-foundation logging failed: {_mf_exc}", file=sys.stderr)

        # Pair Builder output: now powered by System 2's actual selection
        # logic (build_structured_pairs/build_structured_pool -- the same
        # code that builds the .txt report's pairings) instead of the old
        # System 1 (_pb_* functions). Per audit (2026-06-27): System 1 is
        # fully removed: it computed an entirely separate, never-wanted set
        # of formulas. Writes to the same file location/shape the frontend
        # already expects, so no frontend changes are needed to see this.
        try:
            pair_builder_payload = {
                "schema": "pair_builder_system2_v1",
                "date": slate_date.isoformat(),
                "role": slate_label,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "source": "bot_system2",
                "recommended_pairs": pair_sections_json.get("recommended_pairs", []),
                "pools_4man": pair_sections_json.get("pools_4man", []),
                "pools_3man": pair_sections_json.get("pools_3man", []),
                "pools_6man": pair_sections_json.get("pools_6man", []),
                # MINI-BOT AUDIT (2026-08-08, B2): available_pool is the
                # frontend's PRIMARY source for Build-a-Pair — it was built
                # every night and then dropped right here, which is why the
                # site's builder was silently running on fallback data.
                "available_pool": pair_sections_json.get("available_pool", []),
                "recommended_3mans": pair_sections_json.get("recommended_3mans", []),
            }
            pair_dated_path = OUT_DIR / f"mlb_pair_builder_{slate_date.isoformat()}.json"
            pair_latest_path = OUT_DIR / "pair_builder_latest.json"
            pair_dated_path.write_text(json.dumps(pair_builder_payload, indent=2), encoding="utf-8")
            pair_latest_path.write_text(json.dumps(pair_builder_payload, indent=2), encoding="utf-8")
            sync_pair_builder_v2_to_website_repo(slate_date, pair_dated_path, pair_latest_path)
        except Exception as exc:
            print(f"⚠️ Pair Builder (System 2) output failed: {exc}", file=sys.stderr)

        print(f"Saved: {txt_path}", file=sys.stderr)
        print(f"Saved: {json_path}", file=sys.stderr)
        # Sync LAST so you can see this at the bottom of PyCharm.
        # This copies to MLB HR MODEL/public/data and tries to commit/push.
        sync_breakdown_to_website_repo_v2(slate_date, slate_label, json_path, txt_path)
        print(f"✅ FINISHED {slate_label.upper()} BOT: output saved + website repo sync attempted.", file=sys.stderr)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
