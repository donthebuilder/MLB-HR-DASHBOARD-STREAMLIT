#!/usr/bin/env python3
"""
site_data_sync_v2.py — MLB Breakdown website data sync
=======================================================

Purpose
-------
Copies bot output JSON/TXT/PDF files into the website repo's public/data folder,
rebuilds indexes, and optionally commits/pushes to GitHub so Vercel can redeploy.

Why this version exists
-----------------------
- Supports --today-date and --tomorrow-date.
- Lets you explicitly pass --repo-dir so files go to the real GitHub/Vercel repo.
- Pulls/rebases before pushing to avoid the common "fetch first" rejection.
- Keeps stable website paths:
    public/data/today.json
    public/data/tomorrow.json
    public/data/results_live.json
    public/data/index.json
    public/data/results/index.json
    public/data/slates.json
- Also keeps current/ aliases and archive/history files.

Daily examples
--------------
# Normal May 8 sync + push
python3 site_data_sync.py \
  --repo-dir "/Volumes/DONX/USERS/Kingdondondon/Documents/GitHub/MLB HR MODEL" \
  --outputs-dir "/Volumes/DONX/USERS/Kingdondondon/Downloads/mlb_hr_bot_starter/outputs" \
  --today-date 2026-05-08 \
  --tomorrow-date 2026-05-09 \
  --push

# Dry run first
python3 site_data_sync.py --dry-run --today-date 2026-05-08 --tomorrow-date 2026-05-09
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Defaults — can be overridden with CLI flags
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_REPO_CANDIDATES = [
    Path(__file__).resolve().parent.parent,  # repo root (parent of bots/)
    Path.cwd(),
]

DEFAULT_OUTPUT_DIRS = [
    Path(__file__).resolve().parent.parent / "bot_outputs",
    Path(__file__).resolve().parent.parent / "outputs",   # repo_root/outputs
    Path(__file__).resolve().parent / "outputs",          # bots/outputs  (CI pair_history writes here)
    Path.cwd() / "bot_outputs",
    Path.cwd() / "outputs",
    Path.cwd() / "bots" / "outputs",
]

# Pair history files have stable names (no date in filename), so we copy them as-is.
PAIR_HISTORY_FILES = [
    "pair_history_cache.json",
    "pair_history_summary.json",
]

TODAY_PATTERNS = [
    r"mlb_breakdown_today_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_today_pitch_mix_version_v\d+_[A-Z]+_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_daily_breakdown_final_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_today_breakdown_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_today_slate_breakdown_(\d{4}-\d{2}-\d{2})\.json$",
]

TOMORROW_PATTERNS = [
    r"mlb_breakdown_tomorrow_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_tomorrow_pitch_mix_version_v\d+_[A-Z]+_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_tomorrow_early_breakdown_(\d{4}-\d{2}-\d{2})\.json$",
    r"mlb_tomorrow_breakdown_(\d{4}-\d{2}-\d{2})\.json$",
    r"tomorrow_early_breakdown_(\d{4}-\d{2}-\d{2})\.json$",
]

# Higher rank wins per date
RESULTS_PATTERNS_RANKED = [
    (4, r"mlb_results_live_(\d{4}-\d{2}-\d{2})\.json$"),
    (3, r"live_graded_results_(\d{4}-\d{2}-\d{2})\.json$"),
    (2, r"graded_results_(\d{4}-\d{2}-\d{2})\.json$"),
    (1, r"results_(\d{4}-\d{2}-\d{2})\.json$"),
]

# TXT/PDF aliases for results, not used for index decisions but copied when present
RESULTS_SIDE_EXTS = [".txt", ".pdf", ".csv"]


def die(msg: str, code: int = 1) -> None:
    print(f"\n❌ {msg}")
    raise SystemExit(code)


def parse_date(value: str, fallback: dt.date) -> dt.date:
    if not value:
        return fallback
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        die(f"Invalid date '{value}'. Use YYYY-MM-DD.")


def find_repo_root(repo_arg: str = "") -> Path:
    candidates = []
    if repo_arg:
        candidates.append(Path(repo_arg).expanduser())
    candidates.extend(DEFAULT_REPO_CANDIDATES)

    for c in candidates:
        try:
            c = c.resolve()
        except Exception:
            pass
        if (c / ".git").exists() and (c / "public").exists():
            return c

    tried = "\n".join(f"  - {p}" for p in candidates)
    die(
        "Could not find the website Git repo. Pass it manually with:\n"
        "  --repo-dir \"/Volumes/DONX/USERS/Kingdondondon/Documents/GitHub/MLB HR MODEL\"\n\n"
        f"Tried:\n{tried}"
    )


def output_dirs(outputs_arg: str, repo_root: Path) -> List[Path]:
    dirs: List[Path] = []
    if outputs_arg:
        dirs.append(Path(outputs_arg).expanduser())
    dirs.extend(DEFAULT_OUTPUT_DIRS)
    dirs.extend([repo_root / "outputs", repo_root / "scripts" / "outputs"])

    # Deduplicate while preserving order
    seen = set()
    clean = []
    for d in dirs:
        try:
            key = str(d.resolve())
        except Exception:
            key = str(d)
        if key not in seen:
            seen.add(key)
            clean.append(d)
    return clean


def all_output_files(dirs: Iterable[Path]) -> List[Path]:
    files: List[Path] = []
    for d in dirs:
        if d.exists():
            files.extend(d.glob("*.json"))
    return sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def match_candidates(patterns: List[str], files: List[Path]) -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    # Pattern order matters. Newer preferred pattern wins. For same pattern/date, newest mtime wins.
    for pat in patterns:
        matches_by_date: Dict[str, Path] = {}
        for f in files:
            m = re.search(pat, f.name, re.IGNORECASE)
            if not m:
                continue
            date_str = m.group(1)
            old = matches_by_date.get(date_str)
            if old is None or f.stat().st_mtime > old.stat().st_mtime:
                matches_by_date[date_str] = f
        for date_str, f in matches_by_date.items():
            if date_str not in found:
                found[date_str] = f
    return found


def pick_today(files: List[Path], target: dt.date) -> Tuple[Optional[Path], str]:
    c = match_candidates(TODAY_PATTERNS, files)
    if not c:
        return None, ""
    target_s = target.isoformat()
    if target_s in c:
        return c[target_s], target_s
    # If exact missing, use newest past today file. Never let tomorrow file take over today unless no past exists.
    past = [d for d in c if d <= target_s]
    if past:
        d = max(past)
        return c[d], d
    d = min(c)
    return c[d], d


def pick_tomorrow(files: List[Path], today: dt.date, tomorrow: dt.date) -> Tuple[Optional[Path], str]:
    c = match_candidates(TOMORROW_PATTERNS, files)
    if not c:
        return None, ""
    today_s = today.isoformat()
    tomorrow_s = tomorrow.isoformat()
    if tomorrow_s in c:
        return c[tomorrow_s], tomorrow_s
    future = [d for d in c if d > today_s]
    if future:
        d = min(future)
        return c[d], d
    return None, ""


def find_results(files: List[Path]) -> Dict[str, Tuple[Path, int]]:
    by_date: Dict[str, Tuple[Path, int]] = {}
    for rank, pat in RESULTS_PATTERNS_RANKED:
        for f in files:
            m = re.search(pat, f.name, re.IGNORECASE)
            if not m:
                continue
            date_s = m.group(1)
            old = by_date.get(date_s)
            if old is None or rank > old[1] or (rank == old[1] and f.stat().st_mtime > old[0].stat().st_mtime):
                by_date[date_s] = (f, rank)
    return by_date


def month_label(date_str: str) -> str:
    try:
        d = dt.date.fromisoformat(date_str)
        return d.strftime("%b %-d")
    except Exception:
        return date_str


def safe_copy(src: Path, dest: Path, dry_run: bool) -> bool:
    if not src or not src.exists():
        return False
    if dry_run:
        print(f"  [DRY] {src.name} → {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def write_json(path: Path, payload: Any, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY] write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def data_date_hint(path: Path) -> str:
    data = read_json(path)
    if isinstance(data, dict):
        for k in ("date", "slate_date", "game_date", "running_slate_date"):
            if data.get(k):
                return str(data[k])
        cur = data.get("current")
        if isinstance(cur, dict):
            t = cur.get("today")
            if isinstance(t, dict) and t.get("date"):
                return str(t["date"])
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            for k in ("game_time", "game_date", "date", "slate_date"):
                v = first.get(k)
                if v:
                    m = re.search(r"\d{4}-\d{2}-\d{2}", str(v))
                    return m.group(0) if m else str(v)
    return "unknown"


def copy_matching_side_files(src_json: Path, dest_stem_paths: List[Path], dry_run: bool) -> int:
    copied = 0
    for ext in [".txt", ".pdf", ".csv"]:
        side = src_json.with_suffix(ext)
        if not side.exists():
            continue
        for stem_path in dest_stem_paths:
            if safe_copy(side, stem_path.with_suffix(ext), dry_run):
                copied += 1
    return copied


def step_today(files: List[Path], data_dir: Path, today_date: dt.date, dry_run: bool) -> Dict[str, Any]:
    src, date_s = pick_today(files, today_date)
    if not src:
        print("  ⚠️  No today slate JSON found.")
        return {"found": False, "date": "", "source": ""}
    print(f"  ✅ Today slate: {src.name} ({date_s})")

    dests = [
        data_dir / "today.json",
        data_dir / "current" / "today.json",
        data_dir / "archive" / f"slate_{date_s}.json",
        data_dir / f"{date_s}.json",  # legacy root date file
    ]
    for d in dests:
        safe_copy(src, d, dry_run)
    copy_matching_side_files(src, [d.with_suffix("") for d in dests], dry_run)
    return {"found": True, "date": date_s, "source": src.name}


def step_tomorrow(files: List[Path], data_dir: Path, today_date: dt.date, tomorrow_date: dt.date, dry_run: bool) -> Dict[str, Any]:
    src, date_s = pick_tomorrow(files, today_date, tomorrow_date)
    if not src:
        print("  ⚠️  No valid future tomorrow slate JSON found. Keeping existing tomorrow.json if present.")
        return {"found": False, "date": "", "source": ""}
    print(f"  ✅ Tomorrow slate: {src.name} ({date_s})")

    dests = [
        data_dir / "tomorrow.json",
        data_dir / "current" / "tomorrow.json",
        data_dir / "archive" / f"tomorrow_{date_s}.json",
    ]
    for d in dests:
        safe_copy(src, d, dry_run)
    copy_matching_side_files(src, [d.with_suffix("") for d in dests], dry_run)
    return {"found": True, "date": date_s, "source": src.name}


def step_results(files: List[Path], data_dir: Path, dry_run: bool) -> Dict[str, Any]:
    results_dir = data_dir / "results"
    by_date = find_results(files)
    if not by_date:
        print("  ⚠️  No results JSON found.")
        return {"found": False, "count": 0, "newest_date": ""}

    copied = 0
    for date_s, (src, rank) in sorted(by_date.items(), reverse=True):
        canonical = results_dir / f"mlb_results_live_{date_s}.json"
        alias = results_dir / f"live_graded_results_{date_s}.json"
        if safe_copy(src, canonical, dry_run):
            copied += 1
            print(f"  ✅ Results {date_s}: {src.name} → results/{canonical.name}")
        safe_copy(src, alias, dry_run)
        copy_matching_side_files(src, [canonical.with_suffix(""), alias.with_suffix("")], dry_run)

    newest = max(by_date.keys())
    newest_src = results_dir / f"mlb_results_live_{newest}.json"
    if not newest_src.exists():
        newest_src = by_date[newest][0]

    live_dests = [
        data_dir / "results_live.json",
        data_dir / "current" / "results_live.json",
        results_dir / "results_live.json",
    ]
    for d in live_dests:
        safe_copy(newest_src, d, dry_run)
    copy_matching_side_files(by_date[newest][0], [d.with_suffix("") for d in live_dests], dry_run)
    print(f"  ✅ results_live.json → {newest}")
    return {"found": True, "count": copied, "newest_date": newest}


def step_results_index(data_dir: Path, dry_run: bool) -> List[Dict[str, Any]]:
    results_dir = data_dir / "results"
    by_date: Dict[str, Tuple[Path, int]] = {}
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            if f.name in {"index.json", "results_live.json"}:
                continue
            for rank, pat in RESULTS_PATTERNS_RANKED:
                m = re.search(pat, f.name, re.IGNORECASE)
                if not m:
                    continue
                date_s = m.group(1)
                old = by_date.get(date_s)
                if old is None or rank > old[1]:
                    by_date[date_s] = (f, rank)
                break

    entries = []
    for date_s in sorted(by_date.keys(), reverse=True):
        f, _ = by_date[date_s]
        entries.append({
            "date": date_s,
            "file": f.name,
            "path": f"/data/results/{f.name}",
            "label": f"{month_label(date_s)} Results",
        })
    write_json(results_dir / "index.json", entries, dry_run)
    print(f"  ✅ results/index.json → {len(entries)} entries")
    return entries


def step_slates_index(data_dir: Path, dry_run: bool) -> List[Dict[str, Any]]:
    archive_dir = data_dir / "archive"
    entries = []
    if archive_dir.exists():
        for f in archive_dir.glob("slate_*.json"):
            m = re.search(r"slate_(\d{4}-\d{2}-\d{2})\.json$", f.name)
            if not m:
                continue
            date_s = m.group(1)
            entries.append({
                "date": date_s,
                "file": f.name,
                "path": f"/data/archive/{f.name}",
                "label": f"{month_label(date_s)} Slate",
            })
    entries.sort(key=lambda x: x["date"], reverse=True)
    write_json(data_dir / "slates.json", entries, dry_run)
    print(f"  ✅ slates.json → {len(entries)} entries")
    return entries


def step_main_index(data_dir: Path, today_info: Dict[str, Any], tomorrow_info: Dict[str, Any], results_entries: List[Dict[str, Any]], slate_entries: List[Dict[str, Any]], dry_run: bool) -> None:
    current: Dict[str, Any] = {}
    if today_info.get("found"):
        current["today"] = {"date": today_info["date"], "path": "/data/today.json", "source": today_info["source"]}
    if tomorrow_info.get("found"):
        current["tomorrow"] = {"date": tomorrow_info["date"], "path": "/data/tomorrow.json", "source": tomorrow_info["source"]}

    root_files = []
    if data_dir.exists():
        root_files = sorted([f.name for f in data_dir.glob("*.json") if f.name != "index.json"], reverse=True)

    index = {
        "today": "/data/today.json",
        "tomorrow": "/data/tomorrow.json",
        "results_live": "/data/results_live.json",
        "results_index": "/data/results/index.json",
        "slates_index": "/data/slates.json",
        "current_today": "/data/current/today.json",
        "current_tomorrow": "/data/current/tomorrow.json",
        "current_results_live": "/data/current/results_live.json",
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "current": current,
        "history": [{"date": s["date"], "slate": s["path"], "label": s["label"]} for s in slate_entries],
        "results": [r["path"] for r in results_entries[:60]],
        "slates": [s["path"] for s in slate_entries[:60]],
        "files": root_files,
    }
    write_json(data_dir / "index.json", index, dry_run)
    print("  ✅ index.json rebuilt")


def step_pair_history(out_dirs: List[Path], data_dir: Path, dry_run: bool) -> Dict[str, Any]:
    """Copy stable-named pair history files (cache + summary) into public/data."""
    found_any = False
    copied = 0
    details: Dict[str, str] = {}
    for fname in PAIR_HISTORY_FILES:
        # Find newest matching file across all output dirs (incl. repo root for safety).
        candidates: List[Path] = []
        for d in out_dirs:
            p = d / fname
            if p.exists():
                candidates.append(p)
        # Also check repo root / bots root, since pair_history_cache.py writes root aliases there.
        for extra in [Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent, Path.cwd()]:
            p = extra / fname
            if p.exists() and p not in candidates:
                candidates.append(p)
        if not candidates:
            print(f"  ⚠️  No {fname} found in any output dir.")
            continue
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        src = candidates[0]
        dest = data_dir / fname
        if safe_copy(src, dest, dry_run):
            copied += 1
            found_any = True
            details[fname] = src.name
            print(f"  ✅ pair_history: {src} → {dest}")
    return {"found": found_any, "copied": copied, "files": details}


def step_pitch_files(out_dirs: List[Path], data_dir: Path, dry_run: bool) -> Dict[str, int]:
    pitch_dir = data_dir / "pitch"
    if not dry_run:
        pitch_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for d in out_dirs:
        src_pitch = d / "pitch"
        if not src_pitch.exists():
            continue
        for f in src_pitch.glob("*.json"):
            if safe_copy(f, pitch_dir / f.name, dry_run):
                copied += 1
    existing = list(pitch_dir.glob("*.json")) if pitch_dir.exists() else []
    batter = sum(1 for f in existing if f.name.startswith("batter_"))
    pitcher = sum(1 for f in existing if f.name.startswith("pitcher_"))
    print(f"  ✅ pitch/ → {batter} batter files | {pitcher} pitcher files | {copied} copied")
    return {"batter": batter, "pitcher": pitcher, "copied": copied}


def remove_ds_store(root: Path, dry_run: bool) -> int:
    if not root.exists():
        return 0
    count = 0
    for p in root.rglob(".DS_Store"):
        count += 1
        if dry_run:
            print(f"  [DRY] remove {p}")
        else:
            try:
                p.unlink()
            except Exception:
                pass
    return count


def run_git(repo: Path, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def git_push(repo: Path, message: str, dry_run: bool) -> None:
    if dry_run:
        print("  [DRY] git pull/add/commit/push skipped")
        return
    if not (repo / ".git").exists():
        print("  ⚠️  No .git folder found. Skipping push.")
        return

    print("  🔄 git pull --rebase origin main")
    pull = run_git(repo, ["pull", "--rebase", "origin", "main"])
    if pull.returncode != 0:
        print("  ⚠️  Pull/rebase failed. Resolve this before pushing:")
        if pull.stdout.strip():
            print(pull.stdout.strip())
        if pull.stderr.strip():
            print(pull.stderr.strip())
        return
    print("  ✅ Pull/rebase OK")

    run_git(repo, ["add", "public/data"])
    status = run_git(repo, ["status", "--porcelain"])
    if not status.stdout.strip():
        print("  ✅ Nothing to commit — data already up to date.")
        # Still push in case local commits already exist
        push = run_git(repo, ["push", "origin", "main"])
        if push.returncode == 0:
            print("  ✅ Push OK")
        elif push.stderr.strip():
            print("  ⚠️  Push message:", push.stderr.strip())
        return

    commit = run_git(repo, ["commit", "-m", message])
    if commit.returncode != 0:
        # This can happen if only ignored files or no changes.
        print("  ⚠️  Commit message:")
        print((commit.stdout + commit.stderr).strip())

    print("  🚀 git push origin main")
    push = run_git(repo, ["push", "origin", "main"])
    if push.returncode == 0:
        print("  ✅ Pushed to GitHub. Vercel should redeploy.")
    else:
        print("  ❌ Push failed:")
        print((push.stdout + push.stderr).strip())


def print_verify(data_dir: Path) -> None:
    print("\n🔎 Quick verify")
    for rel in ["today.json", "tomorrow.json", "results_live.json", "index.json", "results/index.json", "slates.json", "pair_history_cache.json", "pair_history_summary.json"]:
        p = data_dir / rel
        if p.exists():
            print(f"  ✅ public/data/{rel} | date hint: {data_date_hint(p)}")
        else:
            print(f"  ⚠️  missing public/data/{rel}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync MLB bot output files to website public/data and optionally push.")
    parser.add_argument("--repo-dir", default="", help="Website repo root. Recommended: Documents/GitHub/MLB HR MODEL")
    parser.add_argument("--outputs-dir", default="", help="Bot outputs folder")
    parser.add_argument("--today-date", default="", help="Active today date YYYY-MM-DD")
    parser.add_argument("--tomorrow-date", default="", help="Active tomorrow date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files")
    parser.add_argument("--push", "--all", action="store_true", help="Commit and push public/data after syncing")
    parser.add_argument("--no-push", action="store_true", help="Force no push even if --push is set")
    args = parser.parse_args()

    today = parse_date(args.today_date, dt.date.today())
    tomorrow = parse_date(args.tomorrow_date, today + dt.timedelta(days=1))

    repo = find_repo_root(args.repo_dir)
    data_dir = repo / "public" / "data"
    out_dirs = output_dirs(args.outputs_dir, repo)

    print("=" * 70)
    print("MLB WEBSITE DATA SYNC V2")
    print("=" * 70)
    print(f"Repo:      {repo}")
    print(f"Data dir:  {data_dir}")
    print(f"Today:     {today.isoformat()}")
    print(f"Tomorrow:  {tomorrow.isoformat()}")
    print("Output dirs checked:")
    for d in out_dirs:
        print(f"  {'✅' if d.exists() else '—'} {d}")
    if args.dry_run:
        print("\nDRY RUN — no writes will happen")

    files = all_output_files(out_dirs)
    print(f"\n📂 Found {len(files)} JSON output file(s)")
    if not files:
        print("  ⚠️  No date-stamped slate/results JSON found. Will still try pair_history copy.")

    print("\n1️⃣  Today slate")
    today_info = step_today(files, data_dir, today, args.dry_run)

    print("\n2️⃣  Tomorrow slate")
    tomorrow_info = step_tomorrow(files, data_dir, today, tomorrow, args.dry_run)

    print("\n3️⃣  Results / grades")
    results_info = step_results(files, data_dir, args.dry_run)

    print("\n4️⃣  Results index")
    results_entries = step_results_index(data_dir, args.dry_run)

    print("\n5️⃣  Slates index")
    slate_entries = step_slates_index(data_dir, args.dry_run)

    print("\n6️⃣  Main index")
    step_main_index(data_dir, today_info, tomorrow_info, results_entries, slate_entries, args.dry_run)

    print("\n7️⃣  Pitch files")
    pitch_info = step_pitch_files(out_dirs, data_dir, args.dry_run)

    print("\n7️⃣b  Pair history")
    pair_history_info = step_pair_history(out_dirs, data_dir, args.dry_run)

    print("\n8️⃣  Clean .DS_Store")
    removed = remove_ds_store(repo / "public", args.dry_run)
    print(f"  ✅ removed {removed} .DS_Store file(s)")

    print_verify(data_dir)

    print("\n" + "=" * 70)
    print("SYNC SUMMARY")
    print("=" * 70)
    print(f"  Today:     {today_info.get('date') or 'not found'}  {today_info.get('source') or ''}")
    print(f"  Tomorrow:  {tomorrow_info.get('date') or 'not found'}  {tomorrow_info.get('source') or ''}")
    print(f"  Results:   {results_info.get('count', 0)} copied | newest {results_info.get('newest_date') or 'none'}")
    print(f"  Indexes:   {len(results_entries)} result dates | {len(slate_entries)} slate dates")
    print(f"  Pitch:     {pitch_info['batter']} batter | {pitch_info['pitcher']} pitcher")
    pair_files = pair_history_info.get("files") or {}
    if pair_files:
        print(f"  Pair hist: {pair_history_info.get('copied', 0)} copied — {', '.join(pair_files.keys())}")
    else:
        print(f"  Pair hist: 0 copied")

    if args.push and not args.no_push:
        print("\n9️⃣  Git push")
        git_push(repo, f"sync data {today.isoformat()}", args.dry_run)
    else:
        print("\nNo push requested. To deploy, run with --push or commit/push manually.")

    print("\n✅ Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
