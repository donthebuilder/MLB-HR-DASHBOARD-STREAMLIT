"""
backtest_report.py

Standalone backtest analysis script for MLB-HR-DASHBOARD.

Parses one or more graded_results_YYYY-MM-DD.txt files from public/data/
and produces a tier-segmented report comparing HR-hit-rate across pick
categories (HR Picks, HRR Picks, Hit Picks, Contact Picks, Top Picks),
instead of just looking at aggregate accuracy.

This exists because aggregate "Best HR-producing category" lines in each
daily file only tell you about ONE day. This script aggregates across
however many graded_results files you point it at, so you can see whether
a tier inversion (e.g. Hit Picks outperforming HR Picks) is a real pattern
or single-day variance.

USAGE:
    python backtest_report.py                       # uses all files in public/data/
    python backtest_report.py --dir public/data      # explicit dir
    python backtest_report.py --files a.txt b.txt    # explicit file list
    python backtest_report.py --since 2026-06-13     # only files dated >= this

OUTPUT:
    Prints a per-tier summary table to stdout, plus a day-by-day breakdown.
    Also writes backtest_summary.json to the same directory as this script
    (or --out-dir if given) so a GitHub Action can pick it up as an artifact.
"""

import argparse
import glob
import io
import json
import os
import re
import sys
from collections import defaultdict

# Maps the emoji/label prefix used in each day's "HR CATEGORY BREAKDOWN"
# section to a normalized tier name. Add new emoji here if new tiers appear.
TIER_LABELS = {
    "🏆": "TOP_15_BOARD",
    "🔥": "TOP_PICKS",
    "🧨": "HR_PICKS",
    "🏁": "HRR_PICKS",
    "💠": "HIT_PICKS",
    "⚾": "CONTACT_PICKS",
}

# Regex for lines like: "🧨 HR PICKS → 2 HR"
CATEGORY_BREAKDOWN_RE = re.compile(
    r"^([\U0001F300-\U0001FAFF\u2600-\u27BF])\s+([A-Z0-9+\s]+?)\s*→\s*(\d+)\s*HR",
    re.UNICODE,
)

# Regex for lines like: "🏁 HRR PICKS (28)" header before a hit-rate detail line
TIER_COUNT_HEADER_RE = re.compile(
    r"^([\U0001F300-\U0001FAFF\u2600-\u27BF])\s+([A-Z0-9+\s]+?)\s*\((\d+)\)\s*$",
    re.UNICODE,
)

# Regex to pull ANY "LABEL: NN.N%" segment off a detail line, e.g.
# "1+ Hit: 60.7% | HR: 21.4%"            -> {"1+ Hit": 60.7, "HR": 21.4}
# "2+ HRR: 60.7% | 3+ HRR: 53.6%"        -> {"2+ HRR": 60.7, "3+ HRR": 53.6}
# "XBH: 35.7% | 2+ TB: 35.7% | HR: 21.4%" -> all three captured
# This replaces the old HR-only regex so HRR/Hit/Contact tiers no longer
# lose their primary metrics (2+/3+ HRR, 1+ Hit, XBH, 2+ TB) just because
# the old version only looked for a trailing "HR:" segment.
METRIC_PCT_RE = re.compile(r"([A-Za-z0-9+ ]+?):\s*([\d.]+)%")

# Regex for the standalone overall accuracy line, e.g.:
#   "Base Hit Accuracy: 64.6%"
# Lives under "FULL SHEET BASE HIT PERFORMANCE" -- this is a single
# sheet-wide number, not broken out per tier, so it's stored separately
# from the per-tier metrics dict.
BASE_HIT_ACCURACY_RE = re.compile(r"^Base Hit Accuracy:\s*([\d.]+)%")

