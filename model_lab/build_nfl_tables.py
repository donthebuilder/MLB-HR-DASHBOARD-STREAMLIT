#!/usr/bin/env python3
"""Build and cache the leak-safe NFL player-week tables the lab runs on (one parquet per season).

Uses the PRODUCTION feature builder (bots/nfl/nfl_features.build): trailing-only form over weeks < w,
last season's per-game baseline as carryover until a man has MIN_GP games, schedule spread/total.
    python model_lab/build_nfl_tables.py 2019 2020 ... 2025
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bots", "nfl"))
import polars as pl
import nfl_features as F
from nfl_scoring import derive

CACHE = os.path.join(ROOT, "model_lab", "cache")

def build(season: int) -> pl.DataFrame:
    path = os.path.join(CACHE, f"nfl_{season}.parquet")
    if os.path.exists(path):
        return pl.read_parquet(path)
    t = derive(F.build(season))
    os.makedirs(CACHE, exist_ok=True)
    t.write_parquet(path)
    return t

if __name__ == "__main__":
    for s in [int(a) for a in sys.argv[1:]]:
        t = build(s)
        print(s, t.height, "rows", len(t.columns), "cols", flush=True)
