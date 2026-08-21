#!/usr/bin/env python3
"""
📊 EVAL REPORT — how good are mlb_hr_v3's numbers, actually.

STEP 4, Task 6c. Donovan: "You do not need to wait seven days before
writing the evaluation system. You only need more clean nights before
trusting the new version-specific numbers. Build the report now while
data accumulates."

WHAT THIS ANSWERS
------------------
For the HR market, score-tier'd against real outcomes:

    Score Tier    N     HRs    HR Rate    Lift
    90+
    80-89
    70-79
    60-69
    50-59
    <50

plus overall HR rate, confidence intervals, a monotonicity check, 7/14/30-
day rolling results, model-version filtering, a count of excluded
observations (by reason), and the percentage of predictions with valid
provenance.

Two invariants this report holds itself to, both of them things it used to
get wrong silently:
  · Every INCLUDED row lands in exactly one tier, so sum(tier N) == overall
    N always. tier_table() checks it and reports any residual as
    unbucketed_n rather than letting the tiers and the headline disagree.
  · If the numbers pool more than one model_version, the table SAYS SO, at
    the top, before you read it -- not only in the breakdown at the bottom.

THE ONE-PREDICTION-PER-PLAYER-GAME RULE
-----------------------------------------
today.yml scores a game up to ~13 times before first pitch. Grading every
one of those runs would let the model get "13 chances" to be right about
one player-game, and a late recompute could silently swap in a better (or
worse) number after the fact. Official evaluation here uses ONLY
`prediction_of_record` — pick_lock.py's per-game_pk lock of which run's
numbers are official, taken once, at first pitch, forever (see
bots/pick_lock.py). Every OTHER run of that game still exists in
prediction_log_*.jsonl for drift/research — see intraday_drift.py — this
script never touches those for the headline numbers.

WHERE prediction_of_record ACTUALLY LIVES
-------------------------------------------
pick_lock.json's own "prediction_of_record" key is NOT durable across
slate days — it resets to {} the instant fetch_lock() sees a new slate
date (see pick_lock.py). The durable copy is `por_log_<date>.jsonl`,
written by pick_lock.py's append_por_log() the moment each game_pk locks.
THAT is what this script reads for history. pick_lock.json itself is only
consulted as a same-day supplement, in case a game locked this run before
its por_log line was written (see load_prediction_of_record() below).

INCLUDE IN OFFICIAL EVAL (all must hold):
    run_id exists
    generated_at exists
    a usable hr_score (one tier_for() can actually place -- see _tierable())
    locked_late == False
    valid outcome (graded, final, not void)
    not void
Everything else is preserved (never discarded) but excluded from the
headline tier table, with an honest reason:
    locked_late            -- run_meta says the run itself came after first pitch
    missing_provenance      -- run_id and/or generated_at could not be verified
    legacy_or_unknown_model -- model_version missing (predates stamping, or a
                                registry failure that run)
    missing_prediction_log  -- run_id is known but that run's score data has
                                aged out of retention (300-file cap) or never
                                published; the score itself is unrecoverable
    no_outcome_yet          -- game hasn't gone final / been graded yet
    void                    -- outcome_log marked this player-game void
    missing_score_row       -- the locked run's prediction_log has no row for
                                this specific player (e.g. added to the slate
                                after the lock -- see pick_lock.py's own note
                                on this, "honest, not a bug")
    unusable_hr_score       -- the locked run DID score this player, but
                                scores.hr is null / NaN / inf / not a number,
                                so no tier can hold it. Distinct from
                                missing_score_row: that is a roster event,
                                this is an upstream scoring bug
    config_hash_filtered    -- only with --config-hash: this row's config_hash
                                does not match the one requested

CONFIG-HASH PROVENANCE (2026-08-21, see bots/config_fingerprint.py)
---------------------------------------------------------------------
model_version is a DECLARED label -- a human bumps it when they remember to.
config_hash is a DERIVED fact -- a deterministic fingerprint of the exact
scoring configuration (MODEL_WEIGHTS' HR surface + the AST structure of the
functions that turn it into hr_score) in effect when a row was scored, with
no human step to forget. The two answer different questions and neither
replaces the other. This report NEVER pools rows across distinct config_hash
values into one silently-clean number under a shared model_version -- if a
model_version's included rows carry more than one distinct known config_hash,
that is printed as a loud warning at the very top of the report, before any
tier/rate number, and again in a dedicated CONFIG PROVENANCE section --
regardless of whether --model-version is used to filter down to exactly that
version (filtering to the contaminated version does not suppress its own
warning; see config_hash_drift()). Legacy rows locked before config_hash
existed carry config_hash: null forever -- never backfilled with today's live
hash -- and are counted separately as "unknown provenance," never silently
folded into either "one config" or "many configs."

USAGE
-----
    python bots/eval_report.py --dir public/data/current
    python bots/eval_report.py --dir /tmp/data-checkout/public/data/current --model-version mlb_hr_v3
    python bots/eval_report.py --dir public/data/current --config-hash sha256:abc123...
    python bots/eval_report.py --live                    # best-effort remote fetch, TODAY ONLY (see fetch_live())

Requires a real directory listing (por_log_*.jsonl / outcome_log_*.jsonl /
prediction_log_*.jsonl for however many days you want in the window), so
--dir wants a checkout of the `data` branch's public/data/current, e.g.:
    git worktree add /tmp/data-checkout data
    python bots/eval_report.py --dir /tmp/data-checkout/public/data/current
raw.githubusercontent.com has no directory-listing API, so --live can only
ever see the files named in the CURRENT day's pick_lock.json / run_meta --
it is a convenience for "what does today look like," not a substitute for
--dir when you actually want 7/14/30-day rolling numbers.

Writes eval_report.json (machine-readable) next to the human-readable
stdout report -- same "prints AND writes a summary file" convention as
backtest_report.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

# (label, low_inclusive, high_exclusive), highest first -- the order this
# list is in is also the order monotonicity is checked in.
TIERS: list[tuple[str, float, float]] = [
    ("90+", 90.0, math.inf),
    ("80-89", 80.0, 90.0),
    ("70-79", 70.0, 80.0),
    ("60-69", 60.0, 70.0),
    ("50-59", 50.0, 60.0),
    ("<50", -math.inf, 50.0),
]

Z95 = 1.959963984540054  # two-sided 95% normal critical value


# ── loading ──────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return out


def load_prediction_of_record(data_dir: Path) -> list[dict]:
    """Every prediction_of_record entry this directory can see: every
    por_log_<date>.jsonl file present (the durable history), plus the
    live pick_lock.json's own "prediction_of_record" dict as a same-day
    supplement -- in case a game locked this run before its por_log line
    landed (append_por_log runs right after the ledger write, so this is
    belt-and-suspenders, not the normal path).

    Deduplicated by game_pk (por_log lines win over pick_lock.json's copy
    when both exist -- they should be identical anyway; por_log is the
    canonical durable copy)."""
    by_game: dict[str, dict] = {}
    for p in sorted(data_dir.glob("por_log_*.jsonl")):
        for row in _read_jsonl(p):
            gp = str(row.get("game_pk") or "")
            if gp:
                by_game[gp] = row

    lock_path = data_dir / "pick_lock.json"
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text())
            date = lock.get("date")
            por = lock.get("prediction_of_record") or {}
            for gp, rec in por.items():
                gp = str(gp)
                if gp not in by_game:
                    by_game[gp] = {"prediction_date": date, "game_pk": gp, **rec}
        except Exception:
            pass

    return list(by_game.values())


def load_outcomes(data_dir: Path, dates: set[str]) -> dict[str, dict]:
    """{player_game_id: latest-revision outcome row}, across whichever
    outcome_log_<date>.jsonl files this directory has for the given dates.
    Mirrors live_results_tracker.load_latest_outcome_revisions()'s own
    "highest revision wins" rule."""
    latest: dict[str, dict] = {}
    for date in dates:
        p = data_dir / f"outcome_log_{date}.jsonl"
        if not p.exists():
            continue
        for row in _read_jsonl(p):
            pgid = row.get("player_game_id")
            if not pgid:
                continue
            rev = row.get("revision") or 0
            prior = latest.get(pgid)
            if prior is None or rev >= (prior.get("revision") or 0):
                latest[pgid] = row
    return latest


class PredictionLogCache:
    """Loads prediction_log_<run_id>.jsonl files on demand, once each, and
    indexes each one by (game_pk, player_id). A run_id whose file cannot be
    found (aged out of the 300-file retention cap, or never published) is
    cached as a miss so repeated lookups don't re-hit the filesystem/network."""

    def __init__(self, data_dir: Path, live: bool = False):
        self.data_dir = data_dir
        self.live = live
        self._by_run: dict[str, dict[tuple[str, str], dict] | None] = {}

    def _load(self, run_id: str) -> dict[tuple[str, str], dict] | None:
        if run_id in self._by_run:
            return self._by_run[run_id]
        fname = f"prediction_log_{run_id}.jsonl"
        rows: list[dict] | None = None
        local = self.data_dir / fname
        if local.exists():
            rows = _read_jsonl(local)
        elif self.live:
            try:
                with urllib.request.urlopen(f"{RAW}/{fname}", timeout=20) as r:
                    text = r.read().decode("utf-8")
                rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
            except Exception:
                rows = None
        if not rows:
            self._by_run[run_id] = None
            return None
        idx: dict[tuple[str, str], dict] = {}
        for row in rows[1:]:  # rows[0] is the run_meta header line
            gp = str(row.get("game_pk") or "")
            pid = str(row.get("player_id") or "")
            if gp and pid:
                idx[(gp, pid)] = row
        self._by_run[run_id] = idx
        return idx

    def score_row(self, run_id: str, game_pk: str, player_id: str) -> dict | None:
        idx = self._load(run_id)
        if idx is None:
            return None
        return idx.get((str(game_pk), str(player_id)))

    def all_rows_for_game(self, run_id: str, game_pk: str) -> list[dict]:
        idx = self._load(run_id)
        if idx is None:
            return []
        return [row for (gp, _pid), row in idx.items() if gp == str(game_pk)]


