# DASH model harness — the guide rails

Read this before touching a score, a pick, or a result. It is the model side only; nothing here is about the site.

## The one number
Of the players who actually did the thing (homered, scored, got the hit, hit 25+), what share were on the board **before** the game — and how often were the called picks right. Everything below exists to move that, or to measure it honestly.

## The shape of every model
`FINAL = SCORER × MATCHUP`, capped 100, then `expected = FINAL × p_play`.
- **Scorer** — who is producing right now (recency first, season is only the ceiling).
- **Matchup** — is tonight a good spot (opponent, park, pace…).
- **p_play** — the chance he plays (`bots/injury_status.py`). A score only counts for a player who plays.
- **Angles** — only ones `bots/angle_engine.py` calls **SIGNAL** (survives false-discovery control, same direction in every season, big enough to act on). LEAN = watch, NOISE = ignore.
- Every output row carries `model_version`. A change that can move a number is a **new version** (`bots/model_registry.py`, `bots/nfl/nfl_registry.py`). Records are never rewritten.

## Gates — nothing goes live without all of these
1. Chronological backtest: fit on earlier seasons/nights, report on later ones. Never tune on the holdout. Never shuffle.
2. It must beat **both** the live model and the plain-stat baseline on the same rows (`backtest_*.py`, `tune_models.py`).
3. Report the called picks, not only the whole board (`backtest_picks.py`) — best-in-game vs not, and what it paid where a price exists.
4. Every angle through the engine; every miss through `miss_autopsy.py`.
5. Version bump + registry entry + a line in `dash_models.REGISTRY`.
6. Anything the site must read is added to `PUBLISH_FILES` in `.github/scripts/publish_data.sh` (the `data` branch is rebuilt every publish; unlisted files vanish). Anything that accumulates lives on `bbe-history`.

## Relationship to `bots/model_harness.py` (main)
Two halves of one process, not duplicates.
- `model_harness.py` is the **prospective scoreboard**: locked live records, production vs shadows vs baseline on the same games, nightly, private. It owns the **promotion rule** (Donovan, 2026-10-16): a new score replaces the one in use only when it beats it by 3.0 points over at least 150 games, same games, paired test agrees. Production is frozen four weeks from that date; new ideas run as shadows.
- The backtests and tuners here are the **retrospective gate** (gates 1-4): they decide what is worth running as a shadow. Passing them never promotes anything; only the scoreboard's rule does.
- So `mlb_hr_v5`, the NBA/NHL lane models and every lane in this doc are shadows. Gate 2 above is the entry bar for a shadow, and the 3.0-point / 150-game paired rule is the exit bar to production. Next step to wire them in: add the v5 / lane scores as extra score keys in the adapters in `model_harness.py` once they are logged on locked rows.

## Where things are
| Need | File |
|---|---|
| Every model, one list | `bots/dash_models.py --list` |
| MLB HR / NFL TD / NHL / NBA scorers | `moonshot_v4_scorer.py` · `tuddy_v2_scorer.py` · `nhl_scorer.py` · `buckets_scorer.py` |
| Backtests | `backtest_v5.py` · `backtest_picks.py` · `nfl/nfl_tuddy_backtest.py` · `nhl_backtest.py` · `buckets_backtest.py` |
| Tuning on a holdout | `tune_models.py` (NHL/NBA) · `tune_mlb_props.py` (hits/HRR/contact) |
| Angles | `angle_engine.py` + `angle_lab.py nba\|nhl` |
| Numerology (a lane, never a score) | `numerology_lab.py` (arithmetic + backtests) · `numerology_tail.py` (extreme games, stacked on stats) · `numerology_log.py` (prospective MLB log) |
| Why picks miss | `miss_autopsy.py` (cause) · `autopsy.py` (weekly input pattern) |
| EV scan (prices vs the market) | `devig.py` (maths) · `ev_process.py` (the EV-dingers checklist) · `ev_log.py` (log + grade) · `.github/workflows/ev-scan.yml` (runs on GitHub; needs the `SGO_API_KEY` repo secret) |
| Availability | `injury_status.py` (+ `.github/workflows/injury-snapshot.yml`) · `lineups.py` (posted lineups, probable goalie, spread/total, lock-later rule) |