# Regex for the top "BETTABLE RESULTS" section, e.g.:
#   "Top 15 HR: 4/15 (26.7%)"
#   "HR Picks: 2/14 (14.3%)"
#   "Top Picks: 2/14 (14.3%)"
# This is the ONLY place HR_PICKS / TOP_PICKS / TOP_15_BOARD pool sizes
# are recorded in the current file format, so it's parsed separately
# from the PLAYER TYPE PERFORMANCE section (which only covers
# HRR/Hit/Contact tiers as of 2026-06-19's format).
BETTABLE_LINE_RE = re.compile(
    r"^(Top 15 HR|HR Picks|Top Picks):\s*(\d+)/(\d+)\s*\(([\d.]+)%\)"
)

BETTABLE_LABEL_TO_TIER = {
    "Top 15 HR": "TOP_15_BOARD",
    "HR Picks": "HR_PICKS",
    "Top Picks": "TOP_PICKS",
}

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.txt$")


def find_files(args):
    if args.files:
        return sorted(args.files)
    directory = args.dir or "public/data"
    pattern = os.path.join(directory, "graded_results_*.txt")
    files = sorted(glob.glob(pattern))
    if args.since:
        files = [f for f in files if extract_date(f) and extract_date(f) >= args.since]
    return files


def extract_date(filepath):
    m = DATE_RE.search(os.path.basename(filepath))
    return m.group(1) if m else None


PICK_TYPE_TIER = {
    "TOP15": "TOP_15_BOARD",
    "TOP": "TOP_PICKS",
    "HR": "HR_PICKS",
    "HRR": "HRR_PICKS",
    "HIT": "HIT_PICKS",
    "CONTACT": "CONTACT_PICKS",
}


