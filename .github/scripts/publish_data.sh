#!/usr/bin/env bash
#
# publish_data.sh — push app-facing data to the `data` branch.
#
# Three rules this enforces:
#
#   1. NOTHING is ever pushed to `main`. The old tomorrow.yml committed the
#      full slate output straight to main, which is how the repo reached
#      20+ GB and why Streamlit Cloud timed out cloning it.
#
#   2. The `data` branch is a SINGLE ORPHAN COMMIT, force-pushed every run.
#      History never accumulates, so the branch stays small forever no matter
#      how many times a day the bots run.
#
#   3. Concurrent publishers don't clobber each other. Each workflow now has
#      its own concurrency group (a shared one made GitHub cancel queued
#      runs), so two publishers CAN overlap. Force-pushing blindly would let
#      the slower one wipe the faster one's files. Instead this reads the
#      remote branch, merges anything it isn't regenerating, and re-checks
#      that the remote hasn't moved before pushing -- retrying if it has.
#
# Only small, app-facing files are published. The multi-MB raw slate JSON,
# history/ and archive/ stay on the runner and die with it.
#
# Usage: bash .github/scripts/publish_data.sh "Today slate"

set -euo pipefail

LABEL="${1:-Data}"
STAGE=/tmp/data-out
PREV=/tmp/data-prev
MAX_ATTEMPTS=6

# Files the Streamlit app actually reads.
PUBLISH_FILES=(
  today_slim.json
  tomorrow_slim.json
  today.txt
  tomorrow.txt
  pair_builder_latest.json
  pair_history_summary.json
  hr_companion_latest.json
  results_live.json
  results_live.txt
  results_final.json
  results_final.txt
  backtest_summary.json
  # Official, prediction-of-record HR tier records. Built from the locked
  # pregame rows; the Results page never recalculates membership later.
  eval_report.json
  # the bot's own copy of the site's context lane (2026-08-08)
  context_pack_latest.json
  # fence-line contact board from spray_cache (2026-08-08)
  fence_board.json
  # playoff + World Series odds (2026-09-03) -- ~10 KB, rebuilt daily
  playoff_odds.json
  # comeback wins / blown leads (2026-09-03) -- ~40 KB, rebuilt daily
  comeback_board.json
  # moneyline disagreement log + running grade (2026-09-03)
  moneyline_board.json
  # the OBP/SLG team model's walk-forward grade (2026-09-05) -- ~5 KB
  moneyball_backtest.json
  # ── pick_lock's LEDGER (added 2026-08-15) ──
  # This file was missing from this list from the day pick_lock.py shipped
  # (2026-08-09), and its absence made the whole feature a no-op.
  #
  # pick_lock keeps its state HERE and fetches it back over HTTPS next run,
  # because the runner checks out `main` and a previous run's output only ever
  # exists on `data`. Never publishing it meant every fetch 404'd, every run
  # started from an empty ledger, and a lock taken at 11am was gone by the
  # 11:30 run — so no designation was ever actually frozen and no re-pick was
  # ever actually rejected. Verified 2026-08-15: the URL returns 404.
  #
  # The receipts card has been printing "locked at first pitch, never edited"
  # over that the whole time.
  pick_lock.json
  # The book's line beside the bot's score (2026-08-15). Fetched by the BOT
  # with ODDS_API_KEY from repo secrets — the site is a static build and can
  # never hold a key. Absent when no key is configured; every surface that
  # reads it degrades to the score alone.
  odds_latest.json
  odds_history.json
  odds_status.json
  # THE TRACK RECORD TABLE. build_pick_matrix.py has written this every night
  # since it shipped and it was never in this list, so it died with the runner
  # every time and the site served its frozen static fallback ending 06-22.
  # Exactly the pick_lock.json failure, second instance: a green step, a real
  # file, and no line here to carry it.
  pick_matrix.json
  # The run board -- every hitter's last 30 raw lines, written by
  # player_splits.py on the same fetch it already makes for the splits files.
  runs_latest.json
  # NFL (2026-08-14). The football bot writes into public/data/current/ with an
  # nfl_ prefix specifically so it can ride this script rather than fork it --
  # the orphan-branch force-push and the concurrent-publisher merge below are
  # the parts worth not reimplementing per sport.
  nfl_week.json
  nfl_report_card.json
  nfl_meta.json
  # The research layer (2026-08-14): defence-vs-position by depth role,
  # coverage shells, explosive allowed, team usage, and per-game logs for the
  # hit-rate chart. Separate files on purpose — only one tab reads each, and a
  # 300 KB payload the Games tab never opens is 300 KB it waits for.
  nfl_matchup.json
  nfl_logs.json
  nfl_picks.json
  nfl_results.json
  # NFL ODDS (2026-08-24). bots/nfl/nfl_odds_fetch.py's own two regenerated-
  # every-run outputs, the nfl_ analog of odds_latest.json/odds_status.json
  # above. No nfl_odds_history.json yet -- see that script's module docstring
  # for why the dated-snapshot file was cut from this first pass, so there is
  # no NFL_ODDS_GLOB/KEEP pair to add to the accumulate-and-cap loop below.
  nfl_odds_latest.json
  nfl_odds_status.json
  # MODEL FOUNDATION (2026-08-21, Task 2). One run identity per bot
  # execution -- see docs/MODELS.md and bots/model_registry.py. Small,
  # regenerated every run, so it belongs with the rest of PUBLISH_FILES
  # rather than the accumulating globs below. Written by
  # sync_model_foundation_outputs_to_website_repo() in mlb_dashboard.py.
  # NOT an envelope key on today.json/today_slim.json -- that payload is a
  # bare list, not a dict, so embedding metadata there would have required
  # reshaping it and risked every consumer that assumes a list (make_slim,
  # load_locked_rows_by_game, the Streamlit app). A companion file is the
  # additive-safe equivalent; see the docstring on that sync function.
  today_run_meta.json
  tomorrow_run_meta.json
)