# ── building the eval set ───────────────────────────────────────────────

def build_candidates(por_entries: list[dict], pred_cache: PredictionLogCache,
                      outcomes_by_pgid: dict[str, dict]) -> list[dict]:
    """One candidate row per player who either (a) appears in the
    prediction_log of that game_pk's locked run, or (b) has a graded
    outcome for that game_pk but was NOT in the locked run (joined the
    slate after the lock -- pick_lock.py's own docstring: "a player who
    joins the slate after that lock has no row in it, which is honest, not
    a bug"). (b) exists so that player is preserved and counted as an
    honest exclusion (missing_score_row) rather than never appearing in
    this report at all -- por is per GAME, not per player, so without this
    a late-arriving player who went deep would simply vanish from every
    count."""
    outcomes_by_game: dict[str, list[str]] = {}
    for pgid in outcomes_by_pgid:
        gp, _, pid = pgid.partition("|")
        if gp and pid:
            outcomes_by_game.setdefault(gp, []).append(pid)

    out: list[dict] = []
    for por in por_entries:
        gp = str(por.get("game_pk") or "")
        run_id = por.get("run_id")
        prediction_date = por.get("prediction_date")
        rows = pred_cache.all_rows_for_game(run_id, gp) if run_id else []
        if run_id and not rows:
            # run_id is known but its score data could not be found at all --
            # one placeholder candidate so this game is counted as excluded
            # (missing_prediction_log) rather than silently vanishing from
            # every count in the report.
            out.append({
                "game_pk": gp, "player_id": None, "player": None,
                "prediction_date": prediction_date, "por": por,
                "hr_score": None, "model_version": por.get("model_version"),
                "config_hash": por.get("config_hash"),
                "row": None,
            })
            continue

        seen_pids: set[str] = set()
        for row in rows:
            pid = str(row.get("player_id") or "")
            seen_pids.add(pid)
            out.append({
                "game_pk": gp,
                "player_id": pid,
                "player": row.get("player"),
                "prediction_date": prediction_date or row.get("prediction_date"),
                "por": por,
                "hr_score": (row.get("scores") or {}).get("hr"),
                "model_version": por.get("model_version"),
                # PROVENANCE: from prediction_of_record, NOT from the score
                # row itself -- config_hash describes "what configuration
                # produced the locked run," a per-game_pk fact, same
                # granularity as model_version/run_id, not a per-row one.
                "config_hash": por.get("config_hash"),
                "row": row,
            })

        for pid in outcomes_by_game.get(gp, []):
            if pid in seen_pids:
                continue
            out.append({
                "game_pk": gp, "player_id": pid, "player": None,
                "prediction_date": prediction_date, "por": por,
                "hr_score": None, "model_version": por.get("model_version"),
                "config_hash": por.get("config_hash"),
                "row": None,
            })
    return out


