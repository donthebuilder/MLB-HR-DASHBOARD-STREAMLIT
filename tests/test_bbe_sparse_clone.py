"""The bbe-history writers clone ONLY the folder they write (known-issues #107, 2026-10-26).
`git clone --depth 1 --branch bbe-history` pulled the whole branch (about 1.5 GB of files, 1.3 GB of it bbe_history/, which most of these jobs never
open) into every run: live-prices and live-edge start every half hour. The clone is now partial + sparse: only the folder the job writes is
downloaded and checked out; the commit / `pull --rebase` / `push` loop is unchanged.

Two parts:
 1. the workflows: every writer's clone line is partial + sparse, and its sparse-checkout list names every folder it writes;
 2. git itself: the same commands against a throw-away local "origin" -- a first run on a folder that does not exist yet, a push race against
    a writer of ANOTHER folder (the common case) and against the SAME folder, and the big folder is never downloaded.
Nothing here touches GitHub.
Run: PYTHONPATH=. python3 tests/test_bbe_sparse_clone.py"""
import os, re, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUR = "public/data/current"
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_TERMINAL_PROMPT": "0"}

# ---- 1. the workflows -------------------------------------------------------------------------------------------
WRITES = {   # workflow -> the folders (and files) it writes under public/data/current
    "live-prices.yml": ["live_prices"],
    "live-edge.yml": ["live_edges"],
    "injury-snapshot.yml": ["injury_snapshots", "numerology_log"],
    "first-seen.yml": ["first_seen"],
    "ev-scan.yml": ["ev_log"],
    "my-picks.yml": ["shadow_log"],      # + my_picks_report.txt, a file directly in public/data/current (a cone includes its parent's files)
    "odds-log.yml": ["odds_log"],
    "shadow-mlb.yml": ["shadow_log"],
    "shadow-nba.yml": ["shadow_log"],
    "shadow-nfl.yml": ["shadow_log"],
    "shadow-nhl.yml": ["shadow_log"],
}
for wf, dirs in WRITES.items():
    text = (ROOT / ".github/workflows" / wf).read_text()
    clones = [m for m in re.finditer(r"git clone[^\n]*?--branch bbe-history[^\n]*(?:\\\n[^\n]*)?", text)]
    assert len(clones) == 1, f"{wf}: expected one bbe-history clone, found {len(clones)}"
    c = clones[0].group(0)
    assert "--depth 1" in c and "--filter=blob:none" in c and "--sparse" in c, f"{wf}: the clone must be shallow, blob-less and sparse: {c!r}"
    after = text[clones[0].end():clones[0].end() + 400]
    m = re.search(r"git -C _data sparse-checkout set ([^\n]+)", after)
    assert m, f"{wf}: no `sparse-checkout set` right after the clone"
    listed = m.group(1).split()
    for d in dirs:
        assert f"{CUR}/{d}" in listed, f"{wf}: writes {d} but the sparse list is {listed}"
    # nothing else may be in the list that the job does not write (an extra folder is an extra download)
    assert len(listed) == len(dirs), f"{wf}: sparse list {listed} is longer than the folders it writes {dirs}"

assert "+refs/heads/data:refs/remotes/origin/data" in (ROOT / ".github/workflows/odds-log.yml").read_text(), "odds-log.yml must fetch `data` with an explicit refspec"

