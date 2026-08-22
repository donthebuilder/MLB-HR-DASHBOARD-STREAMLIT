#!/usr/bin/env python3
"""
🕵️ MISSED SIGNALS — what predicts a home run AFTER the model has spoken.

2026-08-09, Donovan: "there has to be a way to find missed signals, things that
we miss on HR hitters."

There is, and it is not the scan we have been running. Every audit so far has
asked: "does this field separate homers?" That question has a fatal flaw for
this purpose — season_iso separates homers beautifully, and hr_score already
knows about season_iso. Finding it again tells you nothing you can act on. A
univariate scan mostly rediscovers the model's own inputs and ranks them by how
loudly they shout.

THE RIGHT QUESTION IS CONDITIONAL: among hitters the model scored THE SAME,
does this field still separate the ones who went deep from the ones who didn't?

That is a residual test, and only a residual can be a missed signal. If a field
still sorts inside a score band, the model is not using it — or is using it
wrongly — and that is free information sitting on the table. If it goes flat
inside the band, the model has already priced it, however impressive it looked
in a whole-pool scan.

    WHOLE-POOL (what we did)          WITHIN-BAND (what this does)
    "does ISO predict homers?"        "among hitters the model rated 70-80,
    yes, hugely — and the model        does ISO STILL predict homers?"
    already uses it. Useless.          if yes, the model is underusing it.

HOW IT WORKS
  1. Split every graded pick into hr_score bands (deciles of the score, so
     each band holds hitters the model considered interchangeable).
  2. Inside each band, split the field at its median and compare HR rates.
  3. Pool the within-band differences across bands — this is a stratified
     comparison, the same trick a Mantel-Haenszel test uses, and it removes
     the model's own opinion from the comparison entirely.
  4. Report the lift with a confidence interval and a permutation p-value.

WHY A PERMUTATION TEST. ~170 fields get tested, so about 8 will clear p<0.05
by chance alone. The permutation shuffles the outcome WITHIN each band a
thousand times and asks how often a lift this big appears by luck, and the
report applies a Benjamini-Hochberg correction on top. Without that this tool
would confidently hand back eight fantasies every run.

WHAT IT CANNOT DO, stated plainly:
  · Graded rows are the bot's DESIGNATED PICKS, not the full slate. Every
    number is conditional on the bot having liked the hitter already.
  · Correlated fields will surface together. season_slg and hr_per_pa are two
    views of one thing; if both rank, that is one finding, not two.
  · It finds ASSOCIATION inside a band, not causation, and not a weight. A
    field near the top is a candidate for the blend, and the way to confirm it
    is to re-run the real pipeline with it, not to trust this number.

Usage:
    python3 bots/missed_signals.py --dir ~/Desktop/results
    python3 bots/missed_signals.py --bands 8 --min-n 300 --outcome hr
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

DEFAULT_DIRS = [
    Path.home() / "Desktop" / "results",
    Path.home() / "results",
    Path(__file__).resolve().parent.parent / "public" / "data" / "current",
]

# Fields that are outcomes, or restatements of the score itself. Testing the
# score against itself inside its own band is meaningless, and testing an
# outcome is circular.
BLOCK_PREFIX = ("actual_", "got_", "hrr_")
BLOCK_EXACT = {
    "hr_score", "hr_score_v2", "hr_score_legacy", "hr_score_old", "hr_score_pure",
    "hr_score_delta", "overall_score", "overall_score_legacy", "player_id",
    "game_pk", "jersey_number", "is_final", "rank",
}

# ── THE MODEL'S OWN OUTPUTS ARE NOT SIGNALS (2026-08-09, second pass) ───────
# The first run reported 38 "surviving" fields out of 164. That is not a
# finding, it is a broken test — a well-specified residual scan on a working
# model should return a handful, and 23% means the control is leaking.
#
# One large leak: seven of the thirty-eight were the model LOOKING AT ITSELF.
# self_check_hr_score, alt_hr_score, matchup_score, damage_conversion_score,
# contact_score, contact_score_legacy, hr_pa_score — every one of those is a
# composite the pipeline computes FROM the same inputs hr_score uses. Learning
# that a second blend of the same ingredients also predicts homers tells you
# nothing you can act on; you cannot "add" it, it is already in there.
#
# A missed signal has to be an INPUT the model could weight differently, not
# an output it already produces.
DERIVED_SCORES = {
    "self_check_hr_score", "alt_hr_score", "best_blend_score", "top_board_score_v2",
    "top_pick_score_v2", "matchup_score", "matchup_power_score", "matchup_tier",
    "damage_conversion_score", "contact_score", "contact_score_v2",
    "contact_score_legacy", "hit_score", "hit_score_v2", "hit_score_legacy",
    "hrr_score", "hrr_score_v2", "hrr_score_legacy", "hr_pa_score",
    "recent_hr_form_score", "hrw_score", "hrw_zone", "batted_ball_power_score",
    "pitcher_attack_score", "high_confidence_hr_score", "hr_confidence",
    "hr_due_score", "expected_hrs_recent_window", "data_quality_score",
    "batter_vs_bullpen_score", "best_blend_score", "pitch_mix_score",
    "lineup_context_score", "top_board_bucket", "numerology_score",
}

# ── ONE FINDING SHOULD REPORT ONCE ─────────────────────────────────────────
# season_iso, season_slg, season_ops, season_hr, hr_per_pa and pa_per_hr are
# six views of "this man has power". The first run listed all six as separate
# discoveries, which triples the apparent count and buries anything genuinely
# new underneath. Fields are grouped and only the strongest member of each
# group is reported, with the rest named beside it.
FAMILIES = {
    "season power":   ["season_iso", "season_slg", "season_ops", "season_hr",
                       "hr_per_pa", "pa_per_hr", "iso_vs_rhp", "iso_vs_lhp"],
    "recent homers":  ["last5_hr", "last7_hr", "last10_hr", "l20pa_hr",
                       "last5_xbh", "last7_xbh", "last10_xbh", "l20pa_xbh"],
    "contact quality":["recent_hard_hit_rate", "l10_hard_hit_rate", "l5_hard_hit_rate",
                       "recent_ev", "l25pa_avg_ev", "recent_barrel_rate",
                       "l10_barrel_rate", "l5_barrel_rate", "recent_xwoba",
                       "l10_xwoba", "l5_xwoba", "recent_ideal_hr_contact"],
    "pull":           ["l5_pull_rate", "l10_pull_rate", "l20pa_pull_rate", "recent_pull_rate"],
    "the arm, general":["pitcher_era", "pitcher_whip", "pitcher_hr9", "pitcher_hr_allowed",
                        "pitcher_babip", "pitcher_k9", "pitcher_k_rate"],
    "the arm vs this side":["pitcher_side_ops", "pitcher_side_slug", "pitcher_hr9_vs_lhb",
                            "pitcher_hr9_vs_rhb", "pitcher_whip_vs_lhb", "pitcher_whip_vs_rhb",
                            "pitcher_weak_side_gap", "pitcher_weak_side_score"],
    "the arm's contact allowed":["pitcher_ev_allowed", "pitcher_hardhit_allowed",
                                 "pitcher_barrel_allowed", "pitcher_375_allowed",
                                 "pitcher_400_allowed", "pitcher_zone_damage_score",
                                 "pitcher_spot_damage_score"],
    "park":           ["park_hr_factor", "park_factor", "park_barrel_factor",
                       "park_hardhit_factor", "park_dist_factor", "park_hits_factor",
                       "park_k_factor"],
    "lineup":         ["lineup_spot", "lineup_context_before_count",
                       "lineup_context_after_count", "lineup_surrounding_recent"],
}
FAMILY_OF = {f: name for name, fields in FAMILIES.items() for f in fields}

# ── WHICH FIELDS THE MODEL ACTUALLY CONSUMES ────────────────────────────────
# Needed to tell "candidate to add" from "already in the blend, re-weight it".
# These are the raw inputs behind hr_blend's terms and the HR gate, named as
# they appear on a graded row. Keep in sync with MODEL_WEIGHTS in
# bots/mlb_dashboard.py -- a field missing here is reported as an ADD
# candidate when it should be a RE-WEIGHT one, which is the more dangerous
# direction to be wrong in.
_MODEL_INPUT_FIELDS = {
    # season power (hr_blend "season_power", 0.24) and its views
    "season_iso", "season_slg", "season_ops", "season_hr", "iso_vs_rhp", "iso_vs_lhp",
    # recent form (0.05) + recency_multiplier + two gate signals + power_gate
    "last5_hr", "last10_hr", "l20pa_hr", "last5_xbh", "l20pa_xbh", "last10_xbh",
    # pa_per_hr (0.03) / hr_per_pa
    "hr_per_pa", "pa_per_hr",
    # k_rate (0.04)
    "season_k_rate",
    # pitcher_damage (0.06) -- largest input pitcher_hr9 -- and the gate
    "pitcher_hr9", "pitcher_hr_allowed", "pitcher_hr9_vs_lhb", "pitcher_hr9_vs_rhb",
    # pitch_fit (0.06) and pitch_match_term (0.05)
    "pitch_mix_score", "pitch_type_match_score", "pitch_type_match_flag",
    # weak_spot_interaction (0.06) and score_hitter's weak_spot_bonus
    "pitcher_weak_side_score", "pitcher_weak_side_gap", "weak_spot_flag", "weak_spot_bonus",
    # pull_launch (0.06)
    "recent_pull_rate", "l5_pull_rate", "l10_pull_rate", "l20pa_pull_rate", "recent_fb_rate",
    # park_weather (0.05)
    "park_factor", "park_hr_factor",
    # batted_shape (0.03) and its sub-inputs
    "batted_shape", "shape_max_ev", "shape_raw_pull_rate", "recent_ev",
    # the 5-of-N HR gate's own signals
    "recent_ideal_hr_contact", "l20pa_ideal_hr_contact", "recent_barrel_rate",
    "l5_barrel_rate", "l10_barrel_rate", "hrw_score",
    # lineup_opportunity (0.01) / times_through (0.02)
    "lineup_spot",
    # bvp_signal (0.02)
    "bvp_ab", "bvp_hr", "bvp_ops", "bvp_avg", "bvp_hits", "bvp_xbh", "bvp_pa",
}


def num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def wilson(ok: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z = 1.96
    p = ok / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - m) / d, 100 * (c + m) / d)


def rows_of(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(k)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


# ── FEATURE PROVENANCE (2026-08-22) ────────────────────────────────────────
# The graded archive's FEATURE columns are a post-game snapshot: results.yml
# grades against the currently-published slate, and today.yml keeps rebuilding
# that slate into the evening. Measured: games_since_last_hr is 0 for 95.7% of
# players who homered and 1.1% of those who didn't; last5_hr is 0 for 2.2% of
# homerers against 32.9% of non-homerers. So the outcome is inside the
# features, and every within-band number this tool produced on that data is
# measuring the outcome against itself.
#
# Roadmap step 9b task 1 stamps each graded row with feature_snapshot =
# "locked" | "post_game" | "unavailable". Once that lands, rows that are not
# "locked" are skipped here by default.
#
# Until then rows carry NO stamp, and refusing unstamped rows would make this
# tool refuse everything. So: explicitly-stamped non-locked rows are dropped,
# unstamped rows are kept and COUNTED, and the count is printed loudly enough
# that nobody mistakes a provisional number for a finding.
_SNAPSHOT_KEY = "feature_snapshot"


def snapshot_split(rows: list[dict]) -> tuple[int, int, int]:
    """(locked, explicitly post-game/unavailable, unstamped)."""
    locked = stamped_bad = unstamped = 0
    for r in rows:
        v = r.get(_SNAPSHOT_KEY)
        if v is None:
            unstamped += 1
        elif v == "locked":
            locked += 1
        else:
            stamped_bad += 1
    return locked, stamped_bad, unstamped


def load(dirs: list[Path]) -> list[dict]:
    seen_dates: set[str] = set()
    out: list[dict] = []
    for d in dirs:
        if not d or not d.is_dir():
            continue
        for p in sorted(d.glob("graded_results_*.json")):
            m = DATE_RE.search(p.name)
            if not m or m.group(1) in seen_dates:
                continue
            try:
                rows = rows_of(json.loads(p.read_text()))
            except Exception:
                continue
            # ── KEY BY (game_pk, player_id), NOT player_id (2026-08-22) ──
            # Same defect Sol audit #2's finding #1 fixed in
            # live_results_tracker.py: a player who bats in both legs of a
            # split doubleheader has two independent, correctly-graded lines,
            # and keying on player_id alone silently threw one away. Measured
            # cost on the 2026-07-27..08-21 archive: 10 rows over 26 dates.
            # Small, but a research tool must not disagree with the grader
            # about what a row IS.
            seen, keep = set(), []
            for r in rows:
                pid = r.get("player_id")
                if pid is None:
                    continue
                rk = (r.get("game_pk"), pid)
                if rk in seen:
                    continue
                seen.add(rk)
                if (num(r.get("actual_ab")) or 0) <= 0:
                    continue                       # never batted: not asked
                # ── SHORT APPEARANCES ARE VOID (2026-08-22) ───────────────
                # live_results_tracker marks a final-game row with fewer than
                # 2 at-bats as void -- pinch-hit for, pulled, or entered late.
                # The pick never got a fair test, so it is neither a win nor a
                # loss, and including it as either would put the manager's
                # decision into the model's residual. Voided in BOTH
                # directions on purpose; voiding only the losses inflates the
                # measured rate by about a third of a point on the hit market.
                if r.get("result_void"):
                    continue
                if num(r.get("hr_score")) is None:
                    continue                       # no model opinion to hold fixed
                keep.append(r)
            if len(keep) >= 10:
                seen_dates.add(m.group(1))
                out += keep
    return out


OUTCOMES = {
    "hr":  ("homered",            lambda r: 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0),
    "hit": ("got a base hit",     lambda r: 1 if (num(r.get("actual_hits")) or 0) >= 1 else 0),
    "tb":  ("2+ total bases",     lambda r: 1 if (num(r.get("actual_tb")) or 0) >= 2 else 0),
}


def band_edges(vals: list[float], k: int) -> list[float]:
    """
    k quantile cuts of the model's own score.

    THE FIRST RUN'S BANDS WERE NOT HOLDING THE MODEL FIXED. With six bands the
    bottom one spanned scores 0-27 with a standard deviation of 6.2, and
    r(hr_score, season_iso) INSIDE that band was still +0.517 — barely below
    the +0.320 whole-pool correlation. So the "controlled" comparison was
    partly just the uncontrolled one again, and season power came back looking
    like a discovery when the model already weights it at 0.24.

    The other five bands were fine (r = +0.015 to +0.070). It was one wide
    band at the bottom doing the damage, which is exactly the failure mode
    quantile cuts produce when a score piles up at one end.

    More bands is the fix, and the diagnostic below prints the within-band
    correlation for every band so this can never hide again.
    """
    s = sorted(vals)
    return [s[int(len(s) * i / k)] for i in range(1, k)]


def within_band_corr(rows, bands, field: str) -> float | None:
    """
    How much of the model's opinion survives inside a band, measured against
    one field. Near zero means the control is working; anything approaching
    the whole-pool correlation means it is not.
    """
    by = defaultdict(list)
    for r, b in zip(rows, bands):
        v, sc = num(r.get(field)), num(r.get("hr_score"))
        if v is not None and sc is not None:
            by[b].append((sc, v))
    # SAMPLE-WEIGHTED MEAN, not the worst band.
    #
    # The first version returned max(|r|) across bands, which with 24 bands is
    # a guaranteed slander: the noisiest 40-row band always throws a big
    # correlation and every finding then reads "WEAK". The question is how
    # much of the model's opinion survives ON AVERAGE inside a band, which is
    # the weighted mean — the same thing a stratified estimator pools.
    num_, den = 0.0, 0
    for pairs in by.values():
        if len(pairs) < 40:
            continue
        n = len(pairs)
        mx = sum(p[0] for p in pairs) / n
        my = sum(p[1] for p in pairs) / n
        cov = sum((p[0] - mx) * (p[1] - my) for p in pairs) / n
        sx = (sum((p[0] - mx) ** 2 for p in pairs) / n) ** 0.5
        sy = (sum((p[1] - my) ** 2 for p in pairs) / n) ** 0.5
        if sx and sy:
            num_ += (cov / (sx * sy)) * n
            den += n
    return num_ / den if den else None


def band_of(v: float, edges: list[float]) -> int:
    i = 0
    for e in edges:
        if v >= e:
            i += 1
    return i


def stratified_lift(pairs_by_band: dict[int, list[tuple[float, int]]],
                    rng: random.Random | None = None) -> tuple[float, int, int, int, int]:
    """
    Within each band, split at the band's own median and compare HR rates.

    Pooling the halves ACROSS bands rather than pooling the raw rows is the
    whole point: it compares like with like, so the model's own ranking cannot
    leak into the answer.
    """
    hi_ok = hi_n = lo_ok = lo_n = 0
    for _b, pairs in pairs_by_band.items():
        if len(pairs) < 20:
            continue
        # ── BREAK TIES AT RANDOM (2026-08-22) ────────────────────────────
        # Sorting by value alone is correct, but Python's sort is STABLE, so
        # ties then fall in original row order — and rows arrive in score
        # order. On a heavily-tied field (last5_hr, any flag, any count) that
        # makes the median split depend on input ordering rather than on the
        # field. A random tiebreak costs two lines and makes the estimator
        # unbiased.
        #
        # Worth knowing how bad this class of bug gets: a first-pass research
        # scan sorted (value, outcome) TUPLES, so ties broke by the OUTCOME
        # itself. On bvp_hr (2,268 zeros, 43 ones) that manufactured a +31.5
        # point lift out of nothing, and 61 of 139 fields cleared q<=0.05.
        # See claude/moonshot-sol-brief-graded-archive-leak.md.
        _r = rng if rng is not None else random
        pairs = sorted(pairs, key=lambda x: (x[0], _r.random()))
        # a field with one repeated value inside a band cannot split it
        if pairs[0][0] == pairs[-1][0]:
            continue
        h = len(pairs) // 2
        lo, hi = pairs[:h], pairs[h:]
        lo_ok += sum(y for _v, y in lo); lo_n += len(lo)
        hi_ok += sum(y for _v, y in hi); hi_n += len(hi)
    if not lo_n or not hi_n:
        return (0.0, 0, 0, 0, 0)
    return (100 * hi_ok / hi_n - 100 * lo_ok / lo_n, hi_ok, hi_n, lo_ok, lo_n)


def permutation_p(pairs_by_band, observed: float, iters: int, rng: random.Random) -> float:
    """
    Shuffle the OUTCOME within each band and see how often chance beats what we
    measured. Within-band shuffling preserves the band structure, so the null
    is "this field carries nothing the model doesn't already have" rather than
    the much weaker "this field carries nothing at all".
    """
    hits = 0
    shuffled = {b: [v for v, _y in p] for b, p in pairs_by_band.items()}
    ys = {b: [y for _v, y in p] for b, p in pairs_by_band.items()}
    for _ in range(iters):
        fake = {}
        for b, vals in shuffled.items():
            yy = ys[b][:]
            rng.shuffle(yy)
            fake[b] = list(zip(vals, yy))
        if abs(stratified_lift(fake, rng)[0]) >= abs(observed):
            hits += 1
    return (hits + 1) / (iters + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", default=None)
    ap.add_argument("--width", type=int, default=4,
                    help="hr_score band width. 4 measured lowest leak; narrower is worse, not better.")
    ap.add_argument("--min-n", type=int, default=400)
    ap.add_argument("--outcome", default="hr", choices=list(OUTCOMES))
    ap.add_argument("--iters", type=int, default=1000)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--include-derived", action="store_true",
                    help="also test the model's own composite scores. Off by "
                         "default: they are outputs, not signals you can add.")
    ap.add_argument("--no-family", action="store_true",
                    help="report every correlated field separately instead of "
                         "one per family.")
    ap.add_argument("--allow-post-game", action="store_true",
                    help="also analyse rows explicitly stamped as post-game "
                         "feature snapshots. Off by default: those rows carry "
                         "the outcome inside the features. For archaeology only.")
    a = ap.parse_args()
    rng = random.Random(a.seed)

    dirs = [Path(os.path.expanduser(d)) for d in (a.dir or [])] + DEFAULT_DIRS
    rows = load(dirs)

    locked, stamped_bad, unstamped = snapshot_split(rows)
    if stamped_bad and not a.allow_post_game:
        rows = [r for r in rows if r.get(_SNAPSHOT_KEY) in (None, "locked")]
        print(f"\n   skipped {stamped_bad} rows stamped as post-game feature "
              f"snapshots (--allow-post-game to include them)")
    if unstamped:
        print(f"\n   ⚠️  {unstamped} of {unstamped + locked} rows carry NO "
              f"feature_snapshot stamp.")
        print("   Those rows predate roadmap step 9b, which means their feature columns are")
        print("   a POST-GAME snapshot — the outcome is inside them. Every number below that")
        print("   rests on them is provisional, not a finding. See")
        print("   claude/moonshot-opus-component-research-findings.md.")

    if len(rows) < 500:
        print(f"only {len(rows)} graded picks found — need ~500. Checked: "
              + ", ".join(str(d) for d in dirs))
        return 1

    what, outcome = OUTCOMES[a.outcome]
    base = sum(outcome(r) for r in rows)
    # ── FIXED-WIDTH BANDS, NOT QUANTILES (2026-08-09, second pass) ──────
    # Quantile bands gave equal COUNTS and wildly unequal score SPANS: with
    # six of them the bottom band ran 0-27 and leaked r=+0.517 against
    # season_iso, barely below the +0.320 whole-pool figure. Two edges also
    # landed on 59 and produced a 35-row band.
    #
    # Fixed width fixes the span directly. Measured leak by width, worst band:
    #   width 10 → 0.317   width 6 → 0.324   width 4 → 0.232
    #   width 3  → 0.196   width 2 → 0.361 (small-n noise takes over)
    # 4 is the floor of that curve; going narrower makes it worse, not better.
    scores = [num(r["hr_score"]) for r in rows]
    bands = [int(s // a.width) for s in scores]

    print(f"\n🕵️  MISSED SIGNALS — {len(rows)} graded picks, {base} {what} "
          f"({100*base/len(rows):.1f}%)")
    print(f"   Holding the model's own opinion fixed in {a.width}-point hr_score bands, then asking")
    print(f"   whether each field STILL separates {what} inside a band.\n")
    counts = defaultdict(int)
    for b in bands:
        counts[b] += 1
    usable = [b for b in sorted(counts) if counts[b] >= 20]
    print(f"   {len(usable)} usable bands of {a.width} points, "
          f"{sum(counts[b] for b in usable)} of {len(rows)} picks inside them")
    # ── IS THE CONTROL ACTUALLY WORKING? ────────────────────────────────
    # Printed every run, because the first version of this tool silently
    # failed here and reported 38 findings out of 164 as a result.
    print()

    # collect every numeric field, banded
    by_field: dict[str, dict[int, list[tuple[float, int]]]] = defaultdict(lambda: defaultdict(list))
    for r, b in zip(rows, bands):
        y = outcome(r)
        for k, v in r.items():
            if k in BLOCK_EXACT or k.startswith(BLOCK_PREFIX):
                continue
            if not a.include_derived and k in DERIVED_SCORES:
                continue
            f = num(v)
            if f is not None:
                by_field[k][b].append((f, y))

    results = []
    for k, banded in by_field.items():
        n = sum(len(v) for v in banded.values())
        if n < a.min_n:
            continue
        if len({v for pairs in banded.values() for v, _ in pairs}) < 3:
            continue
        lift, hok, hn, lok, ln = stratified_lift(banded, rng)
        if not hn or not ln:
            continue
        results.append({"field": k, "n": n, "lift": lift,
                        "hi": 100 * hok / hn, "lo": 100 * lok / ln,
                        "hn": hn, "ln": ln, "banded": banded})

    # Permutation only for the plausible ones — 1,000 shuffles x 170 fields is
    # a lot of arithmetic to spend on fields that measured nothing.
    results.sort(key=lambda r: -abs(r["lift"]))
    for r in results[:40]:
        r["p"] = permutation_p(r["banded"], r["lift"], a.iters, rng)
    for r in results[40:]:
        r["p"] = 1.0

    # Benjamini-Hochberg. ~170 fields means ~8 false positives at p<0.05, so a
    # tool that reported raw p-values would invent a discovery every run.
    tested = sorted([r for r in results if r["p"] < 1.0], key=lambda r: r["p"])
    m = len(tested)
    for i, r in enumerate(tested, start=1):
        r["q"] = min(1.0, r["p"] * m / i)
    for i in range(len(tested) - 2, -1, -1):
        tested[i]["q"] = min(tested[i]["q"], tested[i + 1]["q"])
    for r in results:
        r.setdefault("q", 1.0)

    # ── ONE FINDING REPORTS ONCE ────────────────────────────────────────
    # Group the survivors by family and keep the strongest member of each.
    # The first run listed season_iso, season_slg, season_ops, season_hr,
    # hr_per_pa and pa_per_hr as six separate discoveries. They are one.
    real = [r for r in results if r["q"] < 0.05]
    real.sort(key=lambda r: -abs(r["lift"]))
    if a.no_family:
        groups = [(r["field"], r, []) for r in real]
    else:
        best_of: dict[str, dict] = {}
        others: dict[str, list[str]] = defaultdict(list)
        for r in real:
            fam = FAMILY_OF.get(r["field"], r["field"])
            if fam not in best_of:
                best_of[fam] = r
            else:
                others[fam].append(r["field"])
        groups = [(fam, best_of[fam], others[fam]) for fam in
                  sorted(best_of, key=lambda f: -abs(best_of[f]["lift"]))]

    # ── THE LEAK COLUMN, per finding ────────────────────────────────────
    # A single global control check is not enough: the control can hold for
    # one field and leak badly for another, and the one it leaks on is
    # exactly the one you would most like to believe. So every finding
    # carries its OWN worst within-band correlation with hr_score. High leak
    # means "the model's opinion is still in this comparison" — the lift is
    # then partly the model rediscovering itself, and the finding is weak
    # however small its q-value looks.
    for fam, r, rest in groups:
        r["leak"] = within_band_corr(rows, bands, r["field"])

    print(f"   {'family':<26}{'strongest field':<26}{'n':>6}{'top':>7}{'bot':>7}"
          f"{'lift':>7}{'q':>7}{'leak':>7}  trust")
    for fam, r, rest in groups:
        lk = r.get("leak")
        trust = ("clean" if lk is not None and abs(lk) < 0.15
                 else "WEAK — control leaks here" if lk is not None else "?")
        lks = f"{lk:+.2f}" if lk is not None else "  —"
        print(f"   {fam[:25]:<26}{r['field'][:25]:<26}{r['n']:>6}{r['hi']:>6.1f}%{r['lo']:>6.1f}%"
              f"{r['lift']:>+7.1f}{r['q']:>7.3f}{lks:>7}  {trust}")
        if rest:
            print(f"   {'':<28}same finding: {', '.join(rest[:6])}"
                  + (f" +{len(rest)-6} more" if len(rest) > 6 else ""))

    tested_n = len(results)
    print(f"\n   {len(groups)} distinct findings from {len(real)} surviving fields, "
          f"{tested_n} fields tested.")
    if not groups:
        print("   Nothing clears the bar. That is a real result and the most likely one:")
        print("   the model has already priced everything the archive publishes, and the")
        print("   next gain has to come from a NEW field rather than a re-weight.")
    else:
        clean = [g for g in groups if (g[1].get("leak") is not None and abs(g[1]["leak"]) < 0.15)]
        print(f"\n   {len(clean)} of {len(groups)} findings have a CLEAN control. Those are the")
        print("   ones worth acting on. The rest still co-vary with hr_score inside their own")
        print("   band, so their lift is partly the model rediscovering its own opinion —")
        print("   a small q-value does not rescue a leaky control.")
        if clean:
            # ── TWO DIFFERENT RECOMMENDATIONS, NOT ONE (2026-08-22) ────────
            # This block used to print "LOWER is better — check the sign" for
            # every negative lift, which reads as "add this field, inverted".
            # That is the opposite of what a negative residual means on a
            # field the model ALREADY uses: it means the model is
            # OVER-weighting it. The distinction is not academic — on
            # de-leaked data 9 of 12 surviving findings were negative, and
            # every one of them was a field already in the blend. Acting on
            # the old wording would have raised weights that the measurement
            # said to cut.
            print("\n   CLEAN:")
            for fam, r, _rest in clean:
                used = r["field"] in _MODEL_INPUT_FIELDS
                if used:
                    d = ("the model already uses this — RE-WEIGHT it "
                         + ("UP" if r["lift"] > 0 else "DOWN"))
                else:
                    d = ("candidate to ADD — higher is better" if r["lift"] > 0
                         else "candidate to ADD, inverted — lower is better")
                print(f"     · {fam} ({r['field']})  {r['lift']:+.1f} pts, leak {r['leak']:+.2f}  ({d})")
            print("\n   A NEGATIVE RESIDUAL ON A FIELD THE MODEL USES IS NOT A SIGNAL TO ADD.")
            print("   It means that inside a fixed score band, the hitters who got there on")
            print("   that field did WORSE — i.e. the term is carrying more weight than it")
            print("   earns. Cut it; do not invert it.")
        print("\n   READ THE FAMILY, NOT THE FIELD. A family that survives means the model")
        print("   is under-weighting that IDEA, not that one column is magic. The strongest")
        print("   member is shown because it is the cleanest measurement of it, not because")
        print("   it is the one to add.")

    print("\n   CAVEATS, all load-bearing:")
    print("   · graded rows are the bot's designated picks, so every rate is conditional")
    print("     on the model having liked the hitter already.")
    print("   · correlated fields surface together — slg and hr_per_pa are one finding.")
    print("   · this finds association inside a band, not a weight. Confirm anything here")
    print("     by re-running the real pipeline, not by trusting this number.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
