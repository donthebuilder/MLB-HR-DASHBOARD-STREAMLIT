# PICK FORMULA AUDIT (all sports)

Date 2026-10-13. Nothing in production changed. Companion: `FORMULA_COMPARISON.md`; code: `model_lab/` (NFL harness), data: `model_lab/results/`.

## 0. How to read this

Four different things, never to be conflated:

| word | meaning | NFL TUDDY | NHL LAMP | MLB MOONSHOT | NBA BUCKETS (hidden) |
|---|---|---|---|---|---|
| **score** | what the formula computes | 0-100 weighted blend of within-pool percentiles, rescaled to the MLB score scale | 0-100 mean of three percentiles of tonight's skaters (goal), 2+ goals uses weights 35/30/20/15 | see MLB section | 0-100 mean of equal percentiles of the night's pool |
| **probability** | calibrated chance of the event | **none published.** The lab maps the score to a probability on earlier seasons to be able to score it (never shipped). TD game probability `1-exp(-xTD/gm)` is logged only | **none published**; `1-exp(-goalsPg)` is logged only (gate: 100+ graded calls, every band within 5 points) | see MLB section | none |
| **rank** | position among eligible players that week/night | within-week order of the score; ties broken by xTD then components (game calls) | night rank and game rank by score, ties by shots per game | see MLB section | night rank by score, ties by the market's tie leg |
| **pick status** | CALLED / ON THE BOARD / NOT ON THE BOARD | CALLED = on the bot's top-5 card (site `tdCallStatus`) or the game call; ON THE BOARD = top third of the stored board rank; else NOT | CALLED = top 1 per team per game (goal), top 3 per game (shots, points, assists); board = top third (goal) | see MLB section | CALLED = best per team per game (second only if inside the top third) |

