#!/usr/bin/env python3
"""
Tests for the pitch capture added to bots/backfill_bbe_history.py (2026-08-23).

Runnable BOTH as a pytest module and as a plain script.

WHY IT EXISTS
-------------
Donovan, 2026-08-23: line drives at "90 ish off bat good angle and 210is plus a
few time on eith one or two of the miain ptches they have" then a homer "the
next day or night before or days to come."

The line-drive half of that was testable and came back null once the bat's own
quality was controlled. **The pitch half was not testable at all**, because the
harvest recorded what the ball did and never recorded what was thrown. These
three fields are what make the rest of the hypothesis answerable.

Both source paths are already proven in this repo — details.type.code is what
backfill_hr_events.py reads, pitchData.startSpeed is what
live_results_tracker.py reads — so nothing here is a guessed field name. That
matters: this repo has the receipt for what guessing an API shape costs.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import backfill_bbe_history as B  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        return
    FAILS.append(msg)
    print("  RED  " + msg)


FEED = {
    "liveData": {"plays": {"allPlays": [
        {   # a homer on a hanging slider
            "result": {"event": "Home Run", "eventType": "home_run"},
            "matchup": {"batter": {"id": 1, "fullName": "A"}, "pitcher": {"id": 9},
                        "batSide": {"code": "R"}, "pitchHand": {"code": "L"}},
            "playEvents": [
                {"details": {"code": "B", "type": {"code": "FF", "description": "Four-Seam Fastball"}},
                 "pitchData": {"startSpeed": 95.1}},
                {"details": {"code": "X", "type": {"code": "SL", "description": "Slider"}},
                 "pitchData": {"startSpeed": 84.3},
                 "hitData": {"launchSpeed": 104.0, "launchAngle": 27.0,
                             "totalDistance": 407, "trajectory": "fly_ball"}},
            ],
        },
        {   # a line drive with no pitchData at all — must not fabricate a velocity
            "result": {"event": "Single", "eventType": "single"},
            "matchup": {"batter": {"id": 2, "fullName": "B"}, "pitcher": {"id": 9}},
            "playEvents": [
                {"details": {"code": "X", "type": {"code": "CH", "description": "Changeup"}},
                 "hitData": {"launchSpeed": 91.0, "launchAngle": 15.0,
                             "totalDistance": 214, "trajectory": "line_drive"}},
            ],
        },
        {   # tracked ball, pitch type missing entirely — empty string, not a guess
            "result": {"event": "Flyout", "eventType": "field_out"},
            "matchup": {"batter": {"id": 3, "fullName": "C"}, "pitcher": {"id": 9}},
            "playEvents": [
                {"details": {"code": "X"},
                 "hitData": {"launchSpeed": 88.0, "launchAngle": 30.0,
                             "totalDistance": 300, "trajectory": "fly_ball"}},
            ],
        },
    ]}}
}


def test_pitch_is_captured_from_the_ball_that_was_hit():
    print("the captured pitch is the one that was HIT, not the first of the at-bat")
    rows = B.bbe_from_payload(FEED, 1, date(2026, 8, 1))
    check(len(rows) == 3, f"three batted balls in the fixture, got {len(rows)}")
    hr = [r for r in rows if r["is_hr"]][0]
    check(hr["pitch_type"] == "SL",
          f"the homer came on the slider that was put in play, not the 0-0 fastball: got {hr['pitch_type']}")
    check(hr["pitch_name"] == "Slider", f"pitch name carried through: got {hr['pitch_name']}")
    check(hr["pitch_velocity"] == 84.3, f"velocity is the hit pitch's 84.3, got {hr['pitch_velocity']}")


def test_missing_pitch_data_is_not_invented():
    print("missing pitch data stays missing")
    rows = B.bbe_from_payload(FEED, 1, date(2026, 8, 1))
    ld = [r for r in rows if r["bb_type"] == "line_drive"][0]
    check(ld["pitch_type"] == "CH", "pitch type still captured when pitchData is absent")
    check(ld["pitch_velocity"] is None,
          f"no pitchData means no velocity — never 0.0, got {ld['pitch_velocity']}")
    nop = [r for r in rows if r["batter_id"] == 3][0]
    check(nop["pitch_type"] == "" and nop["pitch_name"] == "",
          "an untyped pitch publishes empty strings, not a guessed type")
    check(nop["pitch_velocity"] is None, "an untyped pitch has no velocity")


def test_the_feed_asks_for_what_it_parses():
    """A parser that reads a key the request never asked for gets None forever,
    silently. That is how zone_profile spent a week 'empty'."""
    print("the field whitelist actually requests the pitch keys")
    for key in ("pitchData", "startSpeed", "type", "description", "code"):
        check(key in B.FEED_FIELDS, f"FEED_FIELDS does not request {key} — the parse would always be empty")


def test_the_ball_fields_still_work():
    print("nothing about the batted ball regressed")
    rows = B.bbe_from_payload(FEED, 1, date(2026, 8, 1))
    hr = [r for r in rows if r["is_hr"]][0]
    check(hr["launch_speed"] == 104.0 and hr["launch_angle"] == 27.0, "EV/LA intact")
    check(hr["is_barrel"] is True, "104 mph at 27 degrees is still a barrel")
    check(hr["total_distance"] == 407 and hr["is_400_plus"] is True, "distance intact")
    ld = [r for r in rows if r["bb_type"] == "line_drive"][0]
    check(ld["is_barrel"] is False, "91 mph at 15 degrees is not a barrel")
    check(ld["total_distance"] == 214, "the 214-foot line drive is recorded as such")


def main() -> int:
    for fn in (test_pitch_is_captured_from_the_ball_that_was_hit,
               test_missing_pitch_data_is_not_invented,
               test_the_feed_asks_for_what_it_parses,
               test_the_ball_fields_still_work):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} RED")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
