import unittest

from modules import WheelSpinner
from modules.wheelCog import _WheelOption


class TestWheelSpinner(unittest.TestCase):
    def setUp(self):
        options_list = [
            "a",
            "b",
            "c",
            "d",
            "e",
        ]
        options = [_WheelOption(option, 1) for option in options_list]
        self.wheel = WheelSpinner.WheelSpinner(options)

    def test_spin(self):
        gif = self.wheel.return_gif()
        with open("out.gif", "wb") as outfile:
            gif.seek(0)
            outfile.write(gif.read())

    def test_faster_spin(self):
        options = [_WheelOption(f"option {i}", 1) for i in range(20)]
        options.append(
            _WheelOption(
                "okay but actually really really long this time because sometimes ausin puts a ton of shit in here",
                999,
            )
        )
        self.wheel = WheelSpinner.WheelSpinner(options)
        anim = self.wheel.animation
        anim.save_svg("out.svg")

    def test_quite_a_few(self):
        options = [_WheelOption(f"option {i}", 1) for i in range(20)]
        options.append(
            _WheelOption(
                "okay but actually really really long this time because sometimes ausin puts a ton of shit in here",
                1,
            )
        )
        self.wheel = WheelSpinner.WheelSpinner(options)
        gif = self.wheel.return_gif()
        with open("out.gif", "wb") as outfile:
            gif.seek(0)
            outfile.write(gif.read())

    def test_abuse(self):
        options = [_WheelOption(f"option {i}", 1) for i in range(100)]
        options.append(
            _WheelOption("some really long fuckoff option with a bunch of text", 1)
        )
        self.wheel = WheelSpinner.WheelSpinner(options)
        self.wheel.animation.save_svg("test.svg")

    def test_probability(self):
        options = [_WheelOption("Rare", 1), _WheelOption("Normal", 9999)]
        wheel = WheelSpinner.WheelSpinner(options)
        assert wheel.weighted_options[0] == options[1]

    def test_get_font_size(self):
        def add_line_breaks(text, soft_wrap=30):
            new_text = ""
            current_line = ""
            longest_line = 0
            for word in text.split(" "):
                if len(current_line) + len(word) > soft_wrap:
                    if len(current_line) > longest_line:
                        longest_line = len(current_line)
                    new_text += current_line + "\n"
                    current_line = ""
                current_line += word + " "
            if len(current_line) > longest_line:
                longest_line = len(current_line)
            new_text += current_line
            return new_text, longest_line

        def get_font_size(text, longest_line, max_width, max_height):
            font_size = 1
            while True:
                width = longest_line * 0.4 * font_size
                height = str(text).count("\n") * font_size * 1.2
                if width <= max_width and height <= max_height:
                    font_size += 1
                else:
                    font_size -= 1
                    return font_size

        text = "some really long fuckoff option with a bunch of text"
        text, max_length = add_line_breaks(text)
        font_size = get_font_size(text, max_length, 100, 40)
        return font_size

    def test_shuffle(self):
        options = [
            "a",
            "b",
            "3",
            "4",
            "5",
            "option 6",
            "option 7",
            "option 8",
            "option 9",
            "option 10",
        ]
        wheel = WheelSpinner.WheelSpinner.create_spindex(options)
        file = wheel.return_gif()
        with open("out.gif", "wb") as outfile:
            file.seek(0)
            outfile.write(file.read())

    def test_many_shuffle(self):
        options = [f"Fake Racer {n}" for n in range(1, 50)]
        wheel = WheelSpinner.WheelSpinner.create_spindex(options)
        file = wheel.return_gif()
        with open("out.gif", "wb") as outfile:
            file.seek(0)
            outfile.write(file.read())
