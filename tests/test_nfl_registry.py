"""nfl_registry.py -- the NFL sibling of tests/test_config_fingerprint.py
and tests/test_model_foundation.py's registry checks, scoped to the NFL
side (see bots/nfl/nfl_registry.py's own docstring for why it exists).

Run: python3 -m pytest tests/test_nfl_registry.py -v

Unlike the rest of this repo's tests (script-style, `python <file>.py`),
this file uses real pytest test functions -- the task this was written
under asked for `pytest -v` output specifically, and pytest's per-test
reporting is the clearer fit for that ask.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_registry as R  # noqa: E402
import nfl_scoring  # noqa: E402


def test_model_family_is_the_nfl_slug():
    assert R.MODEL_FAMILY == "moonshot-nfl"


def test_schema_version_is_a_positive_int():
    assert isinstance(R.SCHEMA_VERSION, int)
    assert R.SCHEMA_VERSION >= 1


def test_all_seven_nfl_scoring_markets_have_a_declared_version():
    """The whole point of this registry: every market nfl_scoring.MODELS
    actually scores must have a version string here. Exercised against the
    REAL production MODELS dict, not a hardcoded list of key names, so this
    fails loudly if nfl_scoring.py ever adds or renames a market and nobody
    updated the registry."""
    assert set(R.MODEL_VERSIONS.keys()) == set(nfl_scoring.MODELS.keys())


def test_expected_market_keys_present():
    # Pinned explicitly too, so a future accidental rename in BOTH files at
    # once (which the set-equality check above can't catch) still fails.
    expected = {"TD", "REC_YDS", "REC", "RUSH_YDS", "RUSH_ATT", "PASS_YDS", "KICK_PTS"}
    assert set(R.MODEL_VERSIONS.keys()) == expected


def test_every_version_string_matches_the_declared_pattern():
    for market, version in R.MODEL_VERSIONS.items():
        assert R._VERSION_PATTERN.match(version), (
            f"{market}={version!r} does not match the '{{sport}}_{{market}}_v{{N}}' shape"
        )


def test_every_version_is_v1_first_ever_release():
    # nfl_registry.py's own docstring: every version is v1 because this is
    # the first time NFL scoring logic has ever been labeled. If any market
    # drifts off v1 this test should be updated deliberately, not silently.
    for market, version in R.MODEL_VERSIONS.items():
        assert version.endswith("_v1"), f"{market}={version!r} expected to end in _v1"


def test_versions_are_nfl_prefixed_not_mlb():
    for market, version in R.MODEL_VERSIONS.items():
        assert version.startswith("nfl_"), f"{market}={version!r} should start with 'nfl_'"


def test_model_versions_snapshot_is_a_defensive_copy():
    snap = R.model_versions_snapshot()
    assert snap == R.MODEL_VERSIONS
    snap["TD"] = "tampered"
    assert R.MODEL_VERSIONS["TD"] != "tampered", "mutating the snapshot must not mutate the registry"


def test_validate_registry_passes_on_the_real_registry():
    # Already ran once at import time (nfl_registry.py calls it at module
    # scope); calling it again here is the explicit, visible assertion.
    R.validate_registry()


def test_validate_registry_rejects_empty_model_family():
    orig = R.MODEL_FAMILY
    try:
        R.MODEL_FAMILY = ""
        try:
            R.validate_registry()
            assert False, "expected ValueError for empty MODEL_FAMILY"
        except ValueError:
            pass
    finally:
        R.MODEL_FAMILY = orig


def test_validate_registry_rejects_empty_model_versions():
    orig = R.MODEL_VERSIONS
    try:
        R.MODEL_VERSIONS = {}
        try:
            R.validate_registry()
            assert False, "expected ValueError for empty MODEL_VERSIONS"
        except ValueError:
            pass
    finally:
        R.MODEL_VERSIONS = orig


def test_validate_registry_rejects_a_malformed_version_string():
    orig = dict(R.MODEL_VERSIONS)
    try:
        R.MODEL_VERSIONS["TD"] = "not-a-valid-version"
        try:
            R.validate_registry()
            assert False, "expected ValueError for a version string that doesn't match the pattern"
        except ValueError:
            pass
    finally:
        R.MODEL_VERSIONS = orig


def test_validate_registry_rejects_non_positive_schema_version():
    orig = R.SCHEMA_VERSION
    try:
        R.SCHEMA_VERSION = 0
        try:
            R.validate_registry()
            assert False, "expected ValueError for SCHEMA_VERSION < 1"
        except ValueError:
            pass
    finally:
        R.SCHEMA_VERSION = orig


def test_smoke_import_does_not_raise():
    """A bad registry should fail at import time (validate_registry() runs
    at module scope), not surface later as a mysteriously-missing
    model_version -- this just re-imports to confirm that guard is live."""
    import importlib
    importlib.reload(R)
    assert R.MODEL_FAMILY == "moonshot-nfl"
