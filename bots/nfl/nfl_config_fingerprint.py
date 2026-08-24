#!/usr/bin/env python3
"""
nfl_config_fingerprint.py — deterministic scoring-configuration
fingerprint for the NFL bot.

WHY THIS EXISTS
----------------
The NFL sibling of bots/config_fingerprint.py, for the same reason:
nfl_registry.py's MODEL_VERSIONS is an honor system with a paper trail --
"declared, not derived, and depend[ing] on whoever changes a weight also
updating this file" (nfl_registry.py's own docstring, quoting
bots/model_registry.py's). `nfl_config_hash()` is the machine-verifiable
backstop -- a fact about what scoring configuration was ACTUALLY in effect
when a row was scored, computed the same way every time, with no human
step to forget.

WHY A SEPARATE FILE RATHER THAN A FUNCTION ADDED TO config_fingerprint.py
----------------------------------------------------------------------------
bots/config_fingerprint.py's `canonical_json()`, `sha256_hex()`,
`_normalize_for_structure_hash()` and `function_structure_hash()` are
already sport-agnostic -- they take a dict and a list of functions, know
nothing about MLB. Only `hr_config_dict()` / `hr_config_hash()` are
MLB-HR-specific wrappers, and that module's own docstring is a HR-specific
design essay (the "ONE-DICT ASSUMPTION IS FALSE" section, the
apply_model_v2_layers() scope note) that an NFL reader has no reason to
wade through, and that this change has no reason to touch or extend.

nfl_bot.py already keeps its own dependency tree deliberately separate
from bots/mlb_dashboard.py's -- see .github/workflows/nfl.yml's own
comment on bots/nfl/requirements.txt: "Deliberately separate from
bots/requirements.txt (the MLB bot's) so a football run never drags
pybaseball and its transitive tree onto the runner, and so a break in one
sport's dependency set can't ground the other." A same-directory sibling
file that imports only the three generic primitives it needs, and leaves
config_fingerprint.py's MLB-specific docstring and wrapper functions
untouched, matches that existing boundary better than either editing
config_fingerprint.py's prose or making nfl_bot.py reach across sports on
its own.

WHY nfl_config_hash() HASHES ALL SEVEN MARKETS AT ONCE, UNLIKE MLB's
PER-MARKET hr_config_hash()
------------------------------------------------------------------------
MLB's config_fingerprint hashes one market (HR) at a time because
MODEL_WEIGHTS' HR surface and the functions that compute hr_score are
their own thing, separable from hit/hrr/contact's own weights and
formulas. NFL has no such split: every market's weights live in ONE dict
(nfl_scoring.MODELS, keyed TD/REC_YDS/REC/RUSH_YDS/RUSH_ATT/PASS_YDS/
KICK_PTS) and every market is scored by the SAME two functions,
nfl_scoring.derive() and nfl_scoring.score() (score() takes `market` as an
argument and reads MODELS[market] -- there is no per-market score_td(),
score_rec_yds(), etc. to hash separately). Splitting the hash per market
would mean hashing derive()/score()'s AST once per market for no
additional coverage -- the exact same two function bodies every time --
which is redundant, not more precise. One hash over the whole MODELS dict
plus derive()/score()'s AST structure is the accurate mirror of "one
config, one shared formula, seven weight tables inside it."

Consequence, same shape as MLB's own documented one: a weight edit to
KICK_PTS changes nfl_config_hash() even though it didn't move TD's score.
That is over-inclusion (an extra "config changed, go check by eye" flag),
never under-inclusion (a missed real change) -- the same safe-failure-mode
tradeoff bots/config_fingerprint.py's module docstring argues for MLB.

WHAT IS DELIBERATELY NEVER HASHED
------------------------------------
Timestamps, run IDs, git SHAs, environment/package versions, or any player
or outcome data -- same as MLB's fingerprint. This answers "what
configuration," never "which run" or "which data." See
build_nfl_run_meta() in nfl_bot.py for the run-identity fields.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

# The one cross-sport reach in this file: bots/config_fingerprint.py's three
# generic primitives live one directory up. nfl_bot.py and its siblings
# otherwise import only same-directory (bots/nfl/) modules by bare name (see
# nfl_bot.py's own import block) -- this insert is scoped to this single
# small file rather than spread into nfl_bot.py's main import list, so the
# cross-sport dependency is visible in exactly one place.
_BOTS_DIR = str(Path(__file__).resolve().parent.parent)
if _BOTS_DIR not in sys.path:
    sys.path.insert(0, _BOTS_DIR)

from config_fingerprint import canonical_json, sha256_hex, function_structure_hash  # noqa: E402


def nfl_config_dict(models: Dict[str, Dict[str, Any]],
                     formula_funcs: Iterable[Callable]) -> Dict[str, Any]:
    """The canonical (hashable) representation of the NFL bot's whole
    scoring configuration -- see this module's docstring for why all seven
    markets are hashed together rather than split per market."""
    return {
        "weights": models,
        "formula_structure_sha256": function_structure_hash(formula_funcs),
    }


def nfl_config_hash(models: Dict[str, Dict[str, Any]],
                     formula_funcs: Iterable[Callable]) -> str:
    """`"sha256:<64 hex chars>"` over `models` (pass nfl_scoring.MODELS --
    all seven markets' weight dicts) plus the AST structure of
    `formula_funcs` (pass [nfl_scoring.derive, nfl_scoring.score], the two
    functions that turn those weights into a score for every market).
    Full digest always kept, never truncated at the source -- a caller that
    wants a short display form slices this string; see
    bots/config_fingerprint.py's short_hash() for the MLB-side equivalent,
    reusable here unchanged since it takes a plain string."""
    digest = sha256_hex(canonical_json(nfl_config_dict(models, formula_funcs)))
    return f"sha256:{digest}"
