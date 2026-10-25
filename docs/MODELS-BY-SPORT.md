# DASH models by sport (bot system, model side) — status as of 2026-10-24

Every number is measured fit-early / grade-late (details and caveats in `DASH-MODEL-HARNESS.md`). "Plain" = his own season average / rate, the bar a model has to clear.
Every lane below is also logged before the game and graded after (`bots/shadow_log.py`, `bots/nfl/nfl_shadow.py`, `bots/team_live.py`; workflows `shadow-nhl/nba/nfl.yml`, on `bbe-history`)
and read by main's scoreboard (`bots/model_harness.py`, promotion rule: +3.0 points over at least 150 games, same games).

## NHL (LAMP)
| Market | Model | Result (graded 2025-26, fit 2023-24) |
|---|---|---|
| Goal 1+ | `nhl_goal_v2` (fitted, Platt + tail) | best per game 37.2% vs 35.8% plain shots; calibrated |
| Points 1+ / Assists 1+ / Shots 3+ | `nhl_{pts,ast,sog}_v1` | 68.5 / 52.0 / 63.3% vs plain 68.1 / 51.0 / 62.1% — a tie on picks, good probabilities |
| Moneyline | `nhl_team_v1` (Elo + form) | win log loss 0.683 vs book 0.677; accuracy 53.9% |
| Spread | n/a | the book spread is a fixed puck line, so there is no spread model |
## NBA (BUCKETS)
| Market | Model | Result |
|---|---|---|
| Points 25+ | `nba_pts_v2` | best per game 54.2% vs 52.5% plain |
| Rebounds 10+ / Assists 8+ / 3PM 4+ / PRA 35+ | `nba_{reb,ast,3pm,pra}_v1` | 49.2 / 41.7 / 35.7 / 60.1% vs plain 48.5 / 40.9 / 36.5 / 59.9% |
| Double-double | `nba_dd_v1` | 44.7% vs plain 46.3% — plain is better; probability lane only |
| First basket (starters) | `nba_first_v1` | top pick 14.8% (usage 13.9%, points 13.6%); top 3 per game 40.0%; real starters from NBA daily lineups |
| Moneyline / spread | `nba_team_v1` | win accuracy 69.2%; spread error 11.34 vs closing 10.90; when it disagrees with the book it covers 48.4% |
## NFL (TUDDY)
| Market | Model | Result |
|---|---|---|
| Anytime TD | `nfl_td_v2` (hand weights, live, ranks) + `nfl_td_fit_v1` (fitted, probabilities) | v2 picks the top better; the fit has better AUC and is calibrated |
| First TD (RB/WR/TE) | `nfl_first_td_v1` | top 3 per game 38.6% vs v2 33.9%, touches 36.8% (171 games) |
| Rec yds 100+, receptions 4+, rush yds 100+, rush att 12+, pass yds 300+, kicking pts 6+, **pass TDs 2+ (new)** | `nfl_<market>_fit_v1` | fitted probabilities, calibrated; AUC matches the hand scores on volume markets, clearly better on pass TDs (0.64 vs 0.55) and kicking (0.59 vs 0.55) |
| Moneyline / spread | `nfl_team_v1` | win accuracy 61.6%; spread error 10.37 vs closing 9.73; when it disagrees with the book it covers 40% |
## MLB (MOONSHOT)
Live `hr_score` (`mlb_hr_v4`), `mlb_hr_v5` (batter x matchup) as the shadow, the full-slate hit / H+R+RBI / total-bases / HR model, injuries and lineups, numerology as a flagged lane. Measured ceilings: hits ~70%, H+R+RBI ~57%, 2+ total bases ~46%, HR best-of-night ~21-27%. No MLB games until spring, so MLB is not in the nightly shadow logs yet.

## Honest read
- A fitted lane beats the plain stat by about 1-3 points on picks at best; its real value is a calibrated probability to price against. The scoreboard decides what is ever promoted.
- The team models do not beat closing lines (they should not). They are the comparator: they tell you how far the market is from "just form and Elo", and they feed blowout risk.
- Everything is only as good as its inputs. The un-tried inputs are in `DASH-MODEL-HARNESS.md` under gaps; real prop lines (edge-vs-line) are the biggest one for NFL.
