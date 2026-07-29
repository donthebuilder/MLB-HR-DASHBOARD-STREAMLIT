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
)

# Nightly graded files, kept so the backtest has more than one day to look at.
# Everything else here is regenerated each run; these ACCUMULATE -- carry_forward
# below re-copies whatever this run didn't produce, so the set grows by one file
# a night. GRADED_KEEP caps it so the branch can't grow without bound.
# .txt feeds backtest_report.py; .json feeds the Results tab's Yesterday view.
GRADED_GLOB="graded_results_*.txt"
GRADED_JSON_GLOB="graded_results_*.json"
GRADED_KEEP=150

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
           "$SRC"/data/$GRADED_JSON_GLOB "$SRC"/data/current/$GRADED_JSON_GLOB; do
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
  for glob in "$GRADED_GLOB" "$GRADED_JSON_GLOB"; do
    n=$(find "$STAGE/public/data/current" -maxdepth 1 -type f -name "$glob" | wc -l)
    if [ "$n" -gt "$GRADED_KEEP" ]; then
      find "$STAGE/public/data/current" -maxdepth 1 -type f -name "$glob" \
        | sort | head -n "$((n - GRADED_KEEP))" | xargs -r rm -f
      echo "Trimmed $((n - GRADED_KEEP)) old $glob file(s), keeping $GRADED_KEEP."
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
  for sub in detail splits; do
    [ -d "$PREV/public/data/current/$sub" ] || continue
    mkdir -p "$STAGE/public/data/current/$sub"
    for slate_dir in "$PREV/public/data/current/$sub"/*; do
      [ -d "$slate_dir" ] || continue
      base="$(basename "$slate_dir")"
      [ -d "$STAGE/public/data/current/$sub/$base" ] \
        || cp -r "$slate_dir" "$STAGE/public/data/current/$sub/"
    done
  done
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
