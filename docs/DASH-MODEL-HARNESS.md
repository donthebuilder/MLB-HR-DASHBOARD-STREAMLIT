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
