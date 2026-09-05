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


def test_by_book_carries_each_books_own_quote():
    """Two books, two prices: the median IS the better one, so only by_book
    can show the disagreement. A book on a different line keeps its line."""
    import odds_fetch as of
    rows = [
        dict(norm="x", market="batter_home_runs", name="X", book="DraftKings", point=0.5, side="over", price=310, away="A", home="B", commence="t"),
        dict(norm="x", market="batter_home_runs", name="X", book="Fanatics", point=0.5, side="over", price=360, away="A", home="B", commence="t"),
        dict(norm="y", market="batter_hits", name="Y", book="DraftKings", point=1.5, side="over", price=120, away="A", home="B", commence="t"),
        dict(norm="y", market="batter_hits", name="Y", book="Fanatics", point=0.5, side="over", price=-250, away="A", home="B", commence="t"),
    ]
    c = of.consensus(rows)
    x = c["x"]["batter_home_runs"]
    assert x["over"] == x["best_over"] == 360          # the thing by_book exists to get around
    assert x["by_book"]["DraftKings"]["over"] == 310
    assert x["by_book"]["Fanatics"]["over"] == 360
    y = c["y"]["batter_hits"]
    assert y["by_book"]["DraftKings"]["line"] == 1.5 and y["by_book"]["Fanatics"]["line"] == 0.5
