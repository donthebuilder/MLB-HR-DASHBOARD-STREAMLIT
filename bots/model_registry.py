#!/usr/bin/env python3
"""
model_registry.py — the single source of truth for "what scoring logic
produced this number."

Why this exists
----------------
Before this module, a prediction row carried no record of which version of
the scoring logic produced it. A weight change, a new blend term, a swapped
model -- none of it left a mark on the output, so a graded archive spanning
a logic change was silently mixing two different models under one label
with no way to tell them apart after the fact (see
`docs/DASH_MODEL_INVENTORY.md` / the 2026-07-14 HR model swap).

This module does not change any score. It is purely additive: a place to
write down, in one spot, what today's logic should be called.

MODEL_FAMILY
------------
A short slug identifying this whole scoring system ("moonshot-mlb"), as
opposed to a sibling system (the NFL bot) that will eventually get its own
registry with the same shape.

MODEL_VERSIONS
--------------
One version string per market. Each market (HR, Hit, HRR, ...) changes on
its own clock -- a blend-weight edit to the HR model does not make the Hit
model a different model -- so they are versioned independently rather than
as one system-wide number. `picks` is included because the designation
logic (`build_game_pick_role_map`) is itself a model: it decides who gets
called TOP/HR/HIT/etc, and that selection logic can change independently of
any of the score formulas.

Version bump rule (write this down before touching a weight, not after):
bump the integer if and only if the change could alter the *numeric output*
for at least one historical input -- a weight edit, a new/removed blend
term, a threshold or cap change, a swapped model, or a bug fix that changes
a produced number. Do NOT bump for a refactor, a rename, a comment, a
display/text change, or anything in the frontend -- none of those change
what a row's score means. See docs/MODELS.md for the full policy and the
change log.

SCHEMA_VERSION
--------------
The *shape* of a published/archived record -- field names and types, not
the model logic. This changes only when a record's structure changes (a
field renamed, added, or dropped from the prediction-log contract), never
when a weight moves. Keeping this separate from MODEL_VERSIONS answers two
different questions independently: "can my parser read this row?" (schema)
vs "what logic produced this number?" (model version).

What this module deliberately does NOT do (out of scope for this pass; see
docs/MODELS.md and the DASH_ROADMAP review for the fuller design this could
grow into):
  - It does not hash config to auto-detect an unbumped drift. Versions here
    are declared, not derived, and depend on whoever changes a weight also
    updating this file. Effectively "an honor system with a paper trail" is
    strictly better than the previous state, which was no trail at all.
  - It does not track per-run or per-row identity -- that is `run_id`,
    built fresh each bot execution (see build_run_meta() in
    mlb_dashboard.py), not a registry concern.
  - It does not touch MLB scoring formulas, MODEL_WEIGHTS, HR Score, PMix,
    Pitch Matchup, or Strike Zone Matchup. Nothing in this file is read by
    the scorer.
"""

from __future__ import annotations

import re
from typing import Dict

# A short slug for this whole scoring system, distinct from a sibling system
# (the NFL bot) that would get its own MODEL_FAMILY under this same shape.
MODEL_FAMILY = "moonshot-mlb"

# One version string per market. Bump the integer per the rule above; never
# reuse a retired version string, never rewrite a version string in place.
#
# Current versions are LABELS FOR TODAY'S LOGIC. They are declared as of
# 2026-08-21, the day this registry was created -- they are not a claim
# about when each market's logic last actually changed, and no historical
# row is retroactively re-labeled with them. Rows written before this
# registry existed carry no model_version at all (an empty string, by
# dataclass default); at analysis time those are `pre_registry`, not
# silently attributed to v1/v2/v3. See docs/MODELS.md.
MODEL_VERSIONS: Dict[str, str] = {
    # v3 -> v4 (2026-08-23). The 0.12 meatball slice of pitcher_damage now
    # reads the pitcher's middle-middle rate AGAINST THIS BAT'S SIDE rather
    # than his overall rate, and the decision engine's pitcher_meatball_high
    # gate does the same. No weight moved and hr_blend still sums to 1.00 --
    # but the registry's rule is about NUMBERS, not weights ("bump if and only
    # if the change could alter the numeric output for at least one historical
    # input"), and this changes numbers. A rule that only fires on weight edits
    # would have let this exact change through unlabeled.
    "hr":        "mlb_hr_v4",
    "hr_shadow": "mlb_hr_v1_recency",
    "hit":       "mlb_hit_v2",
    "hrr":       "mlb_hrr_v2",
    "contact":   "mlb_contact_v2",
    "overall":   "mlb_overall_v2",
    "top_board": "mlb_top_v2",
    "hrw":       "mlb_hrw_v1",
    "multi_hit": "mlb_multihit_v1",
    "picks":     "mlb_pickmap_v3",
}

# The shape of a published/archived record. Bump only when the record
# structure itself changes (a field added/renamed/dropped in the
# prediction-log or run-meta contract) -- never for a scoring/weight change.
SCHEMA_VERSION = 1

# "{market}_v{N}" with an optional trailing qualifier (e.g. the shadow
# model's "mlb_hr_v1_recency") -- the load-bearing part is that a version
# number is present as its own underscore-delimited segment somewhere in
# the string, not that it's the very last token.
_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*_v\d+(_[a-z0-9]+)*$")


def validate_registry() -> None:
    """Raise if the registry is malformed. Called by smoke_test.py so a
    typo'd version string (or an accidentally emptied MODEL_VERSIONS) fails
    loudly instead of silently shipping unlabeled rows."""
    if not MODEL_FAMILY or not isinstance(MODEL_FAMILY, str):
        raise ValueError("MODEL_FAMILY must be a non-empty string")
    if not MODEL_VERSIONS:
        raise ValueError("MODEL_VERSIONS must not be empty")
    for market, version in MODEL_VERSIONS.items():
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"MODEL_VERSIONS[{market!r}] is empty/not a string: {version!r}")
        if not _VERSION_PATTERN.match(version):
            raise ValueError(
                f"MODEL_VERSIONS[{market!r}] = {version!r} does not match "
                f"the '{{sport}}_{{market}}_v{{N}}' shape"
            )
    if not isinstance(SCHEMA_VERSION, int) or SCHEMA_VERSION < 1:
        raise ValueError(f"SCHEMA_VERSION must be a positive int, got {SCHEMA_VERSION!r}")


def model_versions_snapshot() -> Dict[str, str]:
    """A defensive copy of MODEL_VERSIONS for embedding in run_meta / the
    prediction log, so a caller mutating the returned dict can never mutate
    the registry itself."""
    return dict(MODEL_VERSIONS)


# Validate at import time. A bad registry should fail the bot run at
# startup, not surface later as a mysteriously-missing model_version.
validate_registry()
