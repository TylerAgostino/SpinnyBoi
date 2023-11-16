import unittest
from modules import WheelSpinner


class TestWheelSpinner(unittest.TestCase):
    def setUp(self):
        self.wheel = WheelSpinner.WheelSpinner()

    def test_spin(self):
        gif = self.wheel.return_gif()
        with open('out.gif', 'wb') as outfile:
            gif.seek(0)
            outfile.write(gif.read())

    def test_faster_spin(self):
        options = []
        for i in range(20):
            options.append(f'option {i}')
        options.append(('okay but actually really really long this time because sometimes ausin puts a ton of shit in here', 999))
        self.wheel = WheelSpinner.WheelSpinner(options)
        anim = self.wheel.animation
        anim.save_svg('out.svg')

    def test_quite_a_few(self):
        options = []
        for i in range(20):
            options.append(f'option {i}')
        options.append(('some really long fuckoff option with a bunch of text', 999))
        self.wheel = WheelSpinner.WheelSpinner(options)
        gif = self.wheel.return_gif()
        with open('out.gif', 'wb') as outfile:
            gif.seek(0)
            outfile.write(gif.read())

    def test_abuse(self):
        options = []
        for i in range(100):
            options.append(f'option {i}')
        options.append('some really long fuckoff option with a bunch of text')
        self.wheel = WheelSpinner.WheelSpinner(options)
        self.wheel.save_svg('test.svg')


    def test_probability(self):
        options = [('Rare', 1), ('Normal', 9999)]
        wheel = WheelSpinner.WheelSpinner(options)
        assert wheel.weighted_options[0] == options[1]

    def test_get_font_size(self):
        def add_line_breaks(text, soft_wrap=30):
            # a new string of words from the text until we reach the soft_wrap limit, only adding whole words
            new_text = ''
            current_line = ''
            longest_line = 0
            for word in text.split(' '):
                if len(current_line) + len(word) > soft_wrap:
                    if len(current_line) > longest_line:
                        longest_line = len(current_line)
                    new_text += current_line + '\n'
                    current_line = ''
                current_line += word + ' '
            if len(current_line) > longest_line:
                longest_line = len(current_line)
            new_text += current_line
            return new_text, longest_line

        def get_font_size(text, longest_line, max_width, max_height):
            font_size = 1
            while True:
                width = longest_line*0.4*font_size
                height = str(text).count('\n')*font_size*1.2
                if width <= max_width and height <= max_height:
                    font_size += 1
                else:
                    font_size -= 1
                    return font_size
        text = "some really long fuckoff option with a bunch of text"
        text, max_length = add_line_breaks(text)
        font_size = get_font_size(text, max_length, 100, 40)
        return font_size