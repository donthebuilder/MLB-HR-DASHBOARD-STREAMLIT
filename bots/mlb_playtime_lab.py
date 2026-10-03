#!/usr/bin/env python3
"""mlb_playtime_lab.py -- BATCH-MODEL-V2 M2 (playing time), MLB HIT.

    python bots/mlb_playtime_lab.py                       # every night with frozen rows
    python bots/mlb_playtime_lab.py --since 2026-09-09    # needs `git fetch origin data`

The HIT score already carries playing time, crudely: hit_v2's lineup term
gives `lineup_top` = 1.0 to spots 1-5, 0.72 to 6, 0.50 to 7-9, worth
0.17 x 0.50 x 100 = 8.5 points (bots/mlb_dashboard.py ~8671/8705). Plate
appearances actually fall about a tenth per spot all the way down, so spot 1
and spot 5 are not the same bet on "1+ hit". This asks whether an expected-PA
curve in place of the buckets ranks hitters better.

THE RECORD, NOT A REBUILD. Each night's por_rows_<date>.jsonl (frozen at first
pitch, pick_lock.append_por_rows) gives each game's locked run and the HIT
score of record; that run's prediction_log line gives the hitter's lineup spot
(slot_snapshot.lineup_spot). The shadow is the logged score with the lineup
term swapped: hit + 8.5 x (curve(spot) - lineup_top(spot)) -- exact to the
term, before the small soft multiplier the live score applies afterwards.
Graded on each game's final box (statsapi): 1+ hit = hit, no plate appearance
= void. Top-k per night and AUC, the night as the population.

THE PROTOCOL: the first half of the nights CHOOSES the curve, the second half
REPORTS it; a curve ships as a shadow only if it beats live on the choosing
half's top-20 and does not lose AUC there.
"""
from __future__ import annotations
import argparse
import json
import os
import ssl
import subprocess
import urllib.request

try:   # python.org builds on macOS ship without the system roots; certifi has them
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL = ssl.create_default_context()
FETCH_ERRORS: list = []

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LINEUP_WEIGHT = 0.17 * 0.50 * 100          # hit_v2's lineup_top term, in score points


def lineup_top(spot: int) -> float:
    """hit_v2's bucket, verbatim."""
    return 1.0 if spot in (1, 2, 3, 4, 5) else 0.72 if spot == 6 else 0.50


# Expected plate appearances by batting-order spot, as a share of the range
# spot 1 .. spot 9 (MLB per-game averages fall ~0.1 PA a spot). The variants
# keep spot 1 at 1.0 and differ only in how low spot 9 sits.
def curve(floor: float):
    return lambda spot: 1.0 - (1.0 - floor) * (min(max(spot, 1), 9) - 1) / 8


CURVES = {"pa 1.0->0.50": curve(0.50), "pa 1.0->0.65": curve(0.65), "pa 1.0->0.80": curve(0.80)}


def shadow_score(hit: float, spot: int, f) -> float:
    return hit + LINEUP_WEIGHT * (f(spot) - lineup_top(spot))


def _git(*a) -> str:
    return subprocess.check_output(["git", "-C", REPO, *a], text=True)


def _box(pk: str, cache: dict) -> dict | None:
    if pk in cache:
        return cache[pk]
    url = (f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields=gameData,status,abstractGameState,"
           "liveData,boxscore,teams,away,home,players,id,stats,batting,plateAppearances,hits")
    try:
        j = json.load(urllib.request.urlopen(url, timeout=30, context=_SSL))
    except Exception as exc:  # noqa: BLE001 -- counted and printed, never silent
        FETCH_ERRORS.append(f"{pk}: {type(exc).__name__} {exc}"[:160])
        cache[pk] = None
        return None
    if j.get("gameData", {}).get("status", {}).get("abstractGameState") != "Final":
        cache[pk] = None
        return None
    out = {}
    for side in ("away", "home"):
        for k, p in (j.get("liveData", {}).get("boxscore", {}).get("teams", {}).get(side, {}).get("players") or {}).items():
            b = (p.get("stats") or {}).get("batting") or {}
            out[k.replace("ID", "")] = (int(b.get("plateAppearances") or 0), int(b.get("hits") or 0))
    cache[pk] = out
    return out


