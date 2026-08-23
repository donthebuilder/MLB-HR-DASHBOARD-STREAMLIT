"""_pitch_match_term() -- the pitch-match missing-data sentinel (2026-08-22).

calculate_pitch_mix_fit() defaults pitch_type_match_score to 0 when no
candidate pitch clears its five sample gates -- which on real slates is
~65% of rated hitters (452 of 695 pre-game rows, 2026-08-20..22, including
20 of those nights' 47 homerers). The old inline expression fed that 0
through minmax_norm(raw, 0, 120), scoring "no per-pitch data" as the
literal FLOOR of a 0.05-weight hr_blend term, while the sibling PMix term
scores its own N/A as a neutral 50. One missing-data policy now: missing
scores neutral, measured matches unchanged.

Also pins the provenance rule: the helper must be in
_HR_CONFIG_FORMULA_FUNCS, so any future edit to this policy moves
config_hash instead of landing as a silent scoring change (the exact gap
Sol audit #2 and its validation round closed twice before).

Run: python tests/test_pitch_match_term.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.mlb_dashboard import (  # noqa: E402
    _HR_CONFIG_FORMULA_FUNCS,
    _hr2_clip,
    _pitch_match_term,
    minmax_norm,
)

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


# ── missing data scores neutral, matching PMix's own N/A policy ──────────
check("raw 0 (no qualifying match / no data) -> neutral 50, not the floor",
      _pitch_match_term(0.0), 50.0)
check("negative raw (defensive) -> neutral 50", _pitch_match_term(-5.0), 50.0)

# ── measured matches are byte-identical to the old formula ───────────────
for raw in (0.1, 30.0, 55.0, 60.0, 75.5, 90.0, 120.0, 200.0):
    check(f"raw {raw}: real match unchanged from the pre-fix formula",
          _pitch_match_term(raw), _hr2_clip(minmax_norm(raw, 0, 120) * 100))

# ── shape facts the fix relies on ────────────────────────────────────────
check("a strong match still outscores neutral (raw 90 -> 75)",
      _pitch_match_term(90.0), 75.0)
check("raw 120 caps at 100", _pitch_match_term(120.0), 100.0)
check("raw beyond the cap still clips to 100", _pitch_match_term(240.0), 100.0)

# ── provenance: the policy is covered by the config fingerprint ──────────
check("_pitch_match_term is in _HR_CONFIG_FORMULA_FUNCS (a future edit to "
      "the sentinel policy must move config_hash, never land silently)",
      _pitch_match_term in _HR_CONFIG_FORMULA_FUNCS, True)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   pitch-match sentinel: {CHECKS} assertions, missing per-pitch data "
      f"scores the same neutral 50 as PMix N/A instead of the term's floor, "
      f"measured matches are unchanged, and the policy is config_hash-covered")
