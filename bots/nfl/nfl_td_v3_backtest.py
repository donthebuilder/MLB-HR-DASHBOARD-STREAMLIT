#!/usr/bin/env python3
"""nfl_td_v3_backtest.py -- the published TD model (v2) vs the shadow (v3).

    python nfl_td_v3_backtest.py --history 2023 2024 2025   # nflverse, slow first time
    python nfl_td_v3_backtest.py --live 2026 --weeks 1 2 3   # needs `git fetch origin data`

Two honest records, reported separately:

  HISTORY  nfl_features.build(season) -- the same trailing-only table
           nfl_backtest.py grades -- scored the way nfl_td_lab.py scores
           (within-week percentiles). v2's weights were swept on 2024/2025
           and v3's single red-zone term was chosen on 2024/2025, so 2023 is
           the only season neither saw. Read 2023 as the holdout.
  LIVE     the 2026 pregame record: for each player, the latest
           nfl_prediction_log on the data branch generated BEFORE his team's
           kickoff (same rule as nfl_regrade / backfill_td_board). v3 there is
           recomputed from the logged component percentiles, which are rounded
           to whole numbers -- close, not exact. Top-k uses the card's own rule
           (low_sample rows out); a man with no stat line is void, not a miss.

Both variants of the collapsed term are printed (xTD alone, and the mean of
the trio's percentiles) so the choice can be re-checked.
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
from nfl_scoring import MODELS, SHADOW_MODELS, OUTCOME, derive, _pctile  # noqa: E402

RZ = ("f_rz_opp", "f_xtd", "f_gl_opp")
V2 = MODELS["TD"]["w"]
_kept = {k: v for k, v in V2.items() if k not in RZ}
_den = sum(_kept.values()) + V2["f_rz_opp"]
VARIANTS = {
    "v2 (published)": V2,
    "v3 xTD only (shadow)": SHADOW_MODELS["TD"]["w"],
    "v3 mean of trio": {**{k: v / _den for k, v in _kept.items()}, "_rz_mean": V2["f_rz_opp"] / _den},
}


def _prep(tbl: pl.DataFrame) -> pl.DataFrame:
    d = derive(tbl).filter(pl.col("position").is_in(MODELS["TD"]["pos"]))
    d = d.with_columns([_pctile(d, c, False).alias(f"_p_{c}") for c in RZ])
    d = d.with_columns((sum(pl.col(f"_p_{c}") for c in RZ) / 3).alias("_rz_mean"))
    return d.with_columns((OUTCOME["TD"] >= 1).cast(pl.Int8).alias("hit"))


def _score(d: pl.DataFrame, w: dict) -> pl.DataFrame:
    tot = None
    for k, v in w.items():
        p = _pctile(d, k, False) * v
        tot = p if tot is None else tot + p
    return d.with_columns((tot * 100).alias("score"))


def _topk(d: pl.DataFrame, k: int) -> tuple[int, int]:
    n = h = 0
    for _, g in d.group_by("week"):
        s = g.sort(["score", "player_id"], descending=[True, False]).head(k)
        n += s.height
        h += int(s["hit"].sum())
    return h, n


def _auc(d: pl.DataFrame) -> float:
    num = den = 0.0
    for _, g in d.group_by("week"):
        r, y = g["score"].rank("average").to_list(), g["hit"].to_list()
        p = sum(y)
        q = len(y) - p
        if p and q:
            num += sum(ri for ri, yi in zip(r, y) if yi) - p * (p + 1) / 2
            den += p * q
    return num / den if den else float("nan")


def history(seasons: list[int], cache: str | None) -> None:
    from nfl_features import build
    for s in seasons:
        path = os.path.join(cache, f"build_{s}.parquet") if cache else None
        if path and os.path.exists(path):
            tbl = pl.read_parquet(path)
        else:
            tbl = build(s)
            if path:
                os.makedirs(cache, exist_ok=True)
                tbl.write_parquet(path)
        d = _prep(tbl)
        print(f"\n{s}{' (HOLDOUT)' if s == 2023 else ''}: {d.height} player-weeks, "
              f"base {100 * float(d['hit'].mean()):.1f}%")
        for name, w in VARIANTS.items():
            x = _score(d, w)
            (h5, n5), (h15, n15) = _topk(x, 5), _topk(x, 15)
            print(f"  {name:<22} top-5 {h5}/{n5} {100 * h5 / n5:5.1f}%   "
                  f"top-15 {h15}/{n15} {100 * h15 / n15:5.1f}%   AUC {_auc(x):.4f}")


def live(season: int, weeks: list[int], repo: str) -> None:
    import nflreadpy as nfl
    from nfl_regrade import kickoffs

    def git(*a):
        return subprocess.check_output(["git", "-C", repo, *a], text=True)
    names = [ln.rsplit("/", 1)[-1] for ln in
             git("ls-tree", "--name-only", "origin/data", "public/data/current/").splitlines()]
    stats = nfl.load_player_stats(seasons=[season], summary_level="week")
    pooled = {k: [0, 0, 0, 0] for k in VARIANTS}
    for wk in weeks:
        ko = kickoffs(season, wk)
        best: dict = {}
        for n in sorted(x for x in names if x.startswith(f"nfl_prediction_log_{season}-wk{wk:02d}.")):
            body = git("show", f"origin/data:public/data/current/{n}").splitlines()
            h = json.loads(body[0])
            if h.get("mode") != "week" or int(h.get("week") or -1) != wk:
                continue
            gen = dt.datetime.fromisoformat(h["generated_at"].replace("Z", "+00:00"))
            for line in body[1:]:
                r = json.loads(line)
                k = ko.get(str(r.get("team")))
                if (r.get("market") != "TD" or not isinstance(r.get("score"), (int, float))
                        or k is None or gen >= k):
                    continue
                if r["player_id"] not in best or gen > best[r["player_id"]][0]:
                    best[r["player_id"]] = (gen, r)
        rows = [r for _, r in best.values() if not r.get("low_sample")]
        y = {r["player_id"]: (r["rushing_tds"] or 0) + (r["receiving_tds"] or 0)
             for r in stats.filter(pl.col("week") == wk).iter_rows(named=True)}
        print(f"\nweek {wk}: {len(best)} pregame TD rows")
        for name, w in VARIANTS.items():
            def raw(r, w=w, name=name):
                if name.startswith("v2"):
                    return r["score"]
                c = dict(r["components"])
                c["_rz_mean"] = sum(c.get(x, 0) for x in RZ) / 3
                ww = {k: v for k, v in w.items() if k in c}
                return sum(c[k] * v for k, v in ww.items()) / sum(ww.values())
            ranked = sorted(rows, key=lambda r: (-raw(r), r["player_id"]))
            res = []
            for K in (5, 15):
                g = [y[r["player_id"]] >= 1 for r in ranked[:K] if r["player_id"] in y]
                res += [sum(g), len(g)]
            for i in range(4):
                pooled[name][i] += res[i]
            print(f"  {name:<22} top-5 {res[0]}/{res[1]}  top-15 {res[2]}/{res[3]}  "
                  f"{[r['player'] for r in ranked[:5]]}")
    print(f"\nweeks {weeks} pooled (voids excluded):")
    for k, (a, b, c, d) in pooled.items():
        print(f"  {k:<22} top-5 {a}/{b} = {100 * a / max(b, 1):.1f}%   top-15 {c}/{d} = {100 * c / max(d, 1):.1f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=int, nargs="*")
    ap.add_argument("--cache", type=str, default=None)
    ap.add_argument("--live", type=int, default=None)
    ap.add_argument("--weeks", type=int, nargs="*", default=[1, 2, 3])
    ap.add_argument("--repo", type=str, default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    a = ap.parse_args()
    if a.history:
        history(a.history, a.cache)
    if a.live:
        live(a.live, a.weeks, a.repo)
    if not a.history and not a.live:
        ap.print_help()
