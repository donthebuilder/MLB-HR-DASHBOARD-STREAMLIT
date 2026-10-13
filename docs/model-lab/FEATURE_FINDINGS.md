# FEATURE FINDINGS: NFL anytime touchdown

All numbers: `EXPERIMENT_RESULTS.md`. Selection folds 2022-2024 (n = 14,329) unless stated. "Log loss" differences are against the alternative named; negative favours the first. Intervals are 95% paired cluster bootstraps.

## Supported (out-of-sample, stable across folds)

1. **Opportunity features carry most of the gain.** Removing the opportunity family from the full model costs +0.0041 log loss [0.0030, 0.0053]. Snap share is the largest and most stable coefficient (+0.32, +0.30, +0.32 standardised across the three folds), then the role-aware opponent rush-allowed measure (+0.23, +0.21, +0.19), goal-line touches, touches and targets (same sign in all folds).
2. **Team scoring context adds independent value.** Removing the team family (implied total, game total, spread, team red-zone TD rate, drives) costs +0.0028 [0.0019, 0.0038]. Implied total and spread keep a positive sign in every fold. Adding the team family to opportunity alone (F4) is already within 0.0013 log loss of the best model.
3. **Snap share is non-linear.** Adding snap-share-squared improves log loss by 0.0005 [0.0002, 0.0008]; p = 0.001, below the Holm threshold of 0.005 for 10 tests. Real, but 0.1% of log loss.
4. **The shadow v3 score (xTD instead of three correlated red-zone terms) beats v2 in every fold** (selection dLL -0.0035 [-0.0046, -0.0024], 3 of 3 folds; holdout 2025 -0.0025 [-0.0043, -0.0010]). The collapsed term is the better single red-zone signal. Consistent with the v3 note that v2 counted one signal three ways.
5. **The published score is already informative.** Mapped to a probability it beats the position base rate by 0.039 log loss, and its AUC rises from 0.558 to 0.708. Alternatives improve on it by about one fifth of that margin, not more.

## Unsupported (tested, no out-of-sample benefit)

For the family rows, the value is full model minus the model without the family (negative = the family helps).

- **Opponent defence (matchup family):** removing it changes log loss by -0.0001 [-0.0011, +0.0010]. The role-aware rush-defence number has a stable coefficient but carries no information the opportunity and team families do not already carry. Adding the pre-registered interaction "expected TDs x opponent softness" gave +0.00014 [-0.0002, +0.0005] (no gain).
- **Target/share family (target share, WOPR, air-yards share):** -0.0001 [-0.0003, +0.0002].
- **Efficiency family (his own recent TDs, 20+ yard plays):** -0.0003 [-0.0009, +0.0002]. A player's recent touchdown count adds nothing once opportunity is in: consistent with "do not mistake a streak for a skill".
- **Environment (home, wind, roof):** +0.0003 [-0.0001, +0.0007] (removing it does not hurt).
- **Regression term** (his TDs above expected last window): +0.00002, p = 0.16.
- **xTD inside the full model:** its coefficient is negative in every fold (-0.21, -0.17, -0.10) because it is almost the same quantity as red-zone and goal-line touches (the v3 note measured correlations of 0.82 to 0.94); removing it improves log loss by 0.0002. Do not read the sign as "expected TDs lower the chance"; it is collinearity.
- **Touch volume x implied total** interaction: -0.00004, p = 0.55.
- **Empirical-Bayes touchdown rate x touches** and **Poisson on xTD** are much worse than the published score (+0.034 and +0.027 log loss vs B): a player's own TD history is a poor way to rank touchdown chance next week.

## Inconclusive

- **Next Gen Stats (separation, cushion, rush yards over expected, YAC over expected):** coverage is only 14% to 35% of player-weeks (they exist for qualifying players), so the tests have little power. Nominal results: separation +0.00026 (p = 0.045, not significant after Holm), rush % over expected +0.00008, cushion +0.00005, YAC over expected -0.00010. None is distinguishable from zero.
- **Division games** (+0.00003, p = 0.23) and **40+ yard receptions** (+0.00017, p = 0.20): no evidence either way at this sample size.
- **Separate rush and receiving models** combined into an anytime probability (J) are no better than a single model (log loss 0.4454 vs 0.4440), with a worse-or-equal result on every fold; no reason to pay for two models.

## Where the model is least reliable (selection folds)

- **Tight ends:** the full-model improvement over the published score is smallest and its interval includes zero (-0.0035 [-0.0080, +0.0011]; AUC 0.661).
- **High-touch players (14+ touches/game, n = 1,479):** AUC is 0.593 for both models and the improvement is -0.0072 [-0.0186, +0.0066]. Among already-heavily-used players, neither model separates who scores.
- **Stale inputs** (carryover rows, n = 2,687) behave no worse than own-season rows relative to the published score (dLL -0.0066 vs -0.0064).
- **Scale at the bottom:** both the mapped published score and the ensemble over-predict the lowest decile (published: predicts 6.3%, observed 4.8% pooled; holdout 5.7% vs 3.2%).

## Quarterbacks

1,742 QB-weeks, base rate 15.1%. A logistic model on rushing opportunity and team context: log loss 0.3912 vs 0.4255 for the base rate, dLL -0.0343 [-0.0440, -0.0251], AUC 0.697. Designed-run share and opponent QB rush TDs allowed were backtested earlier (QB-only AUC, 3 seasons) and did not clear the noise. This is consistent with the QB TD model shipped in PR 3 using the existing weights.

## Not testable with current data
Route participation, end-zone targets, timestamped injury/availability, timestamped lines, and a recency-window sweep.
