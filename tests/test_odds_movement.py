import unittest

from bots.odds_fetch import attach_movement


class OddsMovementTest(unittest.TestCase):
    def test_shortening_is_positive_probability_move(self):
        old = {
            "batter_home_runs": {
                "line": 0.5,
                "over": 500,
                "implied": 16.7,
            }
        }
        new = {
            "batter_home_runs": {
                "line": 0.5,
                "over": 400,
                "implied": 20.0,
            }
        }
        result = attach_movement(new, old, "2026-08-26T18:00:00+00:00",
                                 "2026-08-26T16:00:00+00:00", True)
        move = result["batter_home_runs"]["movement"]
        self.assertEqual(move["opening_over"], 500)
        self.assertEqual(move["from_open_pp"], 3.3)
        self.assertEqual(move["from_previous_pp"], 3.3)
        self.assertFalse(move["line_changed"])
        self.assertEqual(len(move["history"]), 2)

    def test_line_change_has_no_price_delta(self):
        old = {"batter_hits": {"line": 0.5, "over": -120, "implied": 54.5}}
        new = {"batter_hits": {"line": 1.5, "over": 175, "implied": 36.4}}
        result = attach_movement(new, old, "2026-08-26T18:00:00+00:00",
                                 "2026-08-26T16:00:00+00:00", True)
        move = result["batter_hits"]["movement"]
        self.assertTrue(move["line_changed"])
        self.assertIsNone(move["from_open_pp"])
        self.assertIsNone(move["from_previous_pp"])

    def test_new_slate_resets_opening(self):
        old = {"batter_home_runs": {"line": 0.5, "over": 300, "implied": 25.0}}
        new = {"batter_home_runs": {"line": 0.5, "over": 600, "implied": 14.3}}
        result = attach_movement(new, old, "2026-08-27T15:00:00+00:00",
                                 "2026-08-26T23:00:00+00:00", False)
        move = result["batter_home_runs"]["movement"]
        self.assertEqual(move["opening_over"], 600)
        self.assertEqual(move["from_open_pp"], 0.0)
        self.assertEqual(len(move["history"]), 1)

    def test_unchanged_quote_keeps_original_open_time(self):
        old = {
            "batter_home_runs": {
                "line": 0.5,
                "over": 500,
                "implied": 16.7,
                "movement": {
                    "history": [{"at": "2026-08-26T14:00:00+00:00",
                                 "line": 0.5, "over": 500, "implied": 16.7}]
                },
            }
        }
        new = {"batter_home_runs": {"line": 0.5, "over": 500, "implied": 16.7}}
        result = attach_movement(new, old, "2026-08-26T18:00:00+00:00",
                                 "2026-08-26T16:00:00+00:00", True)
        move = result["batter_home_runs"]["movement"]
        self.assertEqual(move["opened_at"], "2026-08-26T14:00:00+00:00")
        self.assertEqual(len(move["history"]), 1)


if __name__ == "__main__":
    unittest.main()
