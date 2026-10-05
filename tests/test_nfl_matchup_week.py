"""The week's matchup snapshot (write_nfl_matchup_week): written before the first
Sunday kickoff with only the angle keys, never after it, never for the next-week build.
TEST data: two hand-made games, not a real slate."""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_bot as nb  # noqa: E402

GAMES = [  # TEST: a Thursday night game and a Sunday 1 PM ET game (17:00Z in October)
    {"kickoff": "2026-10-09T00:15:00Z"},
    {"kickoff": "2026-10-11T17:00:00Z"},
]
MATCHUP = {"season": 2026, "dvp": {"X": 1}, "roles": {"p1": "WR1"}, "red_zone": {}, "snaps": {}, "field": {"big": True}}


def test_first_sunday_kickoff_skips_thursday():
    assert nb.first_sunday_kickoff(GAMES) == dt.datetime(2026, 10, 11, 17, 0, tzinfo=dt.timezone.utc)


def test_written_before_sunday_with_angle_keys_only():
    with tempfile.TemporaryDirectory() as d:
        p = nb.write_nfl_matchup_week(MATCHUP, GAMES, Path(d), "nfl_", 2026, 6, now=dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc))
        assert p and p.name == "nfl_matchup_week_2026_w06.json"
        body = json.loads(p.read_text())
        assert body["week"] == 6 and body["roles"] == {"p1": "WR1"} and "field" not in body


def test_not_written_after_sunday_kickoff_or_for_next_week():
    with tempfile.TemporaryDirectory() as d:
        after = dt.datetime(2026, 10, 11, 18, tzinfo=dt.timezone.utc)
        assert nb.write_nfl_matchup_week(MATCHUP, GAMES, Path(d), "nfl_", 2026, 6, now=after) is None
        before = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)
        assert nb.write_nfl_matchup_week(MATCHUP, GAMES, Path(d), "nfl_next_", 2026, 7, now=before) is None
        assert list(Path(d).iterdir()) == []
