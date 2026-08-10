#!/usr/bin/env python3
"""
📂 ARCHIVE — one place that knows where the graded nights live.

2026-08-09, Donovan: "why didn't you also look at the results folder on the
computer I gave access to — it has all the results. Refer to those as well when
doing data grades and updates and running results and backtests."

Every accountability script had its own private four-line loader that globbed
`public/data/current` and nothing else. On a laptop that folder is empty (the
graded files live on the `data` branch), and on CI it holds only what the last
few runs published. So each tool independently saw a fraction of the archive
and reported confident numbers from it. The score shootout saw 14 nights,
called the raw-vs-adjusted question a tie, and 37 more nights were sitting in
~/Desktop/results the whole time. On the full set the answer flips.

That is a class of bug, not one bug, so it gets fixed once, here.

TWO THINGS THIS HANDLES THAT THE PRIVATE COPIES DID NOT

  SHAPE. The archive is not one shape. Across 39 local files there are four:
  a bare top-level list (Apr 16 – May 18), a dict under `graded_slots`, a dict
  under `results`, and one schema_version-tagged dict carrying neither. The old
  loaders called payload.get() unguarded, so a bare-list file raised
  AttributeError and took the run down with it — which is precisely why nobody
  had ever pointed one of these tools at the local folder and watched it work.

  PROVENANCE. Callers get told what was read and from where. A number computed
  from 14 nights and a number computed from 51 should not print identically,
  and the only reason the first shootout run was misleading rather than merely
  limited is that it never said which one it was looking at.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

# Searched in order; the first copy of a given date wins, so local files beat
# remote ones. A file on disk is the one the operator can actually open and
# check, which matters more than freshness for an archive of finished nights.
# MOONSHOT_ARCHIVE_DIRS (os.pathsep-separated) prepends to this list.
def archive_dirs(repo_root: Path | None = None) -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("MOONSHOT_ARCHIVE_DIRS", "")
    dirs += [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    if repo_root:
        dirs.append(Path(repo_root) / "public" / "data" / "current")
    dirs += [
        Path.home() / "Desktop" / "results",
        Path.home() / "results",
        Path.home() / "Desktop" / "moonshot-push" / "results",
    ]
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def meta_of(payload: Any) -> dict:
    """
    The header of a payload, as a dict, whatever shape the file is.

    2026-08-10. This is the other half of rows_of, and its absence took the
    slate down. bots/pick_lock.py did:

        base = json.loads(...)
        rows = list(iter_rows(base))       # shape-safe
        date = str(base.get("date") ...)   # NOT shape-safe

    A bare-list payload sails through the first line and raises
    AttributeError on the second. Yesterday I fixed exactly this bug in four
    files and did not grep for the fifth, which is the actual mistake: the
    class was known and I patched instances.

    A list has no header, so this returns {} for one — callers then fall back
    to deriving the date from the rows, which pick_lock already knew how to do.
    """
    return payload if isinstance(payload, dict) else {}


def rows_of(payload: Any) -> list[dict]:
    """The graded rows, whatever shape the file is."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


def load_local(repo_root: Path | None = None,
               extra: list[Path] | None = None) -> tuple[dict[str, Any], list[str]]:
    """
    Every graded night found on disk, keyed by date, plus a provenance list.

    Returns raw payloads rather than rows so callers that want the night-level
    blocks (merged_homers, hr_capture_report, game_status_by_pk) still get them.
    """
    found: dict[str, Any] = {}
    notes: list[str] = []
    for d in (list(extra or []) + archive_dirs(repo_root)):
        if not d or not d.is_dir():
            continue
        n = 0
        for p in sorted(d.glob("graded_results_*.json")):
            m = DATE_RE.search(p.name)
            if not m or m.group(1) in found:
                continue                      # also skips "... copy.txt" etc.
            try:
                found[m.group(1)] = json.loads(p.read_text())
                n += 1
            except Exception:
                continue                      # an unreadable file is not fatal
        if n:
            notes.append(f"{n} nights from {d}")
    return found, notes


def describe(found: dict[str, Any], notes: list[str]) -> str:
    """One line a script can print, or put in its JSON output, so a reader can
    always tell how much data a conclusion rests on."""
    if not found:
        return "archive: nothing on disk"
    ds = sorted(found)
    return (f"archive: {len(ds)} nights {ds[0]}..{ds[-1]}"
            + (" (" + "; ".join(notes) + ")" if notes else ""))
