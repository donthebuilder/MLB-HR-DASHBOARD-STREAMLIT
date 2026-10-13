#!/usr/bin/env python3
"""NFL anytime TD: what price each rank tier of the board needs to be worth betting.

    python model_lab/nfl_td_price_bands.py

No historical NFL prices are available to the lab (the bot's odds feed is blocked by its plan and the site's stored
prices start 2026-09-27), so this does NOT say what the books charged. It answers the structural question from
outcomes alone: at the hit rate a rank tier actually produced, what American price breaks even? A bet at a longer
price than that is profitable in the long run; a shorter one is not. Hit rates are the published v2 score's
within-week rank tiers over 2022-2025 (the score has no fitted parameters, so every season is out of sample for it);
the interval is a 95% Wilson interval on the tier's hit rate, converted to a price range.
"""
import math, os, sys
import polars as pl
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model_lab")); sys.path.insert(0, os.path.join(ROOT, "bots", "nfl"))
import nfl_td_walkforward as W  # noqa: E402

TIERS = [(1, 5), (6, 15), (16, 30), (31, 60), (61, 120), (121, 9999)]

def american(p):
    return -100 * p / (1 - p) if p >= 0.5 else 100 * (1 - p) / p

def fmt(o):
    return f"{o:+.0f}"

def wilson(h, n, z=1.96):
    p = h / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - w, c + w

def main():
    d = W.add_scores(W.load()).filter(pl.col("sea") >= 2022)
    for name in ("score_v2", "score_v3"):
        r = d.with_columns(pl.col(name).rank("ordinal", descending=True).over("sw").alias("rk"))
        print(f"\n{name}: {r['sw'].n_unique()} weeks, 2022-2025, players who played")
        print(f"{'rank tier':10}{'n':>6}{'hit':>8}{'95% range':>16}{'break-even price':>18}{'range of break-even':>24}  positions")
        out = []
        for lo, hi in TIERS:
            t = r.filter(pl.col("rk").is_between(lo, hi)); n = t.height; h = int(t["y"].sum()); p = h / n
            a, b = wilson(h, n)
            pos = {k: round(v / n * 100) for k, v in zip(*[t.group_by("position").len()[c].to_list() for c in ("position", "len")])}
            print(f"{(str(lo) + '-' + str(hi)) if hi < 9999 else str(lo) + '+':10}{n:>6}{p * 100:>7.1f}%{a * 100:>7.1f}-{b * 100:<6.1f}%{fmt(american(p)):>18}{fmt(american(b)) + ' to ' + fmt(american(a)):>24}  {pos}")

if __name__ == "__main__":
    main()