# Nightly graded files, kept so the backtest has more than one day to look at.
# Everything else here is regenerated each run; these ACCUMULATE -- carry_forward
# below re-copies whatever this run didn't produce, so the set grows by one file
# a night. GRADED_KEEP caps it so the branch can't grow without bound.
# .txt feeds backtest_report.py; .json feeds the Results tab's Yesterday view.
GRADED_GLOB="graded_results_*.txt"
GRADED_JSON_GLOB="graded_results_*.json"
GRADED_KEEP=150

# Pre-game odds snapshots, one per slate date, written by bots/odds_fetch.py.
# These ACCUMULATE like the graded files and for the same reason: a closing
# price is not re-fetchable, so the night it isn't kept is a night that can
# never be in the history. bots/odds_history.py joins these to the graded
# files above. odds_20* deliberately does NOT match odds_latest.json or
# odds_history.json -- those two are regenerated every run and live in
# PUBLISH_FILES. ~50 KB a night, so 120 days is about 6 MB.
ODDS_GLOB="odds_20*.json"
ODDS_KEEP=120

# Game moneylines, one file per slate date, written by bots/moneyline_bot.py
# (2026-09-05). Same reason as ODDS_GLOB: a closing h2h price cannot be
# re-fetched, and without these the team model's ROI against the market can
# never be measured -- only its log loss against the outcome. ~3 KB a night.
ML_PRICES_GLOB="moneyline_prices_20*.json"
ML_PRICES_KEEP=200

# MODEL FOUNDATION (2026-08-21, Tasks 4 & 5). Two append-only logs, same
# accumulate-and-cap treatment as GRADED/ODDS above -- carry_forward()'s
# trim loop already generalizes over a list of (glob, keep) pairs, so this
# is additive there too (see below).
#
# prediction_log_*.jsonl: ONE FILE PER RUN (~13/day; the filename embeds
# the run_id, so two concurrent workflows can never write the same file --
# this is why per-run filenames matter, per the roadmap). 300 keeps roughly
# three weeks at that cadence.
PRED_LOG_GLOB="prediction_log_*.jsonl"
PRED_LOG_KEEP=300

# outcome_log_*.jsonl: ONE FILE PER DATE (not per run) -- each grading run
# appends new revisions into that date's file in place (see
# bots/live_results_tracker.py append_outcome_log), so this glob's "keep"
# caps how many distinct DATES survive, same shape as GRADED_JSON_GLOB.
OUTCOME_LOG_GLOB="outcome_log_*.jsonl"
OUTCOME_LOG_KEEP=150

# por_log_*.jsonl: ONE FILE PER DATE, written by bots/pick_lock.py's
# append_por_log() the instant each game_pk's prediction_of_record locks.
# Exists because pick_lock.json's own "prediction_of_record" key resets to
# {} every time the slate date changes (fetch_lock() discards a ledger
# fetched for a different date) -- without this file, eval_report.py could
# only ever see the current slate day. Same accumulate-and-cap shape as
# OUTCOME_LOG_GLOB above, for the same reason (a lock, once taken, is not
# re-derivable later).
POR_LOG_GLOB="por_log_*.jsonl"
POR_LOG_KEEP=150

