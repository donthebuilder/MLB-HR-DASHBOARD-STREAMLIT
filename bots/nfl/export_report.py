#!/usr/bin/env python3
"""export_report.py — writes report_card.json for the site's Report Card tab.

Runs the same backtest the terminal prints, across every season given, and
publishes it as JSON. Weights were tuned on 2025; every other season in here
is out-of-sample and the payload says which is which, because a report card
that hides that is marketing.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import polars as pl
from nfl_features import build
from nfl_scoring import MODELS, OUTCOME, score

FORM_PROXY = {"TD":"f_td_actual","REC_YDS":"f_receiving_yards","REC":"f_receptions",
              "RUSH_YDS":"f_rushing_yards","RUSH_ATT":"f_carries",
              "PASS_YDS":"f_passing_yards","KICK_PTS":"f_fg_made"}
TUNED_ON = 2025

def season_block(season: int, topk: int) -> dict:
    tbl = build(season)
    out = {}
    for key, m in MODELS.items():
        d = score(tbl, key).with_columns(OUTCOME[key].alias("y"))
        d = d.with_columns((pl.col("y") >= m["bar"]).cast(pl.Int8).alias("hit"))
        mp=mh=fp=fh=bn=bh=0
        deciles = {}
        for w in sorted(d["week"].unique().to_list()):
            live = d.filter(pl.col("week") == w)
            if not live.height: continue
            bn += live.height; bh += int(live["hit"].sum())
            sel = live.sort("score", descending=True).head(topk)
            mp += sel.height; mh += int(sel["hit"].sum())
            f = live.sort(FORM_PROXY[key], descending=True).head(topk)
            fp += f.height; fh += int(f["hit"].sum())
        dd = d.with_columns(((pl.col("score").rank("average")/pl.len()*10).ceil().clip(1,10)).alias("dec"))
        for r in dd.group_by("dec").agg(pl.col("hit").mean().alias("rate"), pl.len().alias("n")).iter_rows(named=True):
            deciles[int(r["dec"])] = {"rate": round(100*r["rate"],1), "n": int(r["n"])}
        out[key] = {
            "label": m["label"], "bar": m["bar"], "picks": mp,
            "model": round(100*mh/max(1,mp),1),
            "form":  round(100*fh/max(1,fp),1),
            "base":  round(100*bh/max(1,bn),1),
            "vs_form": round(100*mh/max(1,mp) - 100*fh/max(1,fp),1),
            "deciles": deciles,
        }
    return out

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--topk", type=int, default=15)
    ap.add_argument("--out", type=str, default="../public/data/nfl")
    ap.add_argument("--prefix", type=str, default="")
    a = ap.parse_args()
    payload = {
        "topk": a.topk,
        "tuned_on": TUNED_ON,
        "note": ("Weights were tuned on %d and run untouched everywhere else. "
                 "Out-of-sample seasons are the honest ones." % TUNED_ON),
        "seasons": {str(s): season_block(s, a.topk) for s in a.seasons},
        "markets": [{"key": k, "label": v["label"], "bar": v["bar"]} for k, v in MODELS.items()],
    }
    p = Path(a.out); p.mkdir(parents=True, exist_ok=True)
    (p / f"{a.prefix}report_card.json").write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {p}/{a.prefix}report_card.json ({(p/(a.prefix+'report_card.json')).stat().st_size/1024:.0f} KB)")
    for s, blk in payload["seasons"].items():
        tag = "tuned" if int(s) == TUNED_ON else "OUT-OF-SAMPLE"
        print(f"  {s} ({tag}):", {k: v["vs_form"] for k, v in blk.items()})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
