#!/usr/bin/env bash
# Fetch origin/data, telling a FAILED read from a genuinely absent branch.
#   exit 0  fetched; refs/remotes/origin/data is current
#   exit 3  the data branch does not exist (the real first-ever-run shape)
#   exit 1  the fetch failed (network, auth, GitHub hiccup) after 3 tries
# Callers used `git fetch ... || true` and then read "no origin/data" as a first
# run, so one transient failure made an append-only log look empty and the run
# published its own rows over the real history (bot audit 10-05).
set -u
for i in 1 2 3; do
  if git fetch --depth 1 --force origin '+refs/heads/data:refs/remotes/origin/data' 2>/dev/null; then
    exit 0
  fi
  git ls-remote --exit-code --heads origin data >/dev/null 2>&1
  rc=$?
  if [ "$rc" = 2 ]; then
    exit 3
  fi
  echo "fetch origin/data failed (attempt $i/3, ls-remote rc=$rc)" >&2
  sleep 5
done
exit 1
