"""publish_data.sh fetch_data(): a FAILED read of the data branch stops the publish; an absent branch is still a first run.
Known-issues #90 (2026-10-26). The old function ended in `|| true`, so one failed fetch looked like "no data branch yet" and the
publish fell to a plain `git push --force` of this run's files over the whole branch.

The test runs fetch_data() exactly as written in publish_data.sh (the block is cut out of the real file, not retyped) next to the real
fetch_data_branch.sh, against a throw-away local "origin". Nothing is fetched from or pushed to GitHub.
Run: PYTHONPATH=. python3 tests/test_publish_fetch_guard.py"""
import os, re, shutil, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = (ROOT / ".github/scripts/publish_data.sh").read_text()
HELPER = ROOT / ".github/scripts/fetch_data_branch.sh"
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cmd, cwd, e=None):
    return subprocess.run(cmd, cwd=cwd, env=e or env, capture_output=True, text=True)


# the real block: from the DATA_FETCH= line through the closing brace of fetch_data()
m = re.search(r'^DATA_FETCH=.*?^fetch_data\(\) \{.*?^\}\n', PUBLISH, re.S | re.M)
assert m, "could not find fetch_data() in publish_data.sh"
block = m.group(0)
assert "|| true" not in block, "fetch_data must not swallow a failed fetch"

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    lib = td / "scripts"; lib.mkdir()
    shutil.copy(HELPER, lib / "fetch_data_branch.sh")
    (lib / "harness.sh").write_text("set -euo pipefail\n" + block + '\nfetch_data\necho FETCHED\n')
    fast = td / "bin"; fast.mkdir()                      # skip the 5-second waits between the helper's retries
    (fast / "sleep").write_text("#!/bin/sh\nexit 0\n"); (fast / "sleep").chmod(0o755)
    env2 = {**env, "PATH": f"{fast}:{env['PATH']}"}

    origin = td / "origin.git"
    sh(["git", "init", "-q", "--bare", "-b", "main", str(origin)], td)
    work = td / "work"
    sh(["git", "clone", "-q", str(origin), str(work)], td)
    (work / "a").write_text("x")
    sh(["git", "add", "a"], work); sh(["git", "commit", "-qm", "m"], work); sh(["git", "push", "-q", "origin", "main"], work)

    # 1) no data branch yet: the first-ever run. fetch_data returns and the publish goes on.
    r = sh(["bash", str(lib / "harness.sh")], work, env2)
    assert r.returncode == 0 and "FETCHED" in r.stdout, (r.returncode, r.stdout, r.stderr)

    # 2) the branch exists: fetched, origin/data is the real tip.
    sh(["git", "push", "-q", "origin", "main:data"], work)
    r = sh(["bash", str(lib / "harness.sh")], work, env2)
    assert r.returncode == 0 and "FETCHED" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert sh(["git", "rev-parse", "--verify", "origin/data"], work).returncode == 0

    # 3) origin unreachable: the publish STOPS. No "FETCHED", a non-zero exit, and an ::error:: line the run page shows.
    sh(["git", "remote", "set-url", "origin", str(td / "gone.git")], work)
    r = sh(["bash", str(lib / "harness.sh")], work, env2)
    assert r.returncode != 0, "a failed fetch must stop the publish"
    assert "FETCHED" not in r.stdout, "the publish must not go on after a failed fetch"
    assert "::error::" in r.stderr, r.stderr
print("test_publish_fetch_guard: ok")
