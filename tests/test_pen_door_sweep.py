"""Pen Door Watch: the live sweep's date window and its early stop (known-issues #88, 2026-10-26). TEST data only.

1. The sweep asks for TWO days of games. MLB files a game under its local date, so after 00:00 UTC the evening slate sits under the
   previous date; asking for the UTC date alone saw none of it.
2. With nothing live the run stops after the first look: no 90-second wait, no slate download.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bots import pen_door_watch as pdw  # noqa: E402


class LiveSweepWindow(unittest.TestCase):
    def setUp(self):
        self.saves = (pdw.get_json, pdw.slate_picks, pdw.post, pdw.time.sleep, pdw.MAX_LIVE_SWEEPS)
        self.urls, self.slates, self.sleeps = [], [], []
        pdw.slate_picks = lambda: (self.slates.append(1), {})[1]
        pdw.post = lambda msg: (1, 0)
        pdw.time.sleep = lambda s: self.sleeps.append(s)
        pdw.MAX_LIVE_SWEEPS = 3

    def tearDown(self):
        pdw.get_json, pdw.slate_picks, pdw.post, pdw.time.sleep, pdw.MAX_LIVE_SWEEPS = self.saves

    def _feed(self, live):
        def fake_get(url):
            self.urls.append(url)
            if "/schedule" in url:
                state = "Live" if live else "Preview"
                return {"dates": [{"games": [{"gamePk": 100, "status": {"abstractGameState": state}}]}]}
            return None   # no feed: nothing to announce
        pdw.get_json = fake_get

    def test_the_schedule_window_is_yesterday_through_today(self):
        self._feed(live=False)
        pdw.live_sweep({})
        sched = [u for u in self.urls if "/schedule" in u]
        self.assertEqual(len(sched), 1)
        m = re.search(r"startDate=(\d{4}-\d{2}-\d{2})&endDate=(\d{4}-\d{2}-\d{2})", sched[0])
        self.assertIsNotNone(m, sched[0])
        start, end = (dt.date.fromisoformat(x) for x in m.groups())
        self.assertEqual((end - start).days, 1)
        self.assertNotIn("&date=", sched[0])

    def test_nothing_live_stops_after_one_look_without_waiting_or_downloading_the_slate(self):
        self._feed(live=False)
        sent, failed = pdw.live_sweep({})
        self.assertEqual((sent, failed), (0, 0))
        self.assertEqual(self.sleeps, [], "nothing is live: no 90-second wait")
        self.assertEqual(self.slates, [], "nothing is live: the slate file is not downloaded")
        self.assertEqual(len([u for u in self.urls if "/schedule" in u]), 1, "one look, then stop")

    def test_a_live_game_still_gets_every_sweep_and_the_slate(self):
        self._feed(live=True)
        pdw.live_sweep({})
        self.assertEqual(len(self.slates), 3, "three sweeps, three slate reads, as before")
        self.assertEqual(self.sleeps, [pdw.LIVE_SWEEP_GAP_S, pdw.LIVE_SWEEP_GAP_S])


if __name__ == "__main__":
    unittest.main()
