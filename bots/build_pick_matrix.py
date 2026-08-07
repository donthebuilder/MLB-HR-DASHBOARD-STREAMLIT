#!/usr/bin/env python3
"""build_pick_matrix.py — regenerate the Track-record snapshot nightly.

The site's Track record table (PlayerPickRecord.js) read a STATIC
public/pick_matrix.json generated 2026-08-04 from the local 39-day archive
(2026-04-16 → 2026-06-22) — honest about its range, but frozen. This script
rebuilds the same shape in CI every night:

  base   bots/data/pick_matrix_base.json — the frozen 39-day local archive
         aggregate. Those days predate the data branch's graded archive and
         exist nowhere else, so they ride along as a starting ledger.
  fresh  every graded_results_YYYY-MM-DD.json found in the given dirs
         (the results workflow restores the branch archive to /tmp/histout),
         but ONLY days STRICTLY AFTER base.meta.to — no double counting.

Output: pick_matrix.json, identical shape to what the component parses:
  { meta: {days, from, to, picks, players, minRate, generated, source},
    cats: [...],
    players: [{n, t, p, d, hr, h, tb, ab, r, rbi, last, c: {CAT: [ok, n]}}] }

Grading rules are lifted verbatim from the component's JOBS map — each
category graded on ITS OWN outcome, never on homers across the board.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

CATS = ["HIT", "HRR", "CONTACT", "TOP", "TOP15", "HR"]


def _i(v):
    try:
        x = float(v)
        return int(x) if x == x else 0
    except (TypeError, ValueError):
        return 0


def job_ok(role: str, hr: int, hits: int, runs: int, rbi: int, tb: int) -> bool:
    if role in ("HR", "TOP", "TOP15"):
        return hr > 0
    if role == "HIT":
        return hits > 0
    if role == "CONTACT":
        return tb >= 2
    if role == "HRR":
        return hits + runs + rbi >= 2
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graded-dir", action="append", default=[],
                    help="directory holding graded_results_*.json (repeatable)")
    ap.add_argument("--base", default="", help="frozen base matrix (optional)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # ── base ledger ──
    players: dict[str, dict] = {}
    base_to = ""
    base_from = ""
    base_days = 0
    if args.base:
        bp = Path(args.base)
        if bp.exists():
            try:
                base = json.loads(bp.read_text(encoding="utf-8"))
                base_to = str(base.get("meta", {}).get("to") or "")
                base_from = str(base.get("meta", {}).get("from") or "")
                base_days = _i(base.get("meta", {}).get("days"))
                for p in base.get("players", []):
                    key = str(p.get("n") or "").lower().strip()
                    if key:
                        players[key] = {
                            "n": p.get("n"), "t": p.get("t"),
                            "p": _i(p.get("p")), "d": _i(p.get("d")),
                            "hr": _i(p.get("hr")), "h": _i(p.get("h")),
                            "tb": _i(p.get("tb")), "ab": _i(p.get("ab")),
                            "r": _i(p.get("r")), "rbi": _i(p.get("rbi")),
                            "last": str(p.get("last") or ""),
                            "c": {k: [int(v[0]), int(v[1])] for k, v in (p.get("c") or {}).items()},
                        }
                print(f"base: {len(players)} players through {base_to}")
            except Exception as exc:
                print(f"base unreadable ({exc}) — starting empty", file=sys.stderr)

    # ── fresh graded days, strictly after the base ──
    date_re = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
    files: dict[str, Path] = {}
    for d in args.graded_dir:
        for f in sorted(Path(d).glob("graded_results_*.json")):
            m = date_re.search(f.name)
            if not m:
                continue
            day = m.group(1)
            if base_to and day <= base_to:
                continue
            files[day] = f  # later dirs win on the same date

    fresh_days = 0
    for day in sorted(files):
        try:
            j = json.loads(files[day].read_text(encoding="utf-8"))
        except Exception:
            continue
        slots = j.get("graded_slots") or j.get("results") or (j if isinstance(j, list) else [])
        if not slots:
            continue
        counted = False
        for s in slots:
            role = str(s.get("game_pick_role") or s.get("pick_type") or "").split("/")[0].strip().upper()
            if role not in CATS:
                continue
            nm = str(s.get("name") or "").strip()
            if not nm:
                continue
            hr, hits = _i(s.get("actual_hr")), _i(s.get("actual_hits"))
            runs, rbi = _i(s.get("actual_runs")), _i(s.get("actual_rbi"))
            tb, ab = _i(s.get("actual_tb")), _i(s.get("actual_ab"))
            ok = job_ok(role, hr, hits, runs, rbi, tb)
            key = nm.lower()
            p = players.setdefault(key, {
                "n": nm, "t": str(s.get("team") or ""), "p": 0, "d": 0,
                "hr": 0, "h": 0, "tb": 0, "ab": 0, "r": 0, "rbi": 0,
                "last": "", "c": {},
            })
            p["p"] += 1
            if ok:
                p["d"] += 1
            p["hr"] += hr; p["h"] += hits; p["tb"] += tb
            p["ab"] += ab; p["r"] += runs; p["rbi"] += rbi
            if str(s.get("team") or ""):
                p["t"] = str(s.get("team"))
            if day > p["last"]:
                p["last"] = day
            cell = p["c"].setdefault(role, [0, 0])
            cell[1] += 1
            if ok:
                cell[0] += 1
            counted = True
        if counted:
            fresh_days += 1

    if not players:
        print("nothing to write — no base and no graded files", file=sys.stderr)
        return 1

    all_days = base_days + fresh_days
    to_date = max([p["last"] for p in players.values()] + [base_to])
    total_picks = sum(p["p"] for p in players.values())
    out = {
        "meta": {
            "days": all_days,
            "from": base_from or min(p["last"] for p in players.values()),
            "to": to_date,
            "picks": total_picks,
            "players": len(players),
            "minRate": 3,
            "generated": dt.date.today().isoformat(),
            "source": f"frozen 39-day local base + {fresh_days} branch-archive days, rebuilt nightly in CI",
        },
        "cats": CATS,
        "players": sorted(players.values(), key=lambda p: -p["p"]),
    }
    op = Path(args.out)
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {op}: {all_days} days ({out['meta']['from']} → {to_date}), "
          f"{total_picks} picks, {len(players)} players (+{fresh_days} fresh days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
