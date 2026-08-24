#!/usr/bin/env python3
"""
nfl_registry.py — the single source of truth for "what scoring logic
produced this number," NFL side.

Why this exists
----------------
bots/model_registry.py's own docstring named this file before it existed:
"MODEL_FAMILY ... a short slug identifying this whole scoring system
('moonshot-mlb'), as opposed to a sibling system (the NFL bot) that will
eventually get its own registry with the same shape." This is that
registry. Before this module, an NFL prediction row -- unlike its MLB
sibling -- carried no record of which version of the scoring logic
produced it at all: no registry, no run id, no config hash, nothing. A
weight change to nfl_scoring.MODELS left no mark on any output, and there
was no way to tell, after the fact, whether two rows for the same player in
the same market were scored by the same logic or two different ones.

This module does not change any score. It is purely additive: a place to
write down, in one spot, what today's NFL logic should be called.

MODEL_FAMILY
------------
A short slug identifying this whole scoring system ("moonshot-nfl"), the
sibling of MODEL_FAMILY = "moonshot-mlb" in bots/model_registry.py. Same
shape, same rule, a different sport's dial.

MODEL_VERSIONS
--------------
One version string per market. nfl_scoring.py scores seven markets --
TD, REC_YDS, REC, RUSH_YDS, RUSH_ATT, PASS_YDS, KICK_PTS -- out of its
MODELS dict, and each is versioned independently for the same reason MLB's
per-market split exists: a weight edit to KICK_PTS does not make TD a
different model. Unlike MLB's registry, there is no "picks" entry here yet
-- nfl_picks.build() is not itself an independently-tunable designation
model the way build_game_pick_role_map() is on the MLB side (see
bots/model_registry.py's own docstring for why picks earned a slot there);
if that changes, a "picks" key belongs here on the same terms MLB's does.

Version bump rule (write this down before touching a weight, not after):
bump the integer if and only if the change could alter the *numeric
output* for at least one historical input -- a weight edit, a new/removed
component, a bar (threshold) change, or a bug fix that changes a produced
number. Do NOT bump for a refactor, a rename, a comment, a display/text
change, or anything in the frontend -- none of those change what a row's
score means. Same rule as MLB's registry, verbatim.

SCHEMA_VERSION
--------------
The *shape* of a published/archived record -- field names and types, not
the model logic. This changes only when a record's structure changes (a
field renamed, added, or dropped from the NFL prediction-log contract),
never when a weight moves. Keeping this separate from MODEL_VERSIONS
answers two different questions independently: "can my parser read this
row?" (schema) vs "what logic produced this number?" (model version).

What this module deliberately does NOT do (same scope limits as MLB's
registry, for the same reasons):
  - It does not hash config to auto-detect an unbumped drift. Versions
    here are declared, not derived, and depend on whoever changes a weight
    in nfl_scoring.MODELS also updating this file. See
    bots/nfl/nfl_config_fingerprint.py for the machine-verifiable backstop
    -- the NFL sibling of bots/config_fingerprint.py's hr_config_hash().
  - It does not track per-run or per-row identity -- that is `run_id`,
    built fresh each bot execution (see build_nfl_run_meta() in
    nfl_bot.py), not a registry concern.
  - It does not touch nfl_scoring.MODELS, derive(), score(), or any weight
    or bar in nfl_scoring.py. Nothing in this file is read by the scorer.

FIRST-EVER VERSIONS
--------------------
Every version below is v1 because this is the first time NFL scoring logic
has ever been labeled. Unlike MLB's registry (created 2026-08-21 to label
logic that had already been running, and evolving, for months under no
name at all), there is no backlog of unlabeled history to reconcile here
-- v1 simply means "the logic in nfl_scoring.py as of the day this
registry was created," 2026-08-24.
"""

from __future__ import annotations

import re
from typing import Dict

# A short slug for this whole scoring system, the sibling of
# bots/model_registry.py's MODEL_FAMILY = "moonshot-mlb".
MODEL_FAMILY = "moonshot-nfl"

# One version string per market, keyed exactly like nfl_scoring.MODELS.
# Bump the integer per the rule above; never reuse a retired version
# string, never rewrite a version string in place.
MODEL_VERSIONS: Dict[str, str] = {
    "TD":       "nfl_td_v1",
    "REC_YDS":  "nfl_recyds_v1",
    "REC":      "nfl_rec_v1",
    "RUSH_YDS": "nfl_rushyds_v1",
    "RUSH_ATT": "nfl_rushatt_v1",
    "PASS_YDS": "nfl_passyds_v1",
    "KICK_PTS": "nfl_kickpts_v1",
}

# The shape of a published/archived record. Bump only when the record
# structure itself changes (a field added/renamed/dropped in the NFL
# prediction-log or run-meta contract) -- never for a scoring/weight
# change.
SCHEMA_VERSION = 1

# "{market}_v{N}" with an optional trailing qualifier -- the load-bearing
# part is that a version number is present as its own underscore-delimited
# segment somewhere in the string, not that it's the very last token. Same
# pattern as bots/model_registry.py's, verbatim, so the two registries stay
# interchangeable to anything that validates a version string generically.
_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*_v\d+(_[a-z0-9]+)*$")


def validate_registry() -> None:
    """Raise if the registry is malformed. Called at import time below (and
    should be called by any NFL smoke test) so a typo'd version string
    (or an accidentally emptied MODEL_VERSIONS) fails loudly instead of
    silently shipping unlabeled rows."""
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
