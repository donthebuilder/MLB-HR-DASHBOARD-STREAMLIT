# DEPLOYMENT RECOMMENDATION

Nothing here changes production. Every item that would change live predictions, public scores, grading or publishing needs your approval first, and the current `nfl_td_v2` stays available for rollback.

## NFL anytime touchdown: ranked recommendations

| # | change | class | evidence | action |
|---|---|---|---|---|
| 1 | **Keep scoring a ranking, do not publish a probability** | keep | Calibration is good in deciles 3-10 but both the mapped score and the ensemble over-predict the bottom decile (published 6.3% predicted vs 4.8% observed; 2025: 5.7% vs 3.2%). The project rule against printing a model probability stands. | none |
| 2 | **Shadow v3 (xTD replaces three red-zone terms)** | validated improvement (small) | beats v2 in 3 of 3 selection folds, dLL -0.0035 [-0.0046, -0.0024], and on the 2025 holdout -0.0025 [-0.0043, -0.0010]; simplest change available | already logged in shadow since 2026-10-01; compare prospectively, then decide promotion. |
| 3 | **Learned-weight logistic score on opportunity + team families (G or F4)** | promising, not conclusive | 3 of 3 selection folds (dLL -0.0065 [-0.0088, -0.0040] for G); on the 2025 holdout the direction holds but the interval includes zero (G -0.0023 [-0.0075, +0.0024]; F4 -0.0026 [-0.0067, +0.0010]) | **ready for shadow**: export frozen coefficients as a numpy weight vector under a new version (for example `nfl_td_lr_v1`; no scikit-learn at runtime), log it beside v2/v3 for the rest of this season, refit only in the off-season. |
| 4 | **Calibrated ensemble (logistic + boosting)** | validated improvement on probabilities, extra complexity | selection dLL -0.0079 [-0.0101, -0.0056], holdout -0.0046 [-0.0092, -0.0004], AUC 0.708 to 0.725 and 0.731 to 0.741 | **do not ship yet.** It needs scikit-learn and a training job on the runner for about 0.003 log loss over the simple logistic, and the top-15 hit rate is not demonstrably better. Revisit if #3 holds up prospectively. |
| 5 | Snap-share non-linearity | validated but negligible | dLL -0.0005 [-0.0008, -0.0002] | not worth a model change |
| 6 | Opponent defence, target-share family, own recent TD efficiency, environment, regression term, interactions | no measurable improvement | `FEATURE_FINDINGS.md` | do not add |
| 7 | Separate rushing and receiving models | no measurable improvement | log loss 0.4454 vs 0.4440, worse or equal in every fold | do not build |
| 8 | Per-man alignment, route participation, end-zone targets, timestamped lines/injuries | not testable with current data | | see data needs |

What the evidence supports telling users: the board orders players well (rank 1-5 scored 57.8% of the time, rank 121+ 10.8%), and a recalibrated opportunity-and-team model is about 1 to 2% better in log loss. It does not support "more precise at the top of the board".

## Monitoring and shadow plan (needs approval to wire into the pipeline)
1. Extend the existing shadow machinery (`nfl_scoring.SHADOW_MODELS` / `score_shadow`) to accept the numpy logistic vector, logged exactly like v3 (log lines only, never the card).
2. Weekly: a prospective comparison (log loss, Brier, AUC, top-15) of v2, v3 and `nfl_td_lr_v1` on the locked pregame rows, appended to a report, never rewriting a past line.
3. Decision rule fixed in advance: promote only if the candidate beats v2 on log loss over at least 8 graded weeks with a paired interval below zero and no worse on the top-15 hit rate.

## MLB MOONSHOT
1. **Fix provenance before any modelling.** 356 graded rows have no `model_version`, 241 no config hash, and 5 config hashes appear in 121 days. Stamp every graded row from its locked run and report the ledger by hash.
2. **Build the full-board dataset**: every rated player on every slate (from `prediction_log_*.jsonl`) joined to the box score (played, home runs). Today the published results hold only the picked slots, so no board-level metric or model comparison is possible. Once that exists the NFL harness (`model_lab/nfl_td_walkforward.py`) ports directly: same folds, metrics and bootstrap.

## NHL LAMP
No NHL model, data or grading code exists in this repository, so nothing was tested. To start: per player-game shots, shot attempts, individual xG, TOI by strength (EV/PP/SH), line and power-play-unit assignment, opposing goalie and confirmed starter, and a timestamped lineup/scratch feed, for at least three seasons, plus a timestamped price archive for a market benchmark. The signal families in the research brief (shot volume, PP role, deployment, goalie) cannot be assessed until that data is reachable.

## Data that unlocks the next improvement
1. A timestamped price archive. `odds_snap` / `odds_lines` began 2026-09-27; kept going, it gives the first market baseline for NFL and MLB (NFL: a few more weeks, MLB: a full season next spring).
2. Timestamped injuries, inactives and snapshot lines (so the closing-line leak in the backtest can be closed).
3. Route participation and end-zone targets for 2026 (the participation file is published once a year).
4. The full-board MLB dataset above, and any NHL feed.
5. 2019-2020 NFL feature tables (the production builder fails on those seasons' schema), for a longer backtest.
