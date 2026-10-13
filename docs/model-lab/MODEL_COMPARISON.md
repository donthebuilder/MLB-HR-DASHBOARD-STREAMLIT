# MODEL COMPARISON

## NFL TUDDY anytime touchdown

Pooled selection folds (2022-24, n = 14,329) and the untouched 2025 holdout (n = 4,964). Log loss is the primary metric; lower is better. B is the published `nfl_td_v2` score mapped to a probability on earlier seasons.

| model | selection log loss | selection AUC | selection dLL vs B [95% CI] | holdout log loss | holdout dLL vs B [95% CI] |
|---|---:|---:|---|---:|---|
| A position base rate | 0.4899 | 0.558 | +0.0394 [+0.0350, +0.0442] | 0.4910 | +0.0505 [+0.0433, +0.0582] |
| **B published v2 (mapped)** | 0.4505 | 0.708 | baseline | 0.4405 | baseline |
| C shadow v3 (mapped) | 0.4470 | 0.717 | -0.0035 [-0.0046, -0.0024] | 0.4380 | -0.0025 [-0.0043, -0.0010] |
| F4 logistic: opportunity + team | 0.4449 | 0.722 | -0.0056 [-0.0075, -0.0036] | 0.4379 | -0.0026 [-0.0067, +0.0010] |
| G logistic: all families | 0.4440 | 0.724 | -0.0065 [-0.0088, -0.0040] | 0.4381 | -0.0023 [-0.0075, +0.0024] |
| H boosting: all families | 0.4439 | 0.723 | -0.0066 [-0.0090, -0.0042] | 0.4358 | -0.0047 [-0.0090, -0.0007] |
| **K ensemble (G + H)** | **0.4427** | **0.725** | **-0.0079 [-0.0101, -0.0056]** | 0.4359 | -0.0046 [-0.0092, -0.0004] |
| D Poisson on xTD | 0.4770 | 0.693 | +0.0265 | 0.4597 | +0.0192 |
| E empirical-Bayes rate | 0.4845 | 0.677 | +0.0340 | 0.4698 | +0.0293 |

Reading it honestly:

- **Beat the simple baselines?** Yes, by a wide margin (A). **Beat the current model?** Yes, modestly and consistently: the ensemble improves log loss by 1.7% (selection) and 1.0% (holdout), AUC 0.708 to 0.725 (selection) and 0.731 to 0.741 (holdout). Every candidate with the opportunity and team families beat B in all three selection folds.
- **Selection noise is about 0.002.** On the 2025 holdout the best model was the 3-family boosting model (H2, 0.4343), which was not the selection winner. The selection winner K was fourth of the 21 configurations on the holdout (0.4359). Differences below about 0.002 between candidates should not be used to pick a winner.
- **Ranking at the top is not clearly better.** Top-15 per week hit rate: B 49.0%, K 49.6% (selection, about 810 picks, standard error about 1.8 points); B 48.9%, K 51.5% (holdout, about 270 picks, standard error about 3 points). The gain is in probabilities across the whole board, not demonstrably in who tops the board.
- **Market:** not measurable. There are no timestamped historical prices in this repository. No claim is made against the market.
- **Calibration:** both B and K track the diagonal across deciles 3 to 10; both over-predict the lowest decile (see `EXPERIMENT_RESULTS.md`). K's 8th decile in 2025 under-predicts (26.7% predicted, 33.5% observed); one season, n = 496 per decile.

## MLB MOONSHOT
No alternative could be tested (no full-board data reachable). Baseline: published-pick HR rate rises monotonically with `hr_score` quartile, 11.6% to 20.0% (n = 3,247 locked graded rows), but that is conditional on selection, and the ledger mixes 5 config hashes and 356 rows with no model version. See BASELINE_AUDIT.

## NHL LAMP
Not examined: this repository holds no NHL model, data or grading pipeline, and the NHL data hosts are unreachable from the lab environment. Nothing is claimed. The required data is listed in DEPLOYMENT_RECOMMENDATION.