A score of 80 means "ranks near the top of its own pool tonight". It does not mean the same thing across sports or markets, and across slates it moves with who else plays (a percentile among tonight's players). No sport has a validated probability interpretation yet.

## 0.1 What was tested and what was only read

| sport | formula reconstructed from code | backtested here | why not |
|---|---|---|---|
| NFL (7 markets) | yes | **yes**: 2021-2025, 3 selection folds + untouched 2025 holdout | data reachable |
| NHL | yes (site repo `lib/nhl/*`) | **no** | no NHL history reachable from the lab (NHL API blocked); the repo has its own offline backtests (cited, not re-run) |
| NBA | yes (site repo `lib/nba/*`) | **no** | ESPN data blocked; product hidden |
| MLB | yes (bot repo) | **no** (published-pick ledger only) | box scores blocked; full board not stored with outcomes |

Challenge questions 1-10 are answered per component where there is data (NFL) and by inspection elsewhere, labelled as such. Findings from code reading ("by inspection") are hypotheses for a future backtest, not results.

## 1. NFL TUDDY

### 1.1 Formula specification (all seven markets)

Source of weights: `bots/nfl/nfl_scoring.py` `MODELS` (line 20), copied verbatim by script. Each market's score is

```
for each component c (inverted if the name starts with '-'):
    pct_c  = percentile of his value among the league reference (RB/WR/TE league for TD, the market's own positions otherwise)   # nfl_bot._league_pct
               (a component with no reference falls back to his rank inside this week's pool)
raw     = sum_c  w_c' * pct_c          # w' = published weights renormalised over components that are present and not constant
score   = round( 47 + 11 * NormalInv( share of league composites at or below raw ), 1 ), clipped to [5, 95]      # nfl_bot._mlb_scale
```

A component that is entirely missing or constant this week is dropped and the rest are renormalised (`nfl_bot.score_all`, lines 291-445); context columns are dropped together when the schedule gives no context. Players listed Questionable keep 91% of their opportunity features (`nfl_features.QUESTIONABLE_DAMP`); Out/Doubtful never score. Rows need at least 2 games of history in the window (`MIN_GP = 2`), otherwise last season's per-game baseline stands in (`is_carryover`).

| market | positions | bar | weights (verbatim) |
|---|---|---:|---|
| TD (Anytime TD) | RB/WR/TE | 1 | `f_rz_opp` 0.2222, `implied_total` 0.183, `f_touches` 0.1569, `f_xtd` 0.1569, `f_gl_opp` 0.1503, `f_snap_pct` 0.1307 |
| REC_YDS (Receiving yards) | WR/TE/RB | 40 | `f_wopr` 0.42, `f_receiving_yards` 0.36, `f_receiving_air_yards` 0.12, `implied_total` 0.06, `opp_pass_soft` 0.04 |
| REC (Receptions) | WR/TE/RB | 4 | `f_target_share` 0.5, `f_receptions` 0.35, `f_targets` 0.15 |
| RUSH_YDS (Rushing yards) | RB/QB | 50 | `f_carries` 0.65, `f_rushing_yards` 0.2, `f_rz_car` 0.15 |
| RUSH_ATT (Rushing attempts) | RB | 12 | `f_carries` 0.52, `f_rz_car` 0.16, `f_ngs_rush_yards_over_expected_per_att` 0.12, `oppr_rush_soft` 0.2 |
| PASS_YDS (Passing yards) | QB | 225 | `total_line` 0.26, `opp_pass_soft` 0.22, `f_passing_yards` 0.22, `f_attempts` 0.18, `f_passing_cpoe` 0.12 |
| KICK_PTS (Kicking points) | K | 6 | `implied_total` 0.35, `f_tm_fg_drive_rate` 0.25, `-f_tm_rz_td_rate` 0.15, `f_fg_att` 0.1, `kick_env` 0.08, `f_tm_drives` 0.07 |

Notes from the code: TD weights were set by a documented residual scan and a tune-on-one-season / report-on-the-other protocol (`nfl_scoring.py` comments, `SCORING.md`); RUSH_ATT's `oppr_rush_soft` was swept 0 to 0.32 over three seasons; the other markets' weights are design choices with no sweep cited. KICK_PTS is a 32-man pool ranked on a shared scale only because the reference floor is 20 rows.

### 1.2 Path from raw data to a published status

1. `nfl_features.build` (trailing form over weeks before the game, carryover, schedule context, opponent allowed-rates) and `nfl_scoring.derive` (composite inputs, e.g. `f_touches = tgt_n + car_n`, `opp_*_soft`, `oppr_rush_soft` gated at 3 role-games).
2. `nfl_bot.score_all` -> one 0-100 score per market per player (above); the payload row also carries `low_sample` (targets + carries < 3 for RB/WR/TE) and `questionable`.
3. **Card (the bot's calls):** `nfl_picks.build` (line 108) takes, per market, the top `DEPTH = 5` by score. `_select` (line 70) prefers rows that are not `low_sample`, and only backfills with low-sample rows if fewer than five solid ones exist. Pick lock (`nfl_pick_lock.py`) freezes the rungs at each game's kickoff.
4. **Game calls (TD only):** `nfl_game_calls.py`: the top TD score on each team is a call; the higher of the two is TOP, the other is eligible only if inside the top third of the slate's RB/WR/TE board (`BOARD_SHARE = 1/3`, `ceil(n/3)`); ties by score, then xTD per game, then components in weight order, then player id.
5. **Site status (`lib/callStatus.js` `tdCallStatus`):** CALLED = on the bot's card; ON THE BOARD = stored board rank within the top third (`tdBoardCut = ceil(n/3)`); otherwise NOT ON THE BOARD. Only TD carries ON THE BOARD.
6. **Overrides / hardcoded rules:** the 0.91 Questionable damping; the `low_sample` preference in the card; the board cut of one third; the A+ grade cutoff of 78 and `high_confidence_td_flag = score >= 78`. No manual per-player overrides were found in the bot path. The QB TD model (PR 3) is a separate versioned scoring on the same weights.

The displayed score IS the score used to rank and select (the card sorts on `scores[market]`); the only selection logic beyond rank is the low-sample preference and the per-team/third-of-board rules above.

### 1.3 Worked example (real stored prediction, `nfl_week.json` built 2026-10-12, week 5)

Jahmyr Gibbs (DET, RB), TD market. Stored component percentiles (0-100, rounded): `f_rz_opp` 100, `implied_total` 98, `f_touches` 100, `f_xtd` 99, `f_gl_opp` 99, `f_snap_pct` 86. Published weights 0.222 / 0.183 / 0.157 / 0.157 / 0.150 / 0.131. Weighted percentile sum = 0.222x100 + 0.183x98 + 0.157x100 + 0.157x99 + 0.150x99 + 0.131x86 = **97.49**. Stored score = **79.0**: the league-reference rescaling (`_mlb_scale`) maps a composite of 97.5 to 79 (on the same payload a composite of 49.9 maps to 47 and 8.0 maps to 15). Rank 1 of the week's TD board; `low_sample` false, not carryover. He is CALLED because he is on the card and a game call; the score is not a probability.

### 1.4 Ablation, weight sensitivity, candidates and selection policy (generated from `model_lab/results/*.json`)

Method and limits: see `BACKTEST_METHODOLOGY.md`. The lab scores the published formula as a within-week percentile blend of its own components (the convention of the existing `nfl_td_lab.py`); production ranks against a league reference and rescales, which is monotone in the blend within a week, so ranks should match but this was not byte-verified against stored scores. Population: players who played.

### NFL feature ablation and weight sensitivity (selection folds 2022-24)

Each row removes one component (weights renormalised) or scales its weight by 0.5 or 1.5, on identical player-weeks. dLL = log loss minus the published formula's (probabilities from a train-season logistic map of the score); **negative = the change IMPROVES the formula**. 95% paired cluster bootstrap over (season, week).

**TD** (Anytime TD, bar 1, n = 14329, base rate 0.195); published AUC 0.708

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `f_rz_opp` | 0.222 | -0.0014 [-0.0022, -0.0006] | -0.0009 [-0.0012, -0.0006] | +0.0010 [+0.0007, +0.0012] | 0.17 / 0.13 / 0.09 |
| `implied_total` | 0.183 | +0.0025 [+0.0016, +0.0034] | +0.0005 [+0.0001, +0.0010] | +0.0006 [+0.0002, +0.0011] | 0.17 / 0.16 / 0.18 |
| `f_touches` | 0.157 | +0.0038 [+0.0032, +0.0044] | +0.0016 [+0.0013, +0.0018] | -0.0011 [-0.0013, -0.0008] | 0.34 / 0.40 / 0.41 |
| `f_xtd` | 0.157 | -0.0007 [-0.0010, -0.0005] | -0.0004 [-0.0005, -0.0003] | +0.0004 [+0.0003, +0.0005] | 0.10 / 0.07 / 0.08 |
| `f_gl_opp` | 0.150 | -0.0020 [-0.0026, -0.0014] | -0.0011 [-0.0013, -0.0008] | +0.0011 [+0.0009, +0.0014] | 0.00 / 0.01 / 0.00 |
| `f_snap_pct` | 0.131 | +0.0038 [+0.0031, +0.0044] | +0.0016 [+0.0013, +0.0019] | -0.0011 [-0.0013, -0.0008] | 0.22 / 0.24 / 0.24 |

**REC_YDS** (Receiving yards, bar 40, n = 14329, base rate 0.244); published AUC 0.829

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `f_wopr` | 0.420 | +0.0095 [+0.0079, +0.0113] | +0.0023 [+0.0018, +0.0029] | -0.0008 [-0.0012, -0.0005] | 0.36 / 0.41 / 0.47 |
| `f_receiving_yards` | 0.360 | +0.0021 [+0.0004, +0.0037] | -0.0004 [-0.0010, +0.0002] | +0.0011 [+0.0006, +0.0015] | 0.32 / 0.29 / 0.28 |
| `f_receiving_air_yards` | 0.120 | +0.0004 [-0.0002, +0.0011] | -0.0000 [-0.0003, +0.0003] | +0.0003 [+0.0000, +0.0006] | 0.23 / 0.20 / 0.16 |
| `implied_total` | 0.060 | +0.0006 [+0.0002, +0.0012] | +0.0001 [-0.0001, +0.0004] | +0.0002 [-0.0001, +0.0005] | 0.07 / 0.07 / 0.07 |
| `opp_pass_soft` | 0.040 | -0.0001 [-0.0005, +0.0003] | -0.0001 [-0.0003, +0.0001] | +0.0003 [+0.0001, +0.0005] | 0.02 / 0.02 / 0.01 |

**REC** (Receptions, bar 4, n = 14329, base rate 0.253); published AUC 0.832

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `f_target_share` | 0.500 | +0.0033 [+0.0019, +0.0047] | +0.0003 [-0.0002, +0.0007] | +0.0003 [-0.0000, +0.0005] | 0.34 / 0.34 / 0.34 |
| `f_receptions` | 0.350 | +0.0027 [+0.0017, +0.0037] | +0.0005 [+0.0001, +0.0009] | +0.0001 [-0.0002, +0.0004] | 0.34 / 0.33 / 0.34 |
| `f_targets` | 0.150 | +0.0002 [-0.0001, +0.0005] | +0.0001 [-0.0001, +0.0002] | -0.0000 [-0.0001, +0.0001] | 0.32 / 0.33 / 0.32 |

**RUSH_YDS** (Rushing yards, bar 50, n = 5984, base rate 0.221); published AUC 0.848

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `f_carries` | 0.650 | +0.0079 [+0.0052, +0.0106] | +0.0007 [-0.0000, +0.0014] | +0.0000 [-0.0003, +0.0004] | 0.36 / 0.40 / 0.42 |
| `f_rushing_yards` | 0.200 | +0.0034 [+0.0025, +0.0042] | +0.0013 [+0.0010, +0.0017] | -0.0009 [-0.0011, -0.0006] | 0.39 / 0.43 / 0.45 |
| `f_rz_car` | 0.150 | -0.0015 [-0.0029, -0.0004] | -0.0010 [-0.0016, -0.0005] | +0.0013 [+0.0008, +0.0019] | 0.25 / 0.17 / 0.13 |

**RUSH_ATT** (Rushing attempts, bar 12, n = 4242, base rate 0.299); published AUC 0.870

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `f_carries` | 0.520 | +0.0402 [+0.0321, +0.0480] | +0.0057 [+0.0032, +0.0079] | -0.0005 [-0.0017, +0.0007] | 0.40 / 0.47 / 0.47 |
| `f_rz_car` | 0.160 | -0.0033 [-0.0051, -0.0014] | -0.0022 [-0.0031, -0.0014] | +0.0028 [+0.0021, +0.0036] | 0.28 / 0.21 / 0.17 |
| `f_ngs_rush_yards_over_expected_per_att` | 0.120 | -0.0007 [-0.0033, +0.0017] | -0.0018 [-0.0032, -0.0006] | +0.0043 [+0.0030, +0.0056] | 0.00 / 0.00 / 0.03 |
| `oppr_rush_soft` | 0.200 | +0.0168 [+0.0131, +0.0204] | +0.0059 [+0.0042, +0.0075] | -0.0024 [-0.0039, -0.0010] | 0.31 / 0.31 / 0.33 |

**PASS_YDS** (Passing yards, bar 225, n = 1742, base rate 0.456); published AUC 0.666

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `total_line` | 0.260 | -0.0003 [-0.0067, +0.0058] | -0.0020 [-0.0046, +0.0006] | +0.0032 [+0.0014, +0.0049] | 0.25 / 0.22 / 0.20 |
| `opp_pass_soft` | 0.220 | +0.0013 [-0.0042, +0.0070] | -0.0008 [-0.0034, +0.0021] | +0.0029 [+0.0005, +0.0051] | 0.00 / 0.09 / 0.12 |
| `f_passing_yards` | 0.220 | +0.0098 [+0.0059, +0.0137] | +0.0035 [+0.0018, +0.0053] | -0.0017 [-0.0030, -0.0004] | 0.38 / 0.38 / 0.37 |
| `f_attempts` | 0.180 | +0.0108 [+0.0070, +0.0145] | +0.0044 [+0.0026, +0.0062] | -0.0028 [-0.0043, -0.0012] | 0.20 / 0.26 / 0.27 |
| `f_passing_cpoe` | 0.120 | -0.0005 [-0.0035, +0.0022] | -0.0006 [-0.0022, +0.0006] | +0.0012 [-0.0000, +0.0025] | 0.17 / 0.06 / 0.04 |

**KICK_PTS** (Kicking points, bar 6, n = 1574, base rate 0.647); published AUC 0.574

| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |
|---|---:|---|---|---|---|
| `implied_total` | 0.350 | +0.0079 [+0.0010, +0.0147] | +0.0044 [+0.0013, +0.0075] | -0.0029 [-0.0050, -0.0007] | 0.83 / 0.74 / 0.72 |
| `f_tm_fg_drive_rate` | 0.250 | -0.0035 [-0.0079, +0.0014] | -0.0020 [-0.0039, +0.0001] | +0.0017 [+0.0003, +0.0030] | 0.00 / 0.01 / 0.00 |
| `f_tm_rz_td_rate` | 0.150 | -0.0023 [-0.0043, -0.0003] | -0.0013 [-0.0022, -0.0003] | +0.0013 [+0.0004, +0.0021] | 0.01 / 0.05 / 0.06 |
| `f_fg_att` | 0.100 | -0.0018 [-0.0031, -0.0004] | -0.0009 [-0.0015, -0.0002] | +0.0008 [+0.0002, +0.0014] | 0.05 / 0.07 / 0.08 |
| `kick_env` | 0.080 | +0.0009 [+0.0000, +0.0019] | +0.0004 [-0.0000, +0.0009] | -0.0004 [-0.0008, +0.0001] | 0.10 / 0.12 / 0.14 |
| `f_tm_drives` | 0.070 | -0.0001 [-0.0010, +0.0008] | -0.0001 [-0.0006, +0.0004] | +0.0002 [-0.0003, +0.0006] | 0.01 / 0.00 / 0.00 |

### NFL candidate formulas, selection folds then the untouched 2025 holdout

Probabilities from a logistic map fitted on earlier seasons. `fitted` = non-negative regularised logistic weights on the SAME components; `LR same` = unrestricted logistic on the same components; `LR wide` = logistic on every trailing feature. dLL against the published formula.

| market | n sel / hold | published LL sel / hold | best candidate (selection) | its dLL sel [CI] | its dLL hold [CI] | published AUC -> candidate AUC (hold) |
|---|---|---|---|---|---|---|
| TD | 14329 / 4964 | 0.4505 / 0.4405 | lr_same_components | -0.0059 [-0.0074, -0.0044] | -0.0040 [-0.0074, -0.0005] | 0.731 -> 0.738 |
| REC_YDS | 14329 / 4964 | 0.4191 / 0.3948 | lr_wide_features | -0.0043 [-0.0065, -0.0021] | -0.0054 [-0.0092, -0.0017] | 0.838 -> 0.841 |
| REC | 14329 / 4964 | 0.4240 / 0.3954 | lr_wide_features | -0.0038 [-0.0065, -0.0008] | -0.0041 [-0.0109, +0.0020] | 0.844 -> 0.847 |
| RUSH_YDS | 5984 / 2042 | 0.3818 / 0.3740 | lr_same_components | -0.0018 [-0.0032, -0.0005] | -0.0000 [-0.0026, +0.0025] | 0.855 -> 0.854 |
| RUSH_ATT | 4242 / 1456 | 0.4065 / 0.3726 | lr_same_components | -0.0041 [-0.0067, -0.0018] | -0.0021 [-0.0088, +0.0056] | 0.898 -> 0.898 |
| PASS_YDS | 1742 / 586 | 0.6486 / 0.6400 | lr_wide_features | -0.0162 [-0.0319, -0.0005] | -0.0052 [-0.0338, +0.0212] | 0.675 -> 0.682 |
| KICK_PTS | 1574 / 522 | 0.6415 / 0.6292 | fitted_weights | -0.0062 [-0.0136, +0.0019] | -0.0043 [-0.0154, +0.0068] | 0.552 -> 0.578 |

### Frozen candidate weight vectors, scored once on 2025 (Holm over 7 markets)

Freezing rule fixed beforehand: mean of the three selection-fold fitted weights, 2 decimals, drop < 0.02, renormalise.

| market | stable across folds | published -> candidate weights | holdout n | LL published -> candidate | dLL [95% CI] | p | Holm threshold | result |
|---|---|---|---:|---|---|---:|---:|---|
| TD | yes | rz_opp 0.22, implied_total 0.18, touches 0.16, xtd 0.16, gl_opp 0.15, snap_pct 0.13 -> **rz_opp 0.13, implied_total 0.17, touches 0.38, xtd 0.08, snap_pct 0.23** | 4964 | 0.4405 -> 0.4362 | -0.0043 [-0.0074, -0.0015] | 0.004 | 0.0071 | VALIDATED (significant after Holm) |
| REC_YDS | yes | wopr 0.42, receiving_yards 0.36, receiving_air_yards 0.12, implied_total 0.06, opp_pass_soft 0.04 -> **wopr 0.41, receiving_yards 0.30, receiving_air_yards 0.20, implied_total 0.07, opp_pass_soft 0.02** | 4964 | 0.3948 -> 0.3954 | +0.0006 [-0.0001, +0.0013] | 0.097 | 0.0100 | no measurable improvement |
| REC | yes | target_share 0.50, receptions 0.35, targets 0.15 -> **target_share 0.34, receptions 0.34, targets 0.32** | 4964 | 0.3954 -> 0.3957 | +0.0003 [-0.0001, +0.0006] | 0.133 | 0.0125 | no measurable improvement |
| RUSH_YDS | yes | carries 0.65, rushing_yards 0.20, rz_car 0.15 -> **carries 0.39, rushing_yards 0.42, rz_car 0.18** | 2042 | 0.3740 -> 0.3736 | -0.0004 [-0.0021, +0.0012] | 0.653 | 0.0250 | no measurable improvement |
| RUSH_ATT | yes | carries 0.52, rz_car 0.16, ngs_rush_yards_over_expected_per_att 0.12, oppr_rush_soft 0.20 -> **carries 0.46, rz_car 0.22, oppr_rush_soft 0.32** | 1456 | 0.3726 -> 0.3716 | -0.0010 [-0.0078, +0.0062] | 0.786 | 0.0500 | no measurable improvement |
| PASS_YDS | yes | total_line 0.26, opp_pass_soft 0.22, passing_yards 0.22, attempts 0.18, passing_cpoe 0.12 -> **total_line 0.22, opp_pass_soft 0.07, passing_yards 0.38, attempts 0.24, passing_cpoe 0.09** | 586 | 0.6400 -> 0.6473 | +0.0073 [-0.0001, +0.0147] | 0.052 | 0.0083 | worse on holdout |
| KICK_PTS | yes | implied_total 0.35, tm_fg_drive_rate 0.25, tm_rz_td_rate 0.15, fg_att 0.10, kick_env 0.08, tm_drives 0.07 -> **implied_total 0.77, tm_rz_td_rate 0.04, fg_att 0.07, kick_env 0.12** | 522 | 0.6292 -> 0.6254 | -0.0038 [-0.0144, +0.0064] | 0.473 | 0.0167 | no measurable improvement |

### Selection-policy diagnostics for the top-5 card (selection folds, 54 weeks; holdout 18 weeks)

Hit rate of the player at each within-week rank, then concentration. A cell is one player per week, so each rank cell has only n = 54 (selection) and its standard error is about 6.7 points.

| market | rank 1 | 2 | 3 | 4 | 5 | #1-#2 gap (median score pts) | weeks with 2+ picks in one game | weeks with 2+ on one team | outcome correlation same game / different game (pairs) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| TD | 57% | 61% | 56% | 61% | 54% | 1.1 | 76% | 67% | -0.04 / -0.06 (51 / 489) |
| REC_YDS | 80% | 89% | 69% | 74% | 76% | 1.0 | 54% | 13% | n/a / -0.08 (30 / 510) |
| REC | 89% | 81% | 83% | 80% | 81% | 0.4 | 30% | 13% | n/a / -0.06 (21 / 519) |
| RUSH_YDS | 80% | 65% | 69% | 72% | 72% | 0.9 | 26% | 2% | n/a / +0.03 (16 / 524) |
| RUSH_ATT | 91% | 69% | 80% | 76% | 70% | 2.0 | 26% | 0% | n/a / +0.06 (17 / 523) |
| PASS_YDS | 61% | 76% | 67% | 50% | 63% | 3.5 | 80% | 4% | +0.12 / -0.09 (53 / 487) |
| KICK_PTS | 78% | 72% | 80% | 72% | 63% | 3.5 | 22% | 0% | n/a / -0.00 (12 / 528) |


### 1.5 NFL findings

**Confirmed defects (all NFL, measured out of sample):**

| # | defect | severity | evidence |
|---|---|---|---|
| N1 | **TD formula spends 52.9% of its weight on one idea counted three times** (red-zone touches 22.2, xTD 15.7, goal-line touches 15.0). Removing any of the three IMPROVES log loss; goal-line touches fits to about 0 weight in every fold; touches and snap share are under-weighted (fitted 0.34-0.42 and 0.22-0.24 vs published 0.157 and 0.131) | medium | drop `f_gl_opp` -0.0020 [-0.0026, -0.0014]; drop `f_rz_opp` -0.0014; drop `f_xtd` -0.0007; fitted weights stable across folds. The frozen candidate beats the published formula on the 2025 holdout: -0.0043 log loss [-0.0074, -0.0015], Holm p = 0.004 (AUC 0.731 to 0.739) |
| N2 | **Rushing markets over-credit red-zone carries and a near-empty NGS term.** Removing `f_rz_car` improves RUSH_ATT (-0.0033) and RUSH_YDS (-0.0015); the NGS rush-over-expected component (0.12 of RUSH_ATT) fits to about 0 and exists for few rows | low | tables above. On the 2025 holdout the re-weighted candidates are not distinguishable from the published ones (RUSH_ATT -0.0010, RUSH_YDS -0.0004), so this is a simplification opportunity, not a proven gain |
| N3 | **REC weights 50/35/15 are not earned.** Equal thirds, the fitted weights and the published weights all score the same (dLL +0.0001) | low | complexity without value; harmless |
| N4 | **KICK_PTS is close to chance** (AUC 0.574 selection, 0.552 holdout; base rate 64.7% / 67.8% hit the 6-point bar) and the implied total carries almost all of it (fitted 0.72-0.83); four of the six components can be removed without loss | low-medium | small pool (n = 1,574 / 522). Candidate -0.0038 on holdout, interval includes zero |
| N5 | **PASS_YDS "context-led" design is not supported by incremental value.** Removing `total_line`, `opp_pass_soft` or `f_passing_cpoe` changes log loss by -0.0003, +0.0013, -0.0005 (all intervals include 0); passing yards and attempts carry the signal. The refit candidate is WORSE on the 2025 holdout (+0.0073 [-0.0001, +0.0147]) | low | n = 1,742 / 586: too small to re-weight reliably. Keep the published weights |
| N6 | **Inside the top 5 the order carries no demonstrated information.** TD hit rate by rank 1-5: 57% / 61% / 56% / 61% / 54% (n = 54 each, standard error about 6.7 points); the median score gap between #1 and #2 is 1.1 points of 100. "TOP" implies a distinction the data does not show | medium (labelling) | the card is a set of five roughly equal players; a "best pick" claim is unsupported |
| N7 | **The card's concentration is high.** TD top five contained two or more players from the same game in 76% of selection weeks (56% in 2025) and from the same team in 67% (56%). Measured same-game outcome correlation is -0.04 on only 51 pairs, so no harm is shown, and none can be ruled out | low | needs more pairs |
| N8 | The `low_sample` card filter never changed a single pick in the lab (identical hit rates with and without it) | info | harmless; dead weight |

**What the evidence supports keeping:** the volume markets (REC_YDS, REC, RUSH_YDS, RUSH_ATT) are already near their ceiling: fitted weights, an unrestricted logistic and a wide-feature logistic improve log loss by 0.001 to 0.005 at most, and on the 2025 holdout only REC_YDS's wide-feature logistic (-0.0054 [-0.0092, -0.0017]) clears zero. Matchup softness is worth keeping only in RUSH_ATT (`oppr_rush_soft`: removing it costs +0.0168 [+0.0131, +0.0204]; fitted weight 0.31-0.33 vs published 0.20), where the published design sweep agrees.

**Rushing vs receiving pathways (question asked in the brief):** separate rushing and receiving touchdown models combined as 1-(1-p_rush)(1-p_rec) were no better than one model (log loss 0.4454 vs 0.4440, worse or equal in every fold). Not recommended.

**Questions 1-10 for the six TD components** (purpose; supported?; at prediction time?; adds information?; double-counted?; sensible across seasons?; out-of-sample?; calibration/ranking/selection?; weight basis; effect of removing/scaling): `f_snap_pct` and `f_touches`: purpose = opportunity denominator; supported; available (trailing); adds information (largest removal cost +0.0038 each); not double-counted (touches overlaps targets/carries but not the red-zone terms); stable sign and weight in all three folds; improves ranking and calibration; weight manually tuned and under-weighted; raising 1.5x improves by -0.0011. `implied_total`: purpose = team scoring environment; supported (removal +0.0025); available (schedule line, closing-line caveat); independent; stable; documented in `SCORING.md` as causally upstream; weight about right (fitted 0.16-0.18 vs 0.183). `f_rz_opp`, `f_xtd`, `f_gl_opp`: purpose = proximity to the end zone; real signal as a family (the opportunity family costs +0.0041 to remove) but redundant with each other (correlations 0.82-0.94 measured in `nfl_scoring.py`); double-counted; weights set by residual scan on 2024/25 then reduced; out of sample the family should carry about 0.22 total, not 0.53.

**Score, probability, rank, status for NFL:** the score ranks well (rank 1-5 hit 57.8%, rank 121+ 10.8%) and maps to a probability reasonably across deciles 3-10, but over-predicts the bottom decile (published 6.3% predicted vs 4.8% observed; 2025 5.7% vs 3.2%). Keep calling it a ranking score.

### 1.6 Recommended NFL changes

1. **Shadow-test the re-weighted TD formula** (validated on the untouched holdout; stable weights; same six inputs minus goal-line touches): `f_touches` 0.38, `f_snap_pct` 0.23, `implied_total` 0.17, `f_rz_opp` 0.13, `f_xtd` 0.08 (candidate vector frozen from the selection folds; goal-line touches dropped at fitted weight under 0.02). Version it as a new shadow model beside `nfl_td_v2` and `nfl_td_v3`; never rewrite `nfl_td_v2` rows.
2. Keep the other six markets' weights. Their re-weighted candidates are not distinguishable from the published ones on the holdout.
3. Reconsider the label "TOP" / "best pick" for the card's #1 within a market: the data shows five roughly equal players.
4. Watch the red-zone weight over time: its fitted weight fell each fold (0.17, 0.13, 0.09).
5. Do not change selection rules (top five, top third, per-team game calls) on this evidence: nothing tested shows them harmful.

## 2. NHL LAMP (site repo `lib/nhl/*`; reconstructed from code, NOT backtested here)

Everything in this section is from reading the code. Section 2.4 is inspection, not measurement.

### 2.1 Formulas

| market (public?) | version | features and weights | CALLED / ON THE BOARD |
|---|---|---|---|
| **Anytime goal** (public) | `lamp-goal-v3` (from 2026-10-03) | mean of three equal percentiles among tonight's scored skaters: shots per game, goals per game, time on ice per game (`goalModel.js` 49, 132-140). Pooled over a 82-game window across this and last season; TOI = this season if 5+ games else last season; 3-9 pooled games shrunk to the F/D position average with a 10-game prior (`ROOKIE_PRIOR_GAMES`, `MIN_ROOKIE_GAMES = 3`); under 3 games not scored; **no shrinkage at 10+ games** | CALLED = best score on each team in the game (`CALLED_K_PER_TEAM = 1`; higher of the two is TOP, the other GOAL); ON THE BOARD = night rank within the top third (`BOARD_SHARE = 1/3`); else NOT. Ties: score, shots per game, name. Also CALLED if any public market (SHOTS 3+) called him (`markAnyMarketCalls`, `nhl.js` 90-104) |
| **Shots 3+** (public) | `lamp-sog-v2` | equal percentiles of shots per game, TOI per game, opponent shots against per 60 (`sogModel.js` 25, 37-47; opponent value identical for teammates) | top 3 per game CALLED (`CALLED_K = 3`); everyone else scored is ON THE BOARD; no NOT-ON-BOARD tier |
| **2+ goals** (public, tracked, nothing called) | `goal2_site_v1` | weights 35 shots/game, 30 shooting % (goals/shots), 20 TOI, 15 goals/game (`goal2Model.js` 431); manual, no basis cited | top third ON THE BOARD, nothing CALLED |
| Points 1+, Assists 1+ (shown as TEST, no record until 30 graded nights) | pts/ast v1 | equal percentiles: points (or assists) per game, TOI, opponent goals against per game | top 3 per game CALLED |
| `lamp-goalpos-v1`, `lamp-goalw-v1` (shadow only, no page reads them) | | goalpos = goal board with TOI ranked within forwards/defence; goalw = ridge logistic: intercept -1.88585, goals/game +0.16332, shots/game +0.21976, TOI +0.26761, is-defenceman -0.48006 (standardised), fitted 2025-11-15 to 2026-04-16 (129 nights, 36,239 skater-games, 5,552 goals) | uses `scoreNight`'s rules on P |

Not in any NHL score: position, line, **power-play role**, goalie quality (the confirmed starter is context only), home/away, back-to-back, scratches/injuries (only the posted lineup narrows the pool), shot quality / expected goals. NBA and NHL both print no probability; the goal probability `1-exp(-goals per game)` is logged only.

Worked example: not produced. Stored NHL predictions live in Supabase tables (`lamp_goal_log`), which this environment cannot read.

### 2.2 Data available for validation
Stored per-night rows (`lamp_goal_log`, `lamp_prop_log`) exist only from the first lock nights (regular season began 2026-09-29), so there are about two weeks of locked history. Longer history comes from the repo's own offline replays (`scripts/nhl/goal-v2-backtest.mjs`, test window from 2026-02-01; `lib/nhl/angleBacktest.json`), which re-fetch NHL per-game reports. Their own numbers for the shadow `lamp-goalw-v1`, as stated in its header (not re-run here): called hit rate 0.3515 (live) vs 0.3605 (model) with log loss 0.4060 vs 0.3974 on fold A; 0.3319 vs 0.3463 and 0.4011 vs 0.3949 on fold B; fold B's test nights overlap nights the xG curve has seen, so it is not a clean test.

### 2.3 Defects found by reading (severity is a judgement, not a measurement)
1. **Shot volume is counted twice.** Goals per game is shots per game times shooting %, so shots and goals both load on volume; the third leg (TOI) correlates with both. (medium)
2. **Opportunity and conversion are not separated** in the live goal formula; conversion enters as raw goals per game, unshrunk at 10+ games. Only the 2+ goals formula has an explicit shooting-% term. (medium)
3. **TOI is ranked as raw time across defence and forwards**, which pulls defencemen up (the code cites defence scoring 7.1% of nights vs forwards 19.4%). The shadow models exist to test this; the live board is unchanged. (medium)
4. **Score depends on the slate:** percentiles are among tonight's skaters, so an 80 is not comparable across nights; the top-third cut is relative as well. (low-medium)
5. **Label rules differ by market** (goal: one per team plus a third; shots/points/assists: top 3 per game, no NOT tier), against the project rule that labels come from one place. (low; consistency)
6. **TOI mixes seasons and windows** (this season from 5 games, else last season), while shots and goals use the 82-game window. (low)
7. **No injury/scratch filter** before the lineup posts, so pre-lineup boards can include scratched players. (medium for early reads)
8. Stale comments contradict the code on whether points/assists are hidden or shown (`nhl.js`, `ptsModel.js` vs `Board.js`). (info)
9. `goal2_site_v1` weights 35/30/20/15 are manual with no measured basis. (low)

### 2.4 Challenge (by inspection; every item is a hypothesis for a backtest)
- Features likely redundant: goals per game given shots per game and shooting %. Likely missing and decisive: power-play time (the feed does not carry it), line assignment, shot quality (an expected-goals table exists in the repo but feeds no pick score). Goalie and opponent are deliberately excluded from the live goal score; whether that is right is untestable without stored history.
- Small samples: handled only below 10 games (prior of 10 games). A player with 12 games and 5 goals is scored on raw rates.
- Not answerable now: weights (all equal), calibration, selection cutoffs, correlation of the two team calls in one game.

## 3. NBA BUCKETS (hidden product; audited, not exposed)

`lib/nba/model.js` `NBA_MARKETS` (line 35); core scorer `lib/model/core.js` `scoreSlate` (35-74). Every market is the mean of EQUAL percentiles of its legs among the night's scored pool, rounded; no fitted weights; 82-game pooled window as NHL; fewer than 10 pooled games = not scored (no rookie prior).

| market (bar) | legs | tie-break |
|---|---|---|
| points 25+ | points/g, minutes/g, FGA/g, FTA/g, opponent points allowed | points |
| rebounds 10+ | rebounds/g, minutes/g, opponent rebounds allowed | rebounds |
| assists 8+ | assists/g, minutes/g, FGA/g, opponent assists allowed | assists |
| threes 4+ | 3PM/g, 3PA/g, 3P%, opponent threes allowed | 3PM |
| PRA 35+ | PRA/g, minutes/g, FGA/g, opponent points allowed | PRA |
| double-double / triple-double | rates from his own game log (82 games, min 10, recent 10) x an opponent factor shrunk by `1 + n/(n+20)*(ratio-1)`, clamped 0.8-1.25; players with none in the window are unscored | |
| first basket | FGA share, points/g, minutes/g; starters only | FGA share |

Status: ON THE BOARD = top third of the night; CALLED = the best player of each team in each game, the second only if inside the top third, and the better of the two is always CALLED even if outside the top third (`core.js` 69). A player who scores 25+ and was CALLED in any BUCKETS market is relabelled CALLED (the 0c rule, `lib/record/nba.js`). Data: ESPN averages only; the code states **no NBA per-game minutes history is stored**; a 2025-26 replay exists in `lib/nba/angleBacktest.json` (not re-run). Defects by reading: points, assists and PRA are dominated by collinear volume legs (points + minutes + FGA + FTA = four of five points legs); 3PM counts attempts, makes and percentage; opponent legs are unshrunk raw per-game figures except in the double/triple-double markets. Validation requirement: stored per-game minutes, lineups and injuries before any weight can be tested.

Other sports in the repo: only these four (`lib/routes.js` TABLE). FRANCHISE (fantasy) was not audited by instruction; TOP TOTALS (`lib/totals/core.js`) is a game-level market, not a player pick, and was not traced.

## 4. MLB MOONSHOT (bot repo `bots/mlb_dashboard.py`; reconstructed from code, NOT backtested here)

Provenance of this section: traced from the live call path by a read-only pass, then spot-checked by me against the code (the `hr_raw` weight table, the `hr_score` re-anchor formula, the order of the hard cap versus the gate bonuses, the HRR overwrite and the published overall formula all matched). Line numbers are `mlb_dashboard.py` unless stated; items marked (agent) were not individually re-read.

### 4.1 Pipeline order (this order matters for the defects)
1. `score_hitter` -> `apply_model_v2_layers` (the HR score) -> `apply_decision_engine_v31` (can cap HR at 30 and overall at 35 for "True Avoid").
2. **After** that, `_apply_lineup_context_to_team_rows` (about line 3457-3517) **overwrites** `hrr_score`, `hit_score`, `contact_score` and `overall_score`.
3. `build_top10_alt_board` stamps `board_score`, `board_rank`, `power3_score`; then odds, `build_game_pick_role_map` (11498), the first-pitch lock, best-bet reconciliation, shadow picks.

### 4.2 Home run score (`hr_score`, `mlb_hr_v4`)
**A. Blend** `hr_raw` (weights sum to 1.00, asserted at 411-416): season power 0.24 (ISO/HR per PA/SLG, ISO shrunk to the league with K = 150 PA), damage conversion 0.13, pitcher damage 0.06, pitch fit 0.06, pull/launch 0.06, weak-spot interaction 0.06, park/weather 0.05, recent form 0.05, pitch-match 0.05, pitcher-side production 0.04, strikeout rate 0.04 (positive), batted shape 0.03, plus six terms at 0.01-0.03 (PA per HR, times through the order, yesterday's hitters, bullpen fit, pitcher trend, BvP at 10+ PA, lineup opportunity).
**B. Adjustments:** weak-spot bonus up to +3.4; trap multipliers (x0.72 pitch trap, x0.78 popup, x0.70 low launch angle, x0.80 same-hand weak split, x0.82 no air contact, x0.86 elite pitcher, x0.84 K-trap); hard cap 45 for high exit velocity on the ground; capped at 59 unless "strong confirmed" (max EV >= 97, launch angle 20-35, pull-air >= .28 or FB >= .35, and a pitch signal).
**C. HR gate:** five signals (ISO >= .180, HRW >= 70, recent HR, batted shape >= 80, ideal contact or barrel) -> adjustment from -18 (none, ISO < .160) to +8 (all five); +5 if all nine "yesterday's hitters" checks match.
**D. Re-anchor = the published score** (9151-9155): `hr_score = 0.70 x (0.80 x hr_v2 + 0.20 x season_power) + 0.30 x recent_hr_form_score`. The code documents the 0.30 from a 5-day sample (top-15 rate 25.3% to 29.3%) and says to revisit it.
Then "True Avoid" caps at 30. Published beside it: `hr_score_shadow` (`mlb_hr_v1_recency`), `hr_overlay_v2` tiers (air-rate > .50 and recent EV > 87; plus ISO >= .230 for power), `hr_label_shadow`, `meatball_fit_score` (weight 0, evidence only).

### 4.3 Other MLB scores (published values)
- **hit_score** (`mlb_hit_v2`): 0.29 recent, 0.24 baseline, 0.17 lineup, 0.10 pitch mix, 0.05 + 0.05 + 0.08 + 0.02 others, then a strikeout subtraction (3.0 x normalised K%).
- **hrr_score** (`mlb_hrr_v2`), the **published** value: `100 x (0.48 own contact + 0.28 context layer + 0.18 production quality + 0.06 walk rate) x spot bonus (1.0 spots 1-5, 0.92 spot 6, 0.84 below)`, minus 8 x normalised K%.
- **contact_score** (`mlb_contact_v2`): 0.55 extra-base form, 0.24 pitcher, 0.13 spot/hits, 0.08 power boost, park multipliers, K subtraction 2.0.
- **overall_score** (`mlb_overall_v2`), published: `0.52 hr + 0.25 hrr + 0.15 hit + 0.08 contact`.
- **board_score** = 100 x mean of the slate percentile of `hr_score`, season HR and season average exit velocity (ties by `hr_score` then player id); **power3**, **longest_hr_score**, **hrw_score**, **multi_hit_score** (the code notes zero games graded), **steal_risk** (feeds no live pick) also computed.

### 4.4 From score to pick (this repo) and what the site adds
Per game (`build_game_pick_role_map`, 11498): **TOP** = best `(board_score, hr_score)`, preferring players without a known HR price >= +901 (no price counts as acceptable); **HR** = next best excluding TOP; **HIT** = best `hit_score` excluding TOP, HR; **HRR** = best `hrr_score` excluding those; **CONTACT** = best `contact_score` with no exclusions (may overlap, shown as "TOP/CONTACT"); **WATCH** = next three by `hr_score` (coverage marker, not locked). `pick_lock.py` locks TOP, HR, HIT, HRR, CONTACT at first pitch. Labels "Avoid for HR", "HR Look" etc. come from three writers in sequence (core label function, decision-engine role ladder, reconcile); CALLED / ON THE BOARD / NOT ON THE BOARD are derived on the site from the stored role and `board_rank`, not in this repo (not traced). Shadow-only rule families (`SHADOW_PICK_RULES`) log runs, RBI, steals and total-base picks without publishing.

### 4.5 Defects found by reading (confirmed items marked; severities are judgements)
1. **The safety caps do not bind** (confirmed order): the hard cap 45 and the 59 cap run before the gate (+8) and yesterday's bonus (+5), and the re-anchor then mixes in 0.30 of a recent-form score and 0.14 of season power, neither of which carries the trap multipliers. A hitter capped at 45 can publish above it (45 to about 69 if season power and recent form are both maxed). Trap and gate effects are diluted to about 56%. (high)
2. **HRR used in labels is not the displayed HRR** (confirmed: `lineup_context_score` defaults to 0 and is set at line 3454, after `hrr_v2` reads it at 8864): the decision engine, several best-bet rungs and `best_blend_score` read a deflated HRR, while the published `hrr_score` is replaced later. (medium)
3. **Two scales share the same words:** the pre-anchor HR score drives the labels ("HR Look" at 58, "Strong HR Look" at 62); the post-anchor score drives the decision-engine roles, the board and overall. (medium)
4. **`old_hr_score` is near-constant** (starts at 50, small shifts, per the code's own comment), so the conditions that test it (`>= 42`) are almost always true and the per-hitter "hidden value" condition is practically unreachable. (medium)
5. **Published `overall_score` drops its caps and terms** (the True Avoid cap of 35, the HRW and pitch-fit terms), overwritten by the 0.52/0.25/0.15/0.08 formula. (medium)
6. **A trap-flagged hitter can keep the "Avoid for HR" label while holding an HR role**, except TOP and HR holders (reconcile fixes only those). (low-medium)
7. **Ties** break by row order in `pick_top` and `_board_slot`, not by player id; percentile ranks of equal inputs depend on row order; doubleheader rows count twice in the percentile denominator. (low-medium; reproducibility)
8. **Missing data read as a measurement:** in the pitch-mix function a missing barrel, line-drive or launch-angle value defaults to 0.0 and triggers penalties (-8, -4, -10). `season_power` uses `max(season ISO, split ISO)`, which biases upward. (low-medium)
9. Stale comments (batted shape documented 0.13-0.27, actual 0.03; pitcher damage documented 0.12-0.15, actual 0.06; shadow header says season power 0.08, actual 0.24). (info)
10. Discord shows `top_board_score_v2` for TOP while TOP is chosen by `board_score`; the Top-30 list uses a third ranker (`top10_rank_score`). (low)

### 4.6 Redundancy and manual weights (hypotheses; no data to test)
Same ingredient entering several times: season HR rate (season power, batted shape, PA per HR, the board's `season_hr`), season power (blend 0.24 and re-anchor 0.14, effective about 0.27), recent HR form (blend 0.05, anchor 0.30, plus bonuses in batted shape and the gate; the code itself notes recent HR form measured negative on clean data at 14937-14942), pitcher-side weak-spot terms (five places), damage conversion (four uses), pitch fit (three weights in the blend and in the board, best-blend and alt-HR scores).
Weights with a documented measured basis: season power (a sweep, knee at 0.24), pitcher-side production (corrected significance q = 0.034), recent form, PA per HR, strikeout rate, the 0.30 anchor (short sample), the contact formula's extra-base design. Set by feel or "per audit" with no measurement cited: pitch fit, pull/launch, park/weather, lineup opportunity, bullpen fit, trend, BvP, the trap multipliers, the gate ladder, the hit formula and the overall blend.

### 4.7 What MLB data can say today (`model_lab/mlb_ledger_audit.py`)
3,247 locked, graded, published picks over 121 days: home-run rate by `hr_score` quartile 11.6%, 13.6%, 15.9%, 20.0% (each +/- 2.2 to 2.7 points); AUC of `hr_score` among already-selected players 0.583. Selected sample only. 356 graded rows have no model version and five config hashes appear. **No weight, ablation, calibration or alternative-formula result is possible for MLB until every rated player on every slate is stored with his box-score outcome.**

## 5. Your stated priorities and what the evidence says

You asked for (a) a hit rate that is consistent and **checkable after the event**, (b) a true ranking: best player, best pick, best situation, and (c) to keep today's weights unless there is a reason. You also ranked what you believe matters, in order:

| sport | your order, most to least | what the current formula does | what the data says |
|---|---|---|---|
| NFL touchdowns | red zone / goal line, matchup and recent streaks, team scoring, touches and snaps | red zone family 52.9% of weight, team scoring 18.3%, touches and snaps 28.8%, no matchup or streak term | tested: **touches and snaps are the strongest signal**; team scoring is second; the red zone family helps only once (the three terms overlap); matchup and a player's own recent touchdowns added nothing measurable (10 pre-registered tests). Your ranking and the data disagree on touches and on matchup |
| NHL goals | ice time and power play, shooting skill, goalie and opponent, shot volume | equal weight on shots, goals, ice time; no power-play, no goalie, no opponent | **cannot be tested here** (no NHL history reachable). The live formula has none of your top-3 beliefs except ice time and (through goals) shooting skill |
| MLB home runs | recent form, pitcher weakness, batter power, park and weather | batter power (season 0.24) is the largest weight; recent form is 0.05 plus a 0.30 anchor; pitcher damage 0.06, park/weather 0.05 | **cannot be tested here**. Your order inverts the current weights; the code's own comment says recent HR form measured negative on clean data |

I did not change anything. "Keep today's weights" is the default recommendation everywhere except NFL touchdowns, where a re-weighted version passed an untouched-season test and should be run beside the current one (shadow) so you can see it on live picks before deciding.

**Checkable:** the grading pipelines already store the locked pregame score, status and outcome for NFL and MLB (and NHL/NBA in Supabase), so a hit rate can be re-checked after the event. What is missing is a **full-board** record (every rated player with his outcome) for MLB and NHL; without it only the players the product chose can be checked, which flatters any ranking.

## 6. Questions that cannot be answered with current data

- NHL and NBA: every weight, ablation, calibration and alternative (no history reachable; locked history only since the season began).
- MLB: all of the above (full board with outcomes is not stored; box scores unreachable).
- Whether the call labels (CALLED / ON THE BOARD) add value beyond the rank, for any sport: needs more graded weeks.
- Correlation between simultaneous picks in the same game: far too few same-game pairs (NFL: 14 to 53 per market).
- Any market comparison: no timestamped historical prices.
- Power-play time, line assignment, goalie effect (NHL); scratches (NHL); per-game minutes (NBA).
- Whether the lab's within-week percentile blend equals production's league-reference score exactly (monotone in principle, not byte-checked).

## 7. Promotion status

| change | class | needs |
|---|---|---|
| NFL TD re-weighted formula (touches 0.38, snap 0.23, implied 0.17, red-zone 0.13, xTD 0.08) | validated on untouched 2025 (Holm p = 0.004); **ready for shadow** | your approval to log it as a shadow model; promotion only after 8+ graded weeks beat the current one |
| NFL other six markets | keep as is | none |
| NFL KICK_PTS, PASS_YDS | keep; too little data to re-weight | more seasons |
| MLB defects 1-5 | confirmed by reading; fixing them would change published scores and labels | your approval, a versioned model, shadow first (nothing historical rewritten) |
| NHL / NBA changes | not testable | data |
