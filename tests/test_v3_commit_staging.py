#!/usr/bin/env python3
"""The `git add` trap that silently threw away the shadow lane's first run.

Runnable as a pytest module and as a plain script.

WHAT HAPPENED, 2026-08-24 00:22 UTC. The hr_score_v3 workflow's commit step ran

    git add A B C 2>/dev/null || true

`git add` VALIDATES EVERY PATHSPEC BEFORE STAGING ANY OF THEM. On the first
night hr_v3_record.jsonl does not exist yet, so the command exited 128 having
staged nothing, `2>/dev/null || true` swallowed the message, and the step
printed "nothing changed". The run was green. It had harvested 718 batted
balls, rebuilt 585 batters' features and scored 259 bats — and kept none of it.

The failure mode is the one that keeps recurring in this repo: THE LOSS REPORTS
SUCCESS. Same shape as the orphan force-push that ate the 299-file harvest.

This test pins the trap itself in a throwaway repo, so the behaviour is written
down rather than remembered, and asserts the workflow uses the safe form.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(cond, msg):
    if cond:
        return
    FAILS.append(msg)
    print("  RED  " + msg)


def git(cwd, *args):
    return subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True)


def test_the_trap_is_real():
    print("git add stages nothing when one pathspec is missing")
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        git(r, "init", "-q")
        git(r, "config", "user.email", "t@t")
        git(r, "config", "user.name", "t")
        (r / "kept.txt").write_text("real output\n")
        out = git(r, "add", "kept.txt", "never_written.txt")
        check(out.returncode != 0, "git add must fail when a pathspec does not exist")
        staged = git(r, "diff", "--cached", "--name-only").stdout.strip()
        check(staged == "",
              f"THE TRAP: the file that DID exist must also be unstaged, got {staged!r}")
        # the safe form
        for p in ("kept.txt", "never_written.txt"):
            if (r / p).exists():
                git(r, "add", p)
        staged2 = git(r, "diff", "--cached", "--name-only").stdout.strip()
        check(staged2 == "kept.txt",
              f"staging each existing path on its own keeps the real output, got {staged2!r}")


def test_the_workflow_uses_the_safe_form():
    print("the workflow stages paths one at a time")
    wf = ROOT / ".github" / "workflows" / "hr-v3-shadow.yml"
    check(wf.exists(), "hr-v3-shadow.yml is present")
    if not wf.exists():
        return
    s = wf.read_text()
    check('[ -e "$p" ] && git add "$p"' in s or '[ -e "$f" ] && git add "$f"' in s,
          "the commit step must guard each path with an existence test")
    bad = re.search(r"git add\s+public/data/current/bbe_history\s*\\\s*\n\s*public/data/current/hr_v3_\*\.json", s)
    check(bad is None,
          "the multi-pathspec `git add` is back — one missing file will stage nothing")


def main() -> int:
    for fn in (test_the_trap_is_real, test_the_workflow_uses_the_safe_form):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} RED")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
