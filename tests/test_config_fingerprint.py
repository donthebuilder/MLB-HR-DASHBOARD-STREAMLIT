"""config_hash — deterministic scoring-configuration fingerprints.

WHY THIS EXISTS. model_registry.py's own docstring: "It does not hash
config to auto-detect an unbumped drift. Versions here are declared, not
derived." That is an honor system with a paper trail. config_hash (see
bots/config_fingerprint.py) is the machine-verifiable backstop: a weight OR
an inline threshold/gate/multiplier edit that lands without a model_version
bump must still change this hash, because nothing about computing it
depends on a human remembering anything.

Two layers are tested here independently:
  1. The WEIGHTS layer -- canonical_json + sha256 over MODEL_WEIGHTS' real
     HR surface (hr_blend / hr_gate_thresholds / recency_multiplier).
     Exercised against the REAL production dict in mlb_dashboard.py, not a
     synthetic stand-in, so these assertions fail if that dict's shape ever
     stops being hashable the way this module assumes.
  2. The FORMULA-STRUCTURE layer -- function_structure_hash() over the AST
     of the functions that actually compute hr_score, which is where most
     of the HR pipeline's real tuning knobs live (see
     config_fingerprint.py's own module docstring for the concrete example
     that motivated this: the 2026-07-31 `_hr_form_anchor = 0.30` re-anchor,
     hard-coded inline, nowhere in MODEL_WEIGHTS). Exercised against small
     synthetic functions here, so the mechanism itself is proven in
     isolation from the 13,000-line file it's applied to in production.

Run: python tests/test_config_fingerprint.py
"""
import copy
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config_fingerprint as cf  # noqa: E402
import bots.mlb_dashboard as md  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def checkTrue(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


# ── 1. IDENTICAL CONFIG -> IDENTICAL HASH ────────────────────────────────
weights = {k: md.MODEL_WEIGHTS[k] for k in md._HR_CONFIG_WEIGHT_KEYS}
h1 = cf.hr_config_hash(weights, md._HR_CONFIG_FORMULA_FUNCS)
h2 = cf.hr_config_hash(weights, md._HR_CONFIG_FORMULA_FUNCS)
check("(1) identical config produces an identical hash", h1, h2)
checkTrue("(1) hash has the documented shape", h1.startswith("sha256:") and len(h1) == len("sha256:") + 64)

# Same, but via mlb_dashboard's own real production wrapper -- the thing
# actually called at run time, not just the library function underneath it.
check("(1) production hr_config_hash() is deterministic across calls", md.hr_config_hash(), md.hr_config_hash())

# ── 2. DICTIONARY ORDERING DOES NOT CHANGE THE HASH ──────────────────────
reordered_weights = {k: weights[k] for k in reversed(list(weights.keys()))}
reordered_inner = dict(weights)
reordered_inner["hr_blend"] = dict(reversed(list(weights["hr_blend"].items())))
h_reordered = cf.hr_config_hash(reordered_weights, md._HR_CONFIG_FORMULA_FUNCS)
h_reordered_inner = cf.hr_config_hash(reordered_inner, md._HR_CONFIG_FORMULA_FUNCS)
check("(2) top-level key order is irrelevant", h_reordered, h1)
check("(2) nested dict (hr_blend) key order is irrelevant", h_reordered_inner, h1)

# ── 3. CHANGING ONE RELEVANT HR WEIGHT CHANGES THE HASH ──────────────────
mutated = copy.deepcopy(weights)
mutated["hr_blend"]["season_power"] = round(mutated["hr_blend"]["season_power"] + 0.01, 6)
h_weight_changed = cf.hr_config_hash(mutated, md._HR_CONFIG_FORMULA_FUNCS)
checkTrue("(3) changing one hr_blend weight changes the hash", h_weight_changed != h1)

# A weight edit so small it would be easy to wave off must still register --
# this is exactly the class of change a version bump is easiest to forget.
mutated_tiny = copy.deepcopy(weights)
mutated_tiny["hr_blend"]["k_rate"] = round(mutated_tiny["hr_blend"]["k_rate"] + 1e-6, 9)
h_tiny = cf.hr_config_hash(mutated_tiny, md._HR_CONFIG_FORMULA_FUNCS)
checkTrue("(3) even a 1e-6 weight nudge changes the hash", h_tiny != h1)

# ── 4. CHANGING ONE RELEVANT HR THRESHOLD/GATE CHANGES THE HASH ──────────
mutated_gate = copy.deepcopy(weights)
mutated_gate["hr_gate_thresholds"]["iso"] = round(mutated_gate["hr_gate_thresholds"]["iso"] + 0.01, 6)
h_gate_changed = cf.hr_config_hash(mutated_gate, md._HR_CONFIG_FORMULA_FUNCS)
checkTrue("(4) changing one hr_gate_thresholds value changes the hash", h_gate_changed != h1)

mutated_recency = copy.deepcopy(weights)
mutated_recency["recency_multiplier"]["hot_strong"] = round(mutated_recency["recency_multiplier"]["hot_strong"] + 0.01, 6)
h_recency_changed = cf.hr_config_hash(mutated_recency, md._HR_CONFIG_FORMULA_FUNCS)
checkTrue("(4) changing one recency_multiplier value changes the hash", h_recency_changed != h1)

# ── 5/6/7. RUN ID, GENERATED_AT, GIT SHA DO NOT AFFECT THE HASH ──────────
# Drive this off the REAL build_run_meta() -- run_id embeds a fresh uuid4/
# timestamp and generated_at is wall-clock every call, so two real calls a
# moment apart are guaranteed to differ on both, with zero test-side
# mocking needed for those two. config_hashes["hr"] must be identical
# across both calls despite that.
meta_a = md.build_run_meta(dt.date(2026, 8, 21), "today", type("Args", (), {})())
meta_b = md.build_run_meta(dt.date(2026, 8, 21), "today", type("Args", (), {})())
checkTrue("(5) two real build_run_meta() calls get different run_ids",
          meta_a["run_id"] != meta_b["run_id"])
checkTrue("(6) two real build_run_meta() calls get different generated_at",
          meta_a["generated_at"] != meta_b["generated_at"])
check("(5)(6) despite run_id/generated_at both differing, config_hashes['hr'] is identical",
      meta_a["config_hashes"]["hr"], meta_b["config_hashes"]["hr"])
check("(5)(6) and it matches the standalone hr_config_hash() call", meta_a["config_hashes"]["hr"], h1)

# (7) git_sha: _current_git_sha() deliberately prefers real `git rev-parse
# HEAD` over the GITHUB_SHA env var (a documented 2026-08-21 provenance fix
# in mlb_dashboard.py -- GITHUB_SHA doesn't reliably name the checked-out
# commit for a branch-ref checkout), so it can't be forced to differ from
# here without faking out git itself, which would test git plumbing, not
# this module. The real guarantee is structural instead, and stronger: prove
# hr_config_hash()/build_run_meta()'s config-hash computation never reads
# git_sha (or run_id, or generated_at) as an input in the first place --
# checked directly against the actual function signature, not inferred from
# behavior.
import inspect as _inspect  # noqa: E402
_hr_hash_params = set(_inspect.signature(cf.hr_config_hash).parameters)
checkTrue("(7) hr_config_hash()'s signature has no git_sha/run_id/generated_at parameter at all "
          "-- structurally impossible for any of the three to be an input",
          _hr_hash_params.isdisjoint({"git_sha", "run_id", "generated_at"}))
check("(7) build_run_meta()'s own git_sha field is present (real git plumbing works) "
      "yet config_hashes['hr'] is unaffected by whatever it resolved to",
      meta_a["config_hashes"]["hr"], h1)
checkTrue("(7) git_sha itself is a real non-empty string (sanity: the field exists and isn't broken)",
          bool(meta_a.get("git_sha")))

# ── 8. PREDICTION ROWS RECEIVE THE CORRECT HASH ──────────────────────────
fake_run_meta = {
    "run_id": "2026-08-21.120000Z.test", "generated_at": "2026-08-21T12:00:00+00:00",
    "slate_date": "2026-08-21", "model_versions": {"hr": "mlb_hr_v3"},
    "config_hashes": {"hr": h1},
}
fake_rows_payload = [
    {"player_id": 1, "name": "Ann", "game_pk": 100, "team": "AAA", "opponent": "BBB",
     "pitcher_id": 5, "hr_score": 80.0},
    {"player_id": 2, "name": "Bo", "game_pk": 100, "team": "AAA", "opponent": "BBB",
     "pitcher_id": 5, "hr_score": 55.0},
]
lines = md.build_prediction_log_lines(fake_run_meta, fake_rows_payload)
check("(8) prediction_log's run_meta header line carries config_hashes", lines[0]["config_hashes"]["hr"], h1)
checkTrue("(8) every scored-row line carries the same config_hash as the run",
          all(ln.get("config_hash") == h1 for ln in lines[1:]))
check("(8) exactly the 2 rows plus the header line", len(lines), 3)


# ── FORMULA-STRUCTURE LAYER: proven against small synthetic functions ────
# (isolated from the 13,000-line file it's applied to in production; see
# tests/test_prediction_of_record.py / test_eval_report.py for coverage of
# how the resulting hash actually flows through pick_lock.py/eval_report.py)

def _formula_v1(x):
    """A docstring that explains nothing important."""
    # a harmless inline comment
    gate = 0.180
    return x * 0.24 + gate


def _formula_v1_reformatted(x):
    """A COMPLETELY DIFFERENT docstring -- pure rationale, zero behavior change."""
    # a totally different comment, and extra blank lines below


    gate = 0.180
    return x * 0.24 + gate


# Represents "the SAME function, docstring/comments edited between two
# versions of the file" -- which two separately-named Python objects can't
# express on their own (function_structure_hash's external per-function tag
# is func.__name__, read once per call; see its own docstring). Overriding
# __name__ here does not affect inspect.getsource() (that resolves off
# __code__.co_filename/co_firstlineno, confirmed directly), so this is a
# faithful "same function, different commit" simulation, not a workaround
# that breaks what's actually being tested.
_formula_v1_reformatted.__name__ = _formula_v1.__name__


def _formula_v2_weight_changed(x):
    """A docstring that explains nothing important."""
    gate = 0.180
    return x * 0.25 + gate  # <- the one number that matters, changed


def _formula_v3_threshold_changed(x):
    """A docstring that explains nothing important."""
    gate = 0.190  # <- threshold changed instead
    return x * 0.24 + gate


def _formula_v4_branch_added(x):
    """A docstring that explains nothing important."""
    gate = 0.180
    if x > 100:
        gate += 5.0
    return x * 0.24 + gate


def _formula_v5_renamed(y):
    """Same logic, renamed parameter -- a real (if narrow) refactor."""
    gate = 0.180
    return y * 0.24 + gate


h_base = cf.function_structure_hash([_formula_v1])
h_reformatted = cf.function_structure_hash([_formula_v1_reformatted])
h_weight = cf.function_structure_hash([_formula_v2_weight_changed])
h_threshold = cf.function_structure_hash([_formula_v3_threshold_changed])
h_branch = cf.function_structure_hash([_formula_v4_branch_added])
h_renamed = cf.function_structure_hash([_formula_v5_renamed])

check("(formula) a pure docstring rewrite (the codebase's dominant comment/rationale-edit "
      "style) does NOT change the formula-structure hash",
      h_reformatted, h_base)
checkTrue("(formula) changing an inline blend weight (never in any dict) DOES change the hash",
          h_weight != h_base)
checkTrue("(formula) changing an inline threshold/gate literal DOES change the hash",
          h_threshold != h_base)
checkTrue("(formula) adding a new branch DOES change the hash",
          h_branch != h_base)
checkTrue("(formula) a pure parameter rename (bit-identical output) ALSO changes the hash -- "
          "documented, deliberate over-sensitivity: a refactor can cause a false 'config "
          "changed' flag (safe direction), never mask a real one",
          h_renamed != h_base)

# Function order is part of what's hashed -- a fixed literal list, not a
# derived/sorted one (config_fingerprint.py's own docstring says so).
h_two_funcs_ab = cf.function_structure_hash([_formula_v1, _formula_v2_weight_changed])
h_two_funcs_ba = cf.function_structure_hash([_formula_v2_weight_changed, _formula_v1])
checkTrue("(formula) function order is part of the hash (by design, not an accident)",
          h_two_funcs_ab != h_two_funcs_ba)

# canonical_json itself: stable across dict/set input shapes.
check("(canonical_json) key order never matters",
      cf.canonical_json({"b": 1, "a": 2}), cf.canonical_json({"a": 2, "b": 1}))
check("(canonical_json) a set is rendered as a sorted list, order-independent",
      cf.canonical_json({"x": {3, 1, 2}}), cf.canonical_json({"x": {2, 3, 1}}))

# short_hash: display-only, never fabricates a value for a missing hash.
check("(short_hash) None in, None out", cf.short_hash(None), None)
checkTrue("(short_hash) a real hash gets shortened but keeps the sha256: prefix",
          cf.short_hash(h1).startswith("sha256:") and len(cf.short_hash(h1)) < len(h1))


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   config_fingerprint: {CHECKS} assertions, determinism + canonicalization + "
      f"weight/gate/threshold sensitivity + run_id/generated_at/git_sha independence + "
      f"prediction-log row stamping + formula-structure sensitivity (incl. docstring-edit "
      f"immunity and the documented refactor-oversensitivity tradeoff), against both real "
      f"MODEL_WEIGHTS/build_run_meta() and isolated synthetic functions")
