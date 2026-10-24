#!/usr/bin/env python3
"""
miss_autopsy.py -- how the called picks actually did, why the misses missed, and which score tiers hold up.

    python bots/miss_autopsy.py --dir <graded dir> [--dir <more>]

Three sections, all from the graded files (the bot's pregame board with the outcome stamped on):
  1. RESULTS   every role's hit rate, how many picks never got to play, and first half vs second half of the run
  2. TIERS     hit rate by score quintile and by lineup spot -- does a higher score or a higher spot buy accuracy?
  3. AUTOPSY   each missed pick sorted by CAUSE, so we can see how many misses were knowable before the game:
                 never played        0 AB and 0 BB   (the pick was voided, but it cost a slot)
                 lost playing time   1-2 AB          (pinch-hit, pulled, or walked around)
                 lineup unconfirmed  the lineup was not posted when the board locked
                 full opportunity    3+ AB and still missed -- variance until proven otherwise
              "Knowable" = the first three: a lineup check at lock or a later lock would have caught them.
"""
import argparse
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
ROLES = {                     # role -> (outcome, label)
    "HR":      (lambda r: bool(r.get("got_hr")),                "1+ HR"),
    "HIT":     (lambda r: bool(r.get("got_base_hit")),          "1+ hit"),
    "HRR":     (lambda r: bool(r.get("hrr_2_plus")),            "2+ H+R+RBI"),
    "CONTACT": (lambda r: (r.get("actual_tb") or 0) >= 2,       "2+ total bases"),
}
SCORE = {"HR": "hr_score", "HIT": "hit_score", "HRR": "hrr_score", "CONTACT": "contact_score"}


def rows_of(d):
    if isinstance(d, list):
        return d
    for k in ("graded_slots", "results", "rows"):
        if isinstance(d.get(k), list) and d[k] and isinstance(d[k][0], dict):
            return d[k]
    return []


def load(dirs):
    nights = {}
    for d in dirs:
        for f in sorted(glob.glob(str(Path(d) / "graded_results_*.json"))):
            m = DATE.search(f)
            if m:
                nights[m.group(1)] = rows_of(json.load(open(f)))
    return nights


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", required=True)
    a = ap.parse_args()
    nights = load(a.dir)
    picks = defaultdict(list)                   # role -> rows (one per player-game, de-duplicated)
    for night, rs in sorted(nights.items()):
        seen = set()
        for r in rs:
            role = r.get("pick_type")
            if role not in ROLES or r.get("got_hr") is None:
                continue
            k = (role, r["game_pk"], r["player_id"])
            if k in seen:
                continue
            seen.add(k)
            r = dict(r); r["_night"] = night
            picks[role].append(r)
    allnights = sorted(nights)
    half = allnights[len(allnights) // 2]
    print(f"{len(allnights)} nights ({allnights[0]} .. {allnights[-1]}); halves split at {half}\n")

    print("1. RESULTS (void = never came to the plate; not counted as a miss)")
    print(f"   {'role':<9}{'bar':<16}{'picks':>7}{'void':>7}{'hit rate':>10}{'1st half':>10}{'2nd half':>10}")
    for role, (did, label) in ROLES.items():
        rs = picks[role]
        played = [r for r in rs if (r.get("actual_ab") or r.get("actual_bb"))]
        h1 = [r for r in played if r["_night"] < half]; h2 = [r for r in played if r["_night"] >= half]
        print(f"   {role:<9}{label:<16}{len(rs):>7}{pct(len(rs) - len(played), len(rs)):>7}{pct(sum(did(r) for r in played), len(played)):>10}"
              f"{pct(sum(did(r) for r in h1), len(h1)):>10}{pct(sum(did(r) for r in h2), len(h2)):>10}")

    print("\n2. TIERS -- does a higher score buy accuracy? (picks that played)")
    for role, (did, label) in ROLES.items():
        played = [r for r in picks[role] if (r.get("actual_ab") or r.get("actual_bb")) and r.get(SCORE[role]) not in (None, "")]
        if len(played) < 100:
            continue
        order = sorted(played, key=lambda r: float(r[SCORE[role]]))
        q = len(order) // 5
        cells = []
        for i in range(5):
            chunk = order[i * q:(i + 1) * q if i < 4 else len(order)]
            cells.append(f"Q{i + 1} {pct(sum(did(r) for r in chunk), len(chunk))}")
        spots = defaultdict(lambda: [0, 0])
        for r in played:
            sp = r.get("lineup_spot")
            if isinstance(sp, int):
                b = "1-2" if sp <= 2 else "3-5" if sp <= 5 else "6-9"
                spots[b][0] += did(r); spots[b][1] += 1
        print(f"   {role:<8} by score quintile (low->high): " + "  ".join(cells))
        print(f"   {'':<8} by lineup spot: " + "  ".join(f"{b} {pct(*spots[b])} (n={spots[b][1]})" for b in ("1-2", "3-5", "6-9")))

    print("\n3. AUTOPSY -- every missed pick, by cause")
    print(f"   {'role':<9}{'missed':>7}{'never played':>14}{'lost time (1-2 AB)':>20}{'lineup unconfirmed':>20}{'full chance, missed':>21}")
    for role, (did, label) in ROLES.items():
        rs = picks[role]
        miss = [r for r in rs if not did(r)]
        c = Counter()
        for r in miss:
            ab, bb = r.get("actual_ab") or 0, r.get("actual_bb") or 0
            if not (ab or bb):
                c["never"] += 1
            elif ab <= 2:
                c["lost"] += 1
            elif r.get("lineup_confirmed") is False:
                c["unconf"] += 1
            else:
                c["full"] += 1
        n = len(miss)
        knowable = c["never"] + c["lost"] + c["unconf"]
        print(f"   {role:<9}{n:>7}{pct(c['never'], n):>14}{pct(c['lost'], n):>20}{pct(c['unconf'], n):>20}{pct(c['full'], n):>21}   -> knowable {pct(knowable, n)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
