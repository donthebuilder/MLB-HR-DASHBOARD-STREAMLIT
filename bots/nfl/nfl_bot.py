#!/usr/bin/env python3
"""nfl_bot.py — builds the JSON the NFL side of moonshot reads.

    python nfl_bot.py --mode preseason --out ../public/data/nfl
    python nfl_bot.py --mode week --season 2026 --week 1 --out ../public/data/nfl

Two modes, because August and October are not the same problem.

PRESEASON  Starters play two series. Weekly form does not exist and pretending
           otherwise would be inventing numbers. So the boards are built from
           last season's per-game baselines — the futures read — and every row
           is stamped `carryover: true` so the site can say so out loud.

WEEK       The real thing: trailing form, injuries, game context, the seven
           models scored as documented in SCORING.md.

Writes small files on purpose. moonshot-mlb is a read-only site and the MLB
side already learned the cost of committing 100MB payloads to a repo a
browser has to fetch.
"""
from __future__ import annotations
import argparse
import datetime as dt
import bisect
import json
import math
import os
import platform
import subprocess
import uuid
from pathlib import Path
from statistics import NormalDist

import polars as pl

import nfl_espn
import nfl_pbp
from nfl_splits import splits_for, SPLIT_PAIRS, SPLIT_LABELS
import nfl_dvp
import nfl_gamelog
import nfl_coverage
import nfl_explosive
import nfl_field
import nfl_picks
from nfl_features import build, season_baseline, PLAYER_FORM, USAGE_FORM
from nfl_scoring import MODELS, OUTCOME, score, derive, _pctile

# MODEL FOUNDATION (2026-08-24) -- the NFL side of the same provenance work
# bots/model_registry.py / bots/config_fingerprint.py did for MLB on
# 2026-08-21. Same defensive-import pattern mlb_dashboard.py uses for
# MODEL_REGISTRY/CONFIG_FINGERPRINT: a broken/missing module must never take
# down a slate build over a metadata-only failure.
try:
    import nfl_registry as NFL_REGISTRY
except Exception as _nfl_registry_exc:
    NFL_REGISTRY = None
    print(f"nfl_registry import failed ({_nfl_registry_exc}); model_versions will be empty this run.")

try:
    import nfl_config_fingerprint as NFL_CONFIG_FINGERPRINT
except Exception as _nfl_config_fingerprint_exc:
    NFL_CONFIG_FINGERPRINT = None
    print(f"nfl_config_fingerprint import failed ({_nfl_config_fingerprint_exc}); config_hash will be empty this run.")

PHX = dt.timezone(dt.timedelta(hours=-7))

# What the Research tab shows. Order is the column order on the site.
# (column, short label, description, decimal places, render as percent?)
#
# `dp` is NOT cosmetic. DenseTable defaults to toFixed(0), so every rate on
# this table -- target share, xTD, TDs per game, TDoE -- rendered as 0 or 1
# and the whole board read as noise. Precision is a property of the stat, so
# it's declared here with the stat and shipped in the payload.
RESEARCH = [
    ("f_target_share", "TGT%", "Share of his team's targets", 1, True),
    ("f_wopr", "WOPR", "Weighted opportunity — target share + air yards share", 3, False),
    ("f_targets", "TGT", "Targets per game", 1, False),
    ("f_receptions", "REC", "Receptions per game", 1, False),
    ("f_receiving_yards", "RECYD", "Receiving yards per game", 1, False),
    ("f_receiving_air_yards", "AIRYD", "Air yards per game — depth of target", 1, False),
    ("f_receiving_20", "20+", "Receptions of 20+ yards per game", 2, False),
    ("f_carries", "CAR", "Carries per game", 1, False),
    ("f_rushing_yards", "RUYD", "Rushing yards per game", 1, False),
    ("f_rz_opp", "RZ", "Red-zone touches per game", 2, False),
    ("f_gl_opp", "GL", "Goal-line touches — inside-10 targets, inside-5 carries", 2, False),
    ("f_xtd", "xTD", "Expected TDs per game from field position", 2, False),
    ("f_td_actual", "TD", "Actual TDs per game", 2, False),
    ("td_regression", "TDoE", "Expected minus actual — positive means he's due", 2, False),
    ("f_ngs_avg_separation", "SEP", "Average separation at the catch point (NGS)", 2, False),
    ("f_ngs_avg_yac_above_expectation", "YACOE", "YAC above expected (NGS)", 2, False),
    ("f_ngs_rush_yards_over_expected_per_att", "RYOE", "Rush yards over expected per attempt (NGS)", 2, False),
    ("f_passing_yards", "PAYD", "Passing yards per game", 1, False),
    # PASSING TOUCHDOWNS (2026-09-03). `passing_tds` has been rolled into the
    # feature table by PLAYER_FORM since the NFL bot was written; it was simply
    # never published, because no market scores off it. That was fine while the
    # only consumer was TUDDY's own boards.
    #
    # FRANCHISE is a second consumer and it broke on the gap. `td_actual` is
    # `td_rec + td_rush` (nfl_features.usage) -- receiving plus rushing, never
    # passing -- so the TD column is a QUARTERBACK'S RUSHING TDs and nothing
    # else. FRANCHISE's fantasy projection reads these per-game stats, so every
    # QB projection was low by roughly 6-8 points a game, on the draft board,
    # the lineup page and the coach page alike. Publishing this one column
    # closes all three at once, and it is additive with TD rather than
    # overlapping it.
    ("f_passing_tds", "PATD", "Passing touchdowns per game", 2, False),
    ("f_attempts", "ATT", "Pass attempts per game", 1, False),
    ("f_passing_cpoe", "CPOE", "Completion % over expected", 1, False),
    ("f_fg_made", "FGM", "Field goals made per game", 2, False),
    ("f_pat_made", "PAT", "Extra points made per game", 2, False),
]


