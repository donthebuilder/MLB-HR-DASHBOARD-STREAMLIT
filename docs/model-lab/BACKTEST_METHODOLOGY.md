# BACKTEST METHODOLOGY

Code: `model_lab/nfl_td_walkforward.py`, `model_lab/nfl_td_discovery.py`, `model_lab/build_nfl_tables.py`. Tests: `tests/test_model_lab.py`.

## Target
Anytime touchdown: rushing TDs + receiving TDs >= 1 in the game, per RB/WR/TE player-week. Variants: rushing TD >= 1 and receiving TD >= 1 (experiment J combines separately fitted models as 1-(1-p_rush)(1-p_rec)). QBs are evaluated separately.

## Population (and its limits)
One row per player-week with a stat line, not Out on the injury report, with >= 3 games of history (the production builder's filter). Missing players are not imputed. This is "among players who played".

## Features and leakage control
- Features are an explicit whitelist (`FEATURE_GROUPS`): trailing form over weeks before the game (`nfl_features._roll`), prior-season carryover, schedule context for the game, and opponent trailing allowed-rates. (Next Gen Stats means are used only as discovery candidates, not in the base model.)
- Same-week outcome columns (`rz_car`, `xtd`, `td_rush`, `carries`, ...) are named in `FORBIDDEN_SAME_WEEK`; `assert_no_leak` fails the run if any becomes a feature. A unit test covers it.
- Imputation medians and standardisation are fitted on training rows only.
- Known residual leakage: closing game lines (see BASELINE_AUDIT, limitation 2).

## Splits
Rolling origin by season: fold 2022 trains on 2021; 2023 on 2021-22; 2024 on 2021-23; each predicts the next season. **2025 is a final holdout**: it was run once, after the selection folds were complete and the finalists named (K ensemble, G logistic, F4 simple logistic, plus the published score and the baselines). Hyper-parameters (logistic C, boosting depth) are chosen on the latest training season only (inner validation), never on the test season. Fold 2022 has a single training season and uses fixed defaults.

## Models compared on identical rows
A position base rate. B published score mapped to a probability (logistic on the score, train seasons only). C the shadow v3 score mapped the same way. D Poisson on xTD. E empirical-Bayes touchdown-per-touch rate times expected touches. F1-F9 logistic regressions adding one feature family at a time to the opportunity set. G logistic on all families. H, H2 gradient boosting. I position-specific logistic. J separate rush and receive logistic models combined. K, L ensembles (equal-weight probability averages of independently fitted models).

Not tested, and why: a full hierarchical Bayesian player model and hazard/count models (E is only a crude shrinkage version; no sample-size case was made for more); route participation and end-zone targets (not in available data); recency-window sweep (windows fixed in the production builder); position-by-opponent interactions beyond the pre-registered two.

## Metrics
Log loss (primary), Brier, AUC, top-k per week (k = 15, 30), calibration by decile, segments (position, stale-input rows, opportunity tier, week range). Uncertainty: paired cluster bootstrap over (season, week), 1,500 draws, resampling whole weeks. Model-vs-model gaps are reported as differences with their intervals, never as raw hit rates.

## Multiple comparisons and selection
The experiment table lists every configuration. For the discovery step the ten hypotheses were written into `HYPOTHESES` before the run and corrected with Holm's method. One disclosure: one entry (`f_air_yards_share`) was found to be already in the model when the script first ran and was replaced by `f_ngs_avg_yac_above_expectation` before any discovery result existed.

## What this is not
A betting evaluation (no timestamped prices exist for the history), a market-relative evaluation, or a faithful replay of the public ledger: the lab rebuilds features from nflverse, it does not re-score the stored `nfl_prediction_log` lines.
