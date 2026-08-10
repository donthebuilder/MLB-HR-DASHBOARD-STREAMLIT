#!/usr/bin/env python3
"""
🔬 THE AUTOPSY — why the model was wrong this week.

2026-08-09. Results says WHAT happened: the picks cleared 11 of 40 bars. It
has never said WHY the ones that failed were picked in the first place. That's
the gap between an honest scoreboard and an accountable one, and for a project
whose whole differentiator is receipts, it's the most valuable thing still
unbuilt — every input the model saw is already sitting in the graded file next
to the outcome.

THREE SECTIONS, WEEKLY:

  1. THE WORST MISSES — the highest-scored picks that failed their own bar.
     For each, the inputs that were EXTREME that night (what the model was
     looking at) and what actually happened at the plate.

  2. WHAT IT MISSED — hitters who went deep while the model scored them near
     the bottom, or never designated them at all. A model that only reviews
     its own picks grades its own homework.

  3. THE PATTERN — the honest one. Across the week, compare the failed
     high-score picks against the CLEARED high-score picks and find which
     input separates them most. If nothing separates them, it says so, which
     is the most common and most useful answer: variance, not a broken input.

WHAT THIS DELIBERATELY DOESN'T DO. It doesn't assign blame to a single stat
from a single night — with a 12% base rate, one hitter going 0-for-4 is not
evidence of anything, and a page that says "the park factor let us down" after
one miss is astrology with a monospace font. Every claim here carries its n,
section 3 needs both groups to have a real sample before it speaks, and a
week with no signal prints "nothing separated them".

Output: public/data/current/autopsy_latest.json + a Discord post.

Usage:
    python bots/autopsy.py                 # last 7 graded nights
    python bots/autopsy.py --days 14 --quiet
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
PUBLIC_CURRENT = REPO_ROOT / "public" / "data" / "current"
OUT = PUBLIC_CURRENT / "autopsy_latest.json"
DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

CATEGORIES = ("TOP", "HR", "HIT", "HRR", "CONTACT")

# The inputs worth naming in a post-mortem, with the words to say them in.
INPUTS = [
    ("hr_score", "HR score", "{:.0f}"),
    ("hrw_score", "HR-weather", "{:.0f}"),
    ("season_iso", "ISO", "{:.3f}"),
    ("recent_barrel_rate", "barrel rate", "{:.1%}"),
    ("recent_fb_rate", "fly-ball rate", "{:.0%}"),
    ("pitcher_hr9", "the arm's HR/9", "{:.2f}"),
    ("park_hr_factor", "park factor", "{:.2f}"),
    ("pitch_type_match_score", "pitch match", "{:.0f}"),
    ("lineup_spot", "lineup spot", "{:.0f}"),
]


def num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def rows_of(p: dict) -> list[dict]:
    return p.get("graded_slots") or p.get("results") or []


def role_of(r: dict) -> str:
    return str(r.get("game_pick_role") or "").split("/")[0].strip().upper()


def cleared(role: str, r: dict) -> bool | None:
    if (num(r.get("actual_ab")) or 0) <= 0:
        return None                                   # void — never asked
    hits = num(r.get("actual_hits")) or 0
    combo = hits + (num(r.get("actual_runs")) or 0) + (num(r.get("actual_rbi")) or 0)
    if role in ("HR", "TOP"):
        return (num(r.get("actual_hr")) or 0) >= 1
    if role == "HIT":
        return hits >= 1
    if role == "HRR":
        return combo >= 2
    if role in ("CONTACT", "TB"):
        return (num(r.get("actual_tb")) or 0) >= 2
    return None


def load(days: int) -> tuple[list[tuple[str, list[dict]]], list[str]]:
    files = []
    for p in PUBLIC_CURRENT.glob("graded_results_*.json"):
        m = DATE_RE.search(p.name)
        if m:
            files.append((m.group(1), p))
    files.sort()
    files = files[-days:]
    out, dates = [], []
    for date, p in files:
        try:
            payload = json.loads(p.read_text())
        except Exception:
            continue
        # one row per player per night
        seen, rows = set(), []
        for r in rows_of(payload):
            pid = r.get("player_id")
            if pid is None:
                continue
            key = (pid, role_of(r))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
        out.append((date, rows))
        dates.append(date)
    return out, dates


def line_of(r: dict) -> str:
    """What actually happened, in box-score English."""
    ab = int(num(r.get("actual_ab")) or 0)
    h = int(num(r.get("actual_hits")) or 0)
    k = int(num(r.get("actual_k")) or 0)
    hr = int(num(r.get("actual_hr")) or 0)
    tb = int(num(r.get("actual_tb")) or 0)
    bits = [f"{h}-for-{ab}"]
    if hr:
        bits.append(f"{hr} HR")
    elif tb > h:
        bits.append(f"{tb} TB")
    if k:
        bits.append(f"{k} K")
    return ", ".join(bits)


def extremes(r: dict, pool: list[dict], k: int = 3) -> list[str]:
    """
    Which inputs were unusually high for this hitter, relative to everyone
    else graded the same night. This is the model's own reasoning, recovered —
    not a guess about it.
    """
    out = []
    for key, word, fmt in INPUTS:
        v = num(r.get(key))
        if v is None:
            continue
        vals = [x for x in (num(o.get(key)) for o in pool) if x is not None]
        if len(vals) < 12:
            continue
        rank = sum(1 for x in vals if x < v) / len(vals)
        # lineup_spot is the one where SMALL is the strong end
        if key == "lineup_spot":
            rank = 1 - rank
        if rank >= 0.85:
            try:
                out.append((rank, f"{word} {fmt.format(v)}"))
            except (ValueError, TypeError):
                out.append((rank, f"{word} {v}"))
    out.sort(reverse=True)
    return [t for _, t in out[:k]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    nights, dates = load(a.days)
    if not nights:
        print("no graded archive — nothing to autopsy")
        return 0

    misses, surprises = [], []
    hi_fail, hi_ok = [], []

    for date, rows in nights:
        picks = [r for r in rows if role_of(r) in CATEGORIES]
        for r in picks:
            role = role_of(r)
            v = cleared(role, r)
            if v is None:
                continue
            score = num(r.get("hr_score")) or num(r.get("overall_score")) or 0
            rec = {"date": date, "name": str(r.get("name") or ""), "role": role,
                   "score": round(score, 1), "line": line_of(r),
                   "why": extremes(r, rows), "row": r}
            if v is False:
                misses.append(rec)
                if score >= 75:
                    hi_fail.append(r)
            else:
                if score >= 75:
                    hi_ok.append(r)

        # What went deep that the model was cold on. Only counts hitters it
        # actually rated — an unrated bench bat isn't a miss, it's absence.
        for r in rows:
            if (num(r.get("actual_hr")) or 0) < 1:
                continue
            if role_of(r) in ("HR", "TOP"):
                continue                              # it had him; not a surprise
            s = num(r.get("hr_score"))
            if s is None or s >= 55:
                continue
            surprises.append({"date": date, "name": str(r.get("name") or ""),
                              "score": round(s, 1), "line": line_of(r),
                              "role": role_of(r) or "not designated"})

    misses.sort(key=lambda x: -x["score"])
    surprises.sort(key=lambda x: x["score"])

    # ── section 3: what separated the failures from the successes ──────────
    pattern = None
    MIN = 15
    if len(hi_fail) >= MIN and len(hi_ok) >= MIN:
        gaps = []
        for key, word, fmt in INPUTS:
            f = [x for x in (num(r.get(key)) for r in hi_fail) if x is not None]
            o = [x for x in (num(r.get(key)) for r in hi_ok) if x is not None]
            if len(f) < MIN or len(o) < MIN:
                continue
            mf, mo = statistics.mean(f), statistics.mean(o)
            sd = statistics.pstdev(f + o)
            if sd <= 0:
                continue
            gaps.append({"key": key, "word": word, "failed": round(mf, 3),
                         "cleared": round(mo, 3), "sd_gap": round((mo - mf) / sd, 2)})
        gaps.sort(key=lambda g: -abs(g["sd_gap"]))
        top = gaps[0] if gaps else None
        # A separation under a third of a standard deviation is noise dressed
        # as a finding. Saying "nothing separated them" is the honest week.
        if top and abs(top["sd_gap"]) >= 0.35:
            pattern = {"found": True, "n_failed": len(hi_fail), "n_cleared": len(hi_ok), **top,
                       "reading": (f"On high-scored picks this week, the ones that CLEARED averaged "
                                   f"{top['cleared']} {top['word']} against {top['failed']} for the ones that "
                                   f"failed — about {abs(top['sd_gap'])} standard deviations apart. "
                                   f"Worth a look; one week is not proof.")}
        else:
            pattern = {"found": False, "n_failed": len(hi_fail), "n_cleared": len(hi_ok),
                       "reading": ("Nothing separated the high-scored picks that cleared from the ones that "
                                   "didn't. That's the most common honest answer: at a 12% base rate, a week "
                                   "of misses is usually variance, not a broken input.")}
    else:
        pattern = {"found": False, "n_failed": len(hi_fail), "n_cleared": len(hi_ok),
                   "reading": (f"Not enough high-scored picks graded this week to compare "
                               f"({len(hi_fail)} failed, {len(hi_ok)} cleared; {MIN} of each needed).")}

    report = {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window": {"from": dates[0], "to": dates[-1], "nights": len(dates)},
        "worst_misses": [{k: v for k, v in m.items() if k != "row"} for m in misses[:5]],
        "missed_homers": surprises[:5],
        "pattern": pattern,
        "method": ("Worst misses are the highest-scored designated picks that failed their own category bar. "
                   "'Why' lists the inputs that sat in the top 15% of everyone graded that same night — the "
                   "model's own reasoning, recovered from the file. Void legs (no at-bat) are excluded "
                   "everywhere. Nothing here changes a weight."),
    }
    PUBLIC_CURRENT.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1))

    print(f"autopsy — {len(dates)} nights, {dates[0]} → {dates[-1]}")
    for m in report["worst_misses"]:
        print(f"  ✗ {m['name']:<22} {m['role']:<8} {m['score']:>5}  {m['line']:<20} "
              f"{' · '.join(m['why']) or 'no standout inputs'}")
    for s in report["missed_homers"]:
        print(f"  💥 {s['name']:<22} scored {s['score']:>5} and went deep ({s['role']})")
    print(f"  pattern: {pattern['reading']}")

    if not a.quiet:
        hook = os.environ.get("DISCORD_WEBHOOK", "")
        if hook:
            L = [f"**🔬 Weekly autopsy** — {dates[0]} → {dates[-1]}",
                 "_Where the model was wrong, and what it was looking at when it was._", ""]
            if report["worst_misses"]:
                L.append("**Worst misses**")
                for m in report["worst_misses"][:3]:
                    why = " · ".join(m["why"]) or "no standout inputs"
                    L.append(f"✗ **{m['name']}** ({m['role']} {m['score']}) — {m['line']}\n   _liked for:_ {why}")
            if report["missed_homers"]:
                L.append("\n**Went deep anyway**")
                for s in report["missed_homers"][:3]:
                    L.append(f"💥 **{s['name']}** — scored {s['score']}, {s['line']}")
            L.append(f"\n**The pattern**\n{pattern['reading']}")
            body = json.dumps({"content": "\n".join(L)[:1900]}).encode()
            for url in [u.strip() for u in hook.split(",") if u.strip()]:
                try:
                    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=15).read()
                except Exception as e:
                    print(f"  ! discord post failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
