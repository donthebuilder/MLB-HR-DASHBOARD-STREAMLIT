#!/usr/bin/env python3
"""nfl_backtest.py — the Report Card, calibrated before week 1.

Grades each scored model against real weekly outcomes and reports it next to
two honest reference points:

  BASE   every eligible player that week — the floor
  FORM   rank by trailing average in the market's own stat — the dumb model

A score that doesn't clear FORM isn't a model, it's decoration.

    python nfl_backtest.py --season 2025 --topk 15
"""
from __future__ import annotations
import argparse
import polars as pl
from nfl_features import build
from nfl_scoring import MODELS, OUTCOME, score, derive

FORM_PROXY = {  # the dumb model's ranking column, per market
    "TD": "f_td_actual", "REC_YDS": "f_receiving_yards", "REC": "f_receptions",
    "RUSH_YDS": "f_rushing_yards", "RUSH_ATT": "f_carries",
    "PASS_YDS": "f_passing_yards", "KICK_PTS": "f_fg_made",
}


def run(season: int, topk: int) -> None:
    tbl = build(season)
    print(f"\nfeature table: {tbl.height} player-weeks, weeks "
          f"{tbl['week'].min()}–{tbl['week'].max()}, season {season}")
    print(f"\n{'MARKET':<18}{'BAR':>5}{'N':>6}{'MODEL':>9}{'FORM':>8}{'BASE':>8}"
          f"{'vs FORM':>9}{'vs BASE':>9}")
    print("-" * 72)

    summary = []
    for key, m in MODELS.items():
        d = score(tbl, key).with_columns(OUTCOME[key].alias("y"))
        d = d.with_columns((pl.col("y") >= m["bar"]).cast(pl.Int8).alias("hit"))
        weeks = sorted(d["week"].unique().to_list())

        mp = mh = fp = fh = bn = bh = 0
        for w in weeks:
            live = d.filter(pl.col("week") == w)
            if live.height == 0:
                continue
            bn += live.height
            bh += int(live["hit"].sum())
            sel = live.sort("score", descending=True).head(topk)
            mp += sel.height
            mh += int(sel["hit"].sum())
            fsel = live.sort(FORM_PROXY[key], descending=True).head(topk)
            fp += fsel.height
            fh += int(fsel["hit"].sum())

        mo = 100 * mh / max(1, mp)
        fo = 100 * fh / max(1, fp)
        bo = 100 * bh / max(1, bn)
        summary.append((key, m["label"], m["bar"], mp, mo, fo, bo))
        print(f"{m['label']:<18}{m['bar']:>5}{mp:>6}{mo:>8.1f}%{fo:>7.1f}%{bo:>7.1f}%"
              f"{mo - fo:>+8.1f}{mo - bo:>+8.1f}")

    print(f"\ntop {topk}/wk · MODEL = nfl_scoring · FORM = trailing-average rank "
          f"· BASE = all eligible")
    dead = [s for s in summary if s[4] - s[5] < 1.0]
    if dead:
        print("\nNOT BEATING THE DUMB MODEL — do not ship these as scored picks:")
        for k, lab, *_ in dead:
            print(f"  · {lab}")


def curve(season: int, market: str) -> None:
    """Hit rate by score decile — does the score actually separate?"""
    tbl = build(season)
    m = MODELS[market]
    d = score(tbl, market).with_columns(OUTCOME[market].alias("y"))
    d = d.with_columns((pl.col("y") >= m["bar"]).cast(pl.Int8).alias("hit"))
    d = d.with_columns(((pl.col("score").rank("average") / pl.len() * 10)
                        .ceil().clip(1, 10)).alias("dec"))
    g = d.group_by("dec").agg(pl.col("hit").mean().alias("rate"), pl.len().alias("n")).sort("dec", descending=True)
    print(f"\n{m['label']} — hit rate by score decile (10 = highest scores)")
    for r in g.iter_rows(named=True):
        bar = "█" * int(r["rate"] * 40)
        print(f"  D{int(r['dec']):<3}{r['rate'] * 100:>6.1f}%  n={r['n']:<5} {bar}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--topk", type=int, default=15)
    ap.add_argument("--curve", type=str, default=None, help="market key, e.g. TD")
    a = ap.parse_args()
    if a.curve:
        curve(a.season, a.curve)
    else:
        run(a.season, a.topk)
