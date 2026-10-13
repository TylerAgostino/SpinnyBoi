# pyright: basic
"""Unit tests for the WheelSpinner rng seam and explicit SpinResult (no GIF).

Run: .venv\\Scripts\\python -m unittest tests.test_WheelSpinner_result
"""
import random
import unittest

from modules.WheelSpinner import SpinResult, WheelSpinner


class Opt:
    def __init__(self, option, weight=1, on_select=None, include_text=""):
        self.option = option
        self.weight = weight
        self.on_select = on_select
        self.include_text = include_text


class DeterministicSpinTests(unittest.TestCase):
    def test_same_seed_same_winner(self):
        opts = [Opt("A"), Opt("B"), Opt("C"), Opt("D")]
        first = WheelSpinner([Opt(o.option) for o in opts], rng=random.Random(42))
        second = WheelSpinner([Opt(o.option) for o in opts], rng=random.Random(42))
        self.assertEqual(first.result.winner, second.result.winner)

    def test_winner_follows_seed(self):
        opts = lambda: [Opt("A"), Opt("B"), Opt("C"), Opt("D")]
        winners = {
            WheelSpinner(opts(), rng=random.Random(seed)).result.winner
            for seed in range(25)
        }
        self.assertGreater(len(winners), 1, "rng must decide the winner")
        self.assertTrue(winners <= {"A", "B", "C", "D"})

    def test_heavy_option_wins_deterministically(self):
        spinner = WheelSpinner([Opt("light", weight=0), Opt("heavy", weight=98)])
        self.assertEqual(spinner.result.winner, "heavy")

    def test_result_carries_response_and_next_spin(self):
        opts = [
            Opt("solo", weight=50, include_text="go solo!", on_select="Next Tab some filter")
        ]
        spinner = WheelSpinner(opts, rng=random.Random(7))
        result = spinner.result
        self.assertIsInstance(result, SpinResult)
        self.assertEqual(result.winner, "solo")
        self.assertEqual(result.response, "go solo!")
        self.assertEqual(result.next_spin, ("Next", "Tab some filter"))

    def test_zero_weight_options_raise(self):
        with self.assertRaises(ValueError):
            WheelSpinner([Opt("nothing", weight=0)], rng=random.Random(1))

    def test_create_spindex_still_decides_a_winner(self):
        spinner = WheelSpinner.create_spindex(["x", "y", "z"])
        self.assertIn(spinner.result.winner, {"x", "y", "z"})


if __name__ == "__main__":
    unittest.main()