def _num(v):
    """JSON-safe number. NaN/inf become null rather than invalid JSON."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 3) if math.isfinite(f) else None


# ── preseason: last season's baselines, honestly labelled ─────────────────────

def preseason_rows(prior_season: int, teams: set[str]) -> pl.DataFrame:
    """Per-player baselines from the completed season, for the teams playing."""
    import nflreadpy as nfl
    base = season_baseline(prior_season)
    who = (nfl.load_player_stats(seasons=[prior_season], summary_level="week")
             .filter(pl.col("season_type") == "REG")
             .group_by("player_id").agg(
                 pl.col("player_display_name").last().alias("name"),
                 pl.col("position").last().alias("position"),
                 pl.col("team").last().alias("team")))
    # 2026 rosters place the player on the team he's actually on now.
    try:
        cur = (nfl.load_rosters_weekly(seasons=[prior_season + 1])
                 .group_by("gsis_id").agg(pl.col("team").last().alias("team_now")))
        who = who.join(cur, left_on="player_id", right_on="gsis_id", how="left") \
                 .with_columns(pl.coalesce(["team_now", "team"]).alias("team"))
    except Exception:
        pass
    full = base.join(who, on="player_id", how="inner")
    ren = {c: "f_" + c[2:] for c in full.columns if c.startswith("b_") and c != "b_gp"}
    full = full.rename(ren).with_columns(
        pl.lit(1).cast(pl.Int8).alias("is_carryover"),
        pl.lit(0).cast(pl.Int8).alias("inj_q"),
    )
    # (slate, league reference). The reference is EVERY qualified player, not
    # just the teams on this card — that's what makes the score absolute.
    return full.filter(pl.col("team").is_in(list(teams))), full


def _fill_missing(d: pl.DataFrame) -> pl.DataFrame:
    """Preseason has no game context and no NGS window — neutralise, don't fake."""
    needed = {
        "implied_total": None, "total_line": None, "spread": 0.0, "indoors": 0,
        "wind_mph": 0.0, "f_opp_d_rec_td": None, "f_opp_d_rush_td": None,
        "f_opp_d_pass_yds": None, "f_opp_d_rush_yds": None,
        "f_tm_fg_drive_rate": None, "f_tm_rz_td_rate": None, "f_tm_drives": None,
    }
    add = []
    # _pctile ranks .over("week"); preseason is one slate, so give it one.
    if "week" not in d.columns:
        add.append(pl.lit(0).cast(pl.Int32).alias("week"))
    for c, dflt in needed.items():
        if c not in d.columns:
            add.append(pl.lit(dflt).cast(pl.Float64).alias(c))
    for c, _, _, _dp, _pct in RESEARCH:
        if c not in d.columns and c != "td_regression":
            add.append(pl.lit(None).cast(pl.Float64).alias(c))
    return d.with_columns(add) if add else d


def _league_pct(ref_vals: list[float], invert: bool):
    """Percentile of a value against a FIXED league population.

    This is the difference between a score that means something and a score
    that's just a row number. Ranking inside the slate forces a uniform 0-100
    every week: the best goal-line back among six teams gets a 100 whether he's
    Bijan Robinson or a backup, and a 100 on a three-game preseason card reads
    identically to a 100 on a full Sunday. The MLB side never had this problem
    because hr_score is an absolute model output — 78+ is elite, most of the
    board lives in the 40s and 50s, and a weak slate genuinely scores low.

    So each component is ranked against every qualified player in the league,
    not against whoever happens to be playing. A thin slate now scores thin.
    """
    vals = sorted(v for v in ref_vals if v is not None and math.isfinite(v))
    n = len(vals)
    if n == 0:
        return lambda x: None

    def pct(x):
        if x is None:
            return None
        try:
            f = float(x)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        lo = bisect.bisect_left(vals, f)
        hi = bisect.bisect_right(vals, f)
        q = ((lo + hi) / 2) / n          # midpoint handles ties without bias
        return (1.0 - q) if invert else q
    return pct


