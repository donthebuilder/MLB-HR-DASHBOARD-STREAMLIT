#!/usr/bin/env python3
"""nfl_playtime_lab.py -- BATCH-MODEL-V2 M2 (playing time), NFL.

    python nfl_playtime_lab.py --cache /tmp/nflbuild            # 2023 2024 2025
    python nfl_playtime_lab.py --cache /tmp/nflbuild --seasons 2024 2025
    python nfl_playtime_lab.py --live 2026 --weeks 4 5 6          # the shadows' graded record
                                                                    # (needs `git fetch origin data`)

None of the receiving / rushing markets carries playing time today; only TD
has f_snap_pct (nfl_scoring.MODELS). This asks, per market, whether adding the
trailing snap share (f_snap_pct, the same 4-game roll the TD model reads)
helps: the live weights scaled by (1 - s) plus f_snap_pct at s, for s in
SHARES. Scored the way nfl_td_v3_backtest.py scores (within-week percentiles
over nfl_features.build, trailing-only), graded on the market's own bar
(nfl_scoring.OUTCOME >= MODELS[m]["bar"]).

THE PROTOCOL (nfl_td_lab.py's): the share is CHOSEN ON 2024 and REPORTED ON
2025, with 2023 as a holdout neither side saw. A share wins only if it beats
live on 2024 top-5 AND does not lose on AUC; otherwise the market keeps live
and no shadow is added. Routes run are not here: nflverse participation is
published once a year (no 2026 file), so a route-participation leg could not
be scored live -- snap share is the playing-time leg that exists every week.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nfl_scoring import MODELS, OUTCOME, derive  # noqa: E402
from nfl_td_v3_backtest import _score, _topk, _auc  # noqa: E402

MARKETS = ["REC_YDS", "REC", "RUSH_YDS", "RUSH_ATT"]
SHARES = [0.1, 0.2, 0.3]
SNAP = "f_snap_pct"


def with_snap(w: dict, s: float) -> dict:
    """The live weights scaled to (1 - s), plus snap share at s."""
    out = {k: v * (1 - s) for k, v in w.items()}
    out[SNAP] = out.get(SNAP, 0) + s
    return out


def table(season: int, cache: str | None) -> pl.DataFrame:
    from nfl_features import build
    path = os.path.join(cache, f"build_{season}.parquet") if cache else None
    if path and os.path.exists(path):
        return pl.read_parquet(path)
    tbl = build(season)
    if path:
        os.makedirs(cache, exist_ok=True)
        tbl.write_parquet(path)
    return tbl


def prep(tbl: pl.DataFrame, market: str) -> pl.DataFrame:
    m = MODELS[market]
    d = derive(tbl).filter(pl.col("position").is_in(m["pos"]))
    return d.with_columns((OUTCOME[market] >= m["bar"]).cast(pl.Int8).alias("hit"))


def _w(w: dict) -> dict:
    # _score reads raw columns; an inverted leg ("-x") is not used by these four markets
    assert not any(k.startswith("-") for k in w), "inverted legs not supported here"
    return w


def measure(d: pl.DataFrame, w: dict) -> dict:
    x = _score(d, _w(w))
    (h5, n5), (h15, n15) = _topk(x, 5), _topk(x, 15)
    return {"top5": h5 / n5 if n5 else float("nan"), "h5": h5, "n5": n5,
            "top15": h15 / n15 if n15 else float("nan"), "auc": _auc(x)}


def choose(results: dict) -> float | None:
    """Pick the share on the TUNE season: beats live top-5, not worse on AUC; best top-5, then AUC."""
    live = results[0.0]
    ok = [(s, r) for s, r in results.items() if s and r["top5"] > live["top5"] and r["auc"] >= live["auc"]]
    if not ok:
        return None
    return max(ok, key=lambda sr: (sr[1]["top5"], sr[1]["auc"]))[0]


# ── THE LIVE RECORD (the shadows' graded line, BATCH-MODEL-V2 "PROVE IT") ───
# For each week: every player's LATEST prediction-log line generated before his
# team's kickoff (nfl_regrade / nfl_td_v3_backtest's rule), low-sample rows out
# (the card's rule), top-5 by the published score vs top-5 by the logged shadow
# score, graded on the market's bar from nflverse weekly stats. No stat line =
# void, not a miss. Logs from origin/data via git, or a local folder (--logs).
LIVE_SHADOWS = {"REC": "nfl_rec_snap_v1", "RUSH_ATT": "nfl_rushatt_snap_v1"}
STAT = {"REC": "receptions", "RUSH_ATT": "carries"}


def _log_bodies(season: int, wk: int, repo: str, logs: str | None):
    pre = f"nfl_prediction_log_{season}-wk{wk:02d}."
    if logs:
        for n in sorted(os.listdir(logs)):
            if pre in n:
                yield open(os.path.join(logs, n)).read().splitlines()
        return
    git = lambda *a: subprocess.check_output(["git", "-C", repo, *a], text=True)  # noqa: E731
    for ln in git("ls-tree", "--name-only", "origin/data", "public/data/current/").splitlines():
        n = ln.rsplit("/", 1)[-1]
        if n.startswith(pre):
            yield git("show", f"origin/data:public/data/current/{n}").splitlines()


def pregame_rows(bodies, wk: int, ko: dict, market: str) -> list[dict]:
    """Pure: the latest pregame line per player for one market. Exported for the test."""
    best: dict = {}
    for body in bodies:
        h = json.loads(body[0])
        if h.get("mode") != "week" or int(h.get("week") or -1) != wk:
            continue
        gen = dt.datetime.fromisoformat(h["generated_at"].replace("Z", "+00:00"))
        for line in body[1:]:
            r = json.loads(line)
            k = ko.get(str(r.get("team")))
            if r.get("market") != market or not isinstance(r.get("score"), (int, float)) or k is None or gen >= k:
                continue
            if r["player_id"] not in best or gen > best[r["player_id"]][0]:
                best[r["player_id"]] = (gen, r)
    return [r for _, r in best.values() if not r.get("low_sample")]


def grade_top5(rows: list[dict], key, actual: dict, bar: float) -> tuple[int, int]:
    """Pure: hits / graded in the top 5 by `key` (ties by player_id); voids (no stat line) skipped."""
    ranked = sorted((r for r in rows if key(r) is not None), key=lambda r: (-key(r), r["player_id"]))[:5]
    g = [actual[r["player_id"]] >= bar for r in ranked if r["player_id"] in actual]
    return sum(g), len(g)


def live_record(season: int, weeks: list[int], repo: str, logs: str | None) -> None:
    import nflreadpy as nfl
    from nfl_regrade import kickoffs
    stats = nfl.load_player_stats(seasons=[season], summary_level="week")
    pooled = {m: [0, 0, 0, 0] for m in LIVE_SHADOWS}
    for wk in weeks:
        ko = kickoffs(season, wk)
        bodies = list(_log_bodies(season, wk, repo, logs))
        wkstats = stats.filter(pl.col("week") == wk)
        for m, ver in LIVE_SHADOWS.items():
            rows = pregame_rows(bodies, wk, ko, m)
            actual = {r["player_id"]: (r[STAT[m]] or 0) for r in wkstats.iter_rows(named=True)}
            bar = MODELS[m]["bar"]
            lh, ln = grade_top5(rows, lambda r: r["score"], actual, bar)
            sh, sn = grade_top5(rows, lambda r: ((r.get("shadow") or {}).get(ver) or {}).get("score"), actual, bar)
            for i, v in enumerate((lh, ln, sh, sn)):
                pooled[m][i] += v
            print(f"week {wk} {m:<8} rows {len(rows):>3}  live top-5 {lh}/{ln}   {ver} top-5 {sh}/{sn}")
    print(f"\nweeks {weeks} pooled (voids out):")
    for m, (lh, ln, sh, sn) in pooled.items():
        pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "--"  # noqa: E731
        print(f"  {m:<8} live {lh}/{ln} {pct(lh, ln)}   shadow {sh}/{sn} {pct(sh, sn)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="*", default=[2023, 2024, 2025])
    ap.add_argument("--tune", type=int, default=2024)
    ap.add_argument("--cache", type=str, default=None)
    ap.add_argument("--live", type=int, default=None)
    ap.add_argument("--weeks", type=int, nargs="*", default=[4])
    ap.add_argument("--repo", type=str, default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    ap.add_argument("--logs", type=str, default=None, help="read prediction logs from this folder instead of origin/data")
    a = ap.parse_args()
    if a.live:
        live_record(a.live, a.weeks, a.repo, a.logs)
        return
    tbls = {s: table(s, a.cache) for s in a.seasons}
    picks = {}
    for m in MARKETS:
        live = MODELS[m]["w"]
        print(f"\n== {m} (bar {MODELS[m]['bar']}, live {MODELS[m].get('model_version', '')}) ==")
        per = {}
        for s, tbl in tbls.items():
            d = prep(tbl, m)
            per[s] = {0.0: measure(d, live), **{sh: measure(d, with_snap(live, sh)) for sh in SHARES}}
            base = 100 * float(d["hit"].mean())
            tag = " (TUNE)" if s == a.tune else " (HOLDOUT)" if s == 2023 else " (REPORT)"
            print(f"  {s}{tag}: {d.height} player-weeks, base {base:.1f}%")
            for sh, r in per[s].items():
                name = "live" if not sh else f"+snap {int(sh * 100)}%"
                print(f"    {name:<10} top-5 {r['h5']}/{r['n5']} {100 * r['top5']:5.1f}%   "
                      f"top-15 {100 * r['top15']:5.1f}%   AUC {r['auc']:.4f}")
        pick = choose(per[a.tune]) if a.tune in per else None
        picks[m] = pick
        print(f"  -> chosen on {a.tune}: {'+snap ' + str(int(pick * 100)) + '%' if pick else 'none (live stays, no shadow)'}")
    print("\nPICKS", picks)


if __name__ == "__main__":
    main()
