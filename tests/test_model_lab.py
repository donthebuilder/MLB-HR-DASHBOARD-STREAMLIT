"""model_lab/nfl_td_walkforward.py: the backtest's own machinery (leak guard, metrics, bootstrap, training-only fitting).

TEST DATA, MADE UP. Run: python3 -m pytest tests/test_model_lab.py -v
"""
import os
import sys

import numpy as np
import polars as pl
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "model_lab"))
sys.path.insert(0, os.path.join(ROOT, "bots", "nfl"))

import nfl_td_walkforward as L  # noqa: E402


def test_feature_whitelist_has_no_same_week_outcome():
    L.assert_no_leak(L.ALL_FEATURES)
    with pytest.raises(AssertionError):
        L.assert_no_leak(["f_rz_opp", "rz_car"])
    with pytest.raises(AssertionError):
        L.assert_no_leak(["xtd"])


def test_every_feature_is_a_trailing_or_pregame_column():
    pregame = {"implied_total", "total_line", "spread", "is_home", "wind_mph", "indoors", "is_carryover"}
    for c in L.ALL_FEATURES:
        assert c.startswith("f_") or c in pregame, c


def test_topk_counts_hits_without_int8_wraparound():
    sw = np.array([1] * 40 + [2] * 40)
    p = np.linspace(0, 1, 80)
    y = np.ones(80, dtype=np.int8)                  # int8 sums wrap negative past 127: the lab once printed -13.6%
    assert L.topk_hit(sw, p, y, 15) == 1.0
    assert L.metrics(sw, np.clip(p, 0.01, 0.99), np.r_[np.zeros(40), np.ones(40)].astype(np.int8))["top15"] >= 0


def test_metrics_perfect_and_constant():
    y = np.array([0, 1] * 50); sw = np.repeat(np.arange(10), 10)
    m = L.metrics(sw, np.where(y == 1, 0.99, 0.01), y)
    assert m["auc"] == 1.0 and m["logloss"] < 0.02
    c = L.metrics(sw, np.full(100, 0.5), y)
    assert abs(c["logloss"] - np.log(2)) < 1e-9


def test_paired_bootstrap_sign_and_interval():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 2000); sw = np.repeat(np.arange(100), 20)
    good = np.where(y == 1, 0.7, 0.3); bad = np.full(2000, 0.5)
    d, lo, hi = L.paired_bootstrap(sw, good, bad, y, n_boot=300)
    assert d < 0 and hi < 0 and lo <= d <= hi        # a real improvement: negative with an interval below zero
    d2, lo2, hi2 = L.paired_bootstrap(sw, bad, bad, y, n_boot=50)
    assert d2 == 0 and lo2 == 0 and hi2 == 0


def test_prep_imputes_with_the_training_median_only():
    tr = pl.DataFrame({"a": [1.0, 2.0, 3.0, None], "b": [0.0, 0.0, 0.0, 0.0]})
    te = pl.DataFrame({"a": [None, 100.0], "b": [1.0, 1.0]})
    pr = L.Prep(["a", "b"]).fit(tr)
    X = pr.X(te, scale=False)
    assert X[0, 0] == 2.0                           # the TRAIN median, not anything from the test frame


def test_platt_is_fit_on_train_and_monotone():
    rng = np.random.default_rng(1)
    s = rng.uniform(10, 90, 4000); y = (rng.uniform(0, 1, 4000) < s / 100).astype(np.int8)
    tr = pl.DataFrame({"score_v2": s, "y": y})
    f = L.fit_platt(tr, "score_v2")
    p = f(pl.DataFrame({"score_v2": [20.0, 50.0, 80.0]}))
    assert p[0] < p[1] < p[2]


def test_calibration_bins_cover_every_row():
    p = np.random.default_rng(2).uniform(0, 1, 1000); y = (np.random.default_rng(3).uniform(0, 1, 1000) < p).astype(int)
    c = L.calibration(p, y)
    assert sum(b["n"] for b in c) == 1000 and len(c) == 10


def test_fitted_weights_are_nonnegative_and_sum_to_one():
    import nfl_formula_audit as A
    rng = np.random.default_rng(5)
    P = rng.uniform(0, 1, (3000, 4))
    y = (rng.uniform(0, 1, 3000) < 0.1 + 0.6 * P[:, 0] * P[:, 1]).astype(float)   # columns 0 and 1 matter, 2 and 3 are noise
    w, _ = A.fit_nonneg_weights(P, y)
    assert (w >= 0).all() and abs(w.sum() - 1) < 1e-9
    assert w[0] + w[1] > 0.6


def test_blend_uses_weights_on_percentile_columns():
    import nfl_formula_audit as A
    f = pl.DataFrame({"p_a": [0.0, 1.0], "p_b": [1.0, 0.0]})
    out = A.blend(f, {"a": 0.75, "b": 0.25})
    assert list(np.round(out, 6)) == [25.0, 75.0]


def test_availability_drops_constant_components_and_renormalises():
    import nfl_formula_audit as A
    f = pl.DataFrame({"f_touches": [1.0, 2.0, 3.0], "f_snap_pct": [0.5, 0.5, 0.5], "f_rz_opp": [0.0, 1.0, 2.0],
                      "implied_total": [20.0, 21.0, 22.0], "f_xtd": [0.1, 0.2, 0.3], "f_gl_opp": [0.0, 0.0, 1.0]})
    got = dict(A.available(f, "TD"))
    assert "f_snap_pct" not in got and abs(sum(got.values()) - 1) < 1e-9


if __name__ == "__main__":
    # the repo's CI runs each test file as a script (exit 0 = pass): hand off to pytest so the tests actually run
    raise SystemExit(pytest.main([__file__, "-q"]))
