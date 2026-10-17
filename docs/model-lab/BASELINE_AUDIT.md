# BASELINE AUDIT (DASH model lab, 2026-10-13)

Scope: what the live system is, traced from the code that runs in production, not from notes. No production file was changed by this work.

## What could and could not be examined

| sport | product | examined | why |
|---|---|---|---|
| NFL | TUDDY anytime touchdown | **fully** (code, 5 seasons of reconstructed player-weeks, 2021-2025) | nflverse data reachable from the lab environment |
| MLB | MOONSHOT home run | **ledger only** (121 days of published graded slots, 2026-04-16 to 2026-10-12) | box scores and Statcast hosts are blocked here; a full-board backtest was not possible |
| NHL | LAMP anytime goal | **not examined** | this repository has no NHL model or data (the site builds NHL from the NHL API, which is blocked here). Nothing was tested and nothing is claimed |
| NBA (BUCKETS) | | not examined | same reason; also not a goal/touchdown target |

## NFL TUDDY: the live path

- **Outcome predicted**: anytime TD = rushing TD + receiving TD >= 1 (`nfl_scoring.OUTCOME["TD"]`). Passing TDs are excluded.
- **Score**: `bots/nfl/nfl_scoring.MODELS["TD"]` (model version `nfl_td_v2`): a weighted blend of within-pool percentiles. Weights: red-zone touches 22.2%, team implied total 18.3%, touches 15.7%, xTD 15.7%, goal-line touches 15.0%, snap share 13.1%. It is then ranked against the league reference and rescaled to the MLB score scale (`nfl_bot.score_all`). **It is a heuristic ranking score, not a probability.** No calibrated probability is published.
- **Inputs available at prediction time**: trailing per-game form over weeks before the game, last season's per-game baseline as carryover until a player has 3 games, schedule spread/total/roof/wind for the game, opponent trailing allowed-rates (`nfl_features.build`).
- **Storage and calls**: one prediction log per run (`nfl_prediction_log_<run_id>.jsonl`, 120 on the data branch), a card of the top 5 per market (`nfl_picks.json`), a pregame lock per game (`nfl_pick_lock.json`), and TD game calls locked at kickoff (`nfl_game_calls_log_*`). Each line carries a model version and config hash.
- **Grading**: `nfl_results.py`, bar hit if value >= bar; a player with no stat line is void, not a miss.
- **Shadow models already running**: `nfl_td_v3` (the three red-zone terms collapsed to xTD), plus snap-share variants for REC and RUSH_ATT. They are logged and never published.
- **Newly added this session**: a separate QB anytime-TD model (`nfl_td_qb_v1`), on branch `claude/great-goldberg-65rypv` (PR 3), not yet live.

## NFL baseline performance (reproducible)

Population: RB/WR/TE player-weeks with a recorded stat line, not Out, at least 3 games of history. 2021 is training-only. Selection folds 2022, 2023, 2024 (n = 14,329 player-weeks; anytime-TD base rate 18.9%, 19.1%, 20.6%). Final holdout 2025 (n = 4,964; base rate 19.5%).

The published score is not a probability, so to score it on log loss it is mapped with a logistic fit on earlier seasons only (the "B" model). Results:

| | log loss | Brier | AUC | top-15/week hit | top-30/week hit |
|---|---:|---:|---:|---:|---:|
| position base rate (A), selection | 0.4899 | 0.1559 | 0.558 | 28.4% | 26.8% |
| published score, mapped (B), selection | 0.4505 | 0.1433 | 0.708 | 49.0% | 43.6% |
| published score, mapped (B), holdout 2025 | 0.4405 | 0.1403 | 0.731 | 48.9% | 47.2% |

Within-week rank of the published score vs the anytime-TD rate (selection folds): rank 1-5 57.8% (n = 270), 6-15 44.6% (540), 16-30 38.3% (810), 31-60 32.2% (1,620), 61-120 22.3% (3,240), 121+ 10.8% (7,849). The ordering is monotone and large. The score does rank; the open question is how much better it can rank and how well its scale maps to a probability.

## MLB MOONSHOT: what the published ledger shows (`model_lab/mlb_ledger_audit.py`)

3,247 locked, graded, non-void published slot-rows over 121 days. These are the players the board chose, so every rate below is conditional on selection and cannot support a model comparison.

- HR rate among picked rows 15.2%. By `hr_score` quartile of the picked rows: 11.6% (n = 811), 13.6% (811), 15.9% (813), 20.0% (812), each +/- about 2.2 to 2.7 points (95%). Monotone, but AUC of `hr_score` among already-picked players is only 0.583.
- Provenance finding: 356 of the graded rows carry no `model_version` and 241 no config hash, and five different config hashes appear across the 121 days (versions `mlb_hr_v3` 87 rows, `mlb_hr_v4` 2,804). Rows from different model configurations are mixed in one ledger; any "overall" MLB number needs to be stratified by hash.
- Not measurable here: calibration of the whole board, capture rate against all players who homered, and any alternative model.

## Data limitations (affecting every conclusion)

1. Backtests grade only players who recorded a stat line; production scores the whole slate. A player who was inactive is invisible to the backtest.
2. nflverse `spread_line` / `total_line` are game lines; a production build days before kickoff sees an earlier line. The leakage direction is mild but real, and cannot be removed without timestamped historical odds, which this repository does not hold (its own odds provider returns 403 for player props).
3. Seasons 2019-2020 could not be rebuilt (schema mismatch in the production feature builder), so 2021 has no carryover baseline and is training-only.
4. Injury and role information is nflverse's weekly report, not the timestamped state at publication.
5. Participation (routes, formations) is published once a year and the 2026 file does not exist; route participation and end-zone targets were therefore not testable.
6. The trailing-window lengths are fixed inside the production feature builder; a recency-window sweep was not run.
