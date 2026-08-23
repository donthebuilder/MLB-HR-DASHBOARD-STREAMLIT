"""A workflow that copies scripts must copy what those scripts import.

THE BUG THIS PINS (found 2026-08-23)
====================================
`.github/workflows/accountability.yml` staged its scripts by name:

    cp _src/bots/season_memory.py _src/bots/validate_context.py \\
       _src/bots/autopsy.py _src/bots/score_shootout.py bots/
    rm -rf _src

Three of those four open with `from archive import rows_of`. `archive.py` was
not in the list, and the very next line deleted the only other copy — so
season_memory.py (the NIGHTLY job the site's memory is supposed to come from),
validate_context.py and autopsy.py died on ModuleNotFoundError on every run
since the workflow was written on 2026-08-09.
`public/data/current/season_memory.json` has never existed on the data branch.

A cp list that has to be kept in step with an import graph is a trap that
re-arms every time a script gains a helper. This test walks the graph instead:
for every workflow that copies FROM bots/ INTO a working directory, every
sibling module those copied scripts import must also be copied.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOTS = ROOT / "bots"
WORKFLOWS = ROOT / ".github" / "workflows"

# Modules that ship with Python or come from bots/requirements.txt — an import
# of one of these is not a staging problem.
_STDLIB = set(sys.stdlib_module_names) | {
    "requests", "yaml", "pandas", "numpy", "dateutil", "pytz", "bs4", "lxml",
}


def _local_modules() -> set:
    """Every module name that `import X` could resolve to inside bots/."""
    names = {p.stem for p in BOTS.glob("*.py")}
    names |= {p.name for p in BOTS.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    return names


def _imports_of(path: Path) -> set:
    """Top-level module names imported by one file, local ones only."""
    local = _local_modules()
    out = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:                                  # pragma: no cover
        raise AssertionError(f"{path.name} does not parse: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                head = a.name.split(".")[0]
                if head in local and head not in _STDLIB:
                    out.add(head)
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import, travels with the package
                continue
            head = (node.module or "").split(".")[0]
            if head in local and head not in _STDLIB:
                out.add(head)
    return out


def _staged_by(workflow: Path):
    """The bots/*.py a workflow copies, or None when it copies the lot.

    Returns None for a glob copy (`cp _src/bots/*.py bots/`) — that is the
    shape this test wants people to use, and it has nothing to check.
    """
    text = workflow.read_text(encoding="utf-8")
    if not re.search(r"cp\s+[^\n]*_src/bots/", text):
        return False                       # this workflow does not stage bots/
    if re.search(r"cp\s+[^\n]*_src/bots/\*\.py", text):
        return None                        # copies everything — always safe
    named = set(re.findall(r"_src/bots/([A-Za-z_][\w]*\.py)", text))
    return named


def test_every_staged_script_gets_its_local_imports():
    problems = []
    checked = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        staged = _staged_by(wf)
        if staged is False:
            continue
        checked += 1
        if staged is None:
            continue                       # glob copy, nothing to get wrong
        copied = {Path(n).stem for n in staged}
        for name in sorted(staged):
            src = BOTS / name
            if not src.exists():
                problems.append(f"{wf.name} copies bots/{name}, which does not exist")
                continue
            for dep in sorted(_imports_of(src)):
                if dep not in copied:
                    problems.append(
                        f"{wf.name} copies bots/{name}, which imports `{dep}` — "
                        f"but bots/{dep}.py is not copied, and _src is deleted "
                        f"right after. This job dies on ModuleNotFoundError.")
    assert checked, "no workflow stages bots/ — has the staging shape changed?"
    assert not problems, "\n  · " + "\n  · ".join(problems)


def test_accountability_stages_the_whole_directory():
    """The specific fix, pinned.

    accountability.yml is the one that was broken. Copying the whole directory
    is what makes it un-breakable by a future import; if someone narrows it
    back to a hand-picked list, the test above starts guarding it again — but
    this one says out loud which shape is intended.
    """
    wf = WORKFLOWS / "accountability.yml"
    text = wf.read_text(encoding="utf-8")
    assert "cp _src/bots/*.py bots/" in text, \
        "accountability.yml no longer copies the whole bots directory"


def test_archive_is_importable_by_the_scripts_that_need_it():
    """The three that broke really do import it — so the guard has a subject."""
    for name in ("season_memory.py", "validate_context.py", "autopsy.py"):
        assert "archive" in _imports_of(BOTS / name), \
            f"bots/{name} no longer imports archive — update this test's premise"


def test_odds_probe_leaves_its_answer_on_the_data_branch():
    """A diagnostic whose only output is a log is one nobody runs twice.

    The probe wrote nothing and committed nothing, so a run that changed no
    file was indistinguishable from a run that never happened — reported twice
    as "I ran it and it didn't work".
    """
    text = (WORKFLOWS / "odds-probe.yml").read_text(encoding="utf-8")
    assert "contents: write" in text, "the probe cannot commit its own answer"
    assert "odds_probe.txt" in text, "the probe no longer writes odds_probe.txt"
    assert "ref: data" in text, "the probe no longer checks out the data branch"
    assert "tee /tmp/probe.txt" in text, "the probe output is no longer captured"


if __name__ == "__main__":
    failed, checks = [], 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
            checks += 1
        except AssertionError as e:
            failed.append(f"{_name}: {e}")
        except Exception as e:                                # noqa: BLE001
            failed.append(f"{_name}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   workflow deps: {checks} assertions — every script a workflow "
          f"stages gets the sibling modules it imports (accountability.yml's "
          f"nightly memory job died on a missing archive.py for two weeks), and "
          f"the odds probe leaves its answer on the data branch instead of only "
          f"in a log")
