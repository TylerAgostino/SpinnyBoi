# pyright: basic
import unittest

import discord

from modules.embed_utils import status_embed


class StatusEmbedTests(unittest.TestCase):
    def test_success_shape(self):
        embed = status_embed("Title", "body")
        self.assertEqual(embed.title, "Title")
        self.assertEqual(embed.description, "body")
        self.assertEqual(embed.color, discord.Color.green())

    def test_failure_shape(self):
        embed = status_embed("Nope", "why", ok=False)
        self.assertEqual(embed.color, discord.Color.red())

    def test_description_optional(self):
        embed = status_embed("Bare")
        self.assertIsNone(embed.description)


if __name__ == "__main__":
    unittest.main()
