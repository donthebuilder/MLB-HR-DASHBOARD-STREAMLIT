# Leftovers (bot side) — updated 2026-10-30

## Needs the owner
- [ ] **Merge PR 33** (`fix-settle-and-schedule`): spread / moneyline live-edge alerts now settle, the 12-point edge cap, the late-scheduler fix for the NBA / NHL lane snapshots, the report that counts games. Until it merges, tonight's NBA lane snapshot can be missed again.
- [ ] **Site batch** (separate job in `donthebuilder/moonshot-mlb`, its own rules: plan first, tables lead, phone checks): probability columns from the new data files, the model-vs-book strip, first basket / first TD lists, the joint "scores AND wins" line. File list and shapes: `docs/SITE-HANDOFF.md`.
- [ ] **Site: Copy button for the called list** (per day, still works after skipping days). Bot side: add a ready-to-paste `called_text` field to the day's board / first-basket files; the site only reads it.
- [ ] **Site + bot: shared `headline_lane` for every sport.** The player-model rating should show the lane where the player is CALLED (e.g. Gillespie called for threes, not "25 points"), otherwise his highest-probability lane. One shared helper so receiving yards, hits, threes etc. cannot drift apart. Bot side writes the field; site reads it. Needs the player-model screenshot first.
- [ ] **Members section build-out** (site): list what a member sees vs a free visitor, fill the gap (alerts, full board with probabilities, the graded record), a "Start here" checklist and a Discord welcome message. Wait for 5-10 trial users before judging drop-off; ask the NHL tailer one question a day.
- [ ] **Discord notifications review** (new chat): make the live digest bubbles shorter and easier to read; bring in the thread example the owner sent. Also the check-mark tag on cashed picks (memory note).
- [ ] **Export older TUDDY watchlists from the site** (weeks 5-6 and earlier) and add them to `my_picks/picks.csv`, so the CALLED / ON THE BOARD / not-on-the-board comparison has more than one week.
- [ ] A permission rule that lets Claude merge PRs, if wanted (merges were blocked twice by the permission check).

## Waiting on time (no code to write)
- [ ] First weekly read of the **live edge log** (needs more than the 15 games of the first night), the **live price log** (price at the moment they hit), the **shadow logs**, the **closing-line value** of NBA / NHL props (opening prices only started saving 10-24), and the **week-7 pins** in `my_picks/picks.csv` (graded by the 13:30 UTC run).
- [ ] Staking (`bots/staking.py`) stays paper only until a market has a graded sample.

## Model gaps
- [ ] NBA first basket and NFL first TD have **no book prices** to test against (the snapshots carry none); NHL first goal is priced and the book overprices it.
- [ ] NFL kicker probabilities are mostly noise; NHL / MLB live totals are only calibrated between about 20% and 80%.
- [ ] NHL has no spread model (the book spread is a fixed puck line).
- [ ] MLB: closing lines are not pulled, and hitter form is as of the end of the regular season (the logs do not include the postseason). Refresh the logs each spring (change the cache key in `shadow-mlb.yml` / `boards.yml`).
- [ ] Props: only about two weeks of real prop lines exist (the odds log keeps them from now on); the edge-vs-line reads are small samples.
- [ ] Live edge: small score lags can still fool it (skips a game when our win probability and Kalshi's differ by more than 10 points); the depth behind Kalshi's quotes is not checked; fees are assumed.

## Housekeeping
- [ ] Your local change to `bots/twitter_reply_bot.py` is untouched.
- [ ] The MOONSHOT research items are separate from the bot system.
