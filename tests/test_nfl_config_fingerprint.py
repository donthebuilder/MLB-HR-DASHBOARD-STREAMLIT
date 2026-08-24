"""nfl_config_hash -- deterministic scoring-configuration fingerprint for
the NFL bot. The NFL sibling of tests/test_config_fingerprint.py, same
assertions ported to nfl_config_hash()'s different shape (one hash over
ALL SEVEN markets' weights at once, not per-market -- see
bots/nfl/nfl_config_fingerprint.py's own docstring for why).

Run: python3 -m pytest tests/test_nfl_config_fingerprint.py -v
"""
import copy
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

import nfl_config_fingerprint as cf  # noqa: E402
import nfl_scoring as ns  # noqa: E402


def _real_weights():
    return copy.deepcopy(ns.MODELS)


def _real_funcs():
    return [ns.derive, ns.score]


# ── 1. IDENTICAL CONFIG -> IDENTICAL HASH ────────────────────────────────

def test_identical_config_produces_identical_hash():
    h1 = cf.nfl_config_hash(_real_weights(), _real_funcs())
    h2 = cf.nfl_config_hash(_real_weights(), _real_funcs())
    assert h1 == h2


def test_hash_has_the_documented_shape():
    h = cf.nfl_config_hash(_real_weights(), _real_funcs())
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_production_hash_is_deterministic_across_calls():
    h1 = cf.nfl_config_hash(ns.MODELS, [ns.derive, ns.score])
    h2 = cf.nfl_config_hash(ns.MODELS, [ns.derive, ns.score])
    assert h1 == h2


# ── 2. DICTIONARY ORDERING DOES NOT CHANGE THE HASH ──────────────────────

def test_top_level_key_order_is_irrelevant():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, _real_funcs())
    reordered = {k: weights[k] for k in reversed(list(weights.keys()))}
    h2 = cf.nfl_config_hash(reordered, _real_funcs())
    assert h1 == h2


def test_nested_weight_dict_key_order_is_irrelevant():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, _real_funcs())
    reordered_inner = copy.deepcopy(weights)
    reordered_inner["TD"]["w"] = dict(reversed(list(weights["TD"]["w"].items())))
    h2 = cf.nfl_config_hash(reordered_inner, _real_funcs())
    assert h1 == h2


def test_function_list_order_is_part_of_what_is_hashed():
    # Documented as intentional in function_structure_hash()'s own
    # docstring: order is hashed on purpose. Callers here always pass a
    # fixed literal order ([derive, score]), so this just proves the
    # mechanism does what it says rather than silently ignoring order.
    h_forward = cf.nfl_config_hash(_real_weights(), [ns.derive, ns.score])
    h_reversed = cf.nfl_config_hash(_real_weights(), [ns.score, ns.derive])
    assert h_forward != h_reversed


# ── 3. CHANGING ONE MARKET'S WEIGHT CHANGES THE HASH ─────────────────────

def test_changing_one_weight_changes_the_hash():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, _real_funcs())
    mutated = copy.deepcopy(weights)
    mutated["TD"]["w"]["f_gl_opp"] = round(mutated["TD"]["w"]["f_gl_opp"] + 0.01, 6)
    h2 = cf.nfl_config_hash(mutated, _real_funcs())
    assert h1 != h2


def test_even_a_tiny_weight_nudge_changes_the_hash():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, _real_funcs())
    mutated = copy.deepcopy(weights)
    mutated["KICK_PTS"]["w"]["implied_total"] = round(
        mutated["KICK_PTS"]["w"]["implied_total"] + 1e-6, 9)
    h2 = cf.nfl_config_hash(mutated, _real_funcs())
    assert h1 != h2


def test_a_weight_change_in_ANY_market_moves_the_one_shared_hash():
    """Documented, deliberate over-inclusion (see this module's docstring):
    unlike MLB's per-market hr_config_hash(), nfl_config_hash() hashes all
    seven markets' weights at once because they share ONE formula
    (derive()/score()), so a weight edit anywhere in MODELS must move the
    hash -- there is no narrower "just this market" surface to hash
    instead."""
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, _real_funcs())
    for market in ns.MODELS:
        mutated = copy.deepcopy(weights)
        first_component = next(iter(mutated[market]["w"]))
        mutated[market]["w"][first_component] += 0.001
        h_mut = cf.nfl_config_hash(mutated, _real_funcs())
        assert h_mut != h1, f"a weight change in {market!r} did not move the hash"


