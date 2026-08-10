#!/usr/bin/env python3
"""
⚖️  TWO-LANE VALIDATION — the cron behind the promise.

2026-08-09. The site's standing rule is that context stats are COLLECTED but
never fold into a score until the graded archive says they earned it. Six of
them now log nightly — opponent defense percentile, pull-side wall, expected
PA from the lineup slot, rest and travel, bullpen fatigue, venue history — and
nothing was measuring any of them. A promise with no scheduler behind it is
just a sentence in a comment: in a month there'd be a sample and no habit of
looking at it.

This runs weekly, walks the whole graded archive, and answers one question per
stat: DOES A HITTER IN THE TOP BAND OF THIS STAT ACTUALLY GO DEEP MORE OFTEN
THAN ONE IN THE BOTTOM BAND?

HOW IT MEASURES, and the traps it avoids:

  · BANDS, NOT CORRELATION. A correlation coefficient on a binary outcome is
    a number nobody can act on. Top third vs bottom third, with the actual
    rates printed, is a sentence you can read out loud.

  · WILSON INTERVALS, NOT BARE RATES. 18% vs 14% sounds decisive and is
    meaningless at n=40. Every band gets a 95% interval and the verdict is
    driven by whether those intervals OVERLAP, not by the gap between points.

  · IT CAN RETURN "NO". The verdicts include `harmful` — a stat whose top band
    does WORSE. That outcome has to be reachable or the exercise is theatre.

  · THE OUTCOME MATCHES THE STAT'S CLAIM. Defense-against is a HIT/TB claim,
    not a homer claim, so it's graded on base hits. Wall distance and park are
    homer claims. Grading everything on homers would quietly fail the stats
    that never claimed to predict them.

  · VOID LEGS NEVER COUNT. A hitter with no at-bats wasn't asked. Same rule as
    every other grader here.

Output: public/data/current/context_validation.json, plus a Discord summary if
DISCORD_WEBHOOK is set. It writes a verdict; it does NOT change a weight. A
human decides what to do with "earned".

Usage:
    python bots/validate_context.py
    python bots/validate_context.py --min-n 60 --quiet
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
PUBLIC_CURRENT = REPO_ROOT / "public" / "data" / "current"
OUT = PUBLIC_CURRENT / "context_validation.json"
DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")


def num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def wilson(ok: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval, in percent. The normal approximation is
    wrong at these sample sizes and rates; this one isn't."""
    if not n:
        return (0.0, 100.0)
    z = 1.96
    p = ok / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den
    return (max(0.0, (mid - half) * 100), min(100.0, (mid + half) * 100))


# ── outcomes ────────────────────────────────────────────────────────────────
def _hr(r: dict) -> bool:
    return (num(r.get("actual_hr")) or 0) >= 1


def _hit(r: dict) -> bool:
    return (num(r.get("actual_hits")) or 0) >= 1


def _xbh(r: dict) -> bool:
    return (num(r.get("actual_tb")) or 0) >= 2


OUTCOMES: dict[str, tuple[str, Callable[[dict], bool]]] = {
    "hr": ("homered", _hr),
    "hit": ("got a base hit", _hit),
    "tb": ("2+ total bases", _xbh),
}


# ── the stats under test ────────────────────────────────────────────────────
# key      field on the graded row
# claim    which outcome it says it predicts — graded on THAT, not on homers
# higher   True when a bigger number is supposed to mean a better night
STATS = [
    ("opp_def_pctile", "Opponent defense percentile", "hit", True,
     "How leaky the defense behind the arm is. A HIT/TB claim — it was never a homer claim, so it isn't graded as one."),
    ("park_hr_factor", "Park HR factor", "hr", True,
     "The building's own home-run multiplier."),
    ("weather_hr_effect_pct", "Weather effect", "hr", True,
     "The bot's published weather adjustment for the night."),
    ("xpa", "Expected PA (lineup slot)", "hit", True,
     "More trips to the plate should mean more chances at a hit."),
    ("lineup_spot", "Lineup spot", "hit", False,
     "Batting higher in the order. Inverted: spot 1 is the good end."),
    ("pitcher_hr9", "Opposing starter HR/9", "hr", True,
     "The arm's own leak rate."),
    ("pitch_type_match_score", "Pitch match", "hr", True,
     "His damage pitches overlapping tonight's arsenal."),
    ("hrw_score", "HR Weather score", "hr", True,
     "The composite the bot already trusts — included as a control. If a new stat can't beat this, it hasn't earned anything."),
    ("recent_barrel_rate", "Recent barrel rate", "hr", True,
     "Contact quality, as a sanity check: if barrels don't validate, the harness is broken, not the stat."),
]