## Where we stand (measured, not hoped) — updated 2026-10-24
PLAYER SCORING (what picks who). "Best/game" = the model's #1 in each game; baseline = the plain stat.
| Sport / market | Shipped | Tuned / new | Plain-stat baseline | Live? |
|---|---|---|---|---|
| MLB HR | `hr_score` (mlb_hr_v4) 17.1% #1/game | v5 batter×matchup **21.6%** (292 games, thin) | — | v4 drives the board; v5 written every slate |
| MLB 1+ hit | 68.8% of called picks | top-40% score 72.3%; inputs plateau ~72% | — | board only |
| MLB 2+ H+R+RBI / 2+ TB | 50.9% / 39.3% | top-20%: 55.2% / ≈ base | — | board only |
| NFL TD | `nfl_td_v2` **51.2%** best/game | TUDDY v4 44.5% (worse; not wired) | — | v2 live |
| NHL goal | LAMP v1 33.3% top-1/game | **nhl_goal_v2 37.2%**, calibrated (top tenth 34.4% pred vs 34.8% actual) | season shots/gm 35.8% | board built (`nhl_live.py`), every skater |
| NBA 25+ pts | BUCKETS v1 48.3% | **nba_pts_v2 54.2%** | season ppg 52.5% | weights saved, no live board yet |
GAME MODELS (spread / moneyline / blowout) — team-level, not player-level.
| Model | Where | Measured | Status |
|---|---|---|---|
| NBA game line (margin) | site `lib/nba/gameLines.js` (nba-lines-v1) + bot `forecast_nba.py` | site: MAE 11.51 vs close 11.01; ours 11.13 vs book **10.58**, winner 65.6% vs book 69.9% | **the book beats both** — use the closing spread as the input, don't compete with it |
| Book spread → blowout/star effect | `game_lines.py` (2,463 NBA games, real closes) | club favoured 13+: wins by 15+ 50%; underdog 13+: loses by 15+ 51%; star 25+ rate 43% (fav 10+) vs 23% (dog 10+) | measured; not yet a live flag in BUCKETS |
| MLB moneyline | `moneyline_bot.py` (log5 vs market) | its own simulation: loses 4–8% against the vig at any edge floor | live, graded, honest |
| NFL / NHL / MLB spread or moneyline model | site "Our line": club ratings + home only | not backtested against closes | **gap** (NFL/NHL closing-line study not run) |
| In-game comebacks (NBA) | `comeback_nba.py` | down 20 after Q3: 0.7% to win even from a close pregame spread | measured; no edge at +12,000 |
LANES (never in a score): numerology — MLB only, small lean (+0.7 pp HR for 2+ aligned, t 3.1, six seasons); prospective log running.
AVAILABILITY: `p_play` (NFL measured; others prior until snapshots accrue) · MLB lineup posted/unposted measured (98% / 76%).

## Lanes by sport — what exists, where, and whether the harness has graded it
| Sport | Markets / lanes that exist | Where they live | Backtested + calibrated in the harness |
|---|---|---|---|
| MLB | HR, hit, H+R+RBI, contact, TOP, overall, top-board, HRW, multi-hit, longest, pairs, DUE, numerology | bot `mlb_dashboard.py` (+ v5 `moonshot_v4_scorer.py`) | HR (v4 vs v5), hit/HRR/contact (tiers only), TOP; numerology lane |
| NFL | TD, rec yds, receptions, rush yds, rush att, pass yds, kick pts, def TD | bot `nfl_scoring.py` (percentile composites) | **TD only** (v2/v3/TUDDY v4) |
| NHL | goal, points, assists, shots on goal, goalie quality / give-ups, team xG & projection | site JS `lib/nhl/*` (goal = `lamp-goalw-v1`, ridge on goals/shots/TOI/D) + bot `nhl_goal_v2` | **goal only** |
| NBA | PTS 25+, REB 10+, AST 8+, 3PM 4+, PRA 35+, double-double, triple-double, first basket | site JS `lib/nba/model.js` (`buckets-*-v1`) + bot `nba_pts_v2`, `nba_reb_v1`, `nba_ast_v1`, `nba_3pm_v1`, `nba_pra_v1` | **points, rebounds, assists, 3PM, PRA** (`tune_nba_lanes.py`: REB 49.2% / AST 41.7% / PRA 60.1% best-per-game vs season-average 48.5 / 40.9 / 59.9; 3PM no better than the average; all calibrated). Not yet: double-double, triple-double, first basket |
So four of the five sports have more lanes than the harness has graded. The site's NHL and NBA models are real (not odds-ranking) but live only as JS, backtested by their own scripts, not by this harness. Next: port each remaining lane into the harness with the same fit-on-early / grade-on-late test, one at a time.

## Prices and de-vigging (read before trusting any "+EV")
- A price = true chance + the book's cut. De-vig = take the cut out (two-way: divide each side by the sum; one-sided: use sharp books or the measured overround). `devig_check.py` measures it on our own settled MLB HR bets: the cut is ~8%, and the market is otherwise about right by price band.
- MLB, 1,211 priced board hitters over 45 nights: our top third by `hr_score` homered 19.0% against 16.1% fair → +8.5% ROI, standard error 11.8%. Not proven; ~3,500 bets would settle it.
- **`scripts/nhl/lamp-atg-devig.py` (site repo) reads SGO `firstToScore` = the FIRST goal of the game**, not anytime goal (+800..+1,200 vs ~+150), then joins it to Kalshi's anytime-goal tickers by surname. SGO's anytime-goal market is `points-<P>-game-yn-yes` (SGO calls goals "points"; `goals+assists` is points). The process is in `ev_process.py` (his steps: Kalshi probit devig with depth, deeper order-book levels, sharp book, exchanges, thin quotes ignored); `devig.py` has the maths (probit reproduces his +7% figures); `devig_board.py nhl` compares Kalshi with our calibrated goal chance.
- Model probabilities are calibrated on the holdout and the live boards map raw → calibrated (`models/*.json: calibration`). NHL above 45% raw was overconfident (48.6% predicted, 38.6% actual); NBA above 50% is mildly overconfident. Edges against a market are only worth reading where the book is deep (TRUSTED: $500+ both sides).

