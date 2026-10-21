# pyright: basic
"""ReactionsCog.on_message: trigger matching, emote choice, per-user specials, error isolation."""

import asyncio
import unittest
from unittest import mock

import modules.reactionsCog as reactionsCogModule
from modules.reactionsCog import ReactionsCog

LUCKY_DRIVER_ID = 292447304395522048
SECOND_DRIVER_ID = 267830473328295937


def make_cog(reaction_dict, bot_user=None):
    cog = ReactionsCog.__new__(ReactionsCog)  # skip reactions.txt file load
    cog.bot = mock.MagicMock()
    cog.bot.user = bot_user if bot_user is not None else object()
    cog.reaction_dict = reaction_dict
    return cog


def make_message(author_id=111, content="hi there"):
    message = mock.AsyncMock()
    message.author = mock.MagicMock()
    message.author.id = author_id
    message.content = content
    return message


class ReactionsListenerTests(unittest.TestCase):
    def setUp(self):
        self.choice = mock.Mock(return_value="emote-a")
        self.randint = mock.Mock(return_value=0)
        for patched in (
            mock.patch.object(reactionsCogModule.random, "choice", self.choice),
            mock.patch.object(reactionsCogModule.random, "randint", self.randint),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def run_on(self, cog, message):
        asyncio.run(cog.on_message(message))

    def test_own_bot_message_is_ignored(self):
        bot_user = object()
        cog = make_cog({"hi": ["emote-a"]}, bot_user=bot_user)
        message = make_message(content="hi")
        message.author = bot_user
        self.run_on(cog, message)
        message.add_reaction.assert_not_called()

    def test_substring_match_adds_chosen_emote(self):
        cog = make_cog({"world": ["emote-a", "emote-b"]})
        message = make_message(content="Hello WORLD everyone")
        self.run_on(cog, message)
        self.choice.assert_called_once_with(["emote-a", "emote-b"])
        message.add_reaction.assert_awaited_once_with("emote-a")

    def test_no_match_adds_nothing(self):
        cog = make_cog({"zzz": ["emote-a"]})
        message = make_message(content="nothing here")
        self.run_on(cog, message)
        message.add_reaction.assert_not_called()

    def test_none_content_is_stringified(self):
        cog = make_cog({"none": ["emote-a"]})
        message = make_message(content=None)
        self.run_on(cog, message)
        message.add_reaction.assert_awaited_once_with("emote-a")

    def test_add_reaction_failure_is_logged_not_raised(self):
        cog = make_cog({"aaa": ["first-set"], "bbb": ["second-set"]})
        self.choice.side_effect = ["first-emote", "second-emote"]
        message = make_message(content="aaa bbb")
        message.add_reaction = mock.AsyncMock(
            side_effect=[RuntimeError("boom"), None]
        )
        with self.assertLogs(level="ERROR") as captured:
            self.run_on(cog, message)
        self.assertIn("boom", "\n".join(captured.output))
        # first key failed on first-emote; loop continued to key bbb
        self.assertEqual(
            [c.args[0] for c in message.add_reaction.await_args_list],
            ["first-emote", "second-emote"],
        )


class ReactionsSpecialUserTests(unittest.TestCase):
    def setUp(self):
        self.choice = mock.Mock(return_value="emote-x")
        self.randint = mock.Mock(return_value=0)
        for patched in (
            mock.patch.object(reactionsCogModule.random, "choice", self.choice),
            mock.patch.object(reactionsCogModule.random, "randint", self.randint),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def test_lucky_driver_gets_wheel_below_threshold(self):
        cog = make_cog({"unrelated": ["emote-a"]})
        self.randint.return_value = 5  # < 20
        message = make_message(author_id=LUCKY_DRIVER_ID, content="plain")
        asyncio.run(cog.on_message(message))
        message.add_reaction.assert_awaited_once_with(
            "<a:wheel:1096138684786544883>"
        )

    def test_lucky_driver_skipped_above_threshold(self):
        cog = make_cog({"unrelated": ["emote-a"]})
        self.randint.return_value = 20  # threshold is < 20
        message = make_message(author_id=LUCKY_DRIVER_ID, content="plain")
        asyncio.run(cog.on_message(message))
        message.add_reaction.assert_not_called()

    def test_lucky_driver_fires_at_boundary_below(self):
        cog = make_cog({"unrelated": ["emote-a"]})
        self.randint.return_value = 19  # < 20 boundary value
        message = make_message(author_id=LUCKY_DRIVER_ID, content="plain")
        asyncio.run(cog.on_message(message))
        message.add_reaction.assert_awaited_once_with(
            "<a:wheel:1096138684786544883>"
        )

    def test_second_driver_random_emote_below_threshold(self):
        cog = make_cog({"k1": ["emote-x", "emote-y"], "k2": ["emote-z"]})
        self.randint.return_value = 9  # < 10
        self.choice.side_effect = [["emote-y"], "emote-y"]
        message = make_message(author_id=SECOND_DRIVER_ID, content="plain")
        asyncio.run(cog.on_message(message))
        calls = self.choice.call_args_list
        self.assertEqual(calls[0].args[0], [["emote-x", "emote-y"], ["emote-z"]])
        self.assertEqual(calls[1].args[0], ["emote-y"])  # the picked set, not a list
        message.add_reaction.assert_awaited_once_with("emote-y")

    def test_second_driver_skipped_at_threshold(self):
        cog = make_cog({"k1": ["emote-x"]})
        self.randint.return_value = 10  # threshold is < 10
        message = make_message(author_id=SECOND_DRIVER_ID, content="plain")
        asyncio.run(cog.on_message(message))
        message.add_reaction.assert_not_called()

    def test_second_driver_add_failure_logged_not_raised(self):
        cog = make_cog({"k1": ["emote-x"]})
        self.randint.return_value = 3
        self.choice.side_effect = [["emote-x"], "emote-x"]
        message = make_message(author_id=SECOND_DRIVER_ID, content="plain")
        message.add_reaction = mock.AsyncMock(side_effect=RuntimeError("nope"))
        with self.assertLogs(level="ERROR") as captured:
            asyncio.run(cog.on_message(message))
        self.assertIn("nope", "\n".join(captured.output))

    def test_driver_message_also_matching_key_gets_both_reactions(self):
        cog = make_cog({"crash": ["emote-key-set"]})
        self.choice.return_value = "emote-key"
        self.randint.return_value = 1
        message = make_message(author_id=LUCKY_DRIVER_ID, content="big crash today")
        asyncio.run(cog.on_message(message))
        awaited = [
            c.args[0] for c in message.add_reaction.await_args_list
        ]
        self.assertEqual(awaited, ["emote-key", "<a:wheel:1096138684786544883>"])


if __name__ == "__main__":
    unittest.main()