# MLB hr_score, measured off a published slate: min ~24, median ~45, max ~57,
# and its ladder puts A+ at 78 / A at 70 / A- 62 / B+ 54 / B 46. That is a
# roughly normal spread centred just under 50, NOT a uniform 0-100. Match it.
SCALE_MEAN = 47.0
SCALE_SD = 11.0
_ND = NormalDist()


def _mlb_scale(ref_raw: list[float]):
    """Map a composite onto the MLB score distribution."""
    vals = sorted(v for v in ref_raw if v is not None and math.isfinite(v))
    n = len(vals)

    def to_score(x):
        if x is None or not math.isfinite(float(x)):
            return None
        lo = bisect.bisect_left(vals, float(x))
        hi = bisect.bisect_right(vals, float(x))
        q = ((lo + hi) / 2) / n if n else 0.5
        q = min(max(q, 0.0005), 0.9995)         # keep inv_cdf finite
        return round(max(5.0, min(95.0, SCALE_MEAN + SCALE_SD * _ND.inv_cdf(q))), 1)
    return to_score


def score_all(tbl: pl.DataFrame, context_ok: bool, ref: pl.DataFrame | None = None) -> dict:
    """Score every market. When context is missing, its weight is redistributed
    across the components that ARE present rather than scored as zero.

    `ref` is the league population every component is ranked against. Without
    it the slate ranks against itself and the top row is always ~100.
    """
    out = {}
    for key, m in MODELS.items():
        avail, missing = {}, []
        for raw, w in m["w"].items():
            col = raw.lstrip("-")
            has = col in tbl.columns and tbl[col].null_count() < tbl.height
            if has and (context_ok or not col in CONTEXT_COLS):
                avail[raw] = w
            else:
                missing.append(raw)
        if not avail:
            continue
        total = sum(avail.values())
        avail = {k: v / total for k, v in avail.items()}   # renormalise to 1.0
        d = derive(tbl).filter(pl.col("position").is_in(m["pos"]))
        if d.height == 0:
            continue
        # The reference is the same positions, league-wide — comparing a tight
        # end's red-zone work against quarterbacks would be meaningless.
        rd = None
        if ref is not None:
            rd = derive(ref).filter(pl.col("position").is_in(m["pos"]))
            if rd.height < 30:           # too thin to be a population
                rd = None

        parts, ref_parts = [], []
        for raw, w in avail.items():
            inv = raw.startswith("-")
            col = raw.lstrip("-")
            nm = f"c_{col}"
            if rd is not None and col in rd.columns:
                fn = _league_pct(rd[col].to_list(), inv)
                d = d.with_columns(
                    pl.col(col).map_elements(fn, return_dtype=pl.Float64).alias(nm))
                d = d.with_columns(
                    pl.when(pl.col(nm).is_null()).then(_pctile(d, col, inv))
                      .otherwise(pl.col(nm)).alias(nm))
                # the reference gets the SAME transform, so its composite is
                # measured on the same axis as the slate's
                rd = rd.with_columns(
                    pl.col(col).map_elements(fn, return_dtype=pl.Float64)
                      .fill_null(0.5).alias(nm))
                ref_parts.append(pl.col(nm) * w)
            else:
                d = d.with_columns(_pctile(d, col, inv).alias(nm))
            parts.append(pl.col(nm) * w)
        tot = parts[0]
        for p in parts[1:]:
            tot = tot + p

        # ORDER MATTERS. `tot` is a LAZY expression over the c_ columns, so it
        # must be materialised BEFORE those columns are rescaled to 0-100 --
        # otherwise it resolves against the already-scaled values and the score
        # comes out x10000 (98.88 shipping as 9888).
        d = d.with_columns(tot.alias("_raw"))

        # ── PUT IT ON THE MLB SCALE ──────────────────────────────────────────
        # A weighted average of percentiles is not uniform: averaging k of them
        # piles up near 0.5, so it can't be read as a rank and it can't be
        # graded on the MLB ladder. Two steps fix that.
        #
        #   1. rank the composite against the LEAGUE's composites -> a true
        #      percentile, absolute rather than slate-relative
        #   2. push that percentile through the normal quantile function and
        #      land it at mean 47, sd 11
        #
        # Step 2 is what makes an NFL 78 mean what an MLB 78 means. hr_score
        # runs roughly 24-75 and sits around 45, which is why its ladder puts
        # A+ at 78 and B at 46 — on a uniform 0-100 percentile those cutoffs
        # would hand out A+ to the top fifth of every slate. After this an 80
        # is genuinely ~99.7th percentile of the league, and a thin card full
        # of backups scores in the 30s and 40s the way a bad MLB slate does.
        if ref_parts and rd is not None:
            rtot = ref_parts[0]
            for rp in ref_parts[1:]:
                rtot = rtot + rp
            ref_raw = rd.with_columns(rtot.alias("_raw"))["_raw"].to_list()
            to_scale = _mlb_scale(ref_raw)
            d = d.with_columns(
                pl.col("_raw").map_elements(to_scale, return_dtype=pl.Float64).alias("score"))
        else:
            d = d.with_columns((pl.col("_raw") * 100).alias("score"))
        # Components go out as 0-100 too, not the raw 0-1 percentile. The modal
        # renders them as "90 = top 10% of this slate on that input"; shipping
        # the fraction made every one of them round to 1.
        d = d.with_columns([(pl.col(f"c_{r.lstrip('-')}") * 100).round(0)
                            .alias(f"c_{r.lstrip('-')}") for r in avail])
        out[key] = {"df": d, "dropped": missing,
                    "weights": {k.lstrip("-"): round(v, 3) for k, v in avail.items()}}
    return out