# MODEL FOUNDATION, NFL side (2026-08-24, the #1 gap in the NFL-vs-MLB parity
# audit). Same accumulate-and-cap shape as PRED_LOG_GLOB/OUTCOME_LOG_GLOB
# above, generalized into the same trim loop below -- but NOT the same KEEP
# numbers. MLB runs ~13x/day, every day; the NFL bot fires on a weekly cadence
# (.github/workflows/nfl.yml's cron list: 12 scheduled firings/week -- every
# entry runs the "Build slate" step unconditionally, so that is also the
# prediction-log write rate) that is active roughly Aug through February, not
# 365 days a year the way MLB's slate turns over. Scaling MLB's numbers down
# by cadence, not copying them:
#
# nfl_prediction_log_*.jsonl: ONE FILE PER RUN, same as PRED_LOG_GLOB. 12
# firings/week x ~25 weeks (preseason ramp-up in August, 18 regular-season
# weeks, ~4 postseason weeks through the Super Bowl) is roughly 300 runs --
# a genuine full-season span, landing near MLB's own 300 by coincidence of
# this arithmetic (a ~3-week buffer at MLB's daily cadence), not because it
# was copied from it.
NFL_PRED_LOG_GLOB="nfl_prediction_log_*.jsonl"
NFL_PRED_LOG_KEEP=300

# nfl_outcome_log_*.jsonl: ONE FILE PER DATE, same as OUTCOME_LOG_GLOB --
# append_nfl_outcome_log() in bots/nfl/nfl_results.py appends a new line to
# that date's file every grading pass rather than opening a new file. Real
# NFL games land on roughly 3 distinct calendar dates a week in season
# (Thu/Sun/Mon, with the occasional Fri/Sat in the preseason and playoffs);
# 3 x ~25 weeks (same full-season span as above) is roughly 75 distinct
# dates -- a real full season of grading history, and well under half of
# MLB's 150 (which covers ~150 individual game NIGHTS across an every-day,
# ~180-day season -- a scale NFL's weekly schedule never approaches).
NFL_OUTCOME_LOG_GLOB="nfl_outcome_log_*.jsonl"
NFL_OUTCOME_LOG_KEEP=75

# nfl_results_<season>_w03.json (2026-09-05): ONE FILE PER GRADED WEEK under a
# name the site can guess, rewritten in place on every pass of that week.
# The outcome log above keeps every pass but is named by run date, which a
# static fetch list can't know. ~100 KB a week; 60 keeps three seasons.
# `_20*` so it never matches nfl_results.json itself.
NFL_RESULTS_GLOB="nfl_results_20*.json"
NFL_RESULTS_KEEP=60

# nfl_odds_<date>.json (B1, 2026-08-28 master plan): ONE FILE PER FETCH-RUN
# DATE, written by bots/nfl/nfl_odds_fetch.py's main(), same accumulate-and-
# cap shape as ODDS_GLOB above -- a closing-ish price, once overwritten by
# the next fetch, is not re-fetchable from a free API. Kept without this line
# already lost the exact same way pick_lock.json and pick_matrix.json did
# (see those PUBLISH_FILES comments above) -- a real file, a green step, and
# no line here to carry it forward. KEEP scaled down from MLB's ODDS_KEEP=120
# by the same cadence argument NFL_OUTCOME_LOG_KEEP above already makes: NFL
# fetches ~12x/week in season vs MLB's ~13x/day, so 90 kept files is a
# genuine multi-month span at NFL's cadence rather than a MLB-sized number
# copied over.
NFL_ODDS_GLOB="nfl_odds_20*.json"
NFL_ODDS_KEEP=90

# social/history/social_history_<date>.jsonl (2026-08-21, DASH social
# pipeline). Same accumulate-and-cap shape as the logs above; kept via its
# own trim block in carry_forward() rather than the generic loop, since it
# lives one directory deeper than the rest of PUBLISH_FILES.
SOCIAL_HISTORY_KEEP=180

# ── READING THE BRANCH, AND NOT PUBLISHING BACKWARDS ────────────────────────
#
# Both of these exist because of 2026-08-22, when the site spent the whole day
# flipping between tonight's board and the previous night's. Every "Today" run
# went green and published the right slate; minutes later a Graded-results
# publish put the 03:35 UTC build back. Three separate holes, all in this file.

# fetch_data -- get the data branch into a remote-tracking ref we can trust.
#
# `git fetch origin data` writes FETCH_HEAD and only *opportunistically*
# updates refs/remotes/origin/data. This branch is a force-pushed ORPHAN commit
# every single run, so every update is a non-fast-forward -- exactly the update
# an opportunistic, non-'+' refspec is allowed to refuse. When it does,
# origin/data stays pinned to whatever it was the first time this job looked,
# and everything downstream reads a stale tree: carry_forward() copies an old
# slate forward as if it were current, and the remote_before/remote_now
# comparison below can never see the branch move because both sides read the
# same frozen ref. An explicit '+' refspec makes the update forced, so
# origin/data is always the real current tip.
fetch_data() {
  git fetch --depth 1 --force origin '+refs/heads/data:refs/remotes/origin/data' 2>/dev/null || true
}

