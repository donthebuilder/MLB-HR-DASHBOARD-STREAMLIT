#!/usr/bin/env python3
"""
config_fingerprint.py — deterministic scoring-configuration fingerprints.

WHY THIS EXISTS
----------------
bots/model_registry.py's own docstring says it plainly: "It does not hash
config to auto-detect an unbumped drift. Versions here are declared, not
derived, and depend on whoever changes a weight also updating this file."
That is an honor system with a paper trail -- strictly better than nothing,
but it means a weight (or gate, or threshold) edit that lands without a
version bump is invisible: every row before and after still reads
`model_version: "mlb_hr_v3"`, and nothing before this module could tell you
they were scored by two different scoring configurations.

`config_hash` is the machine-verifiable backstop. `model_version` stays the
human-declared *intent* ("we meant to ship v3"); `config_hash` is a fact
about what was *actually in effect* when a row was scored, computed the
same way every time from the same inputs, with no human step to forget.

THE ONE-DICT ASSUMPTION IS FALSE -- READ THIS BEFORE EXTENDING
-----------------------------------------------------------------
`MODEL_WEIGHTS["hr_blend"]` / `["hr_gate_thresholds"]` / `["recency_multiplier"]`
in mlb_dashboard.py are the *centralized* HR tuning surface -- deliberately
pulled out because they are, per that dict's own header comment, "the
highest-impact tuning knobs." They are NOT the only thing that can move
`hr_score`. `apply_model_v2_layers()` (mlb_dashboard.py, ~1300 lines) also
contains dozens of literal thresholds and multipliers that are just as
capable of changing the number and are not in any dict at all -- trap
multipliers (0.72/0.78/0.70/0.80/0.82/0.86/0.84), the HR-gate penalty/bonus
ladder (-18/-8/-6/+3/+5/+8), the hard-cap trap (45.0), the "strong_confirmed"
signal thresholds, and -- found while building this -- the single most
recently-tuned number in the whole HR pipeline, the 2026-07-31 shadow
re-anchor: `_hr_form_anchor = 0.30` blended against `0.80 * hr_score +
0.20 * season_power`, both hard-coded inline, nowhere in `MODEL_WEIGHTS`.
A fingerprint that hashed `MODEL_WEIGHTS` alone would have missed the exact
kind of change this module exists to catch.

DESIGN
-------
Two layers, combined into one canonical dict before hashing:

1. `weights` -- the centralized `MODEL_WEIGHTS` subset, as literal data.
   Fully inspectable and diffable on its own.
2. `formula_structure_sha256` -- a hash of the *AST structure* (not the
   source text) of the exact functions that turn those weights + the raw
   per-player signals into `hr_score`. AST structure, not text, so that
   whitespace/indentation and every `#` comment (never part of the AST at
   all) cannot flip the hash -- this codebase's dominant style of change is
   exactly that kind of comment/rationale edit (see the giant audit-log
   comment blocks throughout mlb_dashboard.py), and those aren't scoring
   changes. Each function's own leading docstring (which IS an AST node, a
   string-literal statement) is stripped for the same reason. Everything
   else that can change what the function computes -- a literal number, an
   operator, a comparison, a branch, a call -- changes ast.dump()'s output
   and therefore this hash.

   Deliberately conservative in the other direction: a pure rename/refactor
   with bit-identical output CAN still flip this hash, because ast.dump()
   includes identifier names. That is the safe side of the tradeoff for a
   provenance signal built to answer "can I trust that two rows were scored
   the same way" -- an occasional false "config changed" flag on a refactor
   is a two-minute check; a missed real constant change is a wrong
   model-evaluation decision. See docs/MODELS.md.

KNOWN, DOCUMENTED SCOPE LIMIT
-------------------------------
`apply_model_v2_layers()` computes HR alongside several sibling markets
(hit/hrr/contact soft-multiplier adjustments, `best_blend_score`,
`top_board_raw`, `alt_hr_score`, `longest_hr_score`) in one pass -- they are
structurally interleaved in the current code and this module does not
split them apart (that would be a scoring-code refactor, explicitly out of
scope for a provenance-only change). Consequence: `hr_config_hash()` will
also change if one of those *sibling* markets' formula changes, even though
`hr_score` itself did not move. That is over-inclusion, not under-inclusion
-- it can produce an extra false "config changed" flag to go check by eye,
never a missed real HR change. The alternative (hashing `MODEL_WEIGHTS`
alone) is under-inclusive in exactly the way this module's own docstring
above demonstrates actually happened. Over-flagging is the safer failure
mode for a decision that gates changing live weights.

Explicitly NOT covered (raw per-player input *data*, not scoring
*configuration* -- the HR gate reads `h.hrw_score` as one of five gate
signals, but the HRW score's OWN formula is `mlb_hrw_v1`'s config surface,
not HR's): batter/pitcher stat inputs, `hrw_zone_score_value()`, and any
other sibling market's own top-level scoring function.

WHAT IS DELIBERATELY NEVER HASHED
------------------------------------
Timestamps, run IDs, git SHAs, environment/package versions, any player or
outcome data, or anything else that varies per-run rather than per-config.
This fingerprint answers "what configuration," never "which run" or "which
data" -- see build_run_meta() in mlb_dashboard.py for the run-identity
fields (run_id, generated_at, git_sha), which are separate by design.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import textwrap
from typing import Any, Callable, Dict, Iterable, Optional


def canonical_json(obj: Any) -> str:
    """Deterministic JSON text for hashing: sorted keys (dict insertion/
    declaration order never matters), compact separators (no incidental
    whitespace diffs), ASCII-safe. Float formatting is Python's `repr()`
    under the hood (what `json.dumps` already uses) -- a deterministic
    shortest round-trip decimal for a given IEEE-754 double, identical
    across runs/platforms/Python 3 versions; nothing extra needed for
    "stable numeric representation." Also handles sets (order-independent
    config collections) by rendering them as a sorted list."""
    def _default(o: Any):
        if isinstance(o, (set, frozenset)):
            return sorted(o)
        raise TypeError(f"not JSON-hashable: {type(o)!r}")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_default)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_for_structure_hash(func_ast: ast.FunctionDef) -> ast.FunctionDef:
    """Two normalizations before hashing a function's structure -- see the
    module docstring's DESIGN section.

    1. Drop the function's own leading docstring statement (a real AST
       node -- a string-literal Expr statement -- unlike a `#` comment,
       which was never part of the AST at all and needs no handling here).

    2. Blank the FunctionDef's own `.name` field. `function_structure_hash`
       already tags each function's contribution with `func.__name__`
       (real Python identity, read once per call) BEFORE this AST dump is
       computed, so leaving the name inside the dumped structure too would
       be pure redundancy, not extra coverage -- and it would make the
       function's OWN top-level name part of what has to match for two
       ASTs to compare equal, which is wrong: this hash is about a
       function's BODY, and the name is accounted for by the caller.
    """
    body = list(func_ast.body)
    if body and isinstance(body[0], ast.Expr):
        val = getattr(body[0], "value", None)
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            body = body[1:]
    func_ast.body = body
    func_ast.name = ""
    return func_ast


def function_structure_hash(funcs: Iterable[Callable]) -> str:
    """One sha256 over the AST structure of every function in `funcs`, in
    the given order (order is part of what's hashed on purpose -- reads as
    a meaningless-but-harmless input-order dependency only if the caller
    itself passes an unstable order, which callers here never do: they pass
    a fixed literal list). Raises if a function's source can't be located
    (e.g. defined interactively) -- that should fail the run loudly rather
    than silently hash nothing, since a fingerprint that quietly covers
    zero functions is worse than no fingerprint at all."""
    parts = []
    for func in funcs:
        src = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(src)
        fn_def = tree.body[0]
        if not isinstance(fn_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise ValueError(f"{func!r}: getsource did not parse to a function def")
        fn_def = _normalize_for_structure_hash(fn_def)
        parts.append(f"{getattr(func, '__name__', '?')}:"
                      f"{ast.dump(fn_def, annotate_fields=True, include_attributes=False)}")
    return sha256_hex("\n".join(parts))


def hr_config_dict(model_weights_hr: Dict[str, Dict[str, float]],
                    formula_funcs: Iterable[Callable]) -> Dict[str, Any]:
    """The canonical (hashable) representation of the HR market's scoring
    configuration. See this module's docstring for exactly what
    `model_weights_hr` and `formula_funcs` need to cover and the one
    documented scope limit."""
    return {
        "weights": model_weights_hr,
        "formula_structure_sha256": function_structure_hash(formula_funcs),
    }


def hr_config_hash(model_weights_hr: Dict[str, Dict[str, float]],
                    formula_funcs: Iterable[Callable]) -> str:
    """`"sha256:<64 hex chars>"` -- full digest always kept (never truncated
    at the source; a caller that wants a short display form slices this
    string, the canonical value stays intact everywhere it's stored)."""
    digest = sha256_hex(canonical_json(hr_config_dict(model_weights_hr, formula_funcs)))
    return f"sha256:{digest}"


def short_hash(full_hash: Optional[str], n: int = 12) -> Optional[str]:
    """Display-only short form, e.g. "sha256:a1b2c3d4e5f6" from
    "sha256:a1b2c3d4e5f6...". None in, None out -- never fabricate a short
    form for a missing hash."""
    if not full_hash:
        return None
    prefix, _, digest = full_hash.partition(":")
    return f"{prefix}:{digest[:n]}" if digest else full_hash
