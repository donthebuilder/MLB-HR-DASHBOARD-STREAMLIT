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
MAX_ATTEMPTS=4

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
  # the bot's own copy of the site's context lane (2026-08-08)
  context_pack_latest.json
  # fence-line contact board from spray_cache (2026-08-08)
  fence_board.json
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

# social/history/social_history_<date>.jsonl (2026-08-21, DASH social
# pipeline). Same accumulate-and-cap shape as the logs above; kept via its
# own trim block in carry_forward() rather than the generic loop, since it
# lives one directory deeper than the rest of PUBLISH_FILES.
SOCIAL_HISTORY_KEEP=180

git config user.email "bot@mlb-hr-dashboard"
git config user.name "mlb-hr-bot"

# Snapshot this run's output ONCE, before anything touches the working tree.
# Creating the orphan branch wipes the tree, and public/data is untracked
# (gitignored on main), so it does NOT come back on checkout -- a retry would
# otherwise stage an empty directory and publish nothing.
SRC=/tmp/data-src
rm -rf "$SRC" && mkdir -p "$SRC"
[ -d public/data ] && cp -r public/data "$SRC/" || mkdir -p "$SRC/data"

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
           "$SRC"/data/$PRED_LOG_GLOB "$SRC"/data/current/$PRED_LOG_GLOB \
           "$SRC"/data/$OUTCOME_LOG_GLOB "$SRC"/data/current/$OUTCOME_LOG_GLOB \
           "$SRC"/data/$POR_LOG_GLOB "$SRC"/data/current/$POR_LOG_GLOB; do
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
              "$PRED_LOG_GLOB:$PRED_LOG_KEEP" "$OUTCOME_LOG_GLOB:$OUTCOME_LOG_KEEP" \
              "$POR_LOG_GLOB:$POR_LOG_KEEP"; do
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

  git fetch origin data --depth 1 2>/dev/null || true
  remote_before=""
  if git rev-parse --verify origin/data >/dev/null 2>&1; then
    remote_before="$(git rev-parse origin/data)"
    carry_forward origin/data
  fi

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
  git fetch origin data --depth 1 2>/dev/null || true
  remote_now=""
  git rev-parse --verify origin/data >/dev/null 2>&1 && remote_now="$(git rev-parse origin/data)"

  if [ "$remote_before" = "$remote_now" ]; then
    git push --force origin "data-publish-$attempt:data"
    echo "Published $(du -sh public/data | cut -f1) to the data branch:"
    ls -la public/data/current
    exit 0
  fi

  echo "data branch moved mid-publish (attempt $attempt) — re-merging."
  git checkout -q --force main 2>/dev/null || git checkout -q --force -
  attempt=$((attempt + 1))
done

echo "::error::Could not publish after $MAX_ATTEMPTS attempts."
exit 1
