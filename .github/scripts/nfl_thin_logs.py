#!/usr/bin/env python3
"""Thin the per-run NFL pregame logs on the data branch (2026-10-05).

nfl_prediction_log_*.jsonl (~685 KB each) and nfl_signal_log_*.jsonl (~130 KB)
are one file per bot run. They were capped at 300 files, sized for 12 runs a
week; the pregame cadence (nfl.yml, a run every 15 min from T-120 to each
kickoff) is now ~50+ runs a week, so a plain count cap would prune week 1-2 by
about week 7, and a blind raise would cost ~850 MB.

WHAT IS KEPT
  * Every run of the NEWEST week and the week before it (the live lock, the
    grader and the card all read these).
  * For every older week, per game window (one window per distinct kickoff in
    that week's nfl_por_log): the LAST run generated before that kickoff --
    exactly the run nfl_regrade.pregame_rows and nfl_signal_audit pick ("the
    latest run before the team's kickoff"), so --rebuild-card, --lock-replay
    and the signal audit read the same rows they would have read from the full
    set.
  * Safety extras per older week: the final run of the week, and the last run
    of every burst of runs (a gap > 60 min starts a new burst) -- so a game
    that has no lock line, hence no known kickoff, still keeps the run just
    before its window.
  * Prediction and signal logs are kept/dropped together by run_id (the audit
    joins them on it).
  * Nothing is thinned for a week with no kickoff data (no por_log): unknown
    means keep.
  * Preseason (date-keyed) files and *_next_* files are not touched here;
    publish_data.sh's count caps still apply to them (and remain the backstop).

Usage: nfl_thin_logs.py --dir public/data/current [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

BURST_GAP = dt.timedelta(minutes=60)
FILE_RE = re.compile(r"^nfl_(prediction|signal)_log_(\d{4})-wk(\d\d)\.(.+)\.jsonl$")


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _header(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as fh:
            h = json.loads(fh.readline() or "null")
        return h if isinstance(h, dict) else None
    except (OSError, ValueError):
        return None


MIN_LOCKED_GAMES = 4   # a regular week has 13-16; fewer lock lines = an incomplete record


def kickoffs_for(cur: Path, season: int, week: int) -> list[dt.datetime]:
    """Distinct kickoff instants of the week, from its pick-lock record.
    [] (= keep everything) when the record looks incomplete (< MIN_LOCKED_GAMES
    games) or the week is postseason (>= 19: few runs, never worth the risk)."""
    p = cur / f"nfl_por_log_{season}_w{week:02d}.jsonl"
    out = set()
    games = 0
    if week >= 19 or not p.exists():
        return []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            k = json.loads(line).get("kickoff")
            if k:
                out.add(_ts(k))
                games += 1
        except (ValueError, TypeError, AttributeError):
            continue
    return sorted(out) if games >= MIN_LOCKED_GAMES else []


def plan(cur: Path) -> dict:
    """-> {"keep": set(Path), "drop": set(Path), "notes": [str]}"""
    runs: dict[tuple[int, int], dict[str, dict]] = {}   # (season, week) -> run_id -> {files, t}
    for f in sorted(cur.iterdir()):
        m = FILE_RE.match(f.name)
        if not m:
            continue
        kind, season, wk, rest = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        r = runs.setdefault((season, wk), {}).setdefault(f"{season}-wk{wk:02d}.{rest}", {"files": [], "t": None})
        r["files"].append(f)
        if kind == "prediction" or r["t"] is None:
            h = _header(f)
            if h and h.get("generated_at") and h.get("mode", "week") == "week":
                try:
                    r["t"] = _ts(h["generated_at"])
                except ValueError:
                    pass
    keep: set[Path] = set()
    drop: set[Path] = set()
    notes: list[str] = []
    by_season: dict[int, list[int]] = {}
    for (s, w) in runs:
        by_season.setdefault(s, []).append(w)
    for (s, w), rmap in sorted(runs.items()):
        newest = max(by_season[s])
        allfiles = [f for r in rmap.values() for f in r["files"]]
        if w >= newest - 1:
            keep.update(allfiles)
            notes.append(f"{s} wk{w:02d}: {len(rmap)} runs, current/previous week -> keep all")
            continue
        kos = kickoffs_for(cur, s, w)
        timed = sorted((r["t"], rid) for rid, r in rmap.items() if r["t"] is not None)
        if not kos or len(timed) != len(rmap):
            keep.update(allfiles)
            notes.append(f"{s} wk{w:02d}: {len(rmap)} runs, no kickoff/time data -> keep all")
            continue
        keep_ids: set[str] = {timed[-1][1]}
        for k in kos:
            before = [rid for t, rid in timed if t < k]
            if before:
                keep_ids.add(before[-1])
        for i, (t, rid) in enumerate(timed):
            if i == len(timed) - 1 or timed[i + 1][0] - t > BURST_GAP:
                keep_ids.add(rid)
        for rid, r in rmap.items():
            (keep if rid in keep_ids else drop).update(r["files"])
        notes.append(f"{s} wk{w:02d}: {len(rmap)} runs, {len(kos)} kickoff windows -> keep {len(keep_ids)}")
    return {"keep": keep, "drop": drop, "notes": notes}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    cur = Path(a.dir)
    if not cur.is_dir():
        print(f"nfl_thin_logs: {cur} is not a directory", file=sys.stderr)
        return 1
    p = plan(cur)
    for n in p["notes"]:
        print("  " + n)
    mb = sum(f.stat().st_size for f in p["drop"]) / 1e6
    print(f"nfl_thin_logs: {'WOULD DROP' if a.dry_run else 'dropping'} {len(p['drop'])} file(s), {mb:.1f} MB; keeping {len(p['keep'])}")
    if not a.dry_run:
        for f in sorted(p["drop"]):
            f.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
