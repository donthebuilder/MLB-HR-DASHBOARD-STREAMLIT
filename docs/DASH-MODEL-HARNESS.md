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
| Why picks miss | `miss_autopsy.py` (cause) · `autopsy.py` (weekly input pattern) |
| Availability | `injury_status.py` (+ `.github/workflows/injury-snapshot.yml`) |

## Where we stand (measured, not hoped)
| Market | Now | Notes |
|---|---|---|
| MLB HR called picks | 16.1% | top-40% score + lineup 1–5: 20.4% |
| MLB 1+ hit | 68.8% | top-40% score: 72.3%; plateaus there with current inputs |
| MLB 2+ H+R+RBI | 50.9% | top-20%: 55.2% |
| MLB 2+ total bases | 39.3% | tiers barely separate |
| NFL TD best-in-game | 51.2% | `nfl_td_v2` live beats TUDDY v4 |
| NHL goal, best per game | 37.2% | tuned; goalie save% is noise |
| NBA 25+, best per game | 54.0% | tuned |
80% of missed MLB picks had a full chance (3+ AB): with today's inputs most misses read as variance. About 18–20% were knowable (never played, lost time, lineup unconfirmed).

## The gap list, in the order to fill it
1. Confirmed lineups / starters / minutes limits at lock, and locking later (`p_play` is built; live lineup feeds are not).
2. A real game forecast — pregame spread, total, moneyline and their movement — for every sport (blowout *loss* is the star-killer: NBA stars reach 25+ only 11.9% of the time).
3. Player-level matchup, not team-level (NBA defender, MLB pitcher-vs-pitch-type, NFL coverage).
4. Shot / touch quality (NHL xG & shot location, NBA zones, NFL routes & air yards, MLB umpire / wind / bullpen).
5. Prices over time in every sport (MLB has ~48 nights) so return can be graded, not just hits.
6. The injury snapshots (running twice a day once pushed) → measured chance-of-playing for NBA/NHL/MLB.

## Working rules for sessions
- One harness. New models register in `dash_models.REGISTRY`; no stand-alone rankers, no ranking by odds.
- Say what is measured and what is a prior. Say n. Say when a result is inside noise.
- Plain-stat baselines are the bar, not the previous version of our own score.