# ── 4. A BAR (THRESHOLD) CHANGE IS PART OF `weights`, SO IT MOVES THE HASH ──

def test_changing_a_market_bar_changes_the_hash():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, _real_funcs())
    mutated = copy.deepcopy(weights)
    mutated["REC_YDS"]["bar"] = mutated["REC_YDS"]["bar"] + 5
    h2 = cf.nfl_config_hash(mutated, _real_funcs())
    assert h1 != h2


# ── 5. FORMULA-STRUCTURE LAYER: AST, NOT SOURCE TEXT ──────────────────────
#
# Exercised against small synthetic functions, same technique
# tests/test_config_fingerprint.py itself uses: REAL top-level `def`
# functions in this module (inspect.getsource() needs a real file/line to
# read -- a string handed to exec() has neither, so an exec()'d "function"
# raises OSError the moment function_structure_hash() tries to read it),
# isolated from nfl_scoring.py's real derive()/score().

def _sample_v1(x):
    """A docstring that explains nothing important."""
    # a harmless inline comment
    gate = 0.180
    return x * 2 + gate


def _sample_v1_reformatted(x):
    """A COMPLETELY DIFFERENT docstring -- pure rationale, zero behavior change."""
    # a totally different comment, and extra blank lines below


    gate = 0.180
    return x * 2 + gate


# "The SAME function, docstring/comments edited between two versions of the
# file" -- two separately-named Python objects can't express that on their
# own (function_structure_hash's external per-function tag is
# func.__name__, read once per call). Overriding __name__ here does not
# affect inspect.getsource() (that resolves off __code__.co_filename /
# co_firstlineno), so this is a faithful "same function, different commit"
# simulation -- same trick tests/test_config_fingerprint.py uses.
_sample_v1_reformatted.__name__ = _sample_v1.__name__


def _sample_v2_logic_changed(x):
    """A docstring that explains nothing important."""
    gate = 0.180
    return x * 3 + gate  # <- the operator/literal that matters, changed


def _sample_v3_whitespace_only(x):
    """A docstring that explains nothing important."""
    gate   =   0.180


    return   x   *   2   +   gate   # a different comment


# Same reasoning as _sample_v1_reformatted above: function_structure_hash()
# tags each function's AST dump with the REAL func.__name__ as an external
# prefix (not part of the AST itself), so two different Python objects with
# two different names are -- correctly -- never "the same function" to this
# hash no matter how identical their bodies are. To isolate "pure
# whitespace/comment diff on what is conceptually the SAME function" (as
# opposed to "two unrelated functions that happen to compute the same
# thing"), the name has to match too.
_sample_v3_whitespace_only.__name__ = _sample_v1.__name__


def test_a_docstring_only_edit_does_not_change_the_hash():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, [_sample_v1])
    h2 = cf.nfl_config_hash(weights, [_sample_v1_reformatted])
    assert h1 == h2, "a docstring/comment-only change must not move the formula-structure hash"


def test_a_real_logic_change_does_change_the_hash():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, [_sample_v1])
    h2 = cf.nfl_config_hash(weights, [_sample_v2_logic_changed])
    assert h1 != h2, "a changed literal/operator must move the formula-structure hash"


def test_whitespace_and_comment_only_changes_do_not_change_the_hash():
    weights = _real_weights()
    h1 = cf.nfl_config_hash(weights, [_sample_v1])
    h2 = cf.nfl_config_hash(weights, [_sample_v3_whitespace_only])
    assert h1 == h2


# ── 6. THE REAL FORMULA FUNCTIONS ARE ACTUALLY LOAD-BEARING ──────────────

def test_derive_and_score_are_both_load_bearing_in_the_real_hash():
    """Proves the real derive()/score() pair isn't decorative: hashing
    without one of them produces a different hash than hashing with both."""
    weights = _real_weights()
    h_both = cf.nfl_config_hash(weights, [ns.derive, ns.score])
    h_derive_only = cf.nfl_config_hash(weights, [ns.derive])
    h_score_only = cf.nfl_config_hash(weights, [ns.score])
    assert h_both != h_derive_only
    assert h_both != h_score_only
    assert h_derive_only != h_score_only