# flags are two-band by nature — present or not
FLAGS = [
    ("weak_spot_flag", "Weak lineup spot", "hr", "The lineup slot this arm is worst against."),
    ("trap_flag", "Trap flag", "hr", "The bot's own warning. A working trap flag should score LOWER, not higher."),
    ("high_confidence_hr_flag", "High confidence", "hr", "The bot's own confidence marker."),
]


# One shape assumption used to live in each of these three scripts, and all
# three were wrong the same way: the archive also contains bare top-level lists
# (every graded night from 2026-04-16 to 2026-05-18) and a payload.get() on a
# list raises AttributeError. Shared in bots/archive.py now.
from archive import rows_of  # noqa: E402


def load_rows(days: int | None) -> tuple[list[dict], list[str]]:
    # See bots/archive.py — this used to read PUBLIC_CURRENT only, so the
    # weekly verdict on which context stats earn their weight was being made
    # from whatever handful of nights happened to be in the checkout.
    from archive import describe, load_local
    _found, _notes = load_local(REPO_ROOT)
    print("  " + describe(_found, _notes))
    files = sorted(_found.items())
    if days:
        files = files[-days:]
    rows, dates = [], []
    for date, payload in files:
        dates.append(date)
        # ONE ROW PER PLAYER PER DAY. Without this a hitter designated in two
        # categories is counted twice and every rate is quietly weighted
        # toward multi-category picks — the same bug that biased SignalAudit.
        seen = set()
        for r in rows_of(payload):
            pid = r.get("player_id")
            key = (date, pid)
            if pid is None or key in seen:
                continue
            seen.add(key)
            if (num(r.get("actual_ab")) or 0) <= 0:
                continue                     # void: he never batted
            rows.append(r)
    return rows, dates


def band_test(rows: list[dict], key: str, outcome: str, higher: bool, min_n: int) -> dict | None:
    hit = OUTCOMES[outcome][1]
    vals = [(num(r.get(key)), hit(r)) for r in rows]
    vals = [(v, h) for v, h in vals if v is not None]
    if len(vals) < min_n * 2:
        return {"status": "thin", "n": len(vals)}
    vals.sort(key=lambda x: x[0])
    cut = len(vals) // 3
    if cut < min_n:
        return {"status": "thin", "n": len(vals)}
    low, high = vals[:cut], vals[-cut:]
    if not higher:
        low, high = high, low               # "good end" is the small numbers
    lo_ok, hi_ok = sum(1 for _, h in low if h), sum(1 for _, h in high if h)
    lo_ci, hi_ci = wilson(lo_ok, len(low)), wilson(hi_ok, len(high))
    lo_r, hi_r = 100 * lo_ok / len(low), 100 * hi_ok / len(high)

    # The verdict rests on the INTERVALS, not the point gap.
    if hi_ci[0] > lo_ci[1]:
        status = "earned"
    elif lo_ci[0] > hi_ci[1]:
        status = "harmful"
    else:
        status = "not proven"
    return {
        "status": status,
        "n": len(vals),
        "top": {"pct": round(hi_r, 1), "ok": hi_ok, "n": len(high), "ci": [round(hi_ci[0], 1), round(hi_ci[1], 1)]},
        "bottom": {"pct": round(lo_r, 1), "ok": lo_ok, "n": len(low), "ci": [round(lo_ci[0], 1), round(lo_ci[1], 1)]},
        "lift": round(hi_r / lo_r, 2) if lo_r > 0 else None,
    }