def night_rows(date: str, names: set, cache: dict) -> list[dict]:
    """Frozen HIT score + lineup spot + outcome for every hitter on one night."""
    por = [json.loads(l) for l in _git("show", f"origin/data:public/data/current/por_rows_{date}.jsonl").splitlines() if l.strip()]
    spots: dict = {}
    for run in {r.get("run_id") for r in por if r.get("run_id")}:
        n = f"prediction_log_{run}.jsonl"
        if n not in names:
            continue
        for line in _git("show", f"origin/data:public/data/current/{n}").splitlines():
            o = json.loads(line)
            if o.get("prediction_type") == "slate_row":
                sp = (o.get("slot_snapshot") or {}).get("lineup_spot")
                if sp:
                    spots[(str(o.get("game_pk")), str(o.get("player_id")))] = int(sp)
    rows, seen = [], set()
    for r in por:
        k = (str(r.get("game_pk")), str(r.get("player_id")))
        hit = (r.get("scores") or {}).get("hit")
        if k in seen or k not in spots or not isinstance(hit, (int, float)):
            continue
        seen.add(k)
        box = _box(k[0], cache)
        if box is None:
            continue
        pa, h = box.get(k[1], (0, 0))
        if pa == 0:
            continue                         # void: never batted
        rows.append({"date": date, "pk": k[0], "id": k[1], "hit": float(hit), "spot": spots[k], "y": 1 if h > 0 else 0})
    return rows


def topk(rows: list[dict], key, k: int) -> tuple[int, int]:
    by: dict = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    h = n = 0
    for night in by.values():
        top = sorted(night, key=lambda r: (-key(r), r["id"]))[:k]
        n += len(top)
        h += sum(r["y"] for r in top)
    return h, n


def auc(rows: list[dict], key) -> float:
    by: dict = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    num = den = 0.0
    for night in by.values():
        s = sorted(night, key=key)
        ranks, i = {}, 0
        while i < len(s):
            j = i
            while j + 1 < len(s) and key(s[j + 1]) == key(s[i]):
                j += 1
            for t in range(i, j + 1):
                ranks[id(s[t])] = (i + j) / 2 + 1
            i = j + 1
        p = sum(r["y"] for r in night)
        q = len(night) - p
        if p and q:
            num += sum(ranks[id(r)] for r in night if r["y"]) - p * (p + 1) / 2
            den += p * q
    return num / den if den else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-09")
    ap.add_argument("--until", default="2026-12-31")
    a = ap.parse_args()
    names = {ln.rsplit("/", 1)[-1] for ln in _git("ls-tree", "--name-only", "origin/data", "public/data/current/").splitlines()}
    dates = sorted(n[9:19] for n in names if n.startswith("por_rows_") and a.since <= n[9:19] <= a.until)
    cache: dict = {}
    allrows = []
    for d in dates:
        r = night_rows(d, names, cache)
        print(f"  {d}: {len(r)} hitters graded")
        allrows += r
    nights = sorted({r["date"] for r in allrows})
    half = nights[: len(nights) // 2]
    tune = [r for r in allrows if r["date"] in half]
    report = [r for r in allrows if r["date"] not in half]
    variants = {"live (buckets)": lambda r: r["hit"], **{k: (lambda r, f=f: shadow_score(r["hit"], r["spot"], f)) for k, f in CURVES.items()}}
    best = None
    for label, rows in (("CHOOSE", tune), ("REPORT", report)):
        base = sum(r["y"] for r in rows) / max(len(rows), 1)
        print(f"\n{label}: {len({r['date'] for r in rows})} nights, {len(rows)} hitters, base {100 * base:.1f}%")
        res = {}
        for name, key in variants.items():
            (h10, n10), (h20, n20) = topk(rows, key, 10), topk(rows, key, 20)
            res[name] = (h20 / max(n20, 1), auc(rows, key))
            print(f"  {name:<16} top-10 {h10}/{n10} {100 * h10 / max(n10, 1):5.1f}%   top-20 {h20}/{n20} {100 * h20 / max(n20, 1):5.1f}%   AUC {res[name][1]:.4f}")
        if label == "CHOOSE":
            live = res["live (buckets)"]
            ok = [(k, v) for k, v in res.items() if k != "live (buckets)" and v[0] > live[0] and v[1] >= live[1]]
            best = max(ok, key=lambda kv: kv[1])[0] if ok else None
    if FETCH_ERRORS:
        print(f"\n{len(FETCH_ERRORS)} box fetches FAILED (those games are out): {FETCH_ERRORS[:3]}")
    print(f"\nPICK: {best or 'none (live stays, no shadow)'}")


if __name__ == "__main__":
    main()