CONTEXT_COLS = {"implied_total", "total_line", "opp_td_soft", "opp_pass_soft",
                "opp_rush_soft", "pass_script", "run_script", "kick_env",
                "f_tm_fg_drive_rate", "f_tm_rz_td_rate", "f_tm_drives"}


# One wave of football, not the rest of the calendar.
#
# 2026-08-14 shipped 66 games and 545 players on a card labelled "Preseason ·
# Aug 14": the filter was "every game still to come", which in mid-August is
# all of preseason weeks 2 and 3. Most of that card was a week away and the
# boards were ranking players who weren't playing for days.
#
# Anchors on the earliest kickoff STILL AHEAD and takes four days from there,
# which covers a Thursday-through-Monday wave and stops before the next one.
# Deliberately NOT keyed on ESPN's `week` field: fetch() reads that off the
# payload's top-level block, so every game in one response carries the same
# number and grouping by it would be a no-op on a whole-season fetch.
WAVE_DAYS = 4


def _next_wave(games: list[dict], today: dt.date) -> list[dict]:
    day = lambda g: str(g.get("kickoff", ""))[:10]
    ahead = [g for g in games if day(g) and day(g) >= today.isoformat()]
    if not ahead:
        return []
    try:
        first = dt.date.fromisoformat(min(day(g) for g in ahead))
    except ValueError:
        return ahead
    cutoff = (first + dt.timedelta(days=WAVE_DAYS)).isoformat()
    return [g for g in ahead if day(g) <= cutoff]