# meta_time -- the generated_at out of a *_run_meta.json, or '' if there isn't
# one. Deliberately sed and not python: this script runs in five different
# workflows and must not acquire a dependency any of them might not have.
meta_time() {
  [ -f "$1" ] || return 0
  # `| head -n 1` under `set -o pipefail` can hand the whole script a SIGPIPE
  # exit status from sed, which `set -e` then treats as a failed publish. The
  # trailing `|| true` is not decoration.
  sed -n 's/.*"generated_at"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1 || true
}

# guard_slate_regression -- never publish a slate older than the branch's.
#
# Only today.yml and tomorrow.yml BUILD a slate. Every other publisher --
# grading (hourly at :10 since 2026-08-14), spray cache, pair history, NFL --
# reaches the push holding nothing but the copy carry_forward() lifted off the
# branch. If a slate run publishes in the window between that copy and this
# push, the force-push below puts the older slate back, and the site reverts to
# yesterday's games under tonight's header until the next slate run, which the
# next grading run then undoes again.
#
# --force-with-lease closes the window where the branch moved. This closes the
# one where our *view* of the branch was stale to begin with: the two run metas
# carry generated_at, so the copies can simply be ordered. Newer on the branch
# than in our hand means the branch wins for that slate's files, whatever this
# run happens to be holding. A publisher that did not build a slate can now
# only ever leave the slate alone.
# payload_date -- the "date" a results payload claims ('' if absent). The
# results files carry a plain slate date rather than a generated_at, so they
# need their own reader; same sed-not-python reasoning as meta_time above, and
# the same `|| true` for the same SIGPIPE reason.
payload_date() {
  [ -f "$1" ] || return 0
  sed -n 's/.*"date"[[:space:]]*:[[:space:]]*"\([0-9-]\{10\}\)".*/\1/p' "$1" 2>/dev/null | head -n 1 || true
}

# guard_results_regression -- the same rule as the slate guard, for the two
# ACTIVE results files.
#
# 2026-08-23: results_live.json and results_final.json on the branch were still
# dated 2026-08-21 while graded_results_2026-08-22.json (90 slots, complete) and
# graded_results_2026-08-23.json sat beside them, both current. The dated files
# and the active files are written by the SAME sync call in
# live_results_tracker.py, so the branch showing one fresh and the other two days
# old means a publisher put an older copy back after a grading run had landed a
# newer one -- the slate bug, one directory over. Donovan saw it as the Results
# page calling a finished night "tonight".
#
# So: never publish an active results file older than the branch's. Also prints
# what it saw either way, because the question "which publisher last touched
# this file, and with what date" has cost two sessions of guessing at it.
guard_results_regression() {
  local ref="$1" f staged_at remote_at
  for f in results_live.json results_final.json; do
    staged_at="$(payload_date "$STAGE/public/data/current/$f")"
    rm -f /tmp/remote-results.json
    git show "$ref:public/data/current/$f" > /tmp/remote-results.json 2>/dev/null || continue
    remote_at="$(payload_date /tmp/remote-results.json)"
    [ -n "$remote_at" ] || continue
    echo "publish: $f staged=${staged_at:-none} branch=${remote_at}"
    if [ -z "$staged_at" ] || [[ "$remote_at" > "$staged_at" ]]; then
      git show "$ref:public/data/current/$f" > "$STAGE/public/data/current/$f" 2>/dev/null \
        || rm -f "$STAGE/public/data/current/$f"
      # The .txt sibling has to move with it or the two disagree about which
      # night the page is showing.
      git show "$ref:public/data/current/${f%.json}.txt" > "$STAGE/public/data/current/${f%.json}.txt" 2>/dev/null || true
      echo "publish: kept the branch's $f (${remote_at}) over ours (${staged_at:-none})."
    fi
  done
  return 0
}

