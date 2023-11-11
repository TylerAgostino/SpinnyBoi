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

    def test_quite_a_few(self):
        options = []
        for i in range(20):
            options.append(f'option {i}')
        options.append('some really long fuckoff option with a bunch of text')
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