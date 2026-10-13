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