guard_slate_regression() {
  local ref="$1" label staged_at remote_at f
  for label in today tomorrow; do
    staged_at="$(meta_time "$STAGE/public/data/current/${label}_run_meta.json")"
    rm -f /tmp/remote-meta.json
    git show "$ref:public/data/current/${label}_run_meta.json" > /tmp/remote-meta.json 2>/dev/null || continue
    remote_at="$(meta_time /tmp/remote-meta.json)"
    [ -n "$remote_at" ] || continue
    # No staged meta at all means we are not the ones building this slate, so
    # anything we hold for it came from carry_forward and the branch is at
    # least as good. Same restore either way.
    if [ -z "$staged_at" ] || [[ "$remote_at" > "$staged_at" ]]; then
      for f in "${label}_slim.json" "${label}.json" "${label}.txt" "${label}_run_meta.json"; do
        git show "$ref:public/data/current/$f" > "$STAGE/public/data/current/$f" 2>/dev/null \
          || rm -f "$STAGE/public/data/current/$f"
      done
      # ── THE SLATE AND ITS DETAIL ARE ONE UNIT (2026-08-29) ──────────────
      # This guard restored four slate FILES and left detail/<label> alone,
      # so a run that lost the slate race still published its own detail
      # beside the branch's older slate. That is how both detail directories
      # ended up describing a night that was on neither slate file shipped
      # with them -- 0 of 17 game_pks in common, 27 of 30 starters with no
      # arsenal, and every recurring hitter's spray chart quietly showing
      # another game. Whichever slate wins, its detail wins with it.
      rm -rf "$STAGE/public/data/current/detail/${label}"
      if git cat-file -e "$ref:public/data/current/detail/${label}/_manifest.json" 2>/dev/null; then
        mkdir -p "$STAGE/public/data/current/detail"
        rm -rf /tmp/detail-restore && mkdir -p /tmp/detail-restore
        git archive "$ref" "public/data/current/detail/${label}" 2>/dev/null \
          | tar -x -C /tmp/detail-restore 2>/dev/null || true
        [ -d "/tmp/detail-restore/public/data/current/detail/${label}" ] \
          && cp -r "/tmp/detail-restore/public/data/current/detail/${label}" \
                   "$STAGE/public/data/current/detail/" || true
        echo "Kept the branch's ${label} detail with its slate."
      else
        echo "::warning::the branch's ${label} slate has no stamped detail directory -- shipping the slate without one rather than pairing it with ours, which describes a different night."
      fi
      echo "Kept the branch's ${label} slate (${remote_at}) over ours (${staged_at:-none})."
    fi
  done
  return 0
}

git config user.email "bot@mlb-hr-dashboard"
git config user.name "mlb-hr-bot"

# Snapshot this run's output ONCE, before anything touches the working tree.
# Creating the orphan branch wipes the tree, and public/data is untracked
# (gitignored on main), so it does NOT come back on checkout -- a retry would
# otherwise stage an empty directory and publish nothing.
SRC=/tmp/data-src
rm -rf "$SRC" && mkdir -p "$SRC"
[ -d public/data ] && cp -r public/data "$SRC/" || mkdir -p "$SRC/data"

# ── THE LANDMINE GUARD (2026-08-29) ─────────────────────────────────────────
#
# This script's own comments said "public/data is untracked (gitignored on
# main)". It was not: 1,535 stale files -- odds_latest.json from Aug 17,
# odds_status.json from Aug 22, detail/, splits/ and zones/ from Aug 21 --
# were TRACKED on main, so every fresh CI checkout started life holding a
# week-old copy of the entire app-facing set. stage_local() cannot tell a
# checkout leftover from this run's output, so every publisher that did not
# itself run odds_fetch staged the Aug 17 board as if it had just built it --
# and a staged file is exactly the file carry_forward() will NOT restore from
# the branch. A successful 03:26 UTC odds fetch was clobbered 36 minutes
# later by an NFL publish, every day, for twelve days. The slate guard and
# the results guard above are both earlier symptoms of this same disease.
#
# The commit this ships in untracks public/data from main. This guard is the
# belt to that suspenders: a file that is tracked in HEAD and UNMODIFIED in
# this checkout is by definition a committed leftover, not something a bot
# wrote this run -- drop it from the snapshot so carry_forward() takes the
# branch's copy instead. If public/data is ever committed to main again (it
# has happened twice now), this keeps the branch honest anyway.
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  git ls-files -- public/data 2>/dev/null | while IFS= read -r f; do
    if git diff --quiet HEAD -- "$f" 2>/dev/null; then
      rm -f "$SRC/${f#public/}"
    fi
  done
fi

