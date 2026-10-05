"""fetch_data_branch.sh tells a failed read (rc 1) from an absent branch (rc 3).
Run: PYTHONPATH=. python3 tests/test_fetch_data_branch.py"""
import os, subprocess, tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/fetch_data_branch.sh"
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    origin = td / "origin.git"
    sh(["git", "init", "-q", "--bare", "-b", "main", str(origin)], td)
    work = td / "work"
    sh(["git", "clone", "-q", str(origin), str(work)], td)
    (work / "a").write_text("x")
    sh(["git", "add", "a"], work); sh(["git", "commit", "-qm", "m"], work); sh(["git", "push", "-q", "origin", "main"], work)
    # no data branch yet -> rc 3 (first-run shape)
    assert sh(["bash", str(SCRIPT)], work).returncode == 3
    sh(["git", "push", "-q", "origin", "main:data"], work)
    assert sh(["bash", str(SCRIPT)], work).returncode == 0
    assert sh(["git", "rev-parse", "--verify", "origin/data"], work).returncode == 0
    # origin unreachable -> rc 1 (a failed read is never a first run); speed the retries up
    sh(["git", "remote", "set-url", "origin", str(td / "gone.git")], work)
    fast = td / "bin"; fast.mkdir()
    (fast / "sleep").write_text("#!/bin/sh\nexit 0\n"); (fast / "sleep").chmod(0o755)
    env2 = {**env, "PATH": f"{fast}:{env['PATH']}"}
    r = subprocess.run(["bash", str(SCRIPT)], cwd=work, env=env2, capture_output=True, text=True)
    assert r.returncode == 1, (r.returncode, r.stderr)
print("test_fetch_data_branch: ok")
