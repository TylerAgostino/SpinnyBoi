# pyright: basic
"""Tests for per-option weights in custom wheel options (issue #168)."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.wheelCog import WheelCog, parse_custom_options


def _pairs(options):
    return [(o.option, o.weight) for o in parse_custom_options(options)]


class ParseCustomOptionsTests(unittest.TestCase):
    def test_weighted_option(self):
        self.assertEqual(_pairs("sorry I'm late=5"), [("sorry I'm late", 5)])

    def test_mixed_weighted_and_plain(self):
        self.assertEqual(
            _pairs("sorry=5, on time=50, maybe"),
            [("sorry", 5), ("on time", 50), ("maybe", 1)],
        )

    def test_whitespace_around_option_and_weight(self):
        self.assertEqual(_pairs(" late = 3 , early=1 "), [("late", 3), ("early", 1)])

    def test_zero_weight_is_literal_text(self):
        self.assertEqual(_pairs("broken=0"), [("broken=0", 1)])

    def test_bare_equals_is_literal_text(self):
        self.assertEqual(_pairs("what="), [("what=", 1)])

    def test_non_numeric_weight_is_literal_text(self):
        self.assertEqual(_pairs("team=x"), [("team=x", 1)])

    def test_double_equals_is_literal_text(self):
        self.assertEqual(_pairs("a==2"), [("a==2", 1)])

    def test_empty_options_dropped(self):
        self.assertEqual(_pairs("a,, b ,"), [("a", 1), ("b", 1)])

    def test_internal_equals_survives_with_trailing_weight(self):
        self.assertEqual(_pairs("red=x3=2"), [("red=x3", 2)])
        self.assertEqual(_pairs("a=b=3"), [("a=b", 3)])

    def test_weight_at_cap_is_honored(self):
        self.assertEqual(_pairs("a=100"), [("a", 100)])

    def test_weight_over_cap_is_literal_text(self):
        self.assertEqual(_pairs("a=101"), [("a=101", 1)])
        self.assertEqual(_pairs("a=99999999999"), [("a=99999999999", 1)])

    def test_all_empty_yields_no_options(self):
        self.assertEqual(parse_custom_options(" , , "), [])


class SpinCustomWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_spin_custom_passes_parsed_weights_to_wheel(self):
        captured = {}

        def fake_wheel(opts):
            captured["opts"] = opts
            return MagicMock()

        cog = SimpleNamespace(get_message=lambda: "spinning")
        with patch("modules.wheelCog.WheelSpinner.WheelSpinner", side_effect=fake_wheel):
            message, file, filename, role = await WheelCog.generic_spin_custom(
                cog, None, custom_options="sorry=3, not sorry=7, whatever"
            )
        pairs = [(o.option, o.weight) for o in captured["opts"]]
        self.assertEqual(
            pairs, [("sorry", 3), ("not sorry", 7), ("whatever", 1)]
        )
        self.assertEqual(filename, "wheel.gif")
        self.assertEqual(role, None)

    async def test_spin_custom_rejects_all_empty_input(self):
        cog = SimpleNamespace(get_message=lambda: "spinning")
        message, file, filename, role = await WheelCog.generic_spin_custom(
            cog, None, custom_options=" , , "
        )
        self.assertIn("comma-separated options", message)
        self.assertIsNone(file)
        self.assertIsNone(filename)

if __name__ == "__main__":
    unittest.main()