stage_local() {
  rm -rf "$STAGE"
  mkdir -p "$STAGE/public/data/current"
  for f in "${PUBLISH_FILES[@]}"; do
    [ -f "$SRC/data/current/$f" ] && cp "$SRC/data/current/$f" "$STAGE/public/data/current/"
    # Some bots write to public/data/ rather than public/data/current/.
    [ ! -f "$STAGE/public/data/current/$f" ] && [ -f "$SRC/data/$f" ] \
      && cp "$SRC/data/$f" "$STAGE/public/data/current/"
  done
  # This run's graded file(s). Written to public/data/ by live_results_tracker.
  for g in "$SRC"/data/$GRADED_GLOB "$SRC"/data/current/$GRADED_GLOB \
           "$SRC"/data/$GRADED_JSON_GLOB "$SRC"/data/current/$GRADED_JSON_GLOB \
           "$SRC"/data/$ODDS_GLOB "$SRC"/data/current/$ODDS_GLOB \
           "$SRC"/data/$ML_PRICES_GLOB "$SRC"/data/current/$ML_PRICES_GLOB \
           "$SRC"/data/$PRED_LOG_GLOB "$SRC"/data/current/$PRED_LOG_GLOB \
           "$SRC"/data/$OUTCOME_LOG_GLOB "$SRC"/data/current/$OUTCOME_LOG_GLOB \
           "$SRC"/data/$POR_LOG_GLOB "$SRC"/data/current/$POR_LOG_GLOB \
           "$SRC"/data/$NFL_PRED_LOG_GLOB "$SRC"/data/current/$NFL_PRED_LOG_GLOB \
           "$SRC"/data/$NFL_OUTCOME_LOG_GLOB "$SRC"/data/current/$NFL_OUTCOME_LOG_GLOB \
           "$SRC"/data/$NFL_RESULTS_GLOB "$SRC"/data/current/$NFL_RESULTS_GLOB \
           "$SRC"/data/$NFL_ODDS_GLOB "$SRC"/data/current/$NFL_ODDS_GLOB; do
    [ -f "$g" ] && cp "$g" "$STAGE/public/data/current/"
  done

  [ -f "$SRC/data/index.json" ] && cp "$SRC/data/index.json" "$STAGE/public/data/" || true

  # Per-player detail (spray chart, pitch-type profile, pitcher arsenal).
  # The app fetches ONE of these (~82 KB) only when a player is opened, so
  # this never affects normal page load.
  [ -d "$SRC/data/current/detail" ] && cp -r "$SRC/data/current/detail" "$STAGE/public/data/current/" || true
  # Situational splits (day/night, home/away, day-of-week, win/loss), one
  # small file per hitter, fetched on demand by the Player tab.
  [ -d "$SRC/data/current/splits" ] && cp -r "$SRC/data/current/splits" "$STAGE/public/data/current/" || true
  # Zone profiles from spray_cache.py.
  [ -d "$SRC/data/current/zones" ] && cp -r "$SRC/data/current/zones" "$STAGE/public/data/current/" || true
  # DASH social pipeline (2026-08-21): queue.json, fingerprints.json,
  # history/*.jsonl and assets/<date>/*.png, all under current/social/.
  [ -d "$SRC/data/current/social" ] && cp -r "$SRC/data/current/social" "$STAGE/public/data/current/" || true
  return 0
}

