import unittest
from types import SimpleNamespace

from bots import mlb_dashboard as model


def hitter(hr, pa):
    return SimpleNamespace(season_hr=hr, season_pa=pa, hr_per_pa=(hr / pa if pa else 0.0))


class PairPoolProbabilityTests(unittest.TestCase):
    def test_small_samples_are_shrunk(self):
        tiny = model._season_hr_game_probability(hitter(2, 10))
        established = model._season_hr_game_probability(hitter(20, 100))
        self.assertLess(tiny, established)

    def test_two_plus_is_lower_than_one_plus(self):
        players = [hitter(30, 500) for _ in range(4)]
        one = model._ticket_probability_at_least(players, 1)
        two = model._ticket_probability_at_least(players, 2)
        three = model._ticket_probability_at_least(players, 3)
        perfect = model._ticket_probability_at_least(players, 4)
        self.assertGreater(one, two)
        self.assertGreater(two, three)
        self.assertGreater(three, perfect)

    def test_pair_both_probability_is_product(self):
        a, b = hitter(25, 500), hitter(20, 500)
        pa = model._season_hr_game_probability(a)
        pb = model._season_hr_game_probability(b)
        self.assertAlmostEqual(model._ticket_probability_at_least([a, b], 2), pa * pb, places=4)


if __name__ == "__main__":
    unittest.main()
