#!/usr/bin/env python3
"""
Tests for bots/backfill_bbe_history.py.

Runnable BOTH as a pytest module and as a plain script -- every other file in
tests/ is a script and pytest is not installed everywhere this repo runs, so a
pytest-only file would be a test that quietly never executes.

    python3 tests/test_backfill_bbe_history.py
    pytest  tests/test_backfill_bbe_history.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))

import backfill_bbe_history as B  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        return
    FAILS.append(msg)
    print("  RED  " + msg)


# ── a fixture shaped exactly like the StatsAPI feed the repo already parses ──
FEED = {
    "liveData": {"plays": {"allPlays": [
        {   # a homer, fully tracked
            "result": {"event": "Home Run", "eventType": "home_run"},
            "matchup": {"batter": {"id": 111, "fullName": "Big Bat"},
                        "pitcher": {"id": 900, "fullName": "Arm"},
                        "batSide": {"code": "R"}, "pitchHand": {"code": "L"}},
            "playEvents": [
                {"details": {"code": "B"}},
                {"details": {"code": "X", "isInPlay": True},
                 "hitData": {"launchSpeed": 105.2, "launchAngle": 28.0,
                             "totalDistance": 412, "trajectory": "fly_ball",
                             "coordinates": {"coordX": 60.1, "coordY": 90.4}}},
            ],
        },
        {   # a ground out -- tracked, not a barrel, not hard hit
            "result": {"event": "Groundout", "eventType": "field_out"},
            "matchup": {"batter": {"id": 111, "fullName": "Big Bat"},
                        "pitcher": {"id": 900}, "batSide": {"code": "R"},
                        "pitchHand": {"code": "L"}},
            "playEvents": [
                {"details": {"code": "X"},
                 "hitData": {"launchSpeed": 78.0, "launchAngle": -12.0,
                             "totalDistance": 21, "trajectory": "ground_ball",
                             "coordinates": {"coordX": 120.0, "coordY": 150.0}}},
            ],
        },
        {   # a strikeout -- NO hitData, must not become a batted ball
            "result": {"event": "Strikeout", "eventType": "strikeout"},
            "matchup": {"batter": {"id": 222, "fullName": "Whiffer"},
                        "pitcher": {"id": 900}},
            "playEvents": [{"details": {"code": "S"}}],
        },
        {   # a double with distance missing -- must survive with None, not 0
            "result": {"event": "Double", "eventType": "double"},
            "matchup": {"batter": {"id": 222, "fullName": "Whiffer"},
                        "pitcher": {"id": 900}},
            "playEvents": [
                {"details": {"code": "X"},
                 "hitData": {"launchSpeed": 99.0, "launchAngle": 14.0,
                             "trajectory": "line_drive"}},
            ],
        },
    ]}}
}


def test_parse():
    print("parse")
    rows = B.bbe_from_payload(FEED, 777, date(2026, 8, 1))
    check(len(rows) == 3, f"strikeouts must not become batted balls: got {len(rows)}, want 3")
    hr = [r for r in rows if r["is_hr"]]
    check(len(hr) == 1, "exactly one homer in the fixture")
    h = hr[0]
    check(h["launch_speed"] == 105.2 and h["launch_angle"] == 28.0, "EV/LA carried through")
    check(h["total_distance"] == 412, "distance carried through")
    check(h["is_barrel"] is True, "105.2 mph at 28 deg is a barrel (>=98, 24-32)")
    check(h["is_hard_hit"] is True, "105.2 mph is hard hit (>=95)")
    check(h["is_sweet_spot"] is True, "28 deg is sweet spot (8-32)")
    check(h["is_400_plus"] is True and h["is_350_plus"] is True, "412 ft clears 350 and 400")
    check(h["bb_type"] == "fly_ball", "trajectory maps to bb_type")
    check(h["hc_x"] == 60.1 and h["hc_y"] == 90.4, "spray coordinates carried through")
    check(h["stand"] == "R" and h["p_throws"] == "L", "handedness carried through")

    go = [r for r in rows if r["bb_type"] == "ground_ball"][0]
    check(go["is_barrel"] is False and go["is_hard_hit"] is False, "78 mph grounder is neither")
    check(go["is_sweet_spot"] is False, "-12 deg is not sweet spot")

    dbl = [r for r in rows if r["event_type"] == "double"][0]
    check(dbl["total_distance"] is None, "missing distance stays None, never 0")
    check(dbl["is_350_plus"] is False, "untracked distance cannot claim 350+")
    check(dbl["is_xbh"] is True, "a double is an XBH")
    check(dbl["is_barrel"] is False, "99 mph at 14 deg is NOT a barrel (angle too low)")


def test_thresholds_match_the_dashboard():
    print("thresholds match bots/mlb_dashboard.py")
    check(B.BARREL_EV == 98.0 and (B.BARREL_LA_LO, B.BARREL_LA_HI) == (24.0, 32.0),
          "barrel is ev>=98 and 24<=la<=32")
    check(B.HARD_HIT_EV == 95.0, "hard hit is ev>=95")
    check((B.SWEET_LA_LO, B.SWEET_LA_HI) == (8.0, 32.0), "sweet spot is 8<=la<=32")
    check(B.IDEAL_EV == 97.0 and (B.IDEAL_LA_LO, B.IDEAL_LA_HI) == (18.0, 36.0),
          "ideal HR contact is ev>=97 and 18<=la<=36")


def test_leak_invariant():
    """THE test. Features for day D must not see a single event from day D."""
    print("leak invariant")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        # two harmless days, then a day where the batter homers three times
        B.atomic_write_jsonl(out / "bbe_2026-08-01.jsonl", [
            {"batter_id": 1, "batter_name": "X", "game_date": "2026-08-01", "game_pk": 1,
             "launch_speed": 80.0, "launch_angle": 5.0, "total_distance": 100.0,
             "bb_type": "ground_ball", "is_hr": False, "is_barrel": False,
             "is_hard_hit": False, "is_sweet_spot": False, "is_ideal_hr": False,
             "is_350_plus": False, "is_375_plus": False, "is_400_plus": False, "is_xbh": False},
        ])
        B.atomic_write_jsonl(out / "bbe_2026-08-02.jsonl", [
            {"batter_id": 1, "batter_name": "X", "game_date": "2026-08-02", "game_pk": 2,
             "launch_speed": 115.0, "launch_angle": 28.0, "total_distance": 450.0,
             "bb_type": "fly_ball", "is_hr": True, "is_barrel": True,
             "is_hard_hit": True, "is_sweet_spot": True, "is_ideal_hr": True,
             "is_350_plus": True, "is_375_plus": True, "is_400_plus": True, "is_xbh": True}
            for _ in range(3)
        ])
        # as of 08-02, only 08-01 may be visible
        hist = B.load_events(out, date(2026, 8, 2))
        evs = hist[1]
        check(len(evs) == 1, f"as-of 08-02 must see only the 08-01 event, saw {len(evs)}")
        check(all(e["game_date"] < "2026-08-02" for e in evs), "no same-day event may leak in")
        f = B.window_features(evs, "season")
        check(f["season_barrel_rate"] == 0.0, "the 08-02 barrels must not reach the 08-02 row")
        check(f["season_max_distance"] == 100.0, "max distance must be the 08-01 ball, not 450")
        # as of 08-03, both days are visible
        hist2 = B.load_events(out, date(2026, 8, 3))
        check(len(hist2[1]) == 4, f"as-of 08-03 must see all four events, saw {len(hist2[1])}")
        f2 = B.window_features(hist2[1], "season")
        check(abs(f2["season_barrel_rate"] - 0.75) < 1e-9, "3 of 4 barrels once the day has passed")
        check(f2["season_max_ev"] == 115.0, "max EV picks up the big ball the day after")


def test_window_math():
    print("window math")
    rows = [{"is_barrel": i < 3, "is_hard_hit": True, "is_sweet_spot": False,
             "is_ideal_hr": False, "is_hr": False, "is_350_plus": False,
             "is_375_plus": False, "is_400_plus": False, "bb_type": "fly_ball",
             "launch_speed": 90.0 + i, "launch_angle": 20.0, "total_distance": 300.0 + i}
            for i in range(10)]
    f = B.window_features(rows, "l20")
    check(f["l20_bbe"] == 10, "sample size reported")
    check(abs(f["l20_barrel_rate"] - 0.3) < 1e-9, "3/10 barrels")
    check(f["l20_max_ev"] == 99.0 and f["l20_avg_ev"] == 94.5, "max and mean EV")
    check(f["l20_max_distance"] == 309.0, "max distance")
    check(abs(f["l20_air_rate"] - 1.0) < 1e-9, "air rate is fb + ld")
    check(B.window_features([], "l20")["l20_avg_ev"] is None,
          "an empty window reports None for EV, never a fabricated 88.5")


def main() -> int:
    for fn in (test_parse, test_thresholds_match_the_dashboard,
               test_leak_invariant, test_window_math):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} RED")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