def flag_test(rows: list[dict], key: str, outcome: str, min_n: int) -> dict | None:
    hit = OUTCOMES[outcome][1]
    on = [r for r in rows if r.get(key) is True]
    off = [r for r in rows if r.get(key) is False]
    if len(on) < min_n or len(off) < min_n:
        return {"status": "thin", "n": len(on)}
    on_ok, off_ok = sum(1 for r in on if hit(r)), sum(1 for r in off if hit(r))
    on_ci, off_ci = wilson(on_ok, len(on)), wilson(off_ok, len(off))
    on_r, off_r = 100 * on_ok / len(on), 100 * off_ok / len(off)
    if on_ci[0] > off_ci[1]:
        status = "earned"
    elif off_ci[0] > on_ci[1]:
        status = "harmful"
    else:
        status = "not proven"
    return {
        "status": status,
        "n": len(on) + len(off),
        "top": {"pct": round(on_r, 1), "ok": on_ok, "n": len(on), "ci": [round(on_ci[0], 1), round(on_ci[1], 1)]},
        "bottom": {"pct": round(off_r, 1), "ok": off_ok, "n": len(off), "ci": [round(off_ci[0], 1), round(off_ci[1], 1)]},
        "lift": round(on_r / off_r, 2) if off_r > 0 else None,
    }


ICON = {"earned": "✅", "not proven": "⬜", "harmful": "⛔", "thin": "…"}


def post_discord(report: dict) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK", "")
    if not hook:
        return
    ranked = sorted(
        [r for r in report["stats"] if r["result"]["status"] != "thin"],
        key=lambda r: (r["result"]["status"] != "earned", -(r["result"].get("lift") or 0)),
    )
    lines = [f"**⚖️ Context validation** — {report['nights']} graded nights through {report['through']}",
             "_Which context stats have earned a place in the scoring, and which haven't._", ""]
    for r in ranked[:12]:
        res = r["result"]
        lines.append(
            f"{ICON[res['status']]} **{r['label']}** — top {res['top']['pct']}% vs bottom "
            f"{res['bottom']['pct']}% {OUTCOMES[r['outcome']][0]} (n={res['n']})"
        )
    thin = [r["label"] for r in report["stats"] if r["result"]["status"] == "thin"]
    if thin:
        lines.append(f"\n… not enough sample yet: {', '.join(thin[:8])}")
    lines.append("\n_Verdicts compare 95% intervals, not point gaps. Nothing here changes a weight on its own._")
    body = json.dumps({"content": "\n".join(lines)[:1900]}).encode()
    for url in [u.strip() for u in hook.split(",") if u.strip()]:
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"  ! discord post failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--min-n", type=int, default=40, help="minimum rows per band")
    ap.add_argument("--quiet", action="store_true", help="skip the Discord post")
    a = ap.parse_args()

    rows, dates = load_rows(a.days)
    if not rows:
        print("no graded archive — nothing to validate")
        return 0

    results = []
    for key, label, outcome, higher, why in STATS:
        res = band_test(rows, key, outcome, higher, a.min_n)
        results.append({"key": key, "label": label, "outcome": outcome, "why": why, "result": res})
    for key, label, outcome, why in FLAGS:
        res = flag_test(rows, key, outcome, a.min_n)
        results.append({"key": key, "label": label, "outcome": outcome, "why": why, "result": res})

    report = {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "through": dates[-1] if dates else None,
        "nights": len(dates),
        "rows": len(rows),
        "min_band_n": a.min_n,
        "method": ("Top third vs bottom third of each stat across every graded pick, one row per player "
                   "per night, void legs excluded. Verdict compares 95% Wilson intervals — 'earned' means "
                   "the top band's interval sits entirely above the bottom band's. Nothing here changes a "
                   "weight automatically."),
        "stats": results,
    }
    PUBLIC_CURRENT.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1))

    print(f"context_validation.json — {len(rows)} graded rows over {len(dates)} nights")
    for r in results:
        res = r["result"]
        if res["status"] == "thin":
            print(f"  …  {r['label']:<32} thin (n={res['n']})")
        else:
            print(f"  {ICON[res['status']]} {r['label']:<32} {res['top']['pct']:>5}% vs {res['bottom']['pct']:>5}%  "
                  f"lift {res['lift']}  n={res['n']}")
    if not a.quiet:
        post_discord(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