def classify(cand: dict, outcomes_by_pgid: dict[str, dict]) -> tuple[str | None, str]:
    """Returns (exclude_reason_or_None, provenance_status). provenance_status
    is "valid" iff run_id AND generated_at both exist on the por entry --
    reported separately from eval-inclusion because a late-but-provenanced
    run is still "has valid provenance," just not eligible for the
    headline table."""
    por = cand["por"]
    run_id = por.get("run_id")
    generated_at = por.get("generated_at")
    provenance = "valid" if (run_id and generated_at) else "missing"

    if not run_id or not generated_at:
        return "missing_provenance", provenance
    if not cand.get("model_version"):
        return "legacy_or_unknown_model", provenance
    if cand.get("row") is None:
        # run_id resolved but either the whole prediction_log 404'd
        # (no rows at all for this game -> synthetic placeholder candidate
        # from build_candidates) or this specific player never appeared in
        # it.
        if cand.get("player_id") is None:
            return "missing_prediction_log", provenance
        return "missing_score_row", provenance
    # The locked run DID score this player, but published a score no tier can
    # hold. Deliberately its OWN reason rather than folded into
    # missing_score_row: that one means "this player has no row in the locked
    # run at all" -- a late slate join, an honest and expected gap. This one
    # means an upstream scoring bug, which is neither.
    #
    # Before this guard such a row passed classify() clean, counted toward
    # total_n/total_hr in tier_table(), and then fell out of every tier when
    # tier_for() returned None -- so sum(tier N) < overall N, the overall rate
    # absorbed a HR that no tier could account for, and nothing said a word.
    if not _tierable(cand.get("hr_score")):
        return "unusable_hr_score", provenance
    if por.get("locked_late"):
        return "locked_late", provenance

    pgid = f"{cand['game_pk']}|{cand['player_id']}"
    outcome = outcomes_by_pgid.get(pgid)
    if outcome is None or not outcome.get("is_final"):
        return "no_outcome_yet", provenance
    if outcome.get("void"):
        return "void", provenance

    return None, provenance