def build_payload(mode: str, season: int, week: int | None, out_dir: Path) -> dict:
    now = dt.datetime.now(PHX)

    if mode == "preseason":
        games = nfl_espn.fetch(seasontype=1, year=season)
        upcoming = _next_wave(games, now.date())
        # This fallback used to be unreachable: the line above ended in
        # `or games`, so `upcoming` was never empty and the seed never loaded.
        seed = out_dir / "slate_seed.json"
        if not upcoming and seed.exists():
            upcoming = json.loads(seed.read_text()).get("games", [])
        # REST DAYS (2026-08-28, B7). `games` here is already the WHOLE
        # preseason schedule (no week filter above), so it's the right pool
        # to compute a team's prior game from — no extra fetch needed.
        upcoming = nfl_espn.attach_rest_days(games, upcoming)
        teams = {t for g in upcoming for t in (g["home"], g["away"])}
        slate, league = preseason_rows(season - 1, teams)
        tbl = _fill_missing(slate)
        ref = _fill_missing(league)
        context_ok = False
        label = f"Preseason · {now:%b %-d}"
    else:
        tbl = build(season)
        if week:
            tbl = tbl.filter(pl.col("week") == week)
        games = nfl_espn.fetch(seasontype=2, year=season, week=week)
        # REST DAYS (2026-08-28, B7). Unlike the preseason branch, `games`
        # above is scoped to ONE week — a team's prior game lives in an
        # earlier week, so this needs its own whole-season-schedule fetch
        # (schedule-only, not per-player stats — cheap next to everything
        # else this function already calls). Week 1 legitimately comes back
        # with no prior game to measure from (rest_days=None, not a bug —
        # there's nothing before Week 1 in this pool on purpose; the
        # preseason-to-Week-1 turnaround isn't a comparable "short week" the
        # way an in-season Thursday game is).
        season_games = nfl_espn.fetch(seasontype=2, year=season)
        upcoming = nfl_espn.attach_rest_days(season_games or games, games)
        # PBP DRIVE STATE (2026-08-28). A second, independent drive-state
        # source on top of nfl_espn.py's live (but unverified-shape) ESPN
        # situation guess -- nflreadpy's play-by-play schema is well-
        # established and already trusted elsewhere in this codebase
        # (nfl_field.py). Not real-time (nflverse updates during and after
        # game days, not play-by-play as it happens), so it's a same-day
        # confirmation/backfill layer, not a live feed -- see nfl_pbp.py's
        # module docstring. Fails soft to untouched rows on any error.
        try:
            upcoming = nfl_pbp.attach_pbp_state(upcoming, season, week)
        except Exception as exc:
            print(f"nfl_pbp unavailable ({type(exc).__name__}: {exc})")
        context_ok = True
        label = f"Week {week}"
        tbl = tbl.with_columns(pl.col("player_display_name").alias("name"))
        # In-season the reference is every player league-wide in the same week,
        # which is what build() already returns before the week filter.
        ref = build(season)
        if week:
            ref = ref.filter(pl.col("week") == week)
        ref = ref.with_columns(pl.col("player_display_name").alias("name"))

    # SPLITS. Same season the form comes from, so a row's splits and its
    # baseline are describing the same football rather than two different years.
    try:
        splits = splits_for(season - 1 if mode == "preseason" else season)
    except Exception as exc:
        print(f"splits unavailable ({type(exc).__name__}: {exc}) — continuing without")
        splits = {}

    scored = score_all(tbl, context_ok, ref)

    # merge every market's score onto one player row
    players: dict[str, dict] = {}
    for key, blob in scored.items():
        d = blob["df"]
        comp_cols = [c for c in d.columns if c.startswith("c_")]
        for r in d.iter_rows(named=True):
            pid = r["player_id"]
            p = players.setdefault(pid, {
                "player_id": pid,
                "name": r.get("name") or r.get("player_display_name") or "—",
                "team": r.get("team"),
                "opp": r.get("opponent_team"),
                "position": r.get("position"),
                "carryover": bool(r.get("is_carryover") or 0),
                "questionable": bool(r.get("inj_q") or 0),
                # SAMPLE GATE. A goal-line vulture with 0.3 targets a game can
                # percentile-rank above a every-down back, because a rate built
                # on four touches has no business sitting at the same visual
                # weight as one built on two hundred. The site dims these rows
                # rather than hiding them — same call the MLB DenseTable makes.
                "low_sample": (
                    (r.get("f_targets") or 0) + (r.get("f_carries") or 0) < 3.0
                    and (r.get("position") in ("RB", "WR", "TE"))
                ),
                "scores": {}, "components": {}, "stats": {},
                "splits": splits.get(pid, {}),
            })
            p["scores"][key] = _num(r.get("score"))
            p["components"][key] = {c[2:]: _num(r.get(c)) for c in comp_cols
                                    if _num(r.get(c)) is not None}
            for col, short, _, _dp, _pct in RESEARCH:
                v = _num(r.get(col))
                # Skip exact zeros: a running back carries PAYD/ATT/CPOE/FGM/PAT
                # as 0.000 and a wall of zeroes in the modal reads as data when
                # it's really "this stat doesn't apply to him".
                if col in r and v is not None and v != 0:
                    p["stats"][short] = v

    # preseason: attach opponent from the game list
    if mode == "preseason":
        opp = {}
        for g in upcoming:
            opp[g["home"]] = g["away"]
            opp[g["away"]] = g["home"]
        for p in players.values():
            p["opp"] = opp.get(p["team"])

    rows = sorted(players.values(),
                  key=lambda p: -(p["scores"].get("TD") or 0))

    # ── the research layer ───────────────────────────────────────────────────
    # Written as SEPARATE files rather than folded into week.json. The slate is
    # what every tab needs on load; game logs and defence-vs-position are what
    # ONE tab needs, and a 500 KB payload the Games tab never reads is 500 KB
    # the Games tab waits for.
    stat_season = season - 1 if mode == "preseason" else season
    extras: dict = {}
    for name, fn in (
        ("dvp", lambda: nfl_dvp.build(stat_season)),
        ("roles", lambda: nfl_dvp.current_roles(stat_season)),
        ("coverage_team", lambda: nfl_coverage.team_profile(stat_season)),
        ("coverage_player", lambda: nfl_coverage.player_vs_coverage(stat_season)),
        ("def_explosive", lambda: nfl_explosive.defense_explosive(stat_season)),
        ("player_explosive", lambda: nfl_explosive.player_explosive(stat_season)),
        ("usage", lambda: nfl_explosive.team_usage(stat_season)),
        ("field", lambda: nfl_field.build(stat_season)),
    ):
        try:
            extras[name] = fn()
        except Exception as exc:
            print(f"{name} unavailable ({type(exc).__name__}: {exc})")
            extras[name] = {}

    return {
        "extras": extras,
        "stat_season": stat_season,
        "mode": mode,
        "season": season,
        "week": week,
        "label": label,
        "built_at": now.isoformat(),
        "built_at_human": now.strftime("%b %-d, %-I:%M %p") + " PHX",
        "context_available": context_ok,
        "games": upcoming,
        "players": rows,
        "markets": [
            {"key": k, "label": m["label"], "bar": m["bar"], "positions": m["pos"],
             "weights": scored.get(k, {}).get("weights", m["w"]),
             "dropped": scored.get(k, {}).get("dropped", [])}
            for k, m in MODELS.items() if k in scored
        ],
        "research_columns": [{"key": s, "label": s, "desc": d, "dp": dp, "pct": pct}
                             for _, s, d, dp, pct in RESEARCH],
        "split_pairs": SPLIT_PAIRS,
        "split_labels": SPLIT_LABELS,
        "counts": {"players": len(rows), "games": len(upcoming)},
    }


