#!/usr/bin/env python3
"""
🌑 hr_score_v3 — THE SHADOW LANE.

Donovan, 2026-08-23: "get it together and make the necessary adjustments."

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
=============================================
It is a second home-run score, computed for every batter on the slate, published
and graded every night, that CHANGES NOTHING. No pick moves. No badge moves. No
`hr_blend` weight moves. `mlb_dashboard.py` is not touched by this file at all.

That is the repo's own two-lane rule -- "any new term ships as a published,
graded COLUMN first and earns its way into the score afterwards" -- and it is
the right posture here for a specific reason: **earlier the same night, on 328
rows and a leaked archive, I told Donovan that launch angle separates home runs
and exit velocity does not. On 31,673 leak-free batter-days that was backwards.**
A score built on a finding that new belongs on a scoreboard before it belongs in
a pick.

In ~2 weeks the graded record below answers the only question that matters: does
v3 beat the live `hr_score` on the same slates? If yes it earns a place in
`hr_blend` at 9c. If no, this file is deleted and nothing else has to be undone.

WHERE THE TERMS COME FROM
=========================
`bbe_history/features_<date>.jsonl` -- the as-of contact profile built by
bots/backfill_bbe_history.py from every batted ball of the season, using only
games played BEFORE the date it is stamped with. Leak-free by construction, and
asserted as such in tests/test_backfill_bbe_history.py.

Measured on 31,673 batter-days with a 25+ batted-ball profile, 3,717 homers,
11.74% base rate (quartile low -> high, HR rate):

    season avg EV          7.68  10.31  12.91  16.05   z=+18.26   <- strongest
    season hard-hit %      7.83  10.13  12.93  16.05   z=+17.56
    season max EV          8.08  10.39  12.53  15.94   z=+17.40
    season max distance    8.49   9.90  12.64  15.91   z=+16.20
    season barrel %        8.02  11.28  12.63  15.01   z=+14.90
    season FB %            8.97  10.61  12.64  14.72   z=+12.87
    season avg LA          9.12  11.21  12.35  14.26   z=+10.68

THE FOUR TERMS, AND WHY EACH IS HERE
------------------------------------
  season_avg_ev        0.40   the strongest single leak-free predictor found.
                              Ranking on it ALONE reaches 19.6% at ten picks a
                              night out of sample against an 11.5% base.
  season_max_distance  0.25   the largest marginal add on top of EV: +3.80pp in
                              the half where picks actually get made.
  season_hard_hit_rate 0.20   EV consistency rather than EV average -- a bat
                              that lives at 95+ vs one with two loud outliers.
  season_avg_la        0.15   the launch axis. Real (8.97 -> 14.72 on FB rate)
                              and additive, but SMALLER than EV: high-EV +
                              high-FB is 15.77% against 8.12% for low-low, and
                              EV carries more of that gap than launch does.

BARREL RATE IS DELIBERATELY ABSENT, and that is the most load-bearing decision
in this file. Barrel looks strong alone (z=+14.90) and is nearly worthless once
EV is known: +2.81pp marginal against max distance's +3.80 and FB rate's +3.34,
and in a five-term fit its coefficient goes NEGATIVE (-0.015). It is exit
velocity re-expressed with a launch-angle window stapled on. A barrel-led
variant was tested and is the only candidate that is clearly worse: 17.12% at
ten a night against v3's 19.23%.

RECENT WINDOWS ARE ALSO ABSENT. L20 average EV adds +1.85pp over the season
number; L20 barrel adds +0.31pp in the half that matters. Season beats recent on
every metric measured, and the recent windows are exactly the fields step 9
proved leak. They add least and cost most.

HOW THE WEIGHTS WERE SET -- read this before changing them
----------------------------------------------------------
They are NOT swept. They are ordered by measured strength and rounded, then
normalised against league ranges (the panel's 5th-95th percentiles, so a night
of good bats cannot manufacture a strong signal out of the least-bad one). Five
candidate weightings were tested out of sample on 12,461 held-out batter-days
and four of them land inside noise of each other:

    v3 (.40/.20/.25/.15)                AUC 0.603   19.23% @ top-10/day
    EV alone                            AUC 0.589   19.62%
    EV .50 + maxdist .30 + LA .20       AUC 0.605   19.04%
    EV .60 + hard-hit .40               AUC 0.590   20.00%
    barrel .40 + EV .30 + FB .30        AUC 0.588   17.12%   <- the loser

The honest reading is that the AXIS matters and the exact split does not. v3 was
chosen for having the best AUC among the legible sets while keeping four terms a
person can argue with. **Do not tune these against the graded record this file
produces** -- that is the machine that produced season_power = 0.24.

USAGE
-----
    python3 bots/hr_v3_shadow.py score --date 2026-08-24    # tonight's board
    python3 bots/hr_v3_shadow.py grade --date 2026-08-23    # last night's record
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DATA = Path("public/data/current")
BBE = DATA / "bbe_history"

# ── the terms ────────────────────────────────────────────────────────────────
# (field, weight, league_low, league_high) -- lo/hi are the panel's 5th and 95th
# percentiles over 31,673 batter-days, so the normalisation is league-relative
# and not slate-relative.
TERMS = (
    ("season_avg_ev",        0.40,  82.5,  93.0),
    ("season_max_distance",  0.25, 374.0, 454.0),
    ("season_hard_hit_rate", 0.20, 0.245, 0.532),
    ("season_avg_la",        0.15,   6.0,  20.0),
)
MIN_BBE = 25          # below this the profile is noise; the score refuses
NEUTRAL = 50.0        # what an unscoreable bat publishes, so nothing sorts to 0


def num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def mm(v, lo, hi):
    if v is None:
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def score_row(prof: dict) -> tuple[float, str]:
    """(hr_score_v3, status). Never raises, never invents a number."""
    bbe = num(prof.get("season_bbe")) or 0.0
    if bbe < MIN_BBE:
        return NEUTRAL, f"low_sample:{int(bbe)}"
    total = 0.0
    used = 0.0
    for field, w, lo, hi in TERMS:
        n = mm(num(prof.get(field)), lo, hi)
        if n is None:
            continue
        total += w * n
        used += w
    if used < 0.60:
        # More than 40% of the weight is missing. A score built on the rest
        # would wear the same authority with half the evidence.
        return NEUTRAL, "insufficient_terms"
    # Re-base onto the weight actually present, so a missing term dilutes
    # toward neutral instead of silently scoring the bat as zero on it.
    return round(100.0 * total / used, 2), "ok"


# ── SIGNAL CONVERGENCE, SHADOW-FIRST (2026-09-01) ────────────────────────────
#
# load-the-truck's "Aligned Plays" ranks by how many of its overlays agree on
# a bat. The comparison note (moonshot-loadthetruck-comparison-2026-08-24)
# recommended testing the same idea here as a TIE-BREAK beside hr_score_v3,
# shadow-first, the way v3 itself was built -- never let it into the blend
# without evidence. Donovan picked it 2026-09-01.
#
# Seven signals the slate already publishes, one per SOURCE so the count is a
# count of independent-ish evidence and not the same fact seven times:
#   pitch    pitch_type_match_flag     his damage pitch is what the arm throws
#   spot     weak_spot_flag            the arm bleeds to his lineup spot
#   mistake  pitcher_mistake_match     the arm's mistake pitch is his pitch
#   pace     hr_pace_flag              real EV gap, HR-prone arm right now
#   air      wind_boost>0.02 or park_hr_factor>=1.05   the environment helps
#   form     last5_hr>=1               went deep in the last five
#   power    power_watch_flag or high_confidence_hr_flag   the bot's own power tell
#
# The count is published on every v3 row and graded by TIER (0,1,2,3,4+) each
# night, and top-15 by v3 is re-run with the count as tie-break, so in a few
# weeks "do converged bats homer more, and does the tie-break help" is a
# number and not an argument. Zero points anywhere else.
SIGNALS = (
    ("pitch",   lambda r: bool(r.get("pitch_type_match_flag"))),
    ("spot",    lambda r: bool(r.get("weak_spot_flag"))),
    ("mistake", lambda r: bool(r.get("pitcher_mistake_match"))),
    ("pace",    lambda r: bool(r.get("hr_pace_flag"))),
    ("air",     lambda r: (num(r.get("weather_wind_boost")) or 0.0) > 0.02
                          or (num(r.get("park_hr_factor")) or 0.0) >= 1.05),
    ("form",    lambda r: (num(r.get("last5_hr")) or 0.0) >= 1),
    ("power",   lambda r: bool(r.get("power_watch_flag")) or bool(r.get("high_confidence_hr_flag"))),
)


def signals_of(row: dict) -> list[str]:
    """Names of the signals firing on one slate row. Never raises."""
    out = []
    for name, fn in SIGNALS:
        try:
            if fn(row):
                out.append(name)
        except Exception:
            continue
    return out


def tier_of(count: int) -> str:
    return "4+" if count >= 4 else str(int(count))


def load_features(date: str) -> dict[int, dict]:
    path = BBE / f"features_{date}.jsonl"
    if not path.exists():
        return {}
    out = {}
    with path.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            out[int(r["batter_id"])] = r
    return out


def load_slate(slim: Path) -> list[dict]:
    try:
        rows = json.loads(slim.read_text())
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("player_id")]


def cmd_score(args) -> int:
    date = args.date
    feats = load_features(date)
    if not feats:
        print(f"no features_{date}.jsonl — run the BBE backfill for this date first")
        return 1
    slate = load_slate(Path(args.slate))
    if not slate:
        print(f"no slate rows in {args.slate}")
        return 1
    out = []
    for r in slate:
        pid = int(r["player_id"])
        prof = feats.get(pid, {})
        v3, status = score_row(prof)
        sigs = signals_of(r)
        out.append({
            "signals": sigs,
            "signal_count": len(sigs),
            "player_id": pid,
            "name": r.get("name"),
            "team": r.get("team"),
            "game_pk": r.get("game_pk"),
            "hr_score_v3": v3,
            "hr_score_v3_status": status,
            "hr_score_live": num(r.get("hr_score")),
            "game_pick_role": r.get("game_pick_role") or "",
            "season_bbe": num(prof.get("season_bbe")),
            **{f: num(prof.get(f)) for f, _, _, _ in TERMS},
        })
    ok = sum(1 for r in out if r["hr_score_v3_status"] == "ok")
    # Record WHICH slate was actually read. The workflow is scheduled for 15:20
    # UTC, when today_slim.json holds today's slate -- but run by hand at, say,
    # 00:22 UTC it still holds yesterday's, and the board would carry today's
    # date over yesterday's players with nothing to say so.
    _slate_dates = {str(r.get("game_date") or r.get("date") or "") for r in slate}
    _slate_dates.discard("")
    payload = {
        "date": date,
        "slate_dates_seen": sorted(_slate_dates)[:3],
        "n": len(out),
        "scoreable": ok,
        "terms": [{"field": f, "weight": w, "league_low": lo, "league_high": hi}
                  for f, w, lo, hi in TERMS],
        "min_bbe": MIN_BBE,
        "signals": [n for n, _ in SIGNALS],
        "rows": sorted(out, key=lambda r: -r["hr_score_v3"]),
    }
    dest = DATA / f"hr_v3_{date}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, dest)
    print(f"{date}: scored {ok}/{len(out)} bats -> {dest.name}")
    top = payload["rows"][:5]
    for r in top:
        print(f"   {r['hr_score_v3']:6.2f}  {str(r['name'])[:22]:24} (live hr_score {r['hr_score_live']})")
    return 0


def cmd_grade(args) -> int:
    """Grade a published v3 board against the batted balls of that same date."""
    date = args.date
    board_path = DATA / f"hr_v3_{date}.json"
    bbe_path = BBE / f"bbe_{date}.jsonl"
    if not board_path.exists():
        # Not an error: on the first night there is no yesterday. A red step
        # that is expected teaches you to ignore red steps, so this says what
        # it means and exits clean. A board that EXISTS and cannot be graded
        # still fails below.
        print(f"no hr_v3_{date}.json — nothing was published for that date yet "
              f"(expected on the first run; the board scored today gets graded tomorrow)")
        return 0
    if not bbe_path.exists():
        print(f"no bbe_{date}.jsonl — the harvest has not run for this date")
        return 1
    homered, played = set(), set()
    with bbe_path.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            played.add(int(r["batter_id"]))
            if r.get("is_hr"):
                homered.add(int(r["batter_id"]))
    board = json.loads(board_path.read_text())
    rows = [r for r in board["rows"] if int(r["player_id"]) in played]
    if not rows:
        print(f"{date}: no slate bat put a ball in play — nothing to grade")
        return 0

    def topn(key, n, reverse=True):
        vals = [r for r in rows if r.get(key) is not None]
        vals.sort(key=lambda r: r[key], reverse=reverse)
        sel = vals[:n]
        hits = sum(1 for r in sel if int(r["player_id"]) in homered)
        return hits, len(sel)

    rec = {"date": date, "graded": len(rows),
           "homers": sum(1 for r in rows if int(r["player_id"]) in homered)}
    rec["base_rate"] = round(100.0 * rec["homers"] / len(rows), 2)
    for n in (5, 10, 15, 20):
        h3, d3 = topn("hr_score_v3", n)
        hl, dl = topn("hr_score_live", n)
        rec[f"v3_top{n}"] = [h3, d3]
        rec[f"live_top{n}"] = [hl, dl]
    # designation comparison, so the record can be read against the real board
    des = [r for r in rows if "TOP" in str(r.get("game_pick_role") or "").split("/")]
    rec["top_badge"] = [sum(1 for r in des if int(r["player_id"]) in homered), len(des)]
    # ── convergence, graded (2026-09-01) ─────────────────────────────────
    # HR rate by tier, and top-15 by v3 with the count as tie-break vs raw.
    # Rows scored before the field existed carry no count and skip the tier.
    tiers: dict[str, list[int]] = {}
    for r in rows:
        if r.get("signal_count") is None:
            continue
        t = tier_of(int(r["signal_count"]))
        b = tiers.setdefault(t, [0, 0])
        b[1] += 1
        if int(r["player_id"]) in homered:
            b[0] += 1
    rec["sig_tiers"] = tiers
    if any(r.get("signal_count") is not None for r in rows):
        vals = [r for r in rows if r.get("hr_score_v3") is not None]
        vals.sort(key=lambda r: (r["hr_score_v3"], int(r.get("signal_count") or 0)), reverse=True)
        sel = vals[:15]
        rec["v3_sig_top15"] = [sum(1 for r in sel if int(r["player_id"]) in homered), len(sel)]

    ledger = DATA / "hr_v3_record.jsonl"
    prior = []
    if ledger.exists():
        prior = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    prior = [p for p in prior if p.get("date") != date] + [rec]
    prior.sort(key=lambda p: p["date"])
    tmp = ledger.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(p, separators=(",", ":")) for p in prior) + "\n")
    os.replace(tmp, ledger)

    def agg(key):
        a = sum(p[key][0] for p in prior if key in p)
        b = sum(p[key][1] for p in prior if key in p)
        return a, b, (100.0 * a / b if b else 0.0)
    print(f"{date}: {rec['homers']}/{rec['graded']} homered (base {rec['base_rate']}%)")
    print(f"\n{'':10} {'v3':>18} {'live hr_score':>20}")
    for n in (5, 10, 15, 20):
        a3, b3, p3 = agg(f"v3_top{n}")
        al, bl, pl = agg(f"live_top{n}")
        print(f"  top {n:<3}   {a3:4}/{b3:<5} = {p3:5.1f}%   {al:4}/{bl:<5} = {pl:5.1f}%")
    tb = agg("top_badge")
    print(f"  TOP badge {tb[0]:4}/{tb[1]:<5} = {tb[2]:5.1f}%")
    # convergence tiers, pooled across every night that carried the count
    pooled: dict[str, list[int]] = {}
    for p in prior:
        for t, (h, n) in (p.get("sig_tiers") or {}).items():
            b = pooled.setdefault(t, [0, 0]); b[0] += h; b[1] += n
    if pooled:
        print("\n  signals agreeing -> HR rate (shadow; zero points anywhere)")
        for t in ("0", "1", "2", "3", "4+"):
            if t in pooled:
                h, n = pooled[t]
                print(f"    {t:>2} signals  {h:4}/{n:<5} = {(100.0 * h / n if n else 0):5.1f}%")
        a, b, pct = agg("v3_sig_top15")
        a3, b3, p3 = agg("v3_top15")
        print(f"    top-15 by v3, count as tie-break  {a:4}/{b:<5} = {pct:5.1f}%   (raw v3 top-15 {p3:5.1f}%)")
    print(f"\n  {len(prior)} night(s) on the record. It decides v3 at 9c, not before.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--date", required=True)
    s.add_argument("--slate", default=str(DATA / "today_slim.json"))
    s.set_defaults(func=cmd_score)
    g = sub.add_parser("grade")
    g.add_argument("--date", required=True)
    g.set_defaults(func=cmd_grade)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
