#!/usr/bin/env python3
"""eval_shadow_picks.py -- the challengers against the real picks (2026-10-01).

Reads the locked pregame rows (por_rows_<date>.jsonl: frozen at first pitch,
see pick_lock.append_por_rows) and each night's home runs (graded_results_
<date>.json hr_capture_report.all_homer_entries -- only WHO homered, never a
score from that file, which is written after the games). For every game with a
real TOP pick it prints, side by side, how often each pick slot homered:
TOP, HR, and each SHADOW_PICK_RULES challenger's #1 and #2, with n.

  python3 bots/eval_shadow_picks.py --dir public/data/current   (or --fetch)

A challenger replaces a real slot only on Donovan's call, and only once it
leads over enough games for the gap to be bigger than the noise (printed).
"""
from __future__ import annotations
import argparse, glob, json, math, os, sys, urllib.request

RAW = "https://raw.githubusercontent.com/donthebuilder/MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current"


def load(dir_: str | None, fetch: bool):
    por, graded = {}, {}
    if dir_:
        for f in glob.glob(os.path.join(dir_, "por_rows_*.jsonl")):
            por[os.path.basename(f)[9:19]] = [json.loads(l) for l in open(f) if l.strip()]
        for f in glob.glob(os.path.join(dir_, "graded_results_*.json")):
            try:
                graded[os.path.basename(f)[15:25]] = json.load(open(f))
            except Exception:
                pass
    if fetch:
        for d in sorted(por) or []:
            if d not in graded:
                try:
                    with urllib.request.urlopen(f"{RAW}/graded_results_{d}.json", timeout=30) as r:
                        graded[d] = json.loads(r.read().decode())
                except Exception as exc:
                    print(f"  could not fetch graded_results_{d}.json: {exc}", file=sys.stderr)
    return por, graded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="public/data/current")
    ap.add_argument("--fetch", action="store_true", help="fetch graded files missing locally")
    a = ap.parse_args()
    por, graded = load(a.dir, a.fetch)
    slots: dict[str, list[int]] = {}
    games = 0
    for d, rows in sorted(por.items()):
        g = graded.get(d)
        cap = (g or {}).get("hr_capture_report") if isinstance(g, dict) else None
        if not cap or cap.get("all_homer_entries") is None:
            continue
        hit = {(str(h["player_id"]), str(h["game_pk"])) for h in cap["all_homer_entries"]}
        # HIT (1+) and TB (2+) from the night's box lines -- only players the
        # graded file carries a line for; anyone else is ungraded, not a miss.
        box = {}
        for b in (g.get("graded_slots") or g.get("results") or []):
            if b.get("actual_hits") is not None:
                box[(str(b.get("player_id")), str(b.get("game_pk")))] = b
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(str(r.get("game_pk")), []).append(r)
        for gp, rs in by.items():
            if not any("TOP" in str(r.get("game_pick_role") or "").upper().split("/") for r in rs):
                continue
            games += 1
            for r in rs:
                key = (str(r.get("player_id")), gp)
                y = 1 if key in hit else 0
                b = box.get(key)
                y_hit = None if b is None else int((b.get("actual_hits") or 0) >= 1)
                y_tb = None if b is None or b.get("actual_tb") is None else int((b.get("actual_tb") or 0) >= 2)
                roles = set(str(r.get("game_pick_role") or "").upper().split("/"))
                for role in ("TOP", "HR"):
                    if role in roles:
                        slots.setdefault(role, []).append(y)
                if "HIT" in roles and y_hit is not None:
                    slots.setdefault("HIT (1+ hit)", []).append(y_hit)
                if "CONTACT" in roles and y_tb is not None:
                    slots.setdefault("CONTACT (2+ TB)", []).append(y_tb)
                for rule, slot in ((r.get("candidate") or {}).get("shadow_pick") or {}).items():
                    yy = y_hit if rule.startswith("hit:") else y_tb if rule.startswith("tb:") else y
                    if yy is not None:
                        slots.setdefault(f"{rule} #{slot}", []).append(yy)
    if not games:
        print("No locked games with a graded night yet.")
        return 0
    print(f"{games} games with a real TOP pick and a graded night")
    for k in sorted(slots, key=lambda k: (k not in ("TOP", "HR"), k)):
        v = slots[k]
        p = sum(v) / len(v)
        print(f"  {k:28s} {100*p:5.1f}%  ({sum(v)}/{len(v)})  +-{100*1.96*math.sqrt(p*(1-p)/len(v)):.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
