# What the bot now publishes for the site (2026-10-25)

`boards.yml` writes these to the `data` branch (`public/data/current/`) through `publish_data.sh` (all are in `PUBLISH_FILES`). The site reads them like its other bot files. Every probability is a calibrated model chance; nothing here is a CALLED / ON THE BOARD label (those stay in `lib/callStatus.js` / `lib/nhl/goalModel.js`). A model's records are never rewritten: a new model is a new `model_version`.
Schedule: 16:05, 21:05 and 22:35 UTC daily (NHL / NBA / MLB), and Thursday / Sunday / Monday for NFL. Each file carries its own date (or season and week) and `model_version`; a file for a day with no games is not rewritten.

| File | Shape | What a row is |
|---|---|---|
| `nhl_board_latest.json` | `{date, model_version, players[]}` | every skater on tonight's teams: `p_goal` (calibrated), `p_points`, `p_assist`, `p_sog3`, `p_play` (injury report), `p_goal_adj`, `tier`, `opp_goalie`, `lane_base` (his own season rate per lane, the plain bar), `start_utc` |
| `nba_board_latest.json` | `{date, model_version, players[], left_out_injured}` | every rotation player: `p_25` (25+ points), `lanes{reb, ast, 3pm, pra, dd}` (REB 10+, AST 8+, 3PM 4+, PRA 35+, double-double), `p_play`, `blowout_risk`, `lane_base` |
| `nba_first_board_latest.json` | `{date, starters, players[]}` | each starter's `p_first_basket`, `top3_in_game`, `starters_source` (NBA daily lineups: expected / confirmed, or projected), `game_status` |
| `team_board_{nba,nhl,nfl}_latest.json` | `{date, games[]}` | model line beside the book: `model_spread_home` (negative = home favoured), `model_p_home`, `book_spread_home`, `gap_pts`, `book_total`; NHL has the favourite's moneyline string instead of a spread |
| `nhl_sim_latest.json`, `nba_sim_latest.json` | `{date, sims, players[]}` | the joint simulation: `p_goal_sim` / `p_25_sim`, `p_team_win`, and `p_goal_and_win` / `p_25_and_win` (he scores AND his team wins) |
| `nfl_lanes_latest.json` | `{season, week, teams{TEAM: {kickoff, rows[]}}}` | per player: `p_td_fit` (anytime TD), `p_first_td`, `p_lane{rec_yds, rec, rush_yds, rush_att, pass_yds, pass_td, kick_pts, td2, qb_td, pass_comp, pass_int, long_rec}` |
| `mlb_board_latest.json` | `{date, players[]}` | every hitter in a posted lineup: `p_hit`, `p_hrr`, `p_tb2`, `p_hr`, `spot`, `opp_starter`, `lineup_status`, `lane_base` |

Not on the data branch (accumulating records on `bbe-history`, `public/data/current/`): `shadow_log/` (every lane's probability saved before the game and its result), `live_prices/` (Kalshi prices through the games), `live_edges/` (the live moneyline / spread / total paper log), `odds_log/` (every daily prop-line snapshot), `my_picks_report.txt`. The scoreboard (`model_harness.py`) reads the shadow logs and prints the verdict per lane.

What the site would do with them (a separate batch in the site repo, under its own rules: tables lead, phone first, `check-mobile.mjs`, one batch at a time):
1. A probability column on each sport's board from these files (`p_goal`, `p_25`, `p_hit` ...), next to the existing score. The existing CALLED / ON THE BOARD logic does not change.
2. A "model vs book" strip from `team_board_*` (the model's number beside the book's and the gap).
3. First basket and first TD lists (`nba_first_board_latest.json`, `p_first_td`).
4. A joint line on a player card ("scores AND wins") from the sim files.
Nothing in the site changes until that batch is planned there.
