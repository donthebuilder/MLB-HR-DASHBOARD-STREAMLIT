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

# How much a market's model beat a form-only baseline by, out of sample, AT THE
# DEPTH THE PICK CARD ACTUALLY USES.
#
# The report card measures the top 15 a week because that's a board. The pick
# card is five deep, and the edge at 5 is not the edge at 15 — the sharp end of
# a ranking behaves differently from its middle. Stamping a card with a number
# measured on a different slice is the kind of quietly-wrong figure that gets
# believed for a season.
#
# Out-of-sample only. The tuned season's edge is the number the weights were
# fitted to produce and means nothing about next Sunday.
def card_edges(seasons: list[int], depth: int) -> dict:
    oos = [s for s in seasons if s != TUNED_ON]
    if not oos:
        return {}
    blocks = {s: season_block(s, depth) for s in oos}
    out = {}
    for key in MODELS:
        present = [s for s in oos if key in blocks[s]]
        if not present:
            continue
        n = sum(blocks[s][key]["picks"] for s in present)
        pm = sum(blocks[s][key]["model"] * blocks[s][key]["picks"] for s in present) / max(1, n) / 100
        pf = sum(blocks[s][key]["form"] * blocks[s][key]["picks"] for s in present) / max(1, n) / 100
        edge = round(100 * (pm - pf), 1)

        # AN ERROR BAR, NOT JUST A POINT ESTIMATE. Five picks a week for one
        # out-of-sample season is ~90 picks, where a three-percentage-point
        # difference is literally three extra hits. Publishing "+3.3, holds up"
        # off that would be inventing a finding, and this project has already
        # watched two MLB weight candidates get WEAKER as their sample grew.
        #
        # Independent-proportions SE. The two selections overlap (same weeks,
        # often some of the same players), so the true SE is a little smaller
        # and this errs toward calling things noise. That is the right way to
        # be wrong here.
        se = 100 * ((pm * (1 - pm) / max(1, n) + pf * (1 - pf) / max(1, n)) ** 0.5)
        z = edge / se if se > 0 else 0.0
        out[key] = {
            "edge": edge,
            "depth": depth,
            "picks": n,
            "se": round(se, 1),
            "z": round(z, 2),
            "seasons": {str(s): blocks[s][key]["vs_form"] for s in present},
            "hit": round(100 * pm, 1),
            "form_hit": round(100 * pf, 1),
            # ONE definition of trust, here, rather than a threshold re-guessed
            # in every surface that renders a market. Gated on the error bar,
            # not the point estimate: at this sample size almost everything is
            # honestly "thin", and saying so is the whole point.
            "trust": ("holds" if z >= 2 else "fails" if z <= -2
                      else "leans" if z >= 1 else "sinks" if z <= -1 else "thin"),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--topk", type=int, default=15)
    ap.add_argument("--card-depth", type=int, default=5,
                    help="depth of the published pick card; edges are also measured here")
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
        "card_edges": card_edges(a.seasons, a.card_depth),
    }
    p = Path(a.out); p.mkdir(parents=True, exist_ok=True)
    (p / f"{a.prefix}report_card.json").write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {p}/{a.prefix}report_card.json ({(p/(a.prefix+'report_card.json')).stat().st_size/1024:.0f} KB)")
    for s, blk in payload["seasons"].items():
        tag = "tuned" if int(s) == TUNED_ON else "OUT-OF-SAMPLE"
        print(f"  {s} ({tag}):", {k: v["vs_form"] for k, v in blk.items()})
    if payload["card_edges"]:
        print(f"\n  card edges (top {a.card_depth}/wk, out of sample):")
        for k, v in sorted(payload["card_edges"].items(), key=lambda x: -x[1]["edge"]):
            print(f"    {k:<9} {v['edge']:>+5.1f} ±{v['se']:.1f}  z={v['z']:>+5.2f}  "
                  f"{v['trust']:<6} n={v['picks']:<4} hit {v['hit']:.1f}% vs form {v['form_hit']:.1f}%")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