## Do defensive matchups move the models? (team-level, ablation on the holdout — `matchup_ablation.py`)
| Lane | With the defence term | Without | Verdict |
|---|---|---|---|
| NHL goal (opp shots against + goalie save%) | log loss 0.3966, best/game 37.2% | 0.3966, 36.8% | adds nothing |
| NBA points (opp points allowed) | 0.2123, 54.2% | 0.2127, 53.7% | tiny |
| NBA rebounds / assists / 3PM / PRA (opp allowed) | -0.0008 / -0.0005 / -0.0003 / -0.0008 log loss | | tiny, same direction in all four |
| MLB HR v5 (pitcher hand split, FB%, hard-hit, LD%, park), 292 games | batter × matchup 21.6% | batter alone 21.2% | +0.4 pt; the matchup changes the pick in 58 games, 10 vs 9 homers |
| NFL anytime TD (opp TD softness .10) | top-5 57.4% / top-15 50.9% | 59.6% / 51.7% | **hurts** (why v2 zeroed it) |
**Position-level defence (`position_ablation.py`, 2026-10-24) adds nothing either.** NBA (what G / F / C score, rebound, assist against each defence over its last 10, vs the league's average for that group; 802 players, ESPN positions): log loss unchanged in points / rebounds / assists / PRA (0.2039 -> 0.2039, 0.2039 -> 0.2039, 0.1524 -> 0.1525, 0.2167 -> 0.2167), weights about zero; position *replacing* the team number is slightly worse. NFL anytime TD (role-aware defence from `nfl_role_dvp`): top-5 59.6% -> 57.8%, top-15 51.7% -> 50.2% — it hurts, like the team-level term. Not tested: the actual defender / pitch-type / coverage matchup, NHL goalie zones. Team- and position-level defence mostly repeat what a player's own rate already says.

## The deeper matchups and the first-event lanes (tested 2026-10-24)
- **MLB pitch-type matchup** (`pitchtype_data.py` + `pitchtype_study.py`: 455,730 batted balls, 72,786 hitter-games, fit 2025 / graded 2026): a hitter's home runs per batted ball by pitch type, weighted by the opposing starter's mix. Adds nothing to batter x pitcher: log loss 0.34609 -> 0.34600, best pick per game 18.5% -> 18.8%, weight +0.03; the HR rate is ~2 points above prediction in the worst, middle and best fit thirds alike. Caveats: the starter is the announced probable; batted balls (not every pitch) stand in for the mix.
- **NBA first basket** (`nba_first.py` + `nba_first_study.py`, ESPN play-by-play, 3,701 games, graded on 1,099 of 2025-26): the ten starters are eligible (random 10%). Best player (ppg) scores it 13.6% of the time, share of team shots 13.9%, first-4:00 usage 13.1%, his own first-basket history 14.3%, all player terms 14.3%, plus the opponent's first-basket-allowed rate and home 14.7% (top 3 starters 38-40% vs 30% random). Early usage carries the biggest weight in the combined fit (+0.30) but none of the differences is bigger than the noise (about +/-1 point on 1,099 games). The ceiling is ~14-15%: a first event is the most random lane there is.

- **NHL first goal** (`nhl_first.py`, NHL play-by-play for 3,936 games, graded on 1,183 games of 2025-26, ~32 regular skaters a game so random is 3.1%): best player (goals per game) scores it 6.2% of the time, shots per game 7.1%, ice time 2.1% (defencemen), first-6:00 attempt share 4.7%, his own first-goal history 4.9%, all player terms 7.2%, plus the opponent's first-goal-conceded rate and home ice 7.5% (top 5 about 31%). Early usage is WORSE than plain shots per game; the matchup adds nothing distinguishable (+/-0.75 points of noise).
- **NFL first TD** (`nfl/nfl_first.py`, nflverse play-by-play, graded on 171 games of 2025 with ~17 candidates a game so random is 5.8%): `nfl_td_v2` 15.2% (its weights were tuned on 2024-25, so this is its best case), touches per game 15.8%, early-touch share (first 20 plays) 14.6%, first-TD history 8.8%, v2 + early usage 16.4% (top 5 52.0%), + matchup 15.2%. Nothing is separable at n=171.
- **Calendar flavour:** `numerology_lab.pythagorean_date()` flags dates where month^2 + day^2 = yy^2 (2026-10-24: 10^2 + 24^2 = 26^2). Only two exist in 2021-26 (2025-07-24 and 2026-10-24); the next is 2105-03-04. Too few to test; it rides on MLB rows as a date fact, never a score.

## MLB full-slate hit / H+R+RBI / total-bases / HR model (`mlb_hits_data.py` + `mlb_hits_study.py`, 2026-10-24)
Every posted-lineup hitter, 2024-26 regular season (110,882 hitter-games), trailing features only, fit on 2024-25, graded once on 2026 (41,044 hitter-games). Hit rate of the top slice of ALL hitter-games by predicted chance, plain stat vs the full model (his rate + lineup spot/home/PA + his profile + opposing starter + park):
| Outcome (2026 base) | plain stat: top 5% / 10% | full model: top 5% / 10% / 20% / 40% | best pick per game / top 10 per night (full) |
|---|---|---|---|
| 1+ hit (60.9%) | 66.8% / 67.7% | **70.1% / 69.9% / 68.2% / 66.6%** | 69.7% / 70.0% |
| 2+ H+R+RBI (45.8%) | 51.9% / 51.8% | **57.5% / 55.2% / 53.4% / 52.0%** | 53.5% / 55.4% |
| 2+ total bases (35.2%) | 40.9% / 41.6% | **46.1% / 44.2% / 42.2% / 40.9%** | 41.4% / 44.8% |
| 1+ HR (11.6%) | 20.5% / 18.0% | 20.8% / 19.0% / 17.4% / 15.7% | 18.8% / 20.9% |
What moved each lane: lineup spot and plate appearances (+1.5 pts on hits, more on H+R+RBI), the opposing starter (the biggest single step for H+R+RBI and total bases), the park (a further +1-2). The ceiling with these inputs: hits about 70%, H+R+RBI about 57%, total bases about 46%, HR about 21% — below the 75-80% / 60% targets; the plain rate already holds most of a lane.

## Case study: Lucas Raymond, Thu Oct 22, 2026 (DET @ VAN) — `raymond_case.py`
Market-research notes (KNOWN-ISSUES #80): PropFinder had him #1; LAMP's goal score had no opponent-defence-by-position or power-play leg; he scored twice (2 goals, 5 shots).
- **Our harness model had him #9 of 435 skaters, TOP tier, 37.8% (raw 38.3%)** — the leaders were Celebrini / Boldy / DeBrincat at 41% (the calibration plateaus the top block; the gap from #1 to #9 is about 3 points). The site's LAMP goal score is a different, thinner model.
- **Why PropFinder liked him:** VAN's defence is the softest in the league to LEFT WINGS (+0.60 goals a game above the league average for Ls over its last 20 games, #1 of 32) and his own power-play time is 4.30 min a game (#19 of 867). VAN gives opponents few power plays (-3.11 min a game vs the league, #26) — the one leg that cut against him.
- **Does that leg help in general? No.** Added to the tuned NHL model (fit 2023-24, graded on 42,815 skater-games of 2025-26): log loss 0.39658 with or without it, best pick per game 37.2% either way, weight +0.009. Opponent power-play minutes given: the same. A leg that was right on Raymond's night is, over a season, noise.
- **More legs, tested properly (`nhl_buildout.py`, closing totals and moneylines for 4,174 of 4,179 games, three seasons):** closing total + the club's win chance: log loss 0.39649 vs 0.39658, best pick per game 37.0% vs 37.2%, top 10 per night 37.9% vs 38.5%. Opposing defence vs his position by SHOTS allowed (10 / 20 / 40 games): log loss 0.39656 / 0.39658 / 0.39652, best/game 37.1-37.3%. Everything within +/-0.0001 of the model as it is. The NHL goal model is not missing a predictive leg that these data can show.
- **Calibration:** the old isotonic fit flattened the top at 40.6% (ties). It is now a smooth Platt recalibration (a +0.071, b 1.000 on the 2025 holdout) with a tail compression above the 99th percentile of what the holdout saw (42.9% for NHL) instead of a plateau: strictly increasing, no ties.
- **Where we are actually thin:** we rank on volume (shots, ice time, power-play time), not on goals — he had 0.11 goals a game — which is right on average and says nothing about a single night; and nothing in the model knew VAN was soft to his position. One night cannot tell us that is a flaw.

## The gap list, in the order to fill it
1. ~~Confirmed lineups~~ MLB lineups and NHL probable goalies are live (`lineups.py`; an unposted MLB lineup = 76% to play, measured). Still missing: NBA starting fives and NFL inactives (no reachable pregame feed), minutes limits, and `pick_lock.py` calling `lock_decision()`.
2. A real game forecast — pregame spread, total, moneyline and their movement — for every sport (blowout *loss* is the star-killer: NBA stars reach 25+ only 11.9% of the time).
3. Player-level matchup, not team-level (NBA defender, MLB pitcher-vs-pitch-type, NFL coverage).
4. Shot / touch quality (NHL xG & shot location, NBA zones, NFL routes & air yards, MLB umpire / wind / bullpen).
5. Prices over time in every sport (MLB has ~48 nights) so return can be graded, not just hits.
6. The injury snapshots (running twice a day once pushed) → measured chance-of-playing for NBA/NHL/MLB.

## Working rules for sessions
- One harness. New models register in `dash_models.REGISTRY`; no stand-alone rankers, no ranking by odds.
- Say what is measured and what is a prior. Say n. Say when a result is inside noise.
- Plain-stat baselines are the bar, not the previous version of our own score.

## Lanes added (branch dash-lanes-2)
Fit 2023-25, graded once on 2025-26 (`bots/tune_nba_lanes.py`, `bots/tune_nhl_lanes.py`); each saves `models/<sport>_<lane>_v1.json` with Platt + tail calibration; both live boards print every lane.
- NBA rebounds 10+, assists 8+, 3PM 4+, PRA 35+: on `nba_live.py`.
- NHL points 1+ (base 35.2%), assists 1+ (24.4%), shots 3+ (22.1%): on `nhl_live.py`. Best pick per game 68.5% / 52.0% / 63.3% vs 68.1% / 51.0% / 62.1% for his season average. Tuned is a hair above the plain average, not a big edge; calibration by tenth is within ~2 points. Top-10 per night, tuned vs average: 67.2 vs 67.7, 50.2 vs 49.9, 59.8 vs 59.9, i.e. a tie. The value here is calibrated probabilities for pricing, not better picks.

## Do interactions help? (`bots/interaction_test.py`, fit 2023-24, graded 2025-26)
Every pairwise product and square of the lane features vs the plain logistic. NHL points/assists/shots: no gain (log loss 0.5952 vs 0.5959, 0.5216 vs 0.5220, 0.4619 vs 0.4614; best pick per game goes down 0.8-1.0 pt). NBA rebounds/assists/3PM/PRA: log loss improves 1-2% in all four (0.2039 to 0.2004, 0.1524 to 0.1497, 0.2734 to 0.2721, 0.2167 to 0.2151) and best pick per game +0.8 to +0.9 pt in three of four, top 10 per night flat. Read: NHL is limited by the inputs, not the model shape; NBA has a small real gain. Not adopted yet (small; would change the live feature path).

## NFL anytime TD: fitted logistic vs `nfl_td_v2` (`bots/nfl/nfl_tuned_td.py`)
Logistic over trailing-only features (red-zone/goal-line opportunities, xTD, targets/carries, snap %, TDs, target share, game line, opponent TDs allowed, team red-zone TD rate, position), three rotations (fit two seasons, grade the third):
| graded | AUC v2 / tuned | top-15 per week v2 / tuned | best pick per game v2 / tuned |
|---|---|---|---|
| 2025 | 0.7314 / 0.7356 | 48.9 / 52.2 | 49.6 / 46.3 |
| 2023 | 0.7162 / 0.7308 | 51.5 / 50.4 | 47.8 / 44.9 |
| 2024 | 0.7194 / 0.7314 | 53.7 / 52.2 | 50.7 / 49.3 |
Read: the fit ranks the whole board better (AUC up in all three) and is calibrated (log loss 0.44 vs 0.50 base-rate; tenths within ~3 pts), but v2 still picks the best player per game more often in all three (by 1.4 to 3.3 pts). Keep v2 for picks; use the fit's probability as the price check. No change to the live model.
Teammates-out for NFL (`--out-feats`: regulars with 3+ targets or 5+ carries a game who played the team's last game but not this one, summed trailing targets/carries/red-zone opportunity): AUC +0.0005 to +0.0008, log loss within 0.0002, best pick per game +0.8/-0.4/+0.7 across the three rotations. Nothing. The NHL/NBA version of this input helped; NFL's shares are already carried by snap % and trailing usage.

## NBA double-double lane (`nba_dd_v1`)
Two of points/rebounds/assists at 10+. Base 8.5% (2025-26). Fit 2023-25, graded once: best pick per game tuned 44.7% vs 46.3% for his own season double-double rate; top 10 per night 43.1% vs 43.3%. The fit does not beat the plain rate on picks, so this lane is a calibrated probability (tenths within ~3 pts, top tenth 44 predicted vs 40 actual), not a better pick. On the board and in the shadow log.

`nfl_td_fit_v1` (made by `nfl_tuned_td.py --save`: all-season fit, Platt + tail from the 2025 holdout run) is read by `bots/nfl/td_fit.py` and attached as `p_td_fit` in each player's shadow entry in the NFL prediction log (`nfl_bot.py`, best-effort). Price check only; v2 still ranks.

## First-score lanes: probabilities, not new picks
**NBA first basket** (`nba_first_lane.py`, `nba_first_live.py`, `models/nba_first_v1.json`). Pre-game starters only, per-game probabilities rescaled to sum to the starter share, book spread/total in. Fit 2023-24, graded once on 2025-26 (10,829 starter-games): top pick 14.8% (points per game 13.6%, usage 13.9%, random 10%), top 3 per game 40.0% (38.8 / 37.8 / 30). Log loss 0.3207 vs 0.3235 flat; calibration by eighth within ~1 pt. Top weight is early shots per game, then team first-basket rate. The edge over usage is about one point. Live board: `outputs/nba_first_board_latest.json`; starters are PROJECTED (most starts in the last 5 games) until tip, and history runs across the season boundary (rows flagged `carried_from_last_season` until a player has 8 games this season).
**NFL first TD** (`nfl/nfl_first_lane.py`, `models/nfl_first_td_v1.json`). RB/WR/TE with 3+ games, v2 score + touches + early usage + first-TD history + opponent first-TD record + implied total/spread, per-game rescale (81% of first TDs go to a listed RB/WR/TE). 171 games of 2025: top 1 15.2% (v2 15.2, touches 15.8), top 3 38.6% (33.9 / 36.8), top 5 52.0% (49.7 / 48.5). Log loss 0.1809 vs 0.1953 flat. Caveats: small sample (171 games), and the top 2% over-predicts (22.9% predicted, 15.5% actual, ~72 players, within about 1.7 standard errors); treat the top end as an upper bound. Not on a live board yet: it needs the current season's play-by-play early-usage state wired into `nfl_bot.py`.

## Graded and tracked, per sport, on their own schedules
Every lane is logged BEFORE the game and graded after it, and each sport runs as its own workflow (own cron, own concurrency group, all writing to `bbe-history`):
- `shadow-nhl.yml`: goal / points / assists / shots 3+, saved 22:45 UTC, graded 11:30 UTC.
- `shadow-nba.yml`: points / rebounds / assists / 3PM / PRA / double-double (saved in the 22:00 hour, graded next morning) and **first basket**, refreshed every 30 minutes 22:00-02:30 UTC. Starters come from NBA's daily lineups (Expected, then Confirmed near tip), falling back to the last-5-games projection; a game freezes the moment it starts.
- `shadow-nfl.yml`: anytime-TD (fitted) and first-TD, refreshed Thursday/Sunday/Monday until each team's kickoff (rows freeze at kickoff), graded after the week's last game.
`bots/shadow_log.py report` and `bots/nfl/nfl_shadow.py report` print predicted-vs-actual by quartile and the pick hit rates beside the backtest numbers.

## 2026 data
- All saved lane models and `nhl_goal_v2` / `nba_pts_v2` now include the 2026 season to date in the production fit; the fit made on 2023-25 is graded on 2026 first (out of sample): NHL points predicted 34.3% vs actual 34.9%, assists 23.7 vs 24.4, shots 21.3 vs 21.6 (5,517 player-games).
- NFL 2026 weeks 1-7 are a true holdout for the fitted anytime-TD model (fit 2023-25): AUC 0.7191 vs v2 0.7185, best pick per game 56.4% vs 48.9% (94 games), top-5 per week 62.9% vs 82.9% (35 picks), calibration by tenth within ~3 pts. Mixed, same pattern as before: better board, similar top.
- **Bug found by using 2026:** the NBA "teammates out" and "games missed" features carried last season's roster across the offseason (44 ppg "out" vs 17 normally in the first month), pushing early-season probabilities too high. History now resets after a 60+ day gap (`buckets_backtest.run_games`, `nhl_backtest.teammates_out`); in-season rows are unchanged, so earlier backtest numbers stand. After the fix, 2026 NBA rebounds read 9.1% predicted vs 8.1% actual (479 player-games).

## Team models (moneyline / spread), `bots/team_models.py`, `bots/team_live.py`
Elo (margin of victory, home edge, offseason pull toward the mean; K and home edge tuned on 2024 from 2023 only) + a ridge regression on form, rest and back-to-backs; win probability from Normal(expected margin, residual sd). Graded on 2025-26 (NFL 2025 and 2026) against the CLOSING line on the same games:
| | games | win accuracy | win log loss model / book | spread error model / book | when model differs from book |
|---|---|---|---|---|---|
| NBA | 1,231 | 69.2% | 0.596 / 0.570 | 11.34 / 10.90 | its side covered 48.4% (>=2.3 pts, 616 games) |
| NFL 2025 | 271 | 61.6% | 0.645 / 0.609 | 10.37 / 9.73 | its side covered 40.4% (>=2.2 pts, 136 games): the book was right |
| NHL | 1,193 | 53.9% | 0.683 / 0.677 | not comparable (fixed +/-1.5 puck line) | n/a |
The market is the better model in all three, as expected; the model is close in NHL and NBA. A 50/50 blend of model and book beats the book on spread error in NBA 2026 (12.65 vs 12.75, 28 games) and NFL 2026 (9.52 vs 9.30 is worse; no consistent gain). `team_live.py` writes tonight's model spread / win probability beside the book's and logs it pregame, graded next morning.
NFL fitted markets: `bots/nfl/nfl_lanes.py` (receiving yards, receptions, rushing yards/attempts, passing yards, kicking points, passing TDs) and `lane_fit.py` (live scoring, attached to the weekly shadow log).

## Real prop lines, new NFL markets, and a joint game simulation (2026-10-24)
**Where the lines are.** The NFL and NBA bots already snapshot the book's player-prop lines every day (`nfl_odds_<date>.json`, `nba_odds_<date>.json`, `{player: {market: [line, over price, implied]}}`) on the `data` branch, from 2026-10-16. That branch is rebuilt every publish, so `odds-log.yml` now copies them daily to `bbe-history` (`public/data/current/odds_log/`). There is no historical prop-line archive before 10-16, so edge-vs-line can only be measured on what has accumulated, and gets more reliable every week.
**Edge vs line (`bots/nfl/nfl_line_edge.py`, `bots/nba_line_edge.py`).** A logistic per threshold, P(stat > line) read off at the book's own line, fit on earlier games, graded on 2026 lines. First read, week 6 plus Thursday of week 7 (NFL, 28-153 lines per market) and the first NBA nights (103-121 per market): our log loss is worse than the book's near-50/50 implied probability on most markets (NFL receptions 0.726 vs 0.699; NBA points 0.732 vs 0.714), better on NBA threes (0.683 vs 0.702) and NFL kicking points (0.665 vs 0.678). Over bets at our +4 point edge: NFL receptions +18% ROI on 25 bets, NFL receiving yards -9% on 29, NBA points +4% on 39: noise-sized samples, no edge shown. Only the over price is in the snapshot, so unders are not evaluated.
**Bars.** `nfl_scoring.py` is right (receiving yards 100, rushing yards 100, passing yards 300, since v2 2026-10-17, and `nfl_bot.py` ships those bars to the site); `SCORING.md` still said 40 / 50 / 225 and is now corrected.
**New NFL markets (`bots/nfl/nfl_more.py`, fit 2023-24 graded 2025; fit 2023-25 graded 2026 W1-7).**
| market | base | AUC 2025 / 2026 | top-15 per week, fitted vs plain form (2025 / 2026) |
|---|---|---|---|
| 2+ TDs (RB/WR/TE) | 3.3% | 0.81 / 0.79 | 13.7 vs 10.4 / 22.9 vs 15.2 |
| QB rushing TD | 14.3% | 0.68 / 0.64 | 21.5 vs 17.4 / 19.6 vs 15.2 |
| 23+ completions | 30.5% | 0.65 / 0.64 | 39.6 vs 38.9 / 46.7 vs 48.9 |
| 1+ interception | 43.0% | 0.60 / 0.61 | 48.9 vs 47.8 / 56.5 vs 52.2 |
| longest reception 25+ | 15.4% | 0.77 / 0.78 | 48.5 vs 43.3 / 44.8 vs 46.7 |
| defence / ST touchdown | 11.2% | 0.60 / 0.52 | essentially unpredictable (188 team-games in 2026) |
Weather and roof (wind, indoors) added to receiving, rushing, passing and kicking fits: AUC change within +/-0.003 everywhere, nothing. Injuries: the table already drops players listed out, `inj_q` is a feature, teammates-out added nothing.
**Win rates (why NHL is "low").** The closing line itself picks the winner 69.4% of NBA games, 65.7% of NFL, and only 56.3% of NHL (65.5% in its most confident third): NHL games average 6.2 goals, 38% are decided by one goal, and ties go to overtime / shootout. Our model: NBA 69.2% (the book's level), NFL 61.6%, NHL 53.9%. 70% is not reachable in hockey for anyone; the gap to close is 2.4 points.
**Joint simulation (`bots/game_sim.py`).** The player lanes and the team models were separate; a player's goal chance never knew his team's expected goals. `game_sim.py` simulates each game 20,000 times from the book total and our team model's win probability, hands the team's scoring to its players in proportion to their fitted rates (NHL: Poisson goals, scorers and assists drawn per goal; NBA: team points with each player's spread set to match his own fitted probability), samples availability from the injury report, and reports P(player scores), P(team wins) and the joint P(scores AND wins), all from the same simulated games. Coherence with the boards: NHL goal probabilities within 0.8 points on average, NBA 25+ within 0.6. It also exposed a data problem: the NBA board's rosters were stale (last season's list carried traded players and missed others); once a team has played this season, `nba_live.py` now builds the roster from who has played for it this season. The pregame sims are logged and graded by `game_sim.py report` in the NHL and NBA shadow workflows. MLB would use the same sampler; no games until spring.

## On the site's data branch (boards.yml, 2026-10-24)
`boards.yml` (own schedule: 16:05, 21:05, 22:35 UTC daily; NFL Thursday, Sunday, Monday) writes these files to the `data` branch through `publish_data.sh` (all added to `PUBLISH_FILES`): `nhl_board_latest.json` (every skater: goal / points / assists / shots 3+), `nba_board_latest.json` (every rotation player: points 25+, rebounds, assists, 3PM, PRA, double-double), `nba_first_board_latest.json` (first basket, real starters), `team_board_{nba,nhl,nfl}_latest.json` (model moneyline / spread beside the book), `nhl_sim_latest.json` and `nba_sim_latest.json` (joint game simulations), `nfl_lanes_latest.json` (every NFL player: anytime TD, first TD, and 12 lane probabilities). NFL lanes now include 2+ TDs, QB rushing TD, completions, interceptions and longest reception, all in the weekly shadow log and graded.
**Where it stands vs the books (straight):** no edge shown against the real prop lines yet (first week of lines), and the team models do not beat the closing lines. The boards are calibrated probabilities and a logging / grading system, not a proven edge; the shadow logs and the scoreboard are what will show whether any lane earns one.

## Over / under prices, opening vs closing, price at the moment they hit, comebacks (2026-10-24)
- **Over and under prices.** The NFL / NBA / NHL odds snapshots now keep the under price (`[line, over, implied, under]`) and each day's file keeps the OPENING price the file first saw (`open: {player: {market: [line, over, under, seen_at]}}`), beside the last pregame row (the close). `bots/line_tools.py` devigs (`fair_over`) and reads open and close across files; the edge scripts now report the **closing-line value** of our over/under picks (picks at >= 4 pts of edge vs the opening price; did the market move our way by the close). First read, NFL weeks 6-7, over-price only (vig included, so the sign matters, not the size): receptions 38% moved our way vs 30% against, average +1.4 pts (117 picks); rushing yards 48% vs 30%, +0.8 (64); receiving yards 28% vs 30%, +0.3 (120); rush attempts -0.4 (28). Slightly positive, small samples. NBA / NHL opening tracking starts with this deploy (the old files overwrote the open).
- **The price at the moment they hit** (`bots/live_price_log.py`, `live-prices.yml`). Kalshi is the one public live per-player market. A minute-by-minute poll through the games saves every price change for NBA points / rebounds / assists / PRA, NHL goals / points / assists, NFL anytime TD. `report` joins settled markets to ESPN's play-by-play (NBA points: the wall-clock time his running points reached the strike; NHL goals: when his Nth goal was posted) and prints the average pregame price vs the price in the last tick before the hit, for the ones that hit and the ones that did not. Poll and parsing tested live; it needs the games' ticks to say anything, so the first real numbers come the morning after the first scheduled evening run.
- **How low the live price goes before a comeback** (`bots/comeback_prices.py`, ESPN's live win-probability curve for every play; NHL from score state). NBA 2025-26 (1,258 games): teams that won after being at or below 50% had a median low point of 24.4% (+310); teams that fell to <=10% at any moment won 11.8% (169 of 1,427), to <=5% won 6.5%, to <=2% won 2.3%. NFL 2023-25 (816 games): median low point 29.3% (+242); <=10%: 7.0% won, <=5%: 3.1%. NHL (4,174 games): trailing by 1 after the 1st won 32.7% (+206), by 2 after the 1st 19.1% (+425), by 2 after the 2nd 9.3% (+973), by 3 after the 2nd 3.4% (+2,819), never after 4. Caution: "touched <=X%" selects on the curve's lowest point, which is biased low, so the winning share sitting above X is not by itself evidence that live prices are too long; a priced test needs the book's actual live line at those moments, which the live price log now starts collecting.

## Closing-line value on the team lines, and live spreads (2026-10-24)
**CLV, spread and moneyline (`bots/team_clv.py`; ESPN's opening and closing numbers for every game, our out-of-sample model from `team_models.py`).** When our number is 1+ point from the OPENING spread, the line moves toward us more often than against: NBA 2025-26 44% vs 31% (893 games), average CLV +0.35 pts; NFL 2025 41% vs 27% (203 games), +0.24 pts, and 2026 so far 55% vs 31% (74 games), +1.14 pts. Moneyline (our win probability 3+ pts from the open's fair price): NBA 52% vs 41%, +0.81 pts of win probability (839 games); NFL 55% vs 37%, +0.49 (195); NHL 53% vs 37%, +0.63 (570). So the models carry real information the opening market has not priced yet. It is not yet money: our side covered the opening spread 51.0% in NBA (ROI at -110: -3%) and 45.3% in NFL (-14%, 203 games, within noise), because a few tenths of a point of CLV is worth about a percentage point of cover rate and the vig is 4.5. The prospective version runs in the shadow workflows: `team_live.py` saves the model line and the book line before the game, and `grade` pulls the close and reports CLV.
**Live spreads (`bots/live_spread.py`).** From ESPN play-by-play (NBA 1,259 games of 2025-26, NFL 816 of 2023-25) the state (lead or deficit, seconds left, pregame spread) is sampled every 2 / 3 minutes and mapped to the final margin; fitted on the first 70% of games, checked on the last 30% (calibration within a few points in every band; NBA log loss 0.523, NFL 0.534). `ask` answers the live-bet question: NBA, down 15 with 18:00 left, pregame pick'em, offered +12.5 at +120: fair live spread +12.8, covers 48.9% (fair +104), break-even 45.5%, edge +3.5 pts, EV +7.7% per $1. The same state with the team a 6-point pregame underdog: fair +15.3, covers 38.5%, no edge. `table` prints the fair live spread and win probability for every deficit and clock: e.g. NBA down 15 at end of the third (12:00 left) fair spread +13.4 and a 4% chance to win; NFL down 14 with 15:00 left fair +11.8 and 5% to win. The fair numbers sit very close to the deficit itself, so the edge in a live spread comes from the pregame strength of the team (a favourite down 15 is worth more than an underdog down 15), from foul / timeout / pace state this model does not see yet, and from stale or soft live numbers at a particular book, which is what the live price log is collecting.