def parse_json_file(filepath):
    """
    Reads the graded_results_*.json sibling instead of scraping the .txt.

    The text report only ever printed the one or two metrics each tier was
    "about", so the all-time table could never show HR / 1+ Hit / XBH / 2+ HRR
    side by side. The JSON carries every outcome flag on every graded pick, so
    we compute all of them for all six tiers -- including for days whose text
    report predates the uniform metric block.

    Returns the same shape parse_file does, or None if unusable.
    """
    try:
        with io.open(filepath, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None

    payload = payload if isinstance(payload, dict) else {}   # shape guard, see bots/check_shapes.py
    rows = payload.get("graded_slots") or []
    if not rows:
        return None

    def pct(group, key):
        if not group:
            return None
        return round(sum(1 for r in group if r.get(key)) * 100.0 / len(group), 1)

    grouped = defaultdict(list)
    for row in rows:
        tier = PICK_TYPE_TIER.get(row.get("pick_type"))
        if tier:
            grouped[tier].append(row)

    tiers = {}
    for tier, group in grouped.items():
        # tb_2_plus was added to the tracker partway through; fall back to the
        # raw total-bases count so older slates still report it.
        for row in group:
            if row.get("tb_2_plus") is None:
                row["tb_2_plus"] = (row.get("actual_tb") or 0) >= 2
            # Slates graded before the got_xbh fix stored `tb >= 2` under that
            # key, so recompute from the raw counts whenever both are present.
            hits, tb = row.get("actual_hits"), row.get("actual_tb")
            if hits is not None and tb is not None:
                row["got_xbh"] = tb > hits
        # 1+ HRR isn't stored as a flag -- it's any hit, run or RBI at all,
        # which is hrr_total >= 1.
        for row in group:
            if row.get("hrr_1_plus") is None:
                row["hrr_1_plus"] = (row.get("hrr_total") or 0) >= 1
        fields = (
            ("HR", "got_hr"),
            ("1+ Hit", "got_base_hit"),
            ("XBH", "got_xbh"),
            ("2+ TB", "tb_2_plus"),
            ("1+ HRR", "hrr_1_plus"),
            ("2+ HRR", "hrr_2_plus"),
            ("3+ HRR", "hrr_3_plus"),
            # "Did its job" -- graded against what the pick was FOR, not
            # against HR. A HIT pick that singles did its job; grading it on
            # HR calls a working pick a miss.
            ("Did its job", "designed_hit"),
        )
        tiers[tier] = {
            "hr_count": sum(1 for r in group if r.get("got_hr")),
            "pool_size": len(group),
            "metrics": {
                name: value
                for name, key in fields
                for value in (pct(group, key),)
                if value is not None
            },
            # Raw numerator/denominator per metric so the aggregate can pool
            # across days instead of averaging daily percentages -- a 30-pick
            # slate and an 11-pick slate should not count the same.
            "metric_counts": {
                name: [sum(1 for r in group if r.get(key)), len(group)]
                for name, key in fields
            },
        }

    if not tiers:
        return None
    return {"tiers": tiers, "base_hit_accuracy": None}


def parse_file(filepath):
    """
    Parses a single graded_results_*.txt file.

    Returns a dict with two keys:
      "tiers": { tier_name: {
          "hr_count": int,
          "pool_size": int or None,
          "hr_rate_from_detail": float or None,   # kept for backward compatibility
          "metrics": { "1+ Hit": 60.7, "HR": 21.4, ... }   # ALL labeled %s on the detail line
      } }
      "base_hit_accuracy": float or None   # sheet-wide, not tier-specific

    pool_size and metrics come from the "PLAYER TYPE PERFORMANCE" section
    when available (more reliable than back-deriving from the category
    breakdown, which only gives raw HR counts, not pool size).
    """
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    tiers = defaultdict(lambda: {
        "hr_count": 0, "pool_size": None, "hr_rate_from_detail": None, "metrics": {}
    })
    base_hit_accuracy = None

    lines = text.splitlines()

    # Pass 1: HR CATEGORY BREAKDOWN section -> raw HR counts per tier
    for line in lines:
        m = CATEGORY_BREAKDOWN_RE.match(line.strip())
        if m:
            emoji, label, hr_count = m.groups()
            tier = TIER_LABELS.get(emoji)
            if tier:
                tiers[tier]["hr_count"] = int(hr_count)

    # Pass 1b: standalone "Base Hit Accuracy: NN.N%" line (sheet-wide, not per-tier)
    for line in lines:
        m = BASE_HIT_ACCURACY_RE.match(line.strip())
        if m:
            base_hit_accuracy = float(m.group(1))
            break

    # Pass 2: PLAYER TYPE PERFORMANCE section -> pool size + ALL metrics on
    # the following detail line(s), e.g.:
    #   🏁 HRR PICKS (28)
    #   2+ HRR: 60.7% | 3+ HRR: 53.6%
    #
    #   💠 HIT PICKS (28)
    #   1+ Hit: 60.7% | HR: 21.4%
    #
    #   ⚾ CONTACT PICKS (14)
    #   XBH: 35.7% | 2+ TB: 35.7% | HR: 21.4%
    for i, line in enumerate(lines):
        m = TIER_COUNT_HEADER_RE.match(line.strip())
        if m:
            emoji, label, pool_size = m.groups()
            tier = TIER_LABELS.get(emoji)
            if not tier:
                continue
            tiers[tier]["pool_size"] = int(pool_size)
            # scan the next couple non-empty lines for "LABEL: NN.N%" segments
            for j in range(i + 1, min(i + 3, len(lines))):
                detail_line = lines[j].strip()
                if not detail_line:
                    continue
                matches = METRIC_PCT_RE.findall(detail_line)
                if matches:
                    for raw_label, pct in matches:
                        clean_label = raw_label.strip()
                        tiers[tier]["metrics"][clean_label] = float(pct)
                    # keep hr_rate_from_detail populated for backward compatibility
                    # with the existing summary/aggregate logic
                    if "HR" in tiers[tier]["metrics"]:
                        tiers[tier]["hr_rate_from_detail"] = tiers[tier]["metrics"]["HR"]
                    break

    # Pass 3: BETTABLE RESULTS section -> pool size + rate for
    # TOP_15_BOARD / HR_PICKS / TOP_PICKS, which aren't in the
    # PLAYER TYPE PERFORMANCE section in the current file format.
    for line in lines:
        m = BETTABLE_LINE_RE.match(line.strip())
        if m:
            label, hr_count, pool_size, pct = m.groups()
            tier = BETTABLE_LABEL_TO_TIER.get(label)
            if not tier:
                continue
            # Only fill in if not already set by the category breakdown
            # pass (hr_count) -- pool_size/rate always come from here
            # since this is their sole source.
            tiers[tier]["pool_size"] = int(pool_size)
            tiers[tier]["hr_rate_from_detail"] = float(pct)
            tiers[tier]["metrics"]["HR"] = float(pct)
            if tiers[tier]["hr_count"] == 0:
                tiers[tier]["hr_count"] = int(hr_count)

    return {"tiers": dict(tiers), "base_hit_accuracy": base_hit_accuracy}


def aggregate(per_file_results):
    """
    per_file_results: { date_str: {"tiers": {tier: {...}}, "base_hit_accuracy": float|None} }

    Returns aggregated totals per tier across all files, computing an
    overall hr_rate = sum(hr_count) / sum(pool_size) when pool_size is
    available, which is more trustworthy than averaging daily percentages.

    Also averages every other named metric (1+ Hit, 2+ HRR, 3+ HRR, XBH,
    2+ TB, etc.) across the days each tier/metric appears, and averages
    the sheet-wide Base Hit Accuracy across all days that reported it.
    """
    totals = defaultdict(lambda: {"hr_count": 0, "pool_size": 0, "days_seen": 0})
    metric_sums = defaultdict(lambda: defaultdict(lambda: {"sum": 0.0, "count": 0}))
    metric_pool = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    base_hit_acc_values = []

    for date_str, day_data in per_file_results.items():
        tiers = day_data.get("tiers", {})
        for tier, data in tiers.items():
            totals[tier]["hr_count"] += data["hr_count"]
            if data["pool_size"]:
                totals[tier]["pool_size"] += data["pool_size"]
            totals[tier]["days_seen"] += 1
            for metric_name, value in data.get("metrics", {}).items():
                metric_sums[tier][metric_name]["sum"] += value
                metric_sums[tier][metric_name]["count"] += 1
            for metric_name, (num, den) in data.get("metric_counts", {}).items():
                metric_pool[tier][metric_name][0] += num
                metric_pool[tier][metric_name][1] += den

        if day_data.get("base_hit_accuracy") is not None:
            base_hit_acc_values.append(day_data["base_hit_accuracy"])

    summary = {}
    for tier, data in totals.items():
        rate = (data["hr_count"] / data["pool_size"] * 100) if data["pool_size"] else None
        avg_metrics = {
            name: round(stat["sum"] / stat["count"], 1)
            for name, stat in metric_sums[tier].items()
            if stat["count"] > 0
        }
        pooled_metrics = {
            name: round(num * 100.0 / den, 1)
            for name, (num, den) in metric_pool[tier].items()
            if den
        }
        summary[tier] = {
            "total_hr_count": data["hr_count"],
            "total_pool_size": data["pool_size"] or None,
            "hr_rate_pct": round(rate, 1) if rate is not None else None,
            "days_seen": data["days_seen"],
            # avg_metrics = mean of the daily percentages (every day counts
            # equally). pooled_metrics = all picks in one bucket (every pick
            # counts equally). Pooled is the headline; the average is kept so
            # a single lopsided slate skewing the pool is still visible.
            "avg_metrics": avg_metrics,
            "pooled_metrics": pooled_metrics,
            "metric_counts": {k: v for k, v in metric_pool[tier].items()},
        }

    overall_base_hit_accuracy = (
        round(sum(base_hit_acc_values) / len(base_hit_acc_values), 1)
        if base_hit_acc_values else None
    )

    return summary, overall_base_hit_accuracy


def print_report(per_file_results, summary, overall_base_hit_accuracy):
    print("=" * 60)
    print("BACKTEST REPORT — Tier-Segmented Performance")
    print("=" * 60)
    print(f"Days analyzed: {len(per_file_results)}")
    print(f"Date range: {min(per_file_results)} to {max(per_file_results)}" if per_file_results else "No files found.")
    if overall_base_hit_accuracy is not None:
        print(f"Avg Base Hit Accuracy (sheet-wide): {overall_base_hit_accuracy}%")
    print()

    print("-" * 60)
    print(f"{'TIER':<16}{'HR COUNT':<10}{'POOL':<8}{'HR RATE':<10}{'DAYS':<6}")
    print("-" * 60)

    # Sort tiers by hr_rate_pct descending (None goes last) so the
    # under/overperforming tiers are immediately visible at a glance.
    def sort_key(item):
        _, data = item
        return (data["hr_rate_pct"] is None, -(data["hr_rate_pct"] or 0))

    for tier, data in sorted(summary.items(), key=sort_key):
        rate_str = f"{data['hr_rate_pct']}%" if data["hr_rate_pct"] is not None else "n/a"
        pool_str = str(data["total_pool_size"]) if data["total_pool_size"] else "n/a"
        print(f"{tier:<16}{data['total_hr_count']:<10}{pool_str:<8}{rate_str:<10}{data['days_seen']:<6}")

    print()
    print("Other metrics per tier (averaged across days where reported):")
    print("-" * 60)
    for tier, data in sorted(summary.items()):
        if data["avg_metrics"]:
            metrics_str = ", ".join(f"{name}={val}%" for name, val in data["avg_metrics"].items())
            print(f"{tier:<16}{metrics_str}")

    print()
    print("Day-by-day breakdown:")
    print("-" * 60)
    for date_str in sorted(per_file_results.keys()):
        day = per_file_results[date_str]
        print(f"\n{date_str}", end="")
        if day.get("base_hit_accuracy") is not None:
            print(f"  (Base Hit Accuracy: {day['base_hit_accuracy']}%)")
        else:
            print()
        for tier, data in sorted(day.get("tiers", {}).items()):
            pool = data["pool_size"] if data["pool_size"] else "n/a"
            metrics_str = ", ".join(f"{k}={v}%" for k, v in data.get("metrics", {}).items())
            print(f"  {tier:<16} HR={data['hr_count']:<4} pool={pool:<6} {metrics_str}")


def main():
    parser = argparse.ArgumentParser(description="Tier-segmented HR backtest report")
    parser.add_argument("--dir", help="Directory to scan for graded_results_*.txt", default=None)
    parser.add_argument("--files", nargs="*", help="Explicit list of files to parse", default=None)
    parser.add_argument("--since", help="Only include files dated on/after this YYYY-MM-DD", default=None)
    parser.add_argument("--out-dir", help="Where to write backtest_summary.json", default=".")
    args = parser.parse_args()

    files = find_files(args)
    if not files:
        print("No graded_results_*.txt files found. Check --dir or --files.", file=sys.stderr)
        sys.exit(1)

    per_file_results = {}
    for filepath in files:
        date_str = extract_date(filepath) or os.path.basename(filepath)
        try:
            # Prefer the JSON sibling: it has every outcome flag on every
            # pick, so all six metrics come out for all six tiers. The .txt
            # is still parsed as a fallback (and for Base Hit Accuracy,
            # which is sheet-wide and only lives in the text report).
            parsed = parse_file(filepath)
            from_json = parse_json_file(os.path.splitext(filepath)[0] + ".json")
            if from_json:
                from_json["base_hit_accuracy"] = (parsed or {}).get("base_hit_accuracy")
                parsed = from_json
            per_file_results[date_str] = parsed
        except Exception as e:
            print(f"WARNING: failed to parse {filepath}: {e}", file=sys.stderr)

    summary, overall_base_hit_accuracy = aggregate(per_file_results)
    print_report(per_file_results, summary, overall_base_hit_accuracy)

    out_path = os.path.join(args.out_dir, "backtest_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "per_day": per_file_results,
                "summary": summary,
                "overall_base_hit_accuracy": overall_base_hit_accuracy,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