# ── MODEL FOUNDATION: run metadata + prediction log ──────────────────────────
#
# One run_id per bot EXECUTION (~12 scheduled firings/week during an active
# game week -- see .github/workflows/nfl.yml's cron list; every firing runs
# the "Build slate" step unconditionally), mirroring build_run_meta() in
# bots/mlb_dashboard.py. Not a straight import of that function or its two
# small helpers below -- mlb_dashboard.py is a 14,000+ line module that
# imports pybaseball and the rest of the MLB dependency tree at module scope,
# and nfl.yml's own requirements-file comment is explicit that a football run
# must never drag that tree onto the runner. So _current_git_sha() and
# _run_env_metadata() are reimplemented here, small and NFL-local, rather
# than reused across that boundary.

def _current_git_sha() -> str:
    """Commit identity for this run -- same git-rev-parse-first,
    GITHUB_SHA-fallback logic as mlb_dashboard.py's _current_git_sha(), and
    the same reason: actions/checkout's `ref: main` resolves to whatever
    main's tip is AT CHECKOUT TIME, which can be newer than GITHUB_SHA (fixed
    at event-trigger time) if a push lands in the gap between trigger and
    checkout. `git rev-parse HEAD` in the tree that is actually executing
    this process cannot have that race. Never raises -- provenance is
    nice-to-have, not worth failing a slate."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent.parent), "rev-parse", "HEAD"],
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


def _run_env_metadata() -> dict:
    """Environment fingerprint for this run -- the NFL sibling of
    mlb_dashboard.py's _run_env_metadata(), reporting nflreadpy's version
    (this bot's own equivalent of "which pybaseball version scored
    tonight") rather than pybaseball's. Nothing here is required for the bot
    to run; a missing package version reads as "unknown" rather than
    raising."""
    env: dict = {"python": platform.python_version()}
    try:
        import importlib.metadata as _ilm
        env["nflreadpy"] = _ilm.version("nflreadpy")
    except Exception:
        env["nflreadpy"] = "unknown"
    return env


def build_nfl_run_meta(mode: str, season: int, week: int | None, args: "argparse.Namespace") -> dict:
    """One run identity per bot execution. Same shape and field names as
    build_run_meta() in bots/mlb_dashboard.py -- see that function's
    docstring and docs/MODELS.md for the policy this embeds.

    run_id shape: "{key}.{HHMMSSZ}.{source}" -- seconds granularity (not
    minutes) because two firings landing in the same minute (the workflow's
    own Sunday cadence has waves 3 hours apart, but a manual re-run could
    still collide with a scheduled one) would otherwise share a run_id;
    GitHub's own run id is included as the source so a run_id is traceable
    back to the exact Actions run (or "local-<8 hex>" off the runner for a
    Mac execution).

    `key` is season+week ("2026-wk03") in week mode, or the run's own UTC
    date in preseason mode -- the natural per-run identity for each mode,
    mirroring MLB's use of slate_date. WEEK CAN BE None IN WEEK MODE: none
    of nfl.yml's 12 scheduled firings pass --week (only a manual
    workflow_dispatch with the week input filled in does), so `mode=="week"`
    with `week is None` is the common case in production, not an edge case
    -- nfl_espn.fetch(week=None) auto-detects the current week from ESPN's
    schedule instead. run_id must not crash over a None the rest of this
    bot already tolerates; it falls back to "wkNA" rather than embedding the
    literal string "None" in every run's identity.
    """
    now = dt.datetime.now(dt.timezone.utc)
    gha_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    source = f"gha-{gha_run_id}" if gha_run_id else f"local-{uuid.uuid4().hex[:8]}"
    if mode == "week":
        key = f"{season}-{('wk%02d' % week) if week else 'wkNA'}"
    else:
        key = now.strftime("%Y-%m-%d")
    run_id = f"{key}.{now.strftime('%H%M%S')}Z.{source}"

    if NFL_REGISTRY is not None:
        model_family = NFL_REGISTRY.MODEL_FAMILY
        model_versions = NFL_REGISTRY.model_versions_snapshot()
        schema_version = NFL_REGISTRY.SCHEMA_VERSION
    else:
        model_family = "unknown"
        model_versions = {}
        schema_version = 0

    config_hash = None
    if NFL_CONFIG_FINGERPRINT is not None:
        try:
            config_hash = NFL_CONFIG_FINGERPRINT.nfl_config_hash(MODELS, [derive, score])
        except Exception as exc:
            print(f"nfl_config_hash() failed ({exc}); config_hash will be empty this run.")

    return {
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "mode": mode,
        "season": season,
        "week": week,
        # Not GITHUB_WORKFLOW's filename (Actions doesn't expose that) -- the
        # workflow's display NAME, matching build_run_meta()'s own field.
        "trigger": os.environ.get("GITHUB_WORKFLOW", "").strip() or "local",
        "git_sha": _current_git_sha(),
        "model_family": model_family,
        "model_versions": model_versions,
        # Explicit {"nfl": None} rather than an absent key when hashing
        # fails, so "we tried and it failed" is distinguishable from "this
        # run predates config_hash existing" at analysis time -- same
        # reasoning as build_run_meta()'s own config_hashes dict.
        "config_hashes": {"nfl": config_hash},
        "schema_version": schema_version,
        "env": _run_env_metadata(),
    }


def build_nfl_prediction_log_lines(run_meta: dict, players: list[dict]) -> list[dict]:
    """One line per player per market scored this run, mirroring
    build_prediction_log_lines() in bots/mlb_dashboard.py's shape (identity
    keys, run/version metadata, then the score) but built off the already-
    merged `payload["players"]` rows (one dict per player, `scores`/
    `components` keyed by market) rather than a flat per-market DataFrame,
    since that is the shape build_payload() already produces here."""
    lines: list[dict] = [run_meta]
    for pl_row in players:
        if not isinstance(pl_row, dict):
            continue
        for market, sc in (pl_row.get("scores") or {}).items():
            if sc is None:
                continue
            lines.append({
                "player_id": pl_row.get("player_id"),
                "player": pl_row.get("name"),
                "team": pl_row.get("team"),
                "opp": pl_row.get("opp"),
                "position": pl_row.get("position"),
                "market": market,
                "run_id": run_meta.get("run_id"),
                "generated_at": run_meta.get("generated_at"),
                "model_version": (run_meta.get("model_versions") or {}).get(market),
                "config_hash": (run_meta.get("config_hashes") or {}).get("nfl"),
                "score": sc,
                "components": (pl_row.get("components") or {}).get(market, {}),
                "carryover": pl_row.get("carryover", False),
                "questionable": pl_row.get("questionable", False),
                "low_sample": pl_row.get("low_sample", False),
            })
    return lines


def write_nfl_prediction_log(run_meta: dict, players: list[dict], out_dir: Path, prefix: str = "") -> "Path | None":
    """Write {out_dir}/{prefix}prediction_log_{run_id}.jsonl for this run --
    the NFL sibling of write_prediction_log() in bots/mlb_dashboard.py.
    nfl_-prefixed (via `prefix`, already "nfl_" in production per nfl.yml)
    specifically so this file never collides with MLB's own
    prediction_log_*.jsonl glob in .github/scripts/publish_data.sh. Writes
    straight into out_dir (already public/data/current in production --
    unlike MLB's OUT_DIR-then-sync-to-website-repo dance, nfl_bot.py's
    --out already points at the publish directory, so there is no separate
    sync step to wire this into). Best-effort: a logging failure must never
    block the slate the rest of main() already wrote."""
    try:
        run_id = run_meta["run_id"]
        path = out_dir / f"{prefix}prediction_log_{run_id}.jsonl"
        lines = build_nfl_prediction_log_lines(run_meta, players)
        with path.open("w", encoding="utf-8") as f:
            for obj in lines:
                f.write(json.dumps(obj, default=str))
                f.write("\n")
        return path
    except Exception as exc:
        print(f"nfl prediction log write failed: {exc}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preseason", "week"], default="preseason")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--out", type=str, default="../public/data/nfl")
    # publish_data.sh only ever copies files out of public/data/current/, by
    # name. Rather than fork that script -- it handles concurrent publishers
    # and orphan-branch force-push correctly and is not worth reimplementing --
    # the workflow writes straight into current/ with an nfl_ prefix and the
    # three filenames get added to its PUBLISH_FILES list.
    ap.add_argument("--prefix", type=str, default="")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    # In week mode the scheduled workflow passes no --week (it can't know one).
    # Without this the season-long table went unfiltered, the schedule fetch
    # returned every game of the year, and the card was labelled "Week None".
    if a.mode == "week":
        a.week = nfl_espn.resolve_week(a.season, a.week)
    payload = build_payload(a.mode, a.season, a.week, out)

    # MODEL FOUNDATION (2026-08-24). One run identity per execution, and a
    # durable per-run prediction log -- see build_nfl_run_meta() /
    # write_nfl_prediction_log() above. Computed right after the payload so
    # every player row this run scored is available to log; before the
    # extras pop below since that only touches payload["extras"], never
    # payload["players"].
    run_meta = build_nfl_run_meta(a.mode, a.season, a.week, a)
    pred_log_path = write_nfl_prediction_log(run_meta, payload.get("players", []), out, a.prefix)
    if pred_log_path is not None:
        print(f"  prediction log: {pred_log_path.name}")

    extras = payload.pop("extras", {})
    stat_season = payload.get("stat_season")

    # Player-level research is filtered to the slate. Team-level (defence vs
    # position, coverage shells, explosive allowed) is NOT: you look up any
    # defence from the matchup tab, and 32 teams is small. The player maps are
    # the league's 349 receivers, of whom this card has ~100 — shipping the
    # other 249 quadrupled the file for rows nothing can render.
    on_slate = {pp["player_id"] for pp in payload.get("players", [])}
    if extras.get("field"):
        for k in ("player_pass", "player_rush"):
            if extras["field"].get(k):
                extras["field"][k] = {i: v for i, v in extras["field"][k].items() if i in on_slate}
    for k in ("coverage_player", "player_explosive", "roles"):
        if extras.get(k):
            extras[k] = {i: v for i, v in extras[k].items() if i in on_slate}
    if extras.get("usage"):
        teams_on_slate = {g[s] for g in payload.get("games", []) for s in ("home", "away")}
        extras["usage"] = {t: v for t, v in extras["usage"].items() if t in teams_on_slate}

    # Defence-vs-position, coverage, explosive and usage: one file, one tab.
    (out / f"{a.prefix}matchup.json").write_text(json.dumps({
        "season": stat_season,
        "dvp": extras.get("dvp", {}),
        "dvp_roles": nfl_dvp.ROLE_ORDER,
        "dvp_stats": nfl_dvp.DVP_STATS,
        "dvp_labels": nfl_dvp.STAT_LABELS,
        # Which DvP row each player on this card actually belongs to.
        "roles": extras.get("roles", {}),
        "coverage_team": extras.get("coverage_team", {}),
        "coverage_player": extras.get("coverage_player", {}),
        "def_explosive": extras.get("def_explosive", {}),
        "player_explosive": extras.get("player_explosive", {}),
        "usage": extras.get("usage", {}),
        "field": extras.get("field", {}),
        "zones_pass": nfl_field.ZONES_PASS,
        "zones_rush": nfl_field.ZONES_RUSH,
        "rush_labels": nfl_field.RUSH_LABEL,
        "depth_labels": nfl_field.DEPTH_LABEL,
    }, separators=(",", ":")))

    # Game logs, for the hit-rate chart. Only the players on this slate — the
    # league's full log is 2,100 players and nobody is looking at 2,000 of them.
    try:
        on_slate = {p["player_id"] for p in payload["players"]}
        logs = nfl_gamelog.build([stat_season - 1, stat_season])
        logs = {k: v for k, v in logs.items() if k in on_slate}
    except Exception as exc:
        print(f"game logs unavailable ({type(exc).__name__}: {exc})")
        logs = {}
    (out / f"{a.prefix}logs.json").write_text(json.dumps({
        "bars": nfl_gamelog.MARKET_VALUE, "logs": logs}, separators=(",", ":")))

    # ── the pick card ─────────────────────────────────────────────────────────
    # Built from the FINISHED payload rows, not re-scored. The MLB side learned
    # that two surfaces deriving "the pick" separately eventually name different
    # players and nobody can tell which is lying.
    #
    # Edges come off the report card if one has been published. That file is a
    # full two-season backtest and only rebuilds when the WEIGHTS change, so a
    # live Sunday wave reads the last one rather than spending minutes
    # recomputing a number that hasn't moved.
    edges = {}
    rc = out / f"{a.prefix}report_card.json"
    if rc.exists():
        try:
            edges = (json.loads(rc.read_text()) or {}).get("card_edges", {}) or {}
        except Exception as exc:
            print(f"report card unreadable ({type(exc).__name__}) — card ships without edges")
    card = nfl_picks.build(payload["players"], edges=edges, depth=nfl_picks.DEPTH)
    (out / f"{a.prefix}picks.json").write_text(json.dumps({
        # a.season / a.week / a.mode — NOT bare names. Those are locals of
        # build_payload; here they were NameErrors, and the very first live run
        # of this workflow died on this line (2026-08-15, Donovan's Actions
        # log) after matchup.json and logs.json had already been written — so
        # the crash also threw away two files that had built fine. main() sees
        # the argparse namespace and the payload, nothing else.
        "season": a.season, "week": a.week, "mode": a.mode,
        "built_at": payload["built_at"],
        "built_at_human": payload["built_at_human"],
        "depth": nfl_picks.DEPTH,
        "label": payload["label"],
        "card": card,
    }, separators=(",", ":")))
    print(nfl_picks.summary(card))

    (out / f"{a.prefix}week.json").write_text(json.dumps(payload, separators=(",", ":")))
    (out / f"{a.prefix}meta.json").write_text(json.dumps({
        "built_at": payload["built_at"],
        "built_at_human": payload["built_at_human"],
        "mode": payload["mode"], "label": payload["label"],
        "counts": payload["counts"],
        # MODEL FOUNDATION (2026-08-24). nfl_meta.json is already a dict
        # (unlike today_slim.json's bare list on the MLB side -- see
        # sync_model_foundation_outputs_to_website_repo()'s docstring in
        # bots/mlb_dashboard.py for why THAT required a companion
        # *_run_meta.json file), so run_meta nests straight in here rather
        # than needing its own nfl_run_meta.json companion file. Nested
        # under its own key, not flattened, so a future top-level meta.json
        # field can never collide with a run_meta field name.
        "run_meta": run_meta,
    }, indent=2))
    for f in ("week", "matchup", "logs", "meta", "picks"):
        fp = out / f"{a.prefix}{f}.json"
        if fp.exists():
            print(f"  {fp.name:22} {fp.stat().st_size/1024:7.0f} KB")
    print(f"{payload['counts']['players']} players, {payload['counts']['games']} games, "
          f"stats from {stat_season}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
