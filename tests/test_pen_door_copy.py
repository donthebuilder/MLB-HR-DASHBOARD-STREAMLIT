"""PEN-DOOR WORDS AND LINEUP CHECKS (2026-10-09). ALL DATA HERE IS TEST DATA: players are "Test ...",
clubs TST / EXA, ids are made up, get_json and the Discord post are replaced by recorders. Nothing is
fetched and nothing is sent.

Run: PYTHONPATH=. python3 tests/test_pen_door_copy.py
"""
import datetime as dt
import re
import sys
import unittest

from bots import copy_pendoor as words
from bots import pen_door_watch as pdw


class CopyLint(unittest.TestCase):
    def test_samples_have_no_banned_words(self):
        for s in words.samples():
            text = f"{s['title']}\n{s['body']}"
            for pat, why in [(r"\bbots?\b", "bot"), (r"https?:|www\.", "link"), (r"(^|\s)#[A-Za-z]", "hashtag"),
                             (r"chance|probab|odds|likel|\d\s?%", "probability"), (r"Delayed: |DELAYED|POSTPONED", "raw feed word"),
                             (r"\b(bottom|top) \d\b", "raw inning"), (r"Pitching Change:", "raw feed sentence")]:
                self.assertIsNone(re.search(pat, text, re.I if why != "raw feed word" else 0), f"{s['key']}: {why}: {text!r}")

    def test_helpers(self):
        self.assertEqual(words.inning_text("bottom", 7), "Bottom 7th")
        self.assertEqual(words.inning_text("top", 1), "Top 1st")
        self.assertEqual(words.delay_headline("Delayed: RAIN"), "Rain delay")
        self.assertEqual(words.delay_headline("Delayed Start: Rain"), "Rain delay")
        self.assertEqual(words.delay_headline("Postponed"), "Postponed")
        self.assertEqual(words.plural(1, "pick"), "1 pick")
        self.assertEqual(words.plural(2, "pick"), "2 picks")
        self.assertEqual(words.clean_change("Pitching Change: A replaces B."), "A replaces B")

    def test_delay_line_shapes(self):
        self.assertEqual(words.delay_line("TST", "EXA", "Delayed: RAIN", 2), "**Rain delay: TST at EXA.** 2 of your picks play here.")
        self.assertEqual(words.delay_line("TST", "EXA", "Delayed: RAIN", 1), "**Rain delay: TST at EXA.** 1 of your picks plays here.")
        self.assertEqual(words.delay_line("TST", "EXA", "Delayed: RAIN", None), "**Rain delay: TST at EXA.** Lineups are not posted yet.")


GAME = {"gamePk": 100, "status": {"abstractGameState": "Preview", "detailedState": "Delayed: RAIN"},
        "teams": {"home": {"team": {"id": 2}, "score": 0}, "away": {"team": {"id": 1}, "score": 0}}}
ABBRS = {1: "TST", 2: "EXA"}


def pick(pid, name, team, role="HRR"):
    return {"pid": pid, "name": name, "team": team, "role": role}


