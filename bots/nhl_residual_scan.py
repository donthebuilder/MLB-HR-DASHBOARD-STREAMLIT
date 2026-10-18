#!/usr/bin/env python3
"""
nhl_residual_scan.py -- among skaters the LAMP goal score rated the same, what still separates the ones who scored?

This is the NHL port of bots/missed_signals.py (HR side) and of the football residual scan written up in bots/nfl/SCORING.md.
Same question, same method: a whole-pool "does shots-per-game predict goals?" only rediscovers what the score already uses. Only a
RESIDUAL test (inside a band of equal score) can show a field the score under-uses.

HOW IT WORKS
  1. Graded rows (dressed skaters with a graded hit) are put into SCORE BANDS: deciles of the live score WITHIN each night
     (so a band holds skaters the model treated as interchangeable that night; band k of every night is pooled together).
  2. For every candidate field present in the rows, inside each band the field is split at the band's own median (ties broken
     at random, averaged over several jitter draws) and goal rates are compared; the halves are pooled ACROSS bands
     (Mantel-Haenszel style stratified lift, in percentage points).
  3. Each lift gets a Wilson interval on the two group rates, a bootstrap interval (resampling whole NIGHTS, so one night's
     games stay together), a permutation p-value (outcome shuffled within bands), and a Benjamini-Hochberg q-value across every
     field tested in that season.
  4. TREND / usage section: for each skater a time series of his own legs across the nights he appears is built from the rows
     (earlier rows only, nothing from the future): trend_<leg> = the leg on this night minus the same leg 4 appearances earlier.
     That asks "is his role growing or shrinking" with the same stratified test. NOT TESTABLE when too few skaters have 5+ nights.
  5. Verdict per field and the strict add-to-score rule (copied from the football lab):
        holds in both seasons         same sign in both seasons, q <= 0.1 in both, |lift| >= 2 points in both
        only one season               clears the bar in one season, not both
        one short season: cannot confirm   the honest default while only one season exists (or the field is missing in the other)
        no signal                     two seasons, neither clears the bar
     "WOULD I ADD IT TO THE SCORE" is yes only for "holds in both seasons"; even then the protocol is: tune on one season, report
     on the other, ship nothing on AUC alone (check top-15 / top-30 in the harness, noise floor ~2 points, see the jitter column).

LIMITS (printed at the top of every run, not hidden)
  * lamp_goal_log began 2026-09-25, so the live file is ONE short season (weeks, not a year). A single short season cannot
    confirm a signal: with ~20 fields tested, a few will look good by chance. Nothing here is a reason to change the score.
  * Without --prior-season there is no second season at all. A prior file must be a list of rows with the same schema as
    lamp_goal_log_export.json (game_id, player_id, game_date, score, hit, dressed, plus legs / pct / comp ...). The site backtest
    (lib/nhl/angleBacktest.json, scripts/nhl/goal-v2-backtest.mjs) is aggregates, not rows of this shape; a row-level export of
    a past season with a LOCKED score per skater-game is what is needed.
  * The site export carries only: score, pct.{shotsPg,goalsPg,toi}, legs.* (as the site stores them), comp, comp_v1, comp_pick,
    dressed, goals, hit. It does NOT carry context (opponent GA/G, back-to-back, lineup known), power play, xG, rest days or
    night rank. Those are listed as "not in the export" and are not tested. Adding them means adding them to the site route and
    its COLS, in the sibling repo. (If a row does carry a context object, its numeric leaves are tested automatically.)
  * Rows are the board's own scored skaters, not every skater; one skater appears on many nights, so rows are not independent
    (the night-level bootstrap accounts for same-night clustering, not for a skater repeating across nights).
  * Association inside a band, not causation, not a weight.

Run:  python3 bots/nhl_residual_scan.py --data public/data/current [--prior-season path/to/other_season_export.json]
It never changes a score, a weight or a file the site reads, and it never raises on missing data (exit 0 with a note).
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import random
from pathlib import Path
from typing import Any

BANDS = 10             # deciles of the score within a night
MIN_ROWS = 200         # fewer graded dressed rows than this: say so and stop
MIN_STRATUM = 10       # a band with fewer rows than this (after pooling nights) cannot be split
MIN_FIELD_N = 100      # a field needs this many rows with a value
JITTER_DRAWS = 5       # random-tiebreak split draws, averaged (and their spread reported as the noise floor)
BOOTSTRAP = 300
PERMUTATIONS = 300
Q_NEEDED = 0.1         # football rule
LIFT_NEEDED = 2.0      # points
TREND_LAG = 4          # a skater needs 5 appearances: this one and the one 4 earlier
TREND_MIN_SKATERS = 30
TREND_LEGS = ("toi", "shotsPg", "goalsPg")
ID_KEYS = {"game_id", "player_id", "game_date", "game_type", "score", "goals", "hit", "dressed", "model_version"}
WANTED_NOT_IN_EXPORT = ["context.oppGaPg", "context.b2b", "context.lineupKnown", "nightRank / role", "power-play time or points",
                        "xG", "rest days"]
REQUIRED_KEYS = ("game_id", "player_id", "game_date", "score", "hit", "dressed")


# -- small maths --------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1))


def num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    return None


def bh_qvalues(ps: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values, same order as the input."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q = [1.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, ps[i] * m / rank)
        q[i] = prev
    return q


# -- reading rows -------------------------------------------------------------
def schema_ok(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, list):
        return False, "not a list of rows"
    if not payload:
        return False, "empty list"
    first = next((r for r in payload if isinstance(r, dict)), None)
    if first is None:
        return False, "rows are not objects"
    missing = [k for k in REQUIRED_KEYS if k not in first]
    if missing:
        return False, "rows lack " + ", ".join(missing)
    return True, ""


def flatten(r: dict) -> dict[str, float]:
    """Every numeric/boolean candidate field on a row: top-level scalars and the numeric leaves of legs / pct / context."""
    out: dict[str, float] = {}
    for k, v in r.items():
        if k in ID_KEYS:
            continue
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict):
                    continue
                f = num(v2)
                if f is not None:
                    out[k + "." + k2] = f
            if k == "legs" and "rookie" in v:
                # legs.rookie is an object for a rookie line and null otherwise: present/absent is the flag
                out["legs.rookie"] = 1.0 if isinstance(v.get("rookie"), dict) else 0.0
        else:
            f = num(v)
            if f is not None:
                out[k] = f
    return out


def graded_rows(payload: Any, version: str | None = None) -> list[dict]:
    """Dressed, graded rows: {night, game, id, y, score, f:{field:value}}. One row per (game, skater): where a game holds two model
    versions the later version string wins, so a skater is never counted twice."""
    if not isinstance(payload, list):
        return []
    best: dict[tuple[str, str], dict] = {}
    for r in payload:
        if not isinstance(r, dict) or r.get("dressed") is not True or r.get("hit") is None:
            continue
        s = num(r.get("score"))
        if s is None or r.get("game_date") is None:
            continue
        if version and r.get("model_version") != version:
            continue
        key = (str(r.get("game_id")), str(r.get("player_id")))
        row = {"night": str(r["game_date"]), "game": key[0], "id": key[1], "y": 1 if r["hit"] else 0, "score": s,
               "version": str(r.get("model_version") or ""), "f": flatten(r)}
        cur = best.get(key)
        if cur is None or row["version"] >= cur["version"]:
            best[key] = row
    return sorted(best.values(), key=lambda x: (x["night"], x["game"], x["id"]))


def assign_bands(rows: list[dict], k: int = BANDS) -> None:
    """Decile of the score within each night (rank based, so a pile-up of equal scores still splits evenly, ties by id)."""
    by_night: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_night[r["night"]].append(r)
    for rs in by_night.values():
        rs.sort(key=lambda r: (r["score"], r["id"]))
        n = len(rs)
        for i, r in enumerate(rs):
            r["band"] = min(k - 1, int(k * i / n))


def add_trends(rows: list[dict]) -> dict:
    """trend_<leg> = this night's leg minus the same leg TREND_LAG appearances earlier, from his own earlier rows only.
    Returns what the series look like, so the report can say whether usage trends are testable at all."""
    by_p: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_p[r["id"]].append(r)
    counts = []
    skaters5 = 0
    for rs in by_p.values():
        rs.sort(key=lambda r: (r["night"], r["game"]))
        counts.append(len({r["night"] for r in rs}))
        if len(rs) > TREND_LAG:
            skaters5 += 1
        for i in range(TREND_LAG, len(rs)):
            for leg in TREND_LEGS:
                a, b = rs[i]["f"].get("legs." + leg), rs[i - TREND_LAG]["f"].get("legs." + leg)
                if a is not None and b is not None:
                    rs[i]["f"]["trend_" + leg] = a - b
    counts.sort()
    return {"skaters": len(by_p), "skaters_with_5_plus_nights": skaters5,
            "median_nights_per_skater": counts[len(counts) // 2] if counts else 0,
            "max_nights_per_skater": counts[-1] if counts else 0}


# -- the stratified lift ------------------------------------------------------
def build_draws(strata: dict[int, list[tuple[float, int, str]]], rng: random.Random, draws: int) -> list[dict]:
    """For each jitter draw: {night: [hi_ok, hi_n, lo_ok, lo_n]} pooled over bands (bands too small or with one value skipped)."""
    out = []
    for _ in range(draws):
        per_night: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0, 0])
        for pairs in strata.values():
            if len(pairs) < MIN_STRATUM:
                continue
            ordered = sorted(pairs, key=lambda x: (x[0], rng.random()))
            if ordered[0][0] == ordered[-1][0]:
                continue
            h = len(ordered) // 2
            for j, (_v, y, night) in enumerate(ordered):
                c = per_night[night]
                if j >= h:
                    c[0] += y
                    c[1] += 1
                else:
                    c[2] += y
                    c[3] += 1
        out.append(dict(per_night))
    return out


def lift_of(counts: list[list[int]]) -> float | None:
    hk = sum(c[0] for c in counts)
    hn = sum(c[1] for c in counts)
    lk = sum(c[2] for c in counts)
    ln = sum(c[3] for c in counts)
    if not hn or not ln:
        return None
    return 100 * (hk / hn - lk / ln)


def test_field(rows: list[dict], field: str, seed: int = 7) -> dict:
    """One field, one season. Returns the result dict, or {'testable': False, 'why': ...}."""
    have = [r for r in rows if field in r["f"]]
    if len(have) < MIN_FIELD_N:
        return {"field": field, "testable": False, "why": "only %d rows carry it (need %d)" % (len(have), MIN_FIELD_N)}
    if len({r["f"][field] for r in have}) < 2:
        return {"field": field, "testable": False, "why": "constant value: it cannot split anyone"}
    rng = random.Random(seed)
    strata: dict[int, list[tuple[float, int, str]]] = collections.defaultdict(list)
    for r in have:
        strata[r["band"]].append((r["f"][field], r["y"], r["night"]))
    draws = build_draws(strata, rng, JITTER_DRAWS)
    lifts = [x for x in (lift_of(list(d.values())) for d in draws) if x is not None]
    if not lifts:
        return {"field": field, "testable": False, "why": "no band was big enough or varied enough to split"}
    lift = sum(lifts) / len(lifts)
    jitter = (max(lifts) - min(lifts)) if len(lifts) > 1 else 0.0
    first = list(draws[0].values())
    hi_n, hi_k = sum(c[1] for c in first), sum(c[0] for c in first)
    lo_n, lo_k = sum(c[3] for c in first), sum(c[2] for c in first)
    # bootstrap over whole nights
    nights = sorted({n for d in draws for n in d})
    boots = []
    for _ in range(BOOTSTRAP):
        pick = [nights[rng.randrange(len(nights))] for _ in nights]
        vals = []
        for d in draws:
            v = lift_of([d[n] for n in pick if n in d])
            if v is not None:
                vals.append(v)
        if vals:
            boots.append(sum(vals) / len(vals))
    boots.sort()
    if boots:
        ci = (round(boots[int(0.025 * len(boots))], 1), round(boots[min(len(boots) - 1, int(0.975 * len(boots)))], 1))
    else:
        ci = (None, None)
    # permutation p: shuffle the outcome inside each band, keep the first draw's split
    bands = []
    for pairs in strata.values():
        if len(pairs) < MIN_STRATUM:
            continue
        ordered = sorted(pairs, key=lambda x: (x[0], rng.random()))
        if ordered[0][0] == ordered[-1][0]:
            continue
        bands.append(([y for _v, y, _n in ordered], len(ordered) // 2))
    observed = abs(lifts[0])
    exceed = 0
    tot_hi = sum(len(ys) - h for ys, h in bands)
    tot_lo = sum(h for _ys, h in bands)
    for _ in range(PERMUTATIONS):
        hk = lk = 0
        for ys, h in bands:
            sh = ys[:]
            rng.shuffle(sh)
            hk += sum(sh[h:])
            lk += sum(sh[:h])
        if tot_hi and tot_lo and abs(100 * (hk / tot_hi - lk / tot_lo)) >= observed:
            exceed += 1
    p = (exceed + 1) / (PERMUTATIONS + 1)
    return {"field": field, "testable": True, "n": len(have), "lift_pts": round(lift, 1), "jitter_pts": round(jitter, 1),
            "ci_boot": ci, "hi": {"hit": hi_k, "n": hi_n, "pct": round(100 * hi_k / hi_n, 1), "ci": wilson(hi_k, hi_n)},
            "lo": {"hit": lo_k, "n": lo_n, "pct": round(100 * lo_k / lo_n, 1), "ci": wilson(lo_k, lo_n)}, "p": round(p, 4)}


def group_of(field: str) -> str:
    if field.startswith("trend_"):
        return "trend"
    if field.startswith("legs."):
        return "leg"
    if field.startswith("pct."):
        return "percentile"
    if field.startswith("comp"):
        return "composite"
    if field.startswith("context."):
        return "context"
    return "other"


def scan_season(rows: list[dict], label: str) -> dict:
    """Everything for one season file: counts, per-field results with q-values, the trend section."""
    assign_bands(rows)
    series = add_trends(rows)
    nights = sorted({r["night"] for r in rows})
    info = {"label": label, "rows": len(rows), "goals": sum(r["y"] for r in rows), "nights": len(nights),
            "from": nights[0] if nights else None, "to": nights[-1] if nights else None,
            "games": len({r["game"] for r in rows}), "players": len({r["id"] for r in rows}),
            "versions": dict(collections.Counter(r["version"] for r in rows)),
            "board_rate_pct": round(100 * sum(r["y"] for r in rows) / len(rows), 1) if rows else None}
    fields = sorted({k for r in rows for k in r["f"]})
    plain = [f for f in fields if not f.startswith("trend_")]
    results = [test_field(rows, f) for f in plain]
    trend_ok = series["skaters_with_5_plus_nights"] >= TREND_MIN_SKATERS
    for leg in TREND_LEGS:
        f = "trend_" + leg
        if trend_ok and any(f in r["f"] for r in rows):
            results.append(test_field(rows, f))
        else:
            why = ("only %d skaters have %d+ nights (need %d); median nights per skater is %d"
                   % (series["skaters_with_5_plus_nights"], TREND_LAG + 1, TREND_MIN_SKATERS, series["median_nights_per_skater"]))
            results.append({"field": f, "testable": False, "why": why})
    tested = [r for r in results if r["testable"]]
    for r, q in zip(tested, bh_qvalues([r["p"] for r in tested])):
        r["q"] = round(q, 4)
    for r in results:
        r["group"] = group_of(r["field"])
    return {"info": info, "series": series, "fields": results}


def clears(r: dict | None) -> bool:
    return bool(r and r.get("testable") and r["q"] <= Q_NEEDED and abs(r["lift_pts"]) >= LIFT_NEEDED)


def verdict(live: dict | None, prior: dict | None, has_prior_file: bool) -> tuple[str, bool, str]:
    """(verdict, add_to_score, note). The strict rule: same sign in both seasons, q <= 0.1 in both, lift >= 2 points in both."""
    if not live or not live.get("testable"):
        return ("not testable", False, (live or {}).get("why", "no rows carry this field"))
    if not has_prior_file or not prior or not prior.get("testable"):
        lean = ""
        if clears(live):
            lean = "this season alone it leans %s (%+.1f pts, q=%.3f), which is not a confirmation" % (
                "up" if live["lift_pts"] > 0 else "down", live["lift_pts"], live["q"])
        note = lean or "no sign this season"
        if has_prior_file and not (prior and prior.get("testable")):
            note += "; the prior season has no usable values for it"
        return ("one short season: cannot confirm", False, note)
    a, b = clears(live), clears(prior)
    same_sign = (live["lift_pts"] > 0) == (prior["lift_pts"] > 0)
    if a and b and same_sign:
        return ("holds in both seasons", True,
                "same sign, q <= %g and lift >= %g points in both: a candidate for a SHADOW, then the harness top-15/top-30 test" % (Q_NEEDED, LIFT_NEEDED))
    if a or b:
        return ("only one season", False, "clears the bar in the %s season only (or the signs differ)" % ("live" if a else "prior"))
    return ("no signal", False, "neither season clears q <= %g and %g points" % (Q_NEEDED, LIFT_NEEDED))


# -- run ----------------------------------------------------------------------
def load_rows(path: Path) -> tuple[list[dict] | None, str]:
    if not path.exists():
        return None, "file not found: %s" % path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, "could not read %s (%s)" % (path, type(e).__name__)
    ok, why = schema_ok(payload)
    if not ok:
        return None, "%s does not match the lamp_goal_log_export schema (%s)" % (path.name, why)
    return payload, ""


def build(data_dir: Path, prior_path: Path | None = None, version: str | None = None) -> dict:
    out: dict[str, Any] = {
        "generated_by": "bots/nhl_residual_scan.py", "status": "ok", "notes": [], "not_in_export": WANTED_NOT_IN_EXPORT,
        "rule": "add to the score only if: same sign in both seasons, q <= %g in both, lift >= %g points in both" % (Q_NEEDED, LIFT_NEEDED)}
    payload, why = load_rows(data_dir / "lamp_goal_log_export.json")
    if payload is None:
        out["status"] = "no data"
        out["notes"].append(why + ". Nothing to scan (the file is written by bots/export_lamp_goal_log.py; it is missing when the site export was unavailable).")
        return out
    rows = graded_rows(payload, version)
    if len(rows) < MIN_ROWS:
        out["status"] = "too few rows"
        out["notes"].append("only %d graded dressed rows (need %d): too few to scan." % (len(rows), MIN_ROWS))
        out["live"] = {"rows": len(rows)}
        return out
    live = scan_season(rows, "live")
    prior = None
    if prior_path is not None:
        ppayload, pwhy = load_rows(prior_path)
        if ppayload is None:
            out["notes"].append("prior season not used: " + pwhy + ". It needs rows shaped like lamp_goal_log_export.json (game_id, player_id, game_date, score, hit, dressed, legs, pct ...).")
        else:
            prows = graded_rows(ppayload, version)
            if len(prows) < MIN_ROWS:
                out["notes"].append("prior season not used: only %d graded dressed rows (need %d)." % (len(prows), MIN_ROWS))
            else:
                prior = scan_season(prows, "prior")
    pf = {r["field"]: r for r in prior["fields"]} if prior else {}
    table = []
    for r in live["fields"]:
        v, add, note = verdict(r, pf.get(r["field"]), prior is not None)
        table.append({"field": r["field"], "group": r["group"], "live": r, "prior": pf.get(r["field"]),
                      "verdict": v, "add_to_score": add, "note": note})
    out.update({"live": live["info"], "series": live["series"], "prior": prior["info"] if prior else None, "fields": table})
    if prior is None:
        out["notes"].append("ONE SHORT SEASON: no second season was given, so no field can be confirmed; every verdict is 'cannot confirm'.")
    return out


def print_report(res: dict) -> None:
    print("NHL RESIDUAL SCAN  -- among skaters the goal score rated the same, what still separates the ones who scored?")
    for n in res.get("notes", []):
        print("  note: " + n)
    if res["status"] != "ok":
        return
    li, pi = res["live"], res["prior"]
    print("  LIVE: %d graded dressed skater-games, %d goals (%s%%), %d nights (%s to %s), %d games, %d players. versions %s"
          % (li["rows"], li["goals"], li["board_rate_pct"], li["nights"], li["from"], li["to"], li["games"], li["players"], li["versions"]))
    if pi:
        print("  PRIOR: %d rows, %d nights (%s to %s), %d players" % (pi["rows"], pi["nights"], pi["from"], pi["to"], pi["players"]))
    print("  LIMITS: lamp_goal_log began 2026-09-25, so the live data is one short season. A single short season cannot confirm a signal;")
    print("          expect a few fields to look good by chance. Nothing below is a reason to change the score.")
    s = res["series"]
    print("  TREND data: %d skaters, %d with %d+ nights, median %d nights each"
          % (s["skaters"], s["skaters_with_5_plus_nights"], TREND_LAG + 1, s["median_nights_per_skater"]))
    print("  NOT IN THE EXPORT (not tested): " + ", ".join(res["not_in_export"]))
    print("  RULE: " + res["rule"])
    print("")
    print("  %-18s %-10s %6s %7s %7s %8s %-16s %6s %6s  %s" % ("field", "group", "n", "high%", "low%", "lift", "boot 95% CI", "q", "jit", "verdict"))
    for t in res["fields"]:
        r = t["live"]
        if not r.get("testable"):
            print("  %-18s %-10s NOT TESTABLE: %s" % (t["field"], t["group"], r.get("why")))
            continue
        ci = "[%+.1f, %+.1f]" % tuple(r["ci_boot"]) if r["ci_boot"][0] is not None else "-"
        print("  %-18s %-10s %6d %6.1f%% %6.1f%% %+7.1f %-16s %6.3f %6.1f  %s"
              % (t["field"], t["group"], r["n"], r["hi"]["pct"], r["lo"]["pct"], r["lift_pts"], ci, r["q"], r["jitter_pts"], t["verdict"]))
        if t["prior"] and t["prior"].get("testable"):
            p = t["prior"]
            print("  %-18s %-10s prior season: lift %+.1f, q=%.3f" % ("", "", p["lift_pts"], p["q"]))
    yes = [t["field"] for t in res["fields"] if t["add_to_score"]]
    print("")
    print("  WOULD I ADD ANYTHING TO THE SCORE? " + ("candidates (shadow first, harness top-15/30 must agree): " + ", ".join(yes) if yes else "No."))
    print("  jit = spread of the lift across %d random tie-break draws: differences under ~2 points are noise." % JITTER_DRAWS)


def main() -> int:
    ap = argparse.ArgumentParser(description="NHL residual scan: what still separates scorers among equal-score skaters")
    ap.add_argument("--data", default="public/data/current")
    ap.add_argument("--prior-season", default=None, help="a second-season file with the same schema as lamp_goal_log_export.json")
    ap.add_argument("--version", default=None, help="only rows of this model_version")
    ap.add_argument("--out", default=None, help="where to write nhl_residual_scan.json (default: the --data directory)")
    a = ap.parse_args()
    try:
        res = build(Path(a.data), Path(a.prior_season) if a.prior_season else None, a.version)
        print_report(res)
        out = Path(a.out or a.data)
        out.mkdir(parents=True, exist_ok=True)
        (out / "nhl_residual_scan.json").write_text(json.dumps(res, separators=(",", ":"), default=str), encoding="utf-8")
        print("\nwrote %s" % (out / "nhl_residual_scan.json"))
    except Exception as e:      # a research printout must never fail the run it rides on
        print("nhl_residual_scan: skipped (%s: %s)" % (type(e).__name__, e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
