#!/usr/bin/env python3
"""The 2026-10-18 scoring cutoff (nfl_features.scoring_cutoff).

Donovan: score depth chart QB1, RB1-2, WR1-3, TE1-2 plus anyone at 20%+ snaps;
everyone else is a visible not-scored row. The cutoff's men (`fresh`) are scored
AGAINST the old population, so widening the pool must move nobody already on it.
These checks pin that promise on the one place a slate-relative rank survives:
nfl_bot._slate_pct, the fallback for a value the league ruler cannot place.

No network, no nflreadpy: hand-built frames.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bots", "nfl"))

FAILED: list[str] = []


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


import polars as pl                                          # noqa: E402
import nfl_bot as nb                                         # noqa: E402
import nfl_features as nf                                    # noqa: E402

old = pl.DataFrame({"week": [8] * 4, "fresh": [0] * 4, "x": [1.0, None, 3.0, 4.0]})
wide = pl.concat([old, pl.DataFrame({"week": [8] * 3, "fresh": [1] * 3, "x": [9.0, 0.5, None]})])

a = old.with_columns(nb._slate_pct(old, "x", False).alias("p"))["p"].to_list()
b = wide.with_columns(nb._slate_pct(wide, "x", False).alias("p"))["p"].to_list()[:4]
check("an old man's slate rank does not move when the pool widens", b, a)
check("without a fresh column it is plain _pctile", 
      old.drop("fresh").with_columns(nb._slate_pct(old.drop("fresh"), "x", False).alias("p"))["p"].to_list(),
      old.drop("fresh").with_columns(nb._pctile(old, "x", False).alias("p"))["p"].to_list())
fr = wide.with_columns(nb._slate_pct(wide, "x", False).alias("p")).filter(pl.col("fresh") == 1)["p"].to_list()
check("a fresh man ranks against everyone (top of 7)", fr[0], 7 / 7)

check("the cutoff is QB1, RB1-2, WR1-3, TE1-2", nf.DEPTH_CUT, {"QB": 1, "RB": 2, "WR": 3, "TE": 2})
check("the snap floor is 20% in his last 3", (nf.SNAP_FLOOR, nf.SNAP_WEEKS), (0.20, 3))

if FAILED:
    print("\n".join(FAILED))
print(f"scoring cutoff: {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