# Copy anything already on the data branch that THIS run didn't regenerate,
# so a grading run doesn't drop the slate and a slate run doesn't drop results.
carry_forward() {
  rm -rf "$PREV" && mkdir -p "$PREV"
  git archive "$1" public/data 2>/dev/null | tar -x -C "$PREV" || return 0
  if [ -d "$PREV/public/data/current" ]; then
    for f in "$PREV/public/data/current"/*; do
      [ -f "$f" ] || continue
      base="$(basename "$f")"
      [ -f "$STAGE/public/data/current/$base" ] || cp "$f" "$STAGE/public/data/current/"
    done
  fi
  # Trim the graded backlog oldest-first. Filenames are ISO-dated, so a plain
  # sort is chronological.
  #
  # find, not ls: with `set -o pipefail` a glob that matches nothing makes ls
  # exit 2, the whole pipeline inherits it, the $(...) assignment fails, and
  # `set -e` kills the publish before anything ships. That is exactly what
  # broke Today #14 -- on the first run there were no graded files yet, so the
  # glob matched nothing and the script died trying to count zero files.
  # find exits 0 on no matches.
  for spec in "$GRADED_GLOB:$GRADED_KEEP" "$GRADED_JSON_GLOB:$GRADED_KEEP" "$ODDS_GLOB:$ODDS_KEEP" \
              "$ML_PRICES_GLOB:$ML_PRICES_KEEP" \
              "$PRED_LOG_GLOB:$PRED_LOG_KEEP" "$OUTCOME_LOG_GLOB:$OUTCOME_LOG_KEEP" \
              "$POR_LOG_GLOB:$POR_LOG_KEEP" \
              "$NFL_PRED_LOG_GLOB:$NFL_PRED_LOG_KEEP" "$NFL_OUTCOME_LOG_GLOB:$NFL_OUTCOME_LOG_KEEP" \
              "$NFL_RESULTS_GLOB:$NFL_RESULTS_KEEP" \
              "$NFL_ODDS_GLOB:$NFL_ODDS_KEEP"; do
    glob="${spec%:*}"; keep="${spec##*:}"
    n=$(find "$STAGE/public/data/current" -maxdepth 1 -type f -name "$glob" | wc -l)
    if [ "$n" -gt "$keep" ]; then
      find "$STAGE/public/data/current" -maxdepth 1 -type f -name "$glob" \
        | sort | head -n "$((n - keep))" | xargs -r rm -f
      echo "Trimmed $((n - keep)) old $glob file(s), keeping $keep."
    fi
  done

  [ -f "$PREV/public/data/index.json" ] && [ ! -f "$STAGE/public/data/index.json" ] \
    && cp "$PREV/public/data/index.json" "$STAGE/public/data/" || true
  # detail/ and splits/ are only rebuilt by the slate workflows, so a grading
  # run must carry them forward or every player view goes blank.
  #
  # Merge PER SLATE, not on the parent directory. The old check was
  # `[ ! -d $STAGE/.../detail ]` -- all or nothing. The tomorrow bot stages
  # detail/tomorrow, which made that test false, so the carry-forward was
  # skipped and detail/today was dropped from the branch entirely. Every
  # night after the tomorrow run, today's spray charts, pitch profiles and
  # splits silently vanished until the next today run rebuilt them.
  for sub in detail splits zones social; do
    [ -d "$PREV/public/data/current/$sub" ] || continue
    mkdir -p "$STAGE/public/data/current/$sub"
    for slate_dir in "$PREV/public/data/current/$sub"/*; do
      # ── DO NOT CARRY FORWARD ANOTHER NIGHT'S DETAIL (2026-08-29) ────────
      # This loop is what kept the site's per-player layer alive through a
      # grading run, and it is also what silently kept it alive for MONTHS
      # past its slate. Measured on the live branch that morning: both
      # detail/today and detail/tomorrow were coherent snapshots of a night
      # that was on NEITHER slate file published beside them -- 0 of 17
      # game_pks in common. 27 of 30 starters had no arsenal, and every
      # hitter who plays most nights kept his player_id, so his stale
      # batter_<id>.json was found and rendered as if it were tonight.
      #
      # make_slim.py now stamps each detail directory with a _manifest.json
      # naming the slate it describes. If the manifest says a different day
      # than the slate file we are shipping, carrying that directory forward
      # is worse than dropping it: the site renders "no detail published",
      # which is true, instead of another game's numbers, which is a lie.
      #
      # Only detail/ is checked. splits/, zones/ and social/ have no such
      # stamp and are keyed differently; they keep the old behaviour.
      if [ "$sub" = "detail" ] && [ -d "$slate_dir" ]; then
        label="$(basename "$slate_dir")"
        want=""
        [ -f "$STAGE/public/data/current/${label}_run_meta.json" ] \
          && want="$(sed -n 's/.*"slate_date"[[:space:]]*:[[:space:]]*"\([0-9-]*\)".*/\1/p' \
               "$STAGE/public/data/current/${label}_run_meta.json" | head -1)"
        have=""
        [ -f "$slate_dir/_manifest.json" ] \
          && have="$(sed -n 's/.*"slate_date"[[:space:]]*:[[:space:]]*"\([0-9-]*\)".*/\1/p' \
               "$slate_dir/_manifest.json" | head -1)"
        if [ -n "$want" ] && [ "$want" != "$have" ]; then
          echo "::warning::dropping carried-forward detail/${label} -- it describes ${have:-an unstamped slate}, the ${label} slate being published is ${want}."
          continue
        fi
      fi
      [ -e "$slate_dir" ] || continue
      base="$(basename "$slate_dir")"
      if [ -d "$slate_dir" ]; then
        [ -d "$STAGE/public/data/current/$sub/$base" ] \
          || cp -r "$slate_dir" "$STAGE/public/data/current/$sub/"
      else
        # social/ also carries two flat files (queue.json, fingerprints.json)
        # alongside its history/ and assets/ subdirectories -- those two are
        # ALWAYS this run's freshest copy when a social bot ran, so only
        # carry them forward when this run didn't touch social/ at all.
        [ -f "$STAGE/public/data/current/$sub/$base" ] \
          || cp "$slate_dir" "$STAGE/public/data/current/$sub/"
      fi
    done
  done

  # social/history/*.jsonl is a per-date append-only log, same shape as
  # OUTCOME_LOG_GLOB above -- carry forward every date this run didn't
  # touch, then trim the oldest once the whole set is assembled.
  if [ -d "$STAGE/public/data/current/social/history" ]; then
    n=$(find "$STAGE/public/data/current/social/history" -maxdepth 1 -type f -name '*.jsonl' | wc -l)
    if [ "$n" -gt "$SOCIAL_HISTORY_KEEP" ]; then
      find "$STAGE/public/data/current/social/history" -maxdepth 1 -type f -name '*.jsonl' \
        | sort | head -n "$((n - SOCIAL_HISTORY_KEEP))" | xargs -r rm -f
      echo "Trimmed $((n - SOCIAL_HISTORY_KEEP)) old social history file(s), keeping $SOCIAL_HISTORY_KEEP."
    fi
  fi
  return 0
}

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  stage_local

  fetch_data
  remote_before=""
  if git rev-parse --verify origin/data >/dev/null 2>&1; then
    remote_before="$(git rev-parse origin/data)"
    carry_forward origin/data
    guard_slate_regression origin/data
    guard_results_regression origin/data
  fi

  # ── LAST WORD ON THE DETAIL DIRECTORIES (2026-08-29) ───────────────────
  # Every path above -- this run's own staging, carry_forward, and the slate
  # regression guard -- can put a detail/<label> next to a <label>_run_meta
  # that describes a different night. Rather than trust that all three now
  # agree, check the thing that actually matters once, here, after all of
  # them have run, and drop any directory that disagrees.
  #
  # Dropping is the right call over keeping: "no detail published" is a true
  # sentence the site already renders honestly, and another night's arsenal
  # rendered as tonight's is not. A directory with no manifest at all is a
  # pre-2026-08-29 publish; it gets the same treatment, which is what heals
  # the branch on the first run after this ships.
  for label in today tomorrow; do
    dir="$STAGE/public/data/current/detail/$label"
    [ -d "$dir" ] || continue
    meta="$STAGE/public/data/current/${label}_run_meta.json"
    [ -f "$meta" ] || continue
    want="$(sed -n 's/.*"slate_date"[[:space:]]*:[[:space:]]*"\([0-9-]\{10\}\)".*/\1/p' "$meta" 2>/dev/null | head -n 1 || true)"
    [ -n "$want" ] || continue
    have=""
    [ -f "$dir/_manifest.json" ] \
      && have="$(sed -n 's/.*"slate_date"[[:space:]]*:[[:space:]]*"\([0-9-]\{10\}\)".*/\1/p' "$dir/_manifest.json" 2>/dev/null | head -n 1 || true)"
    if [ "$want" != "$have" ]; then
      echo "::warning::detail/${label} describes ${have:-no stamped slate} but the ${label} slate is ${want} -- dropping it. The site will say no detail is published for this slate, which is true; it will not show another night's numbers as tonight's."
      rm -rf "$dir"
    else
      echo "publish: detail/${label} matches its slate (${want})."
    fi
  done

  # Fresh orphan branch: no parent, so no history to inherit or grow.
  git checkout -q --orphan data-publish-$attempt
  git rm -rf --cached . >/dev/null 2>&1 || true
  find . -mindepth 1 -maxdepth 1 -not -name '.git' -exec rm -rf {} + 2>/dev/null || true
  cp -r "$STAGE/." .

  # -f because .gitignore excludes public/data on main; here we want it.
  git add -A -f public/data
  if git diff --staged --quiet; then
    echo "Nothing to publish."
    exit 0
  fi
  git commit -q -m "$LABEL $(date -u +'%Y-%m-%d %H:%M') UTC"

  # Did another publisher land while we were staging? If so, redo the merge
  # against their commit instead of force-pushing over the top of it.
  fetch_data
  remote_now=""
  git rev-parse --verify origin/data >/dev/null 2>&1 && remote_now="$(git rev-parse origin/data)"

  if [ "$remote_before" = "$remote_now" ]; then
    # --force-with-lease, not --force. The check above and the push below are
    # two separate round trips, and a slate publish landing in between used to
    # be overwritten silently -- the whole 2026-08-22 flapping. The lease makes
    # the server itself refuse the push unless data is still the commit we
    # merged against, so a race costs one retry instead of a night's board.
    # Plain --force only when the branch does not exist yet (nothing to lose).
    if [ -z "$remote_before" ]; then
      push_rc=0
      git push --force origin "data-publish-$attempt:data" || push_rc=$?
    else
      push_rc=0
      git push --force-with-lease="data:$remote_before" origin "data-publish-$attempt:data" || push_rc=$?
    fi
    if [ "$push_rc" -eq 0 ]; then
      echo "Published $(du -sh public/data | cut -f1) to the data branch:"
      ls -la public/data/current
      exit 0
    fi
    echo "push rejected — the data branch moved between the check and the push (attempt $attempt)."
  else
    echo "data branch moved mid-publish (attempt $attempt) — re-merging."
  fi
  git checkout -q --force main 2>/dev/null || git checkout -q --force -
  attempt=$((attempt + 1))
done

echo "::error::Could not publish after $MAX_ATTEMPTS attempts."
exit 1
