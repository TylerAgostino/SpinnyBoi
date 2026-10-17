# pyright: basic
"""Data-file resolution tests: the bot's canned-message and reaction files
load relative to the repo, never the process CWD (issue #103)."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.reactionsCog import ReactionsCog
from modules.wheelCog import WheelCog

REPO_ROOT = Path(__file__).resolve().parent.parent


def _real_messages():
    with open(REPO_ROOT / "messages.txt", encoding="utf-8") as fp:
        return {line.strip("\n") for line in fp if line.strip()}


class GetMessageTests(unittest.TestCase):
    def test_message_comes_from_real_messages_file(self):
        message = WheelCog.get_message()
        self.assertIn(message, _real_messages())

    def test_get_message_ignores_process_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertIn(WheelCog.get_message(), _real_messages())
            finally:
                os.chdir(cwd)

    def test_missing_data_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            WheelCog.get_message("spindex_messages.txt")


class SpinOrderTests(unittest.TestCase):
    # __wrapped__ skips the wheel_command defer/webdriver scaffolding.
    def test_spintermix_returns_message_file_and_name(self):
        # Regression for #103: spintermix used to read the nonexistent
        # "spindex_messages.txt" and crash after rendering the GIF.
        stub = SimpleNamespace(get_message=WheelCog.get_message)
        message, file, name = asyncio.run(
            WheelCog.spintermix.__wrapped__(stub, None, custom_options="a, b, c")
        )
        self.assertIn(message, _real_messages())
        self.assertIsNotNone(file)
        self.assertEqual(name, "wheel.gif")

    def test_spintermix_without_options_asks_for_them(self):
        stub = SimpleNamespace(get_message=WheelCog.get_message)
        message, file, name = asyncio.run(
            WheelCog.spintermix.__wrapped__(stub, None, custom_options=None)
        )
        self.assertIn("comma-separated", message)
        self.assertIsNone(file)
        self.assertIsNone(name)


class ReactionsCogStartupTests(unittest.TestCase):
    def test_cog_builds_reaction_dict_from_any_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                cog = ReactionsCog(bot=None)
            finally:
                os.chdir(cwd)
        self.assertTrue(cog.reaction_dict)


if __name__ == "__main__":
    unittest.main()
