import unittest
from modules import WheelSpinner


class TestWheelSpinner(unittest.TestCase):
    def setUp(self):
        self.wheel = WheelSpinner.WheelSpinner()

    def test_spin(self):
        print(self.wheel.save_svg('test.svg'))

    def test_quite_a_few(self):
        options = []
        for i in range(20):
            options.append(f'option {i}')
        options.append('some really long fuckoff option with a bunch of text')
        self.wheel = WheelSpinner.WheelSpinner(options)
        print(self.wheel.save_svg('test.svg'))

    def test_abuse(self):
        options = []
        for i in range(100):
            options.append(f'option {i}')
        options.append('some really long fuckoff option with a bunch of text')
        self.wheel = WheelSpinner.WheelSpinner(options)
        self.wheel.save_svg('test.svg')
