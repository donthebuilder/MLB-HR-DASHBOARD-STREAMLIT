#!/usr/bin/env python3
"""The 2026-09-13 TD reweight: snap share and touch volume.

Both terms came out of the residual scan in bots/nfl/nfl_td_lab.py, not out of
anyone's hunch, and both were measured on two seasons before shipping. These
checks guard the things that would silently break the result:

  · the weights still sum to 1.00 (they are percentages of a score, and the
    whole "a 0.30 really is 30%" contract rests on it)
  · f_touches is carries + targets, not targets alone — the targets-only
    version ranks every running back last for a reason that has nothing to do
    with touchdowns, and cost 3-5 points of top-15 hit rate in 2024 when it
    was measured
  · a missing snap row percentile-ranks as ZERO, not as the median. The
    median reads better in prose and measured WORSE in both seasons.
  · snap share reaches the scorer through the same trailing roll as
    everything else, so scoring week w never sees week w

No network, no nflreadpy: every check runs against hand-built frames whose
shape matches what the real loaders return.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bots", "nfl"))

CHECKS = 0
FAILED: list[str] = []


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def check_true(name, cond):
    check(name, bool(cond), True)


import polars as pl                                          # noqa: E402
import nfl_scoring as ns                                     # noqa: E402

# ── 1. the weight contract ───────────────────────────────────────────────────
for market, m in ns.MODELS.items():
    check(f"{market} weights sum to 1.00", round(sum(m["w"].values()), 9), 1.0)

td = ns.MODELS["TD"]["w"]
check_true("TD scores snap share", "f_snap_pct" in td)
check_true("TD scores touch volume", "f_touches" in td)
check("TD has 8 components", len(td), 8)
# The two new terms are a fifth of the score between them. Much more and they
# start selecting rather than modulating, which is the failure SCORING.md's own
# "context modulates, volume selects" rule exists to prevent.
check_true("the two new terms stay a minority of the score",
           td["f_snap_pct"] + td["f_touches"] <= 0.25)

# ── 2. f_touches is position-fair ────────────────────────────────────────────
row = {
    "f_tgt_n": [9.0, 0.0, 4.0], "f_car_n": [0.0, 14.0, 5.0],
    "f_receptions": [6.0, 0.0, 3.0], "f_targets": [9.0, 0.0, 4.0],
    "f_xtd": [0.4, 0.6, 0.5], "f_td_actual": [0.2, 0.3, 0.4],
    "f_opp_d_rec_td": [1.0, 1.0, 1.0], "f_opp_d_rush_td": [0.5, 0.5, 0.5],
    "f_opp_d_pass_yds": [200.0, 200.0, 200.0], "f_opp_d_rush_yds": [100.0, 100.0, 100.0],
    "spread": [-3.0, -3.0, -3.0], "indoors": [1, 1, 1], "wind_mph": [0.0, 0.0, 0.0],
}
d = ns.derive(pl.DataFrame(row))
check("a pure receiver's touches", d["f_touches"][0], 9.0)
check("a pure runner's touches", d["f_touches"][1], 14.0)
check("a dual-role back's touches", d["f_touches"][2], 9.0)
check_true("the runner is not ranked below the receiver on touches",
           d["f_touches"][1] > d["f_touches"][0])

# ── 3. the null policy, which is the part that was measured twice ────────────
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "bots", "nfl", "nfl_scoring.py"), encoding="utf-8").read()
check_true("_pctile fills nulls with zero", 'e = pl.col(col).fill_null(0)' in src)
check_true("no median fill crept back in", ".median().over(\"week\")" not in src)

pool = pl.DataFrame({
    "week": [1, 1, 1, 1],
    "f_snap_pct": [0.90, 0.55, None, 0.20],
})
ranked = pool.with_columns(ns._pctile(pool, "f_snap_pct", False).alias("p"))
vals = ranked["p"].to_list()
check_true("the unmeasured player ranks below the 20%-of-snaps player",
           vals[2] < vals[3])
check_true("the 90%-of-snaps player ranks top", vals[0] == max(vals))

# ── 4. the feature is trailing-only ──────────────────────────────────────────
# The scorer must never see week w's own snap share. _roll is what guarantees
# it, and this asserts the guarantee rather than trusting the call site.
from nfl_features import _roll, FORM_W                        # noqa: E402

hist = pl.DataFrame({
    "player_id": ["a"] * 5,
    "week": [1, 2, 3, 4, 5],
    "snap_pct": [0.10, 0.20, 0.30, 0.40, 0.99],
})
rolled = _roll(hist, ["snap_pct"], prefix="f_", gp="f_snap_gp")
w5 = rolled.filter(pl.col("week") == 5)
check("week 5 gets a window", w5.height, 1)
check("week 5's window is weeks 1-4, never week 5",
      round(float(w5["f_snap_pct"][0]), 6), round((0.10 + 0.20 + 0.30 + 0.40) / 4, 6))
check_true("the 0.99 in week 5 never reaches week 5's own score",
           float(w5["f_snap_pct"][0]) < 0.99)
check("the window is FORM_W weeks wide", FORM_W, 4)

# ── 5. weekly_pct's contract, without the network ────────────────────────────
import nfl_snaps                                              # noqa: E402

fake = pl.DataFrame({
    "gsis_id": ["a", "b", "c", "d"],
    "week": [1, 1, 1, 1],
    "position": ["WR", "RB", "CB", "TE"],
    "offense_pct": [0.9, 0.7, None, 0.5],
})
real = nfl_snaps._snaps
try:
    nfl_snaps._snaps = lambda season: fake
    out = nfl_snaps.weekly_pct(2025)
    check("offence only, nulls dropped", out.height, 3)
    check("columns are what nfl_features joins on",
          sorted(out.columns), ["player_id", "snap_pct", "week"])
    check_true("the defender is gone", "c" not in out["player_id"].to_list())
    nfl_snaps._snaps = lambda season: fake.head(0)
    check("an empty season fails soft", nfl_snaps.weekly_pct(2025).height, 0)
finally:
    nfl_snaps._snaps = real

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
print("ok   TD reweight: weights sum, touches are position-fair, a missing snap "
      "row ranks last (measured, not assumed), and the feature is trailing-only.")
