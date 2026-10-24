#!/usr/bin/env python3
"""
backtest_v5.py -- MOONSHOT v5 (batter x matchup) against hr_score, on the same
graded rows, the same nights.

    python bots/backtest_v5.py [--dir ~/Desktop/results/DATA]

Rows are the bot's saved pre-game rows with the outcome stamped on (got_hr).
They are scored through the SAME adapter the nightly run uses
(dash_models.moonshot_inputs), so this measures the shipped code, not a copy.

Rules that keep it honest:
  * spray-log entries dated on/after the game date are dropped (no peeking)
  * only rows with a real spray log and a HR outcome are scored
  * Statcast pulls are off: pitcher LD%-top2 is the league constant for every
    row, and hard-hit% is the saved value or the league constant
  * graded files hold the bot's designated picks, not the full slate, so every
    rate is conditional on the bot already liking the hitter (same caveat as
    backtest_all.py)

Reported: HR rate in each model's top 5 / top 10 per night, rank AUC over all
scored rows, and the same numbers for hr_score on the identical rows.
"""
import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dash_models as dm            # noqa: E402
import mlb_data_fixes as fixes      # noqa: E402
import moonshot_v4_scorer as ms     # noqa: E402

fixes.fetch_pitcher_hardhit_pct = lambda *_: fixes.LEAGUE_HARDHIT
fixes.fetch_pitcher_ld_pct_top2 = lambda *_: fixes.LEAGUE_LD_TOP2
dm.mlb_data_fixes = fixes

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")


def rows_of(d):
    if isinstance(d, list):
        return d
    for k in ("graded_slots", "results", "rows"):
        if isinstance(d.get(k), list) and d[k] and isinstance(d[k][0], dict):
            return d[k]
    return []


def v5_score(row, night):
    row = dict(row)
    row["batted_ball_log"] = [
        b for b in (row.get("batted_ball_log") or []) if str(b.get("date") or "")[:10] < night
    ]
    if not row["batted_ball_log"]:
        return None
    gaps = dm.Gaps()
    b_in, m_in, hr10, hr_pg = dm.moonshot_inputs(row, int(night[:4]), gaps)
    out = ms.build_output_row(
        player_id=0, name="", team="", bats="", opponent="", pitcher_id=0, pitcher_name="",
        pitcher_hand="", park_name="", game_pk=0, batter_inputs=b_in, matchup_inputs=m_in,
        recent_hr_last10=hr10, season_hr_per_game=hr_pg, game_date=night)
    return out["final_score"]


def auc(pairs):
    """pairs = [(score, hit)] -> rank AUC, ties averaged."""
    pos = [s for s, h in pairs if h]
    neg = [s for s, h in pairs if not h]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "Desktop/results/DATA"))
    ap.add_argument("--min-night", type=int, default=8, help="skip nights with fewer scored rows")
    a = ap.parse_args()

    nights = defaultdict(list)
    for f in sorted(glob.glob(str(Path(a.dir) / "graded_results_*.json"))):
        m = DATE_RE.search(f)
        if not m:
            continue
        night = m.group(1)
        for r in rows_of(json.load(open(f))):
            if r.get("got_hr") is None or r.get("hr_score") in (None, ""):
                continue
            try:
                s = v5_score(r, night)
            except Exception:
                s = None
            if s is not None:
                nights[night].append({"v5": s, "old": float(r["hr_score"]), "hit": bool(r["got_hr"])})

    nights = {k: v for k, v in nights.items() if len(v) >= a.min_night}
    allrows = [r for v in nights.values() for r in v]
    if not allrows:
        print("no scorable nights found")
        return 1
    base = sum(r["hit"] for r in allrows) / len(allrows)
    print(f"{len(nights)} nights, {len(allrows)} scored rows, base HR rate {100*base:.1f}%\n")
    print(f"{'':14}{'v5 (new)':>12}{'hr_score (old)':>18}")
    for n in (5, 10):
        res = {}
        for key in ("v5", "old"):
            hit = tot = 0
            for v in nights.values():
                top = sorted(v, key=lambda r: r[key], reverse=True)[:n]
                hit += sum(r["hit"] for r in top)
                tot += len(top)
            res[key] = 100 * hit / tot
        print(f"top {n:<2}/night  {res['v5']:>10.1f}%{res['old']:>17.1f}%")
    print(f"{'AUC':14}{auc([(r['v5'], r['hit']) for r in allrows]):>12.3f}{auc([(r['old'], r['hit']) for r in allrows]):>18.3f}")
    wins = ties = 0
    for v in nights.values():
        t = {k: sum(r["hit"] for r in sorted(v, key=lambda r: r[k], reverse=True)[:5]) for k in ("v5", "old")}
        wins += t["v5"] > t["old"]
        ties += t["v5"] == t["old"]
    print(f"\nnights v5 caught more HR in its top 5: {wins} of {len(nights)} (tied {ties})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
