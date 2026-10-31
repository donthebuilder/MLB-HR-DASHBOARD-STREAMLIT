"""today.yml "Verify today's own por_log restored cleanly": a FAILED read of the data branch stops the run; an absent branch is a first run.
Known-issues #90 (2026-10-26). The step used to read "no origin/data" as the first-ever run whatever the reason, so a flaky fetch in the restore step let
pick_lock.py rebuild today's lock history from scratch (a lock, once lost, cannot be derived again).

The step's script is cut out of the real workflow file and run in a throw-away clone against a throw-away origin. Nothing touches GitHub.
Run: PYTHONPATH=. python3 tests/test_today_verify_fetch.py"""
import os, shutil, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def step_script(workflow_text, name_start):
    """The `run: |` block of the one step whose name starts with name_start. Plain text on purpose: the CI test job has no PyYAML."""
    lines = workflow_text.splitlines()
    at = [i for i, l in enumerate(lines) if l.strip().startswith("- name: " + name_start)]
    assert len(at) == 1, f"expected exactly one step named {name_start!r}, found {len(at)}"
    i = next(k for k in range(at[0] + 1, len(lines)) if lines[k].strip() == "run: |")
    base = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for l in lines[i + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= base:
            break
        body.append(l[base + 2:] if l.strip() else "")
    return "\n".join(body).rstrip() + "\n"


script = step_script((ROOT / ".github/workflows/today.yml").read_text(), "Verify today's own por_log")
assert "fetch_data_branch.sh" in script and script.count("exit 1") == 2, "the cut-out script is not the verify step"
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cmd, cwd, e=None):
    return subprocess.run(cmd, cwd=cwd, env=e or env, capture_output=True, text=True)


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    origin = td / "origin.git"
    sh(["git", "init", "-q", "--bare", "-b", "main", str(origin)], td)
    work = td / "work"
    sh(["git", "clone", "-q", str(origin), str(work)], td)
    (work / ".github/scripts").mkdir(parents=True)
    shutil.copy(ROOT / ".github/scripts/fetch_data_branch.sh", work / ".github/scripts/fetch_data_branch.sh")
    (work / "a").write_text("x")
    sh(["git", "add", "-A"], work); sh(["git", "commit", "-qm", "m"], work); sh(["git", "push", "-q", "origin", "main"], work)
    fast = td / "bin"; fast.mkdir()
    (fast / "sleep").write_text("#!/bin/sh\nexit 0\n"); (fast / "sleep").chmod(0o755)
    env2 = {**env, "PATH": f"{fast}:{env['PATH']}"}
    today = subprocess.run(["bash", "-c", "TZ=America/Phoenix date +%F"], capture_output=True, text=True).stdout.strip()

    def run_step():
        return subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env2, capture_output=True, text=True)

    # 1) no data branch at all: a first run -> exit 0
    r = run_step()
    assert r.returncode == 0 and "first-ever run" in r.stdout, (r.returncode, r.stdout, r.stderr)

    # 2) origin unreachable: the step must STOP (exit 1) with an ::error:: line
    sh(["git", "remote", "set-url", "origin", str(td / "gone.git")], work)
    r = run_step()
    assert r.returncode == 1 and "::error::" in r.stdout + r.stderr, (r.returncode, r.stdout, r.stderr)
    sh(["git", "remote", "set-url", "origin", str(origin)], work)

    # 3) the branch holds today's lock file and it did NOT land locally: stop. (A restore that failed silently.)
    d = td / "dseed"
    sh(["git", "init", "-q", "-b", "data", str(d)], td)
    (d / "public/data/current").mkdir(parents=True)
    (d / f"public/data/current/por_log_{today}.jsonl").write_text('{"locked": true}\n')
    sh(["git", "add", "-A"], d); sh(["git", "commit", "-qm", "data"], d); sh(["git", "push", "-q", str(origin), "data"], d)
    r = run_step()
    assert r.returncode == 1 and "did not land" in r.stdout + r.stderr, (r.returncode, r.stdout, r.stderr)

    # 4) ... and when it did land, the step passes
    (work / "public/data/current").mkdir(parents=True)
    (work / f"public/data/current/por_log_{today}.jsonl").write_text('{"locked": true}\n')
    r = run_step()
    assert r.returncode == 0 and "restored" in r.stdout, (r.returncode, r.stdout, r.stderr)
print("test_today_verify_fetch: ok")
