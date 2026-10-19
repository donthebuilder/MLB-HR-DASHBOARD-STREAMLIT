"""nfl_field._def_stops -- who makes the stop, by run lane (2026-10-18).

Test data, made up for this file: one defence, a handful of carries. No
network: the frame is passed in. Checks:
  1. lanes use def_rush's keys; a carry with no run_gap is not counted.
  2. a solo, an assisted and a with-assist credit each count once a carry;
     the same player credited twice on one carry is still one tackle.
  3. a TFL counts in tfl; a tackle by the offence's team is not a stop.
  4. top 3 per lane by tkl (then tfl), names carried.

Run: PYTHONPATH=. python3 tests/test_nfl_def_stops.py
"""
import os
import sys

import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
import nfl_field  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


PRE = nfl_field.STOP_TACKLE + nfl_field.STOP_TFL


def _play(pid, loc, gap, **credits):
    r = dict(game_id="G1", play_id=float(pid), defteam="ATL", posteam="NO",
             run_location=loc, run_gap=gap)
    for p in PRE:
        r[f"{p}_player_id"] = None
        r[f"{p}_player_name"] = None
        if p not in nfl_field.STOP_TFL:
            r[f"{p}_team"] = None
    for p, (who, team) in credits.items():
        r[f"{p}_player_id"] = who
        r[f"{p}_player_name"] = f"T.{who}"
        if p not in nfl_field.STOP_TFL:
            r[f"{p}_team"] = team
    return r


rows = [
    _play(1, "left", "end", solo_tackle_1=("A", "ATL")),
    _play(2, "left", "end", solo_tackle_1=("A", "ATL"), tackle_for_loss_1=("A", None)),
    _play(3, "left", "end", assist_tackle_1=("B", "ATL"), assist_tackle_2=("C", "ATL")),
    _play(4, "left", "end", tackle_with_assist_1=("B", "ATL"), assist_tackle_1=("B", "ATL")),
    _play(5, "left", "end", solo_tackle_1=("D", "ATL")),
    _play(6, "left", "end", solo_tackle_1=("X", "NO")),           # offence's tackle, after a turnover
    _play(7, "middle", None, solo_tackle_1=("C", "ATL")),          # middle has no gap: still a lane
    _play(8, "right", None, solo_tackle_1=("E", "ATL")),           # no run_gap: not counted
]
df = pl.DataFrame(rows, infer_schema_length=None)
got = nfl_field._def_stops(df)

check("lanes", sorted(got["ATL"]), ["left|end", "middle|middle"])
le = got["ATL"]["left|end"]
check("cap 3", len(le), 3)
check("order", [x["player_id"] for x in le], ["A", "B", "C"])     # A 2 (1 tfl), B 2, C 1 beats D 1 by id
check("A", le[0], {"player_id": "A", "name": "T.A", "tkl": 2, "tfl": 1})
check("B once a carry", le[1]["tkl"], 2)
check("offence tackle out", any(x["player_id"] == "X" for x in le), False)
check("middle", got["ATL"]["middle|middle"], [{"player_id": "C", "name": "T.C", "tkl": 1, "tfl": 0}])
check("no gap out", any(x["player_id"] == "E" for v in got["ATL"].values() for x in v), False)
check("empty", nfl_field._def_stops(df.drop([c for c in df.columns if c.endswith("_player_id")])), {})

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