def _tierable(score: Any) -> bool:
    """True iff tier_for() will actually place `score` in a tier -- the
    precondition classify() enforces so that every INCLUDED row is guaranteed
    to land in exactly one.

    TIERS spans -inf..+inf, so every finite real number lands somewhere. That
    makes this precisely the set that does not:

      · None            -- tier_for returns None outright.
      · a non-numeric   -- tier_for would raise TypeError comparing it to a
                           float bound; loud, but still a crash mid-report.
      · NaN             -- compares False against every bound, so it silently
                           falls through all six tiers.
      · +/-inf          -- not a real score either way, and +inf additionally
                           falls through every tier because the top tier's
                           upper bound is exclusive (`inf < inf` is False).

    bool is rejected too: `isinstance(True, int)` is True in Python, so a JSON
    `true` in a score field would otherwise quietly tier as "<50" rather than
    being caught as the corruption it is.
    """
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    return math.isfinite(score)


def tier_for(score: float) -> str | None:
    if score is None:
        return None
    for label, lo, hi in TIERS:
        if lo <= score < hi:
            return label
    return None


# ── stats ────────────────────────────────────────────────────────────────

def wilson_ci(hits: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial rate. None for n == 0 --
    an interval around an undefined rate is not a number, and reporting
    (0, 0) or (0, 1) either one would misrepresent "no data" as "a
    measured result.\""""
    if n == 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def tier_table(included: list[dict]) -> dict[str, dict]:
    by_tier: dict[str, list[dict]] = {label: [] for label, _, _ in TIERS}
    for c in included:
        label = tier_for(c["hr_score"])
        if label:
            by_tier[label].append(c)

    total_n = len(included)
    total_hr = sum(1 for c in included if c["went_yard"])
    overall_rate = (total_hr / total_n) if total_n else None

    table: dict[str, dict] = {}
    for label, _, _ in TIERS:
        rows = by_tier[label]
        n = len(rows)
        hrs = sum(1 for c in rows if c["went_yard"])
        rate = (hrs / n) if n else None
        ci = wilson_ci(hrs, n)
        lift = (rate / overall_rate) if (rate is not None and overall_rate) else None
        table[label] = {
            "n": n, "hrs": hrs, "hr_rate": rate,
            "ci_95": ci, "lift": lift,
        }
    # RECONCILIATION BACKSTOP. classify()'s _tierable() guard is what should
    # make this impossible now, but the bug it closes was invisible for one
    # specific reason: nothing ever compared these two totals. Now something
    # does. Any FUTURE route to the same class of failure -- a row counted in
    # OVERALL that no tier holds, quietly depressing every tier rate while the
    # headline rate stays right -- announces itself instead of passing.
    tiered_n = sum(t["n"] for t in table.values())
    tiered_hr = sum(t["hrs"] for t in table.values())
    return {
        "tiers": table,
        "overall": {
            "n": total_n, "hrs": total_hr, "hr_rate": overall_rate,
            "ci_95": wilson_ci(total_hr, total_n),
        },
        "unbucketed_n": total_n - tiered_n,
        "unbucketed_hrs": total_hr - tiered_hr,
    }


def monotonicity(table: dict[str, dict], min_n: int = 20) -> dict:
    """Adjacent-tier comparisons, highest tier first. A pair where either
    side has fewer than min_n observations is reported but flagged
    low_confidence rather than a real inversion -- with this little data,
    tier-to-tier noise is expected, not a model problem."""
    pairs = []
    labels = [label for label, _, _ in TIERS]
    for hi_label, lo_label in zip(labels, labels[1:]):
        hi, lo = table[hi_label], table[lo_label]
        if hi["hr_rate"] is None or lo["hr_rate"] is None:
            pairs.append({"higher": hi_label, "lower": lo_label, "status": "no_data"})
            continue
        inverted = hi["hr_rate"] < lo["hr_rate"]
        low_conf = hi["n"] < min_n or lo["n"] < min_n
        pairs.append({
            "higher": hi_label, "lower": lo_label,
            "higher_rate": hi["hr_rate"], "lower_rate": lo["hr_rate"],
            "inverted": inverted,
            "status": "low_confidence" if low_conf else ("inverted" if inverted else "ok"),
        })
    real_violations = [p for p in pairs if p.get("status") == "inverted"]
    return {"pairs": pairs, "monotonic": len(real_violations) == 0, "violations": real_violations}


def rolling_window(included: list[dict], as_of: dt.date, days: int) -> dict:
    start = (as_of - dt.timedelta(days=days - 1)).isoformat()
    end = as_of.isoformat()
    window = [c for c in included if c["prediction_date"] and start <= c["prediction_date"] <= end]
    n = len(window)
    hrs = sum(1 for c in window if c["went_yard"])
    return {
        "days": days, "start": start, "end": end,
        "n": n, "hrs": hrs,
        "hr_rate": (hrs / n) if n else None,
        "ci_95": wilson_ci(hrs, n),
    }


# ── report ───────────────────────────────────────────────────────────────

def fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def fmt_ci(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0]*100:.1f}%, {ci[1]*100:.1f}%]" if ci else "n/a"


def render_text(report: dict) -> str:
    lines = []
    mv = report["model_version_filter"]
    present = report.get("model_versions_present") or []
    # "ALL" alone reads as a deliberate scope when it is really just the
    # default. Name the single version when there is only one, so the header
    # never implies a comparison the data cannot support.
    if mv:
        mv_label = mv
    elif len(present) == 1:
        mv_label = f"ALL (only {present[0]} present)"
    else:
        mv_label = "ALL"
    lines.append(f"MOONSHOT MLB HR MODEL — {mv_label}")
    lines.append(f"as of {report['as_of']}  ·  {report['n_por_entries']} game(s) with a prediction_of_record considered")
    # PROVENANCE (2026-08-21): printed FIRST, before a single number, and
    # never gated by --model-version -- this is exactly the drift a
    # model_version filter cannot see or suppress (config_hash_drift() is
    # computed on whatever `included` this report already filtered to; see
    # its own docstring). "impossible to hide" per the brief this satisfies.
    if report["config_hash_warnings"]:
        lines.append("")
        for w in report["config_hash_warnings"]:
            lines.append(f"⚠ CONFIG DRIFT: {w['model_version']} contains {len(w['hashes'])} "
                          f"distinct scoring configurations:")
            for h in w["hashes"]:
                lines.append(f"    {h}   ({w['n_by_hash'][h]} row(s))")
        lines.append("⚠ Do not treat this model_version as one comparable population until this is resolved "
                      "— see docs/MODELS.md.")
    lines.append("")
    # Above the table, not below it -- you cannot read the numbers without
    # having read why they might not mean what they look like.
    for w in report.get("warnings") or []:
        lines.append("!" * 74)
        lines.extend(textwrap.wrap(w, 74))
        lines.append("!" * 74)
        lines.append("")
    lines.append(f"{'Score Tier':<12}{'N':>6}{'HRs':>6}{'HR Rate':>10}{'Lift':>8}   95% CI")
    for label, _, _ in TIERS:
        t = report["tier_table"]["tiers"][label]
        lift = f"{t['lift']:.2f}x" if t["lift"] is not None else "n/a"
        lines.append(f"{label:<12}{t['n']:>6}{t['hrs']:>6}{fmt_pct(t['hr_rate']):>10}{lift:>8}   {fmt_ci(t['ci_95'])}")
    ov = report["tier_table"]["overall"]
    lines.append("")
    lines.append(f"OVERALL HR RATE: {fmt_pct(ov['hr_rate'])}  (N={ov['n']}, {ov['hrs']} HR)  95% CI {fmt_ci(ov['ci_95'])}")
    unb = report["tier_table"].get("unbucketed_n") or 0
    if unb:
        lines.append(f"  ⚠ {unb} included row(s) ({report['tier_table'].get('unbucketed_hrs') or 0} HR) "
                      f"landed in NO tier — every tier rate above is understated "
                      f"against this overall rate. This is a bug, not a data gap.")
    lines.append("")
    mono = report["monotonicity"]
    mono_status = "holds" if mono["monotonic"] else f"{len(mono['violations'])} inversion(s)"
    lines.append(f"MONOTONICITY: {mono_status}")
    for p in mono["pairs"]:
        if p["status"] == "no_data":
            lines.append(f"  {p['higher']:>6} vs {p['lower']:<6} — no data")
            continue
        flag = {"ok": "", "low_confidence": "  (low N — not conclusive)", "inverted": "  ⚠ INVERTED"}[p["status"]]
        lines.append(f"  {p['higher']:>6} {fmt_pct(p['higher_rate']):>7}  vs  {p['lower']:<6} {fmt_pct(p['lower_rate']):>7}{flag}")
    lines.append("")
    lines.append("ROLLING RESULTS")
    for w in report["rolling"]:
        lines.append(f"  {w['days']:>2}-day ({w['start']}..{w['end']}): N={w['n']:<5} HR={w['hrs']:<4} "
                      f"rate={fmt_pct(w['hr_rate']):<7} CI {fmt_ci(w['ci_95'])}")
    lines.append("")
    if report["by_model_version"]:
        lines.append("BY MODEL VERSION (cross-version comparison is not apples-to-apples — see docs/MODELS.md)")
        for mv2, stats in sorted(report["by_model_version"].items()):
            lines.append(f"  {mv2:<20} N={stats['n']:<5} HR={stats['hrs']:<4} rate={fmt_pct(stats['hr_rate'])}")
        lines.append("")
    lines.append(f"PROVENANCE: {fmt_pct(report['provenance_valid_pct'])} of {report['n_candidates']} "
                  f"candidate player-games have a verified run_id + generated_at "
                  f"(across {report['n_por_entries']} locked game(s))")
    if report["config_hashes_by_model_version"]:
        lines.append("")
        lines.append("CONFIG PROVENANCE (config_hash — see docs/MODELS.md; distinct from model_version above)")
        for mv2, entry in sorted(report["config_hashes_by_model_version"].items()):
            n_hashes = len(entry["hashes"])
            flag = "  ⚠ MULTIPLE CONFIGS" if n_hashes >= 2 else ""
            lines.append(f"  {mv2:<20} {n_hashes} distinct hash(es), {entry['n_unknown']} row(s) unknown provenance{flag}")
            for h, n in sorted(entry["hashes"].items()):
                lines.append(f"      {h}  N={n}")
    lines.append("")
    lines.append(f"EXCLUDED FROM HEADLINE EVAL: {report['n_excluded']} of {report['n_candidates']} candidate player-games "
                  f"({fmt_pct(report['n_excluded'] / report['n_candidates'] if report['n_candidates'] else None)})")
    for reason, n in sorted(report["excluded_by_reason"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {reason:<24} {n}")
    return "\n".join(lines)


def config_hash_drift(included: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """PROVENANCE (2026-08-21). For every model_version present among
    INCLUDED rows, which config_hash value(s) actually produced them --
    and, separately, a loud warning wherever more than one *known* hash
    shows up under the same model_version. That is the exact contamination
    `model_version` alone cannot see: a weight/threshold edit that landed
    without a version bump (see bots/config_fingerprint.py).

    Returns (config_hashes_by_model_version, warnings):
      config_hashes_by_model_version: {mv: {"n": total rows, "hashes":
        {hash: row_count}, "n_unknown": rows with no config_hash at all}}.
        "unknown" rows (config_hash is None -- pre-provenance history, or a
        run where hashing itself failed) are counted but never folded into
        "hashes", so an unknown-provenance gap and a real multi-hash
        contamination are never confused with each other.
      warnings: one dict per model_version with 2+ DISTINCT KNOWN hashes --
        {"model_version": mv, "hashes": [sorted hash list], "n_by_hash":
        {hash: count}}. Never suppressed by a --model-version filter --
        build_report() computes this AFTER filtering, on whatever `included`
        it was given, so filtering to exactly the contaminated version does
        not hide its own contamination (the opposite of hiding it, in fact:
        it's the one view where the warning is guaranteed to still fire).

    Deliberately never used to EXCLUDE anything -- mixed hashes are reported,
    not filtered out from under the caller. Silently dropping the
    contaminated rows would hide the exact problem this function exists to
    surface; the loud warning is the whole point, per the brief this
    satisfies ("Do NOT automatically exclude mixed hashes without
    reporting them.").
    """
    by_mv: dict[str, dict] = {}
    for c in included:
        mv = c.get("model_version") or "unknown"
        entry = by_mv.setdefault(mv, {"n": 0, "hashes": {}, "n_unknown": 0})
        entry["n"] += 1
        ch = c.get("config_hash")
        if ch:
            entry["hashes"][ch] = entry["hashes"].get(ch, 0) + 1
        else:
            entry["n_unknown"] += 1

    warnings: list[dict] = []
    for mv, entry in sorted(by_mv.items()):
        if len(entry["hashes"]) >= 2:
            warnings.append({
                "model_version": mv,
                "hashes": sorted(entry["hashes"]),
                "n_by_hash": dict(sorted(entry["hashes"].items())),
            })
    return by_mv, warnings


def build_report(data_dir: Path, model_version: str | None, as_of: dt.date, live: bool = False,
                  config_hash: str | None = None) -> dict:
    por_entries = load_prediction_of_record(data_dir)
    dates = {p.get("prediction_date") for p in por_entries if p.get("prediction_date")}
    outcomes = load_outcomes(data_dir, dates)
    pred_cache = PredictionLogCache(data_dir, live=live)

    candidates = build_candidates(por_entries, pred_cache, outcomes)

    n_provenance_valid = 0
    excluded_by_reason: dict[str, int] = {}
    included: list[dict] = []

    for cand in candidates:
        reason, provenance = classify(cand, outcomes)
        if provenance == "valid":
            n_provenance_valid += 1
        if model_version and cand.get("model_version") != model_version:
            reason = reason or "model_version_filtered"
        # PROVENANCE: an explicit --config-hash filter is a stricter version
        # of --model-version, not a replacement for it -- both can be given
        # together (e.g. "mlb_hr_v3 rows scored under exactly this
        # fingerprint"). Checked independently so either filter alone still
        # excludes correctly with its own honest reason.
        if config_hash and cand.get("config_hash") != config_hash:
            reason = reason or "config_hash_filtered"
        if reason:
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            continue
        pgid = f"{cand['game_pk']}|{cand['player_id']}"
        outcome = outcomes[pgid]  # guaranteed present -- classify() would have excluded otherwise
        cand["went_yard"] = bool(outcome.get("went_yard"))
        included.append(cand)

    table = tier_table(included)
    mono = monotonicity(table["tiers"])
    rolling = [rolling_window(included, as_of, d) for d in (7, 14, 30)]

    by_mv: dict[str, dict] = {}
    for c in included:
        mv = c.get("model_version") or "unknown"
        s = by_mv.setdefault(mv, {"n": 0, "hrs": 0})
        s["n"] += 1
        s["hrs"] += 1 if c["went_yard"] else 0
    for s in by_mv.values():
        s["hr_rate"] = (s["hrs"] / s["n"]) if s["n"] else None

    # CROSS-VERSION POOLING WARNING (see docs/MODELS.md). Run without a
    # filter, every model version lands in ONE tier table, one monotonicity
    # check and one set of rolling windows -- so a tier's rate is an average
    # across different weightings, not a measurement of any single version.
    # That caveat already existed, but only on the BY MODEL VERSION breakdown
    # at the bottom, which is not the number anyone reads to decide whether a
    # version is good. It belongs on the headline.
    #
    # Fires only when the included data is ACTUALLY mixed. Warning on every
    # unfiltered run -- including single-version ones, where pooling changes
    # nothing -- is how a warning gets trained into background noise.
    versions_present = sorted(by_mv.keys())
    warnings: list[str] = []
    if model_version is None and len(versions_present) > 1:
        warnings.append(
            f"HEADLINE NUMBERS POOL {len(versions_present)} MODEL VERSIONS "
            f"({', '.join(versions_present)}). The tier table, monotonicity "
            f"check and rolling windows below all mix them, so every rate "
            f"here is an average across different weightings rather than a "
            f"measurement of any one version. Re-run with --model-version "
            f"<name> before making a weight decision off these numbers."
        )

    # CONFIG-HASH DRIFT (see bots/config_fingerprint.py). Computed from
    # `included` -- i.e. AFTER model_version/config_hash filtering has
    # already been applied above -- specifically so that filtering down to
    # one model_version can never suppress this warning: a contaminated
    # version stays contaminated whether or not --model-version narrows the
    # report to exactly it.
    config_hashes_by_mv, config_warnings = config_hash_drift(included)

    n_por = len(por_entries)
    return {
        "as_of": as_of.isoformat(),
        "model_version_filter": model_version,
        "model_versions_present": versions_present,
        "warnings": warnings,
        "config_hash_filter": config_hash,
        "n_por_entries": n_por,
        "n_candidates": len(candidates),
        "n_included": len(included),
        "n_excluded": len(candidates) - len(included),
        "excluded_by_reason": excluded_by_reason,
        # Denominator is CANDIDATES (player-games), matching excluded_by_reason's
        # own granularity -- not n_por_entries (games). A game with 20 players
        # and missing provenance should count as 20 affected predictions, not 1.
        "provenance_valid_pct": (n_provenance_valid / len(candidates)) if candidates else None,
        "tier_table": table,
        "monotonicity": mono,
        "rolling": rolling,
        "by_model_version": by_mv,
        "config_hashes_by_model_version": config_hashes_by_mv,
        "config_hash_warnings": config_warnings,
    }


def fetch_live(dest: Path) -> None:
    """Best-effort remote pull of TODAY's known-by-name files, for --live.
    See the module docstring for why this can never cover a real rolling
    window -- raw.githubusercontent.com has no directory listing, so only
    filenames this function can already guess (today's date, today's
    run_id) are reachable."""
    dest.mkdir(parents=True, exist_ok=True)
    names = ["pick_lock.json", "today_run_meta.json"]
    for name in names:
        try:
            with urllib.request.urlopen(f"{RAW}/{name}", timeout=20) as r:
                (dest / name).write_bytes(r.read())
        except Exception as e:
            print(f"  · could not fetch {name}: {e}", file=sys.stderr)

    try:
        lock = json.loads((dest / "pick_lock.json").read_text())
        date = lock.get("date")
    except Exception:
        date = None
    if date:
        for name in (f"por_log_{date}.jsonl", f"outcome_log_{date}.jsonl"):
            try:
                with urllib.request.urlopen(f"{RAW}/{name}", timeout=20) as r:
                    (dest / name).write_bytes(r.read())
            except Exception as e:
                print(f"  · could not fetch {name}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None, help="Directory containing por_log_*.jsonl, outcome_log_*.jsonl, "
                                                  "prediction_log_*.jsonl, pick_lock.json (a data-branch checkout's "
                                                  "public/data/current). Default: REPO_ROOT/public/data/current.")
    ap.add_argument("--live", action="store_true", help="Best-effort remote fetch of today's files only "
                                                          "(see module docstring). Ignored if --dir is given.")
    ap.add_argument("--model-version", default=None, help="Restrict the headline eval to one model_version "
                                                            "(e.g. mlb_hr_v3). Default: all versions together.")
    ap.add_argument("--config-hash", default=None, help="Restrict the headline eval to one config_hash "
                                                          "(e.g. sha256:abc123...) -- see CONFIG PROVENANCE in the "
                                                          "report / docs/MODELS.md. Composes with --model-version "
                                                          "(both may be given together). Default: all configs together, "
                                                          "with any multi-config drift under one model_version reported "
                                                          "as a loud warning rather than silently pooled.")
    ap.add_argument("--as-of", default=None, help="Date (YYYY-MM-DD) the rolling windows end on. Default: today (UTC).")
    ap.add_argument("--out", default=None, help="Where to write the JSON summary. Default: eval_report.json "
                                                  "next to this script.")
    a = ap.parse_args()

    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else dt.datetime.now(dt.timezone.utc).date()

    if a.dir:
        data_dir = Path(a.dir)
    elif a.live:
        data_dir = Path("/tmp/eval_report_live")
        print("fetching today's files (best-effort, see --live in --help)…", file=sys.stderr)
        fetch_live(data_dir)
    else:
        data_dir = REPO_ROOT / "public" / "data" / "current"

    if not data_dir.exists():
        print(f"no such directory: {data_dir}", file=sys.stderr)
        return 1

    report = build_report(data_dir, a.model_version, as_of, live=a.live, config_hash=a.config_hash)

    print(render_text(report))

    out_path = Path(a.out) if a.out else (SCRIPT_DIR / "eval_report.json")
    try:
        out_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nwrote {out_path}", file=sys.stderr)
    except Exception as e:
        print(f"\ncould not write {out_path}: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
