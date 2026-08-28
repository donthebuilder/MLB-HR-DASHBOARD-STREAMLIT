#!/usr/bin/env python3
"""Merge recoverable historical graded_results_<date>.json/.txt files into a
copy of the current `data` branch's public/data tree, then regenerate
backtest_summary.json over the combined set.

Used by SHIP-ARCHIVE-BACKFILL.sh -- see that script for the git orchestration
(fetch, carry-forward extraction, orphan commit, push-with-lease). This file
does none of the git work itself; it only reads/writes plain files under
--data-checkout and --out, so it is safe to test against throwaway
directories before anything touches a real branch.

Never overwrites a date that's already present in the carried-forward tree
(the live branch's own graded nights always win), and never publishes a
.txt for a date it isn't also publishing a .json for -- that combination is
exactly what used to make the "Past nights" picker offer a date the site
then failed to load (Results.js: an entry in backtest_summary.per_day with
no matching graded_results_<date>.json behind it).

IMPORTANT SCHEMA NOTE (found while building this, not a guess): the bot
repo's own `trim_row()`/`SLOT_FIELDS` in bots/live_results_tracker.py is
NOT what shrinks historical rows down to the public shape -- that function
serves a different, unrelated "tracking_slots" list and its whitelist is
missing fields the site actually needs (pick_type, actual_ab/hr/hits/tb,
got_hr, got_base_hit, is_final, rank, ...). Verified directly against a
currently-published file (graded_results_2026-07-27.json): its rows have
58 fields, and running them through trim_row() drops 16 of those,
including pick_type and every actual_*/got_* outcome field -- exactly the
fields a night's grade/outcome depend on. Using trim_row() here would have
silently shipped archive nights that render with blank grades.

Historical raw files instead turn out to be strict supersets of today's
public field set (confirmed for the two dict-shaped raw samples used to
build this) plus, for older dates, missing a handful of fields that
simply didn't exist yet (e.g. `game_pick_role`, added 2026-08-08) -- which
is the correct, honest outcome for an old night, not a bug to paper over.
So instead of a hardcoded whitelist, this script derives the real public
schema empirically from the carried-forward live files themselves (whatever
fields real player-row dicts on the live branch actually have, unioned
across every live file), then projects every recovered row -- and every
nested player-row dict inside merged_homers/pair_pool_results, wherever
one turns up -- onto that same set. "results" (the frontend-ready array
with grade/bet_type/outcome_text) is not trusted from the old file at all;
it's rebuilt from the trimmed graded_slots using the exact three functions
main() in live_results_tracker.py uses to build it today, copied verbatim
below, so a recovered night's outcome labels are computed the same way a
live one's are, not carried forward from a possibly-older labeling rule.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

GRADED_JSON_RE = "graded_results_????-??-??.json"
GRADED_TXT_RE = "graded_results_????-??-??.txt"


def date_of(path: Path) -> str:
    return path.stem.replace("graded_results_", "")


def collect_schema_keys(out_current: Path) -> set:
    """Union of every key ever seen on a real player-row dict (anything with
    a player_id) across the carried-forward live files. This is today's
    actual public schema, derived from the files themselves rather than
    hardcoded, so it stays correct as the live shape evolves."""
    keys = set()

    def walk(node):
        if isinstance(node, dict):
            if "player_id" in node:
                keys.update(node.keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for f in out_current.glob(GRADED_JSON_RE):
        try:
            walk(json.loads(f.read_text()))
        except Exception as e:
            print(f"  (schema scan) skipping {f.name}: {e}")
    return keys


def deep_project(node, schema_keys: set):
    """Recursively project every player-row dict (anything with a
    player_id) onto schema_keys, in place structurally (returns a new
    tree; does not mutate the input)."""
    if isinstance(node, dict):
        if "player_id" in node:
            node = {k: v for k, v in node.items() if k in schema_keys}
        return {k: deep_project(v, schema_keys) for k, v in node.items()}
    if isinstance(node, list):
        return [deep_project(v, schema_keys) for v in node]
    return node


# ── ported verbatim from live_results_tracker.py's main() (lines ~3673-3716,
# 2026-08-28) so a recovered night's grade/bet_type/outcome_text are computed
# the same way a live night's are, rather than trusting whatever an old raw
# file happened to store under "results". ──────────────────────────────────
def _grade_for_row(r, live_mode=False):
    if not live_mode:
        if int(r.get("got_hr", 0)) == 1:
            return "WIN"
        if int(r.get("got_base_hit", 0)) == 1 and r.get("pick_type") in ("HIT", "HRR", "CONTACT", "TOP", "TOP15"):
            return "WIN"
        if int(r.get("actual_ab", 0)) > 0:
            return "LOSS"
        return "DNP"
    if int(r.get("got_hr", 0)) == 1:
        return "HIT"
    if int(r.get("got_base_hit", 0)) == 1:
        return "HIT"
    if int(r.get("actual_ab", 0)) > 0:
        return "LIVE"
    return "PENDING"


def _bet_for_row(r):
    pt = (r.get("pick_type") or "").upper()
    return {
        "HR": "HR", "TOP": "TOP", "TOP15": "TOP15",
        "HIT": "HIT", "HRR": "HRR", "CONTACT": "TB",
    }.get(pt, pt or "PICK")


def _outcome_text(r):
    ab = int(r.get("actual_ab", 0))
    hits = int(r.get("actual_hits", 0))
    hr = int(r.get("actual_hr", 0))
    tb = int(r.get("actual_tb", 0))
    rbi = int(r.get("actual_rbi", 0))
    runs = int(r.get("actual_runs", 0))
    if ab == 0 and hits == 0 and hr == 0:
        return "Game not started"
    line = f"{hits}/{ab}"
    extras = []
    if hr:
        extras.append(f"{hr} HR")
    if tb:
        extras.append(f"{tb} TB")
    if rbi:
        extras.append(f"{rbi} RBI")
    if runs:
        extras.append(f"{runs} R")
    if extras:
        line += " · " + ", ".join(extras)
    return line


def rebuild_results(graded_slots, live_mode=False):
    return [
        {**slot, "grade": _grade_for_row(slot, live_mode),
         "bet_type": _bet_for_row(slot), "outcome_text": _outcome_text(slot)}
        for slot in graded_slots if isinstance(slot, dict)
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive-source", required=True,
                     help="dir holding the raw local graded_results_<date>.json "
                          "(e.g. ~/Desktop/results/DATA)")
    ap.add_argument("--archive-txt-source", required=True,
                     help="dir (searched recursively) holding graded_results_<date>.txt")
    ap.add_argument("--data-checkout", required=True,
                     help="a directory already holding the current data branch's "
                          "public/data/current, extracted via git archive")
    ap.add_argument("--out", required=True,
                     help="output dir -- will contain the new public/data/current")
    ap.add_argument("--repo-root", default=None,
                     help="bot-ship repo root, to find bots/backtest_report.py. "
                          "Defaults to this script's own directory (works when this "
                          "file lives inside the repo; SHIP-ARCHIVE-BACKFILL.sh runs "
                          "it from a standalone /tmp copy and always passes this.)")
    ap.add_argument("--before", default=None,
                     help="only recover dates strictly before this (YYYY-MM-DD). "
                          "Defaults to the earliest date already on the live branch, "
                          "so this never has to be hand-updated as the branch grows.")
    ap.add_argument("--graded-keep", type=int, default=150)
    args = ap.parse_args()

    src_json = Path(os.path.expanduser(args.archive_source))
    src_txt_root = Path(os.path.expanduser(args.archive_txt_source))
    checkout = Path(os.path.expanduser(args.data_checkout))
    out = Path(os.path.expanduser(args.out))

    out_current = out / "current"
    if out_current.exists():
        shutil.rmtree(out_current)
    checkout_current = checkout / "current"
    if checkout_current.exists():
        shutil.copytree(checkout_current, out_current)
    else:
        out_current.mkdir(parents=True)

    existing_dates = {date_of(p) for p in out_current.glob(GRADED_JSON_RE)}
    print(f"carried forward {len(existing_dates)} existing graded nights from the live branch")

    before = args.before or (min(existing_dates) if existing_dates else None)
    if not before:
        print("::error:: no --before given and no existing graded nights to infer it from")
        sys.exit(1)
    if not args.before:
        print(f"--before not given; inferred {before} (earliest night already on the live branch)")

    schema_keys = collect_schema_keys(out_current)
    print(f"derived live public schema: {len(schema_keys)} player-row fields "
          f"(from {len(existing_dates)} carried-forward files)")
    if len(schema_keys) < 20:
        print("::error:: derived schema looks too small to be real -- refusing to trim "
              "against it (would silently gut every recovered row). Aborting.")
        sys.exit(1)

    # ── recover JSON, projected onto today's real public schema ────────────
    recovered_json = []
    for f in sorted(src_json.glob(GRADED_JSON_RE)):
        d = date_of(f)
        if d >= before or d in existing_dates:
            continue
        try:
            with open(f) as fh:
                data = json.load(fh)
        except Exception as e:
            print(f"  SKIP {f.name}: read/parse error: {e}")
            continue
        data = deep_project(data, schema_keys)
        if isinstance(data, dict) and isinstance(data.get("graded_slots"), list):
            data["results"] = rebuild_results(data["graded_slots"], bool(data.get("live_mode")))
        out_path = out_current / f.name
        with open(out_path, "w") as fh:
            json.dump(data, fh, separators=(",", ":"))
        recovered_json.append(d)
    print(f"recovered {len(recovered_json)} historical json nights: "
          f"{recovered_json[0] if recovered_json else '-'} .. {recovered_json[-1] if recovered_json else '-'}")

    # ── recover matching .txt, ONLY for dates we just published a .json for ─
    # (existing live-branch .txt files were already carried forward above.)
    txt_by_date = {}
    for f in src_txt_root.rglob("graded_results_????-??-??.txt"):
        if "copy" in f.name.lower():
            continue
        txt_by_date.setdefault(date_of(f), f)

    recovered_txt = []
    all_json_dates = existing_dates | set(recovered_json)
    for d in recovered_json:
        f = txt_by_date.get(d)
        if not f:
            continue  # no txt for this date -- fine, json alone still works via direct fetch
        shutil.copy(f, out_current / f"graded_results_{d}.txt")
        recovered_txt.append(d)
    print(f"recovered {len(recovered_txt)} matching .txt files for backtest_report.py")

    # ── GRADED_KEEP trim (safety net -- should be a no-op at this scale) ────
    for pattern, keep in ((GRADED_JSON_RE, args.graded_keep), (GRADED_TXT_RE, args.graded_keep)):
        files = sorted(out_current.glob(pattern))
        if len(files) > keep:
            drop = files[: len(files) - keep]
            for f in drop:
                f.unlink()
            print(f"trimmed {len(drop)} old {pattern} file(s), keeping {keep}")

    # ── regenerate backtest_summary.json over the FULL combined .txt set ───
    repo_root = Path(os.path.expanduser(args.repo_root)) if args.repo_root else Path(__file__).parent
    backtest_script = repo_root / "bots" / "backtest_report.py"
    if not backtest_script.exists():
        print(f"::error:: can't find bots/backtest_report.py under {repo_root} "
              f"(pass --repo-root pointing at the bot-ship repo)")
        sys.exit(1)
    subprocess.run(
        [sys.executable, str(backtest_script), "--dir", str(out_current), "--out-dir", str(out_current)],
        check=True, stdout=subprocess.DEVNULL,
    )
    summary = json.loads((out_current / "backtest_summary.json").read_text())
    per_day = summary.get("per_day", {})
    dates = sorted(per_day.keys())
    print(f"\nbacktest_summary.json regenerated: {len(dates)} days in per_day, "
          f"{dates[0] if dates else '-'} .. {dates[-1] if dates else '-'}")

    # ── auto-heal: drop any per_day date with no graded_results json behind it ──
    # backtest_report.py builds per_day from whatever .txt files are on disk,
    # with no awareness of whether a matching .json exists. That mismatch is
    # exactly what makes the "Past nights" picker offer a date the site then
    # can't load -- so this isn't a hypothetical to guard against, it's a
    # pre-existing gap already live on the current data branch (found while
    # testing this script: 2026-07-26 has a .txt but no .json today). Fix it
    # here rather than just refusing to add to it.
    missing_json = [d for d in dates if not (out_current / f"graded_results_{d}.json").exists()]
    if missing_json:
        print(f"healing {len(missing_json)} pre-existing dead-link date(s) with no "
              f"graded_results json behind them: {missing_json}")
        for d in missing_json:
            del per_day[d]
        summary["per_day"] = per_day
        with open(out_current / "backtest_summary.json", "w") as fh:
            json.dump(summary, fh, separators=(",", ":"))
        dates = sorted(per_day.keys())
        print(f"backtest_summary.json re-written: {len(dates)} days in per_day after healing, "
              f"{dates[0] if dates else '-'} .. {dates[-1] if dates else '-'}")

    still_missing = [d for d in dates if not (out_current / f"graded_results_{d}.json").exists()]
    if still_missing:
        print(f"::error:: healing pass failed, {len(still_missing)} date(s) still dangling: {still_missing}")
        sys.exit(1)
    print("verified: every per_day date has a matching graded_results_<date>.json on disk")


if __name__ == "__main__":
    main()

