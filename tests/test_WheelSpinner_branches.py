# pyright: basic
"""WheelSpinner branches missed by the gif-suite: get_slice default colour,
add_line_breaks/get_font_size maths, Comic Sans branch, failing driver quit."""

import io
import unittest
from unittest import mock

from PIL import Image

import modules.WheelSpinner as wsModule
from modules.WheelSpinner import WheelSpinner




def render(piece):
    import drawsvg

    canvas = drawsvg.Drawing(200, 200)
    canvas.append(piece)
    return canvas.as_svg()


class GetSliceDefaultColourTests(unittest.TestCase):
    def test_default_colour_is_hsl_from_three_random_rolls(self):
        calls = []

        def roll(a, b):
            calls.append((a, b))
            return {0: 7, 1: 40, 2: 60}[len(calls) - 1]

        with mock.patch.object(wsModule.random, "randint", side_effect=roll):
            piece = WheelSpinner.get_slice(0, 30, "opt")
        self.assertEqual(calls, [(0, 360), (30, 100), (30, 100)])
        self.assertIn("hsl(7, 40%, 60%)", render(piece))

    def test_explicit_colour_skips_random(self):
        with mock.patch.object(wsModule.random, "randint") as roll:
            piece = WheelSpinner.get_slice(0, 30, "opt", color="crimson")
        roll.assert_not_called()
        self.assertIn("crimson", render(piece))


class ComicSansBranchTests(unittest.TestCase):
    def options(self):
        from modules.wheelCog import _WheelOption

        return [_WheelOption("alpha", 1), _WheelOption("beta", 1)]

    def test_one_in_thirty_rolls_selects_comic_sans(self):
        with mock.patch.object(
            wsModule.random,
            "randint",
            side_effect=lambda a, b: 0 if (a, b) == (0, 30) else 1,
        ):
            wheel = WheelSpinner(self.options())
        self.assertIn("Comic Sans MS", wheel.animation.as_svg())

    def test_other_rolls_keep_the_normal_font(self):
        with mock.patch.object(
            wsModule.random,
            "randint",
            side_effect=lambda a, b: 1 if (a, b) == (0, 30) else 5,
        ):
            wheel = WheelSpinner(self.options())
        self.assertNotIn("Comic Sans MS", wheel.animation.as_svg())

    def test_spindex_animation_has_the_same_coin_flip(self):
        wheel = WheelSpinner(self.options())
        with mock.patch.object(
            wsModule.random,
            "randint",
            side_effect=lambda a, b: 0 if (a, b) == (0, 30) else 1,
        ):
            spindex = wheel.generate_spindex_animation()
        self.assertIn("Comic Sans MS", spindex.as_svg())


class AddLineBreaksTests(unittest.TestCase):
    def test_short_text_stays_on_one_line(self):
        text, longest = WheelSpinner.add_line_breaks("alpha beta", soft_wrap=20)
        self.assertEqual(text, "alpha beta ")
        self.assertEqual(longest, len("alpha beta "))

    def test_wraps_at_word_boundaries_within_soft_wrap(self):
        text, longest = WheelSpinner.add_line_breaks(
            "alpha beta gamma delta", soft_wrap=12
        )
        lines = text.split("\n")
        self.assertEqual(lines, ["alpha beta ", "gamma delta "])
        for line in lines:
            self.assertLessEqual(len(line), 12)
        self.assertEqual(" ".join(text.split()), "alpha beta gamma delta")
        self.assertEqual(longest, 12)


class GetFontSizeTests(unittest.TestCase):
    def test_size_fits_and_next_integer_overflows(self):
        size = WheelSpinner.get_font_size("abc\n", 10, 40, 30)
        self.assertEqual(size, 8)
        width = 10 * 0.5 * size
        height = (1 + 1) * size * 1.2
        self.assertLessEqual(width, 40)
        self.assertLessEqual(height, 30)
        too_big = 10 * 0.5 * (size + 1) > 40 or (1 + 1) * (size + 1) * 1.2 > 30
        self.assertTrue(too_big)

    def test_more_text_shrinks_font(self):
        short = WheelSpinner.get_font_size("abc", 3, 40, 30)
        tall = WheelSpinner.get_font_size("a\nb\nc\nd\ne", 1, 40, 30)
        self.assertLess(tall, short)


class ReturnGifQuitFailureTests(unittest.TestCase):
    class FakeDriver:
        def __init__(self, quit_boom=False):
            self.quit_boom = quit_boom
            self.quit_called = False
            self.shots = 0

        def get(self, url):
            pass

        def execute_script(self, script):
            pass

        def execute_async_script(self, script, t):
            pass

        def get_screenshot_as_png(self):
            buf = io.BytesIO()
            Image.new("RGB", (4, 3), (self.shots * 40, 10, 10)).save(buf, format="PNG")
            self.shots += 1
            return buf.getvalue()

        def quit(self):
            self.quit_called = True
            if self.quit_boom:
                raise RuntimeError("geckodriver already dead")

    def setUp(self):
        from modules.wheelCog import _WheelOption

        self.wheel = WheelSpinner(
            [_WheelOption("alpha", 1), _WheelOption("beta", 1)]
        )

    def test_own_driver_quit_failure_is_logged_not_fatal(self):
        driver = self.FakeDriver(quit_boom=True)
        fake_selenium = mock.MagicMock()
        fake_selenium.Firefox.return_value = driver
        import os

        created = []
        real_makedirs = os.makedirs

        def spy_makedirs(path, *args, **kwargs):
            created.append(path)
            return real_makedirs(path, *args, **kwargs)

        with mock.patch("os.makedirs", side_effect=spy_makedirs):
            with mock.patch("selenium.webdriver", fake_selenium):
                with mock.patch.object(self.wheel.animation, "save_html"):
                    with self.assertLogs(level="ERROR") as captured:
                        fh = self.wheel.return_gif(frame_count=2)
        self.assertTrue(driver.quit_called)
        self.assertIn("Failed to quit", "\n".join(captured.output))
        self.assertEqual(Image.open(fh).n_frames, 2)
        self.assertFalse(os.path.exists(created[0]))

    def test_run_directory_is_removed_after_capture(self):
        driver = self.FakeDriver()
        import os

        created = []
        real_makedirs = os.makedirs

        def spy_makedirs(path, *args, **kwargs):
            created.append(path)
            return real_makedirs(path, *args, **kwargs)

        with mock.patch("os.makedirs", side_effect=spy_makedirs):
            with mock.patch.object(self.wheel.animation, "save_html"):
                self.wheel.return_gif(driver=driver, frame_count=2)
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))


if __name__ == "__main__":
    unittest.main()
