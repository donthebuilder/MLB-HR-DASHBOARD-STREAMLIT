#!/usr/bin/env bash
#
# publish_data.sh — push app-facing data to the `data` branch.
#
# Two rules this enforces, both of which were being violated before:
#
#   1. NOTHING is ever pushed to `main`. The old tomorrow.yml committed the
#      full slate output straight to main, which is how the repo reached
#      20+ GB and why Streamlit Cloud timed out cloning it.
#
#   2. The `data` branch is a SINGLE ORPHAN COMMIT, force-pushed every run.
#      History never accumulates, so the branch stays a few MB forever no
#      matter how many times a day the bots run.
#
# Only small, app-facing files are published. The multi-MB raw slate JSON,
# history/, and archive/ stay on the runner and die with it.
#
# Usage: bash .github/scripts/publish_data.sh "Today slate"

set -euo pipefail

LABEL="${1:-Data}"
STAGE=/tmp/data-out

# Files the Streamlit app actually reads.
PUBLISH_FILES=(
  today_slim.json
  tomorrow_slim.json
  today.txt
  tomorrow.txt
  pair_builder_latest.json
  results_live.json
  results_live.txt
  results_final.json
  results_final.txt
)

git config user.email "bot@mlb-hr-dashboard"
git config user.name "mlb-hr-bot"

rm -rf "$STAGE"
mkdir -p "$STAGE/public/data/current"

for f in "${PUBLISH_FILES[@]}"; do
  if [ -f "public/data/current/$f" ]; then
    cp "public/data/current/$f" "$STAGE/public/data/current/"
  fi
done
[ -f public/data/index.json ] && cp public/data/index.json "$STAGE/public/data/" || true

# Carry forward anything already on the data branch that this run didn't
# regenerate -- e.g. the grading workflow's results files when the picks
# workflow is the one publishing, and vice versa.
git fetch origin data --depth 1 2>/dev/null || true
if git rev-parse --verify origin/data >/dev/null 2>&1; then
  rm -rf /tmp/data-prev && mkdir -p /tmp/data-prev
  git archive origin/data public/data 2>/dev/null | tar -x -C /tmp/data-prev || true
  if [ -d /tmp/data-prev/public/data/current ]; then
    for f in /tmp/data-prev/public/data/current/*; do
      [ -f "$f" ] || continue
      base="$(basename "$f")"
      if [ ! -f "$STAGE/public/data/current/$base" ]; then
        cp "$f" "$STAGE/public/data/current/"
      fi
    done
  fi
  if [ -f /tmp/data-prev/public/data/index.json ] && [ ! -f "$STAGE/public/data/index.json" ]; then
    cp /tmp/data-prev/public/data/index.json "$STAGE/public/data/"
  fi
fi

# Fresh orphan branch: no parent, so no history to inherit or grow.
git checkout --orphan data-publish
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
git push --force origin data-publish:data
echo "Published $(du -sh public/data | cut -f1) to the data branch:"
ls -la public/data/current