# ---- 2. git itself ----------------------------------------------------------------------------------------------
def sh(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f"{cmd} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    origin = td / "origin.git"
    sh(["git", "init", "-q", "--bare", "-b", "bbe-history", str(origin)], td)
    sh(["git", "config", "uploadpack.allowFilter", "true"], origin)
    sh(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], origin)
    seed = td / "seed"
    sh(["git", "clone", "-q", str(origin), str(seed)], td)
    big = seed / CUR / "bbe_history"; big.mkdir(parents=True)
    for i in range(6):
        (big / f"features_{i}.jsonl").write_text(("x" * 4000 + "\n") * 50 + f"file {i}\n")      # ~200 KB each, all different
    (seed / CUR / "live_prices").mkdir(parents=True)
    (seed / CUR / "live_prices" / "price_ticks_a.jsonl").write_text("a1\n")
    (seed / CUR / "hr_v3_2026-10-01.json").write_text("{}")
    sh(["git", "add", "-A"], seed); sh(["git", "commit", "-qm", "seed"], seed); sh(["git", "push", "-q", "origin", "bbe-history"], seed)
    url = f"file://{origin}"

    def clone(name, folder):
        # exactly the workflow's commands
        sh(["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", "bbe-history", url, name], td)
        sh(["git", "-C", name, "sparse-checkout", "set", f"{CUR}/{folder}"], td)
        (td / name / CUR / folder).mkdir(parents=True, exist_ok=True)
        return td / name

    def commit_push(work, folder, fname, text):
        (work / CUR / folder / fname).write_text(text)
        sh(["git", "add", f"{CUR}/{folder}"], work)
        sh(["git", "commit", "-qm", f"{folder} {fname}"], work)
        for _ in range(5):
            if sh(["git", "pull", "-q", "--rebase", "origin", "bbe-history"], work, check=False).returncode == 0 and \
               sh(["git", "push", "-q", "origin", "HEAD:bbe-history"], work, check=False).returncode == 0:
                return
        raise AssertionError("push never landed")

    a = clone("a", "live_prices")
    assert not list((a / CUR / "bbe_history").glob("*")) if (a / CUR / "bbe_history").exists() else True, "the big folder must not be checked out"
    assert (a / CUR / "live_prices" / "price_ticks_a.jsonl").exists()
    missing = sh(["git", "rev-list", "--objects", "--all", "--missing=print"], a).stdout.split("\n")
    n_missing = sum(1 for x in missing if x.startswith("?"))
    assert n_missing >= 6, f"the six big files should not have been downloaded (missing blobs: {n_missing})"

    # a push race against a writer of ANOTHER folder (and a change to the big folder): ours must still land
    other = td / "other"
    sh(["git", "clone", "-q", url, str(other)], td)
    (other / CUR / "bbe_history" / "features_new.jsonl").write_text("new\n")
    sh(["git", "add", "-A"], other); sh(["git", "commit", "-qm", "other writer"], other); sh(["git", "push", "-q", "origin", "bbe-history"], other)
    commit_push(a, "live_prices", "price_ticks_b.jsonl", "b1\n")
    # ... and against the SAME folder, a different file
    (other / CUR / "live_prices" / "price_ticks_c.jsonl").write_text("c1\n")
    sh(["git", "add", "-A"], other); sh(["git", "commit", "-qm", "same-folder writer"], other); sh(["git", "pull", "-q", "--rebase", "origin", "bbe-history"], other); sh(["git", "push", "-q", "origin", "bbe-history"], other)
    commit_push(a, "live_prices", "price_ticks_d.jsonl", "d1\n")
    tree = sh(["git", "ls-tree", "-r", "--name-only", "bbe-history"], origin).stdout
    for want in ["live_prices/price_ticks_a.jsonl", "live_prices/price_ticks_b.jsonl", "live_prices/price_ticks_c.jsonl", "live_prices/price_ticks_d.jsonl",
                 "bbe_history/features_new.jsonl", "bbe_history/features_0.jsonl", "hr_v3_2026-10-01.json"]:
        assert want in tree, f"{want} missing from origin after the races:\n{tree}"

    # a first run: the folder does not exist on the branch yet
    f = clone("f", "first_seen")
    commit_push(f, "first_seen", "x.jsonl", "1\n")
    assert "first_seen/x.jsonl" in sh(["git", "ls-tree", "-r", "--name-only", "bbe-history"], origin).stdout
    # odds-log.yml also reads files OUT of the `data` branch (a second branch, fetched shallow into the same sparse clone)
    d = td / "dseed"
    sh(["git", "init", "-q", "-b", "data", str(d)], td)
    (d / CUR).mkdir(parents=True)
    (d / CUR / "nba_odds_2026-10-26.json").write_text('{"test": true}\n')
    (d / CUR / "today.txt").write_text("not an odds file\n")
    sh(["git", "add", "-A"], d); sh(["git", "commit", "-qm", "data"], d); sh(["git", "push", "-q", url, "data"], d)
    o = clone("o", "odds_log")
    sh(["git", "fetch", "--quiet", "--depth", "1", "origin", "+refs/heads/data:refs/remotes/origin/data"], o)   # as odds-log.yml writes it
    names = sh(["git", "ls-tree", "--name-only", "origin/data", f"{CUR}/"], o).stdout.split()
    got = [n for n in names if re.search(r"/(nfl|nba|nhl)_odds_20[0-9-]+\.json$", n)]
    assert got == [f"{CUR}/nba_odds_2026-10-26.json"], names
    assert sh(["git", "show", f"origin/data:{got[0]}"], o).stdout == '{"test": true}\n'
    (o / CUR / "odds_log" / "nba_odds_2026-10-26.json").write_text(sh(["git", "show", f"origin/data:{got[0]}"], o).stdout)
    sh(["git", "add", f"{CUR}/odds_log"], o); sh(["git", "commit", "-qm", "odds"], o)
    assert sh(["git", "pull", "--rebase", "origin", "bbe-history"], o).returncode == 0
    assert sh(["git", "push", "origin", "HEAD:bbe-history"], o).returncode == 0
    assert "odds_log/nba_odds_2026-10-26.json" in sh(["git", "ls-tree", "-r", "--name-only", "bbe-history"], origin).stdout
print("test_bbe_sparse_clone: ok")
