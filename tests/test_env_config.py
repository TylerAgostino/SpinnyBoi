# pyright: basic
"""Direct tests for the shared env-var parsing shapes in modules/env_config."""
import unittest

from modules.env_config import env_enabled, env_float, env_int, env_int_or_none


class EnvEnabledTests(unittest.TestCase):
    def test_unset_is_default_true(self):
        self.assertTrue(env_enabled({}, "X"))

    def test_false_words_disable(self):
        for raw in ("0", "false", "FALSE", " off ", "no"):
            self.assertFalse(env_enabled({"X": raw}, "X"), raw)

    def test_other_values_enable(self):
        self.assertTrue(env_enabled({"X": "yes"}, "X"))

    def test_default_false(self):
        self.assertFalse(env_enabled({}, "X", default=False))
        self.assertTrue(env_enabled({"X": "1"}, "X", default=False))


class EnvIntTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(env_int({"X": "7"}, "X", 5, minimum=1, maximum=10), 7)

    def test_unset_or_garbage_is_default(self):
        self.assertEqual(env_int({}, "X", 5, minimum=1), 5)
        self.assertEqual(env_int({"X": "  "}, "X", 5, minimum=1), 5)
        self.assertEqual(env_int({"X": "abc"}, "X", 5, minimum=1), 5)

    def test_clamps_to_bounds(self):
        self.assertEqual(env_int({"X": "0"}, "X", 5, minimum=1), 1)
        self.assertEqual(env_int({"X": "99"}, "X", 5, minimum=1, maximum=10), 10)


class EnvIntOrNoneTests(unittest.TestCase):
    def test_unset_or_garbage_is_none(self):
        self.assertIsNone(env_int_or_none({}, "X"))
        self.assertIsNone(env_int_or_none({"X": "junk"}, "X"))

    def test_valid(self):
        self.assertEqual(env_int_or_none({"X": "42"}, "X"), 42)


class EnvFloatTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(env_float({"X": "0.5"}, "X", 0.35, 0.0, 1.0), 0.5)

    def test_unset_or_garbage_is_default(self):
        self.assertEqual(env_float({}, "X", 0.35, 0.0, 1.0), 0.35)
        self.assertEqual(env_float({"X": "nan-ish"}, "X", 0.35, 0.0, 1.0), 0.35)

    def test_clamps_to_bounds(self):
        self.assertEqual(env_float({"X": "9"}, "X", 0.35, 0.0, 1.0), 1.0)
        self.assertEqual(env_float({"X": "-9"}, "X", 0.35, 0.0, 1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