class StatusSections(unittest.TestCase):
    def setUp(self):
        self._gj = pdw.get_json

    def tearDown(self):
        pdw.get_json = self._gj

    def box(self, home, away):
        return {"teams": {"home": {"team": {"id": 2}, "battingOrder": home}, "away": {"team": {"id": 1}, "battingOrder": away}}}

    def run_sections(self, box, picks):
        pdw.get_json = lambda url: box
        return pdw.gather_status_sections({}, [GAME], {100: picks}, ABBRS)

    def test_delay_counts_only_picks_in_the_posted_order_and_names_no_one(self):
        picks = [pick(1, "Test Alpha", "TST"), pick(2, "Test Bravo", "TST"), pick(3, "Test Charlie", "EXA")]
        order_h = [3, 10, 11, 12, 13, 14, 15, 16, 17]
        order_a = [1, 20, 21, 22, 23, 24, 25, 26, 27]      # Bravo (2) is NOT in his team's order
        sections, _ = self.run_sections(self.box(order_h, order_a), picks)
        delay = dict(sections)[words.SECTION_HEADS["delay"]]
        self.assertEqual(delay, ["**Rain delay: TST at EXA.** 2 of your picks play here."])
        self.assertNotIn("Bravo", " ".join(delay))
        scratch = dict(sections)[words.SECTION_HEADS["scratch"]]
        # the SAME run lists Bravo as out of the lineup, and the delay line above never named him
        self.assertEqual(scratch, ["**Test Bravo** (HRR pick) is out of tonight's TST lineup."])

    def test_delay_before_the_lineup_says_so_and_names_no_one(self):
        picks = [pick(1, "Test Alpha", "TST"), pick(2, "Test Bravo", "TST")]
        sections, _ = self.run_sections(self.box([], []), picks)
        delay = dict(sections)[words.SECTION_HEADS["delay"]]
        self.assertEqual(delay, ["**Rain delay: TST at EXA.** Lineups are not posted yet."])

    def test_lineup_and_final_sections_are_off_by_default(self):
        sections, _ = self.run_sections(self.box([1] * 9, [2] * 9), [pick(1, "Test Alpha", "EXA")])
        d = dict(sections)
        self.assertEqual(d[words.SECTION_HEADS["lineup"]], [])
        self.assertEqual(d[words.SECTION_HEADS["final"]], [])

    def test_scratch_watch_still_lists_a_pick_who_is_out(self):
        g = {**GAME, "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"}}
        pdw.get_json = lambda url: self.box([10] * 9, [20] * 9)
        sections, _ = pdw.gather_status_sections({}, [g], {100: [pick(1, "Test Alpha", "TST")]}, ABBRS)
        self.assertEqual(dict(sections)[words.SECTION_HEADS["scratch"]], ["**Test Alpha** (HRR pick) is out of tonight's TST lineup."])


class PitchingChange(unittest.TestCase):
    def test_a_pick_listed_out_of_the_lineup_is_never_named_as_playing(self):
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        feed = {"gameData": {"teams": {"home": {"abbreviation": "EXA"}, "away": {"abbreviation": "TST"}}},
                "liveData": {"plays": {"allPlays": [{"about": {"halfInning": "top", "inning": 7},
                                                      "playEvents": [{"startTime": now, "details": {"eventType": "pitching_substitution",
                                                                                                    "description": "Pitching Change: Test Reliever replaces Test Starter."}}]}],
                                       "currentPlay": {}}, "linescore": {}}}
        box = {"teams": {"home": {"battingOrder": [9] * 9}, "away": {"battingOrder": [1, 3, 21, 22, 23, 24, 25, 26, 27]}}}
        picks = {100: [pick(1, "Test Alpha", "TST"), pick(2, "Test Bravo", "TST"), pick(3, "Test Charlie", "EXA")],
                 200: [pick(4, "Test Other Game", "TST")]}
        sent = []

        def fake_get(url):
            if "/schedule" in url:
                return {"dates": [{"games": [{"gamePk": 100, "status": {"abstractGameState": "Live"}}]}]}
            if "feed/live" in url:
                return feed
            if "boxscore" in url:
                return box
            return None

        saves = (pdw.get_json, pdw.slate_picks, pdw.post, pdw.time.sleep)
        try:
            pdw.get_json = fake_get
            pdw.slate_picks = lambda: picks
            pdw.post = lambda msg: (sent.append(msg), (1, 0))[1]
            pdw.time.sleep = lambda s: None
            pdw.MAX_LIVE_SWEEPS = 1
            pdw.live_sweep({})
        finally:
            pdw.get_json, pdw.slate_picks, pdw.post, pdw.time.sleep = saves
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("Test Alpha", sent[0])
        self.assertNotIn("Test Bravo", sent[0], "Bravo is listed out of the TST order: he must not be named as playing")
        self.assertNotIn("Test Charlie", sent[0], "Charlie is on the other team")
        self.assertNotIn("Test Other Game", sent[0], "a pick from another game is never named")
        self.assertIn("Bottom", words.inning_text("bottom", 7))
        self.assertIn("Top 7th", sent[0])
        self.assertIn("Test Reliever replaces Test Starter", sent[0])
        self.assertNotIn("Pitching Change:", sent[0])


if __name__ == "__main__":
    unittest.main()
