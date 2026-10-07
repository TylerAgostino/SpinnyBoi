"""Unit tests for the strands-agents invocation paths in modules/ChatHandler.

These mock at the ``_make_agent`` boundary (the only place a strands Agent is
constructed), so no LLM or Discord connection is required. The pre-existing
tests/test_ChatHandler.py remains a live-Discord integration harness.

Run: python -m unittest tests.test_ChatHandler_unit
"""
import datetime
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules import ChatHandler


class FakeAgentResult:
    """Minimal stand-in for strands AgentResult."""

    def __init__(self, text):
        self.message = {"role": "assistant", "content": [{"text": text}]}


def fake_agent(reply_texts):
    """Agent double whose invoke_async yields reply_texts in order."""
    agent = MagicMock()
    results = [FakeAgentResult(t) for t in reply_texts]
    calls = {"n": 0}

    async def invoke_async(prompt):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    agent.invoke_async = AsyncMock(side_effect=invoke_async)
    return agent


def make_discord_message(content="hello", nick="tyler", name="tyler_l"):
    m = MagicMock()
    m.author.nick = nick
    m.author.name = name
    m.author.id = 123
    m.content = content
    m.created_at = datetime.datetime(2026, 1, 2, 3, 4, 5)
    return m


def make_bot_user(history_messages):
    channel = MagicMock()

    class HistIter:
        def __init__(self, items):
            self.items = items

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.items:
                return self.items.pop(0)
            raise StopAsyncIteration

    channel.history = MagicMock(return_value=HistIter(list(history_messages)))
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


class ParseChatReplyTests(unittest.TestCase):
    def test_clean_json(self):
        msg, content = ChatHandler._parse_chat_reply(
            '{"user": "SpinnyBoi", "content": "hi there"}'
        )
        self.assertEqual(content, "hi there")

    def test_think_block_stripped(self):
        raw = "some reasoning\n\n{\"content\": \"real answer\"}"
        msg, content = ChatHandler._parse_chat_reply(raw)
        self.assertEqual(content, "real answer")

    def test_leading_text_before_brace(self):
        msg, content = ChatHandler._parse_chat_reply(
            'Sure! {"user": "SpinnyBoi", "content": "ok"}'
        )
        self.assertEqual(content, "ok")

    def test_trailing_text_after_brace(self):
        msg, content = ChatHandler._parse_chat_reply(
            '{"user": "SpinnyBoi", "content": "ok"} trailing chatter'
        )
        self.assertEqual(content, "ok")

    def test_newline_unescaping(self):
        msg, content = ChatHandler._parse_chat_reply('{"content": "line1\\nline2"}')
        self.assertEqual(content, "line1\nline2")

    def test_unparseable_returns_none_content(self):
        msg, content = ChatHandler._parse_chat_reply("no json here at all")
        self.assertIsNone(content)
        self.assertIsInstance(msg, str)

    def test_missing_content_key_raises(self):
        # Preserves the old behavior: parseable JSON without "content" raises.
        with self.assertRaises(KeyError):
            ChatHandler._parse_chat_reply('{"user": "SpinnyBoi"}')


class RespondInChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_json_reply(self):
        trigger = make_discord_message("@SpinnyBoi what's up")
        hist = [make_discord_message("earlier message", nick="someone")]
        bot = make_bot_user(hist)
        agent = fake_agent(['{"user": "SpinnyBoi", "content": "yo"}'])
        with patch.object(ChatHandler, "_make_agent", return_value=agent), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "yo")
        self.assertEqual(agent.invoke_async.await_count, 1)

    async def test_simracerhub_tool_registered_when_enabled(self):
        trigger = make_discord_message("who is winning the championship")
        bot = make_bot_user([])
        agent = fake_agent(['{"content": "tucker"}'])
        srh = MagicMock()
        srh.stats_tool.return_value = "the-tool"
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_simracerhub", return_value=srh):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "tucker")
        _, kwargs = make_agent.call_args
        self.assertEqual(kwargs["tools"], ["the-tool"])

    async def test_no_tools_when_simracerhub_disabled(self):
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        agent = fake_agent(['{"content": "yo"}'])
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            await ChatHandler.respond_in_chat(trigger, bot)
        _, kwargs = make_agent.call_args
        self.assertEqual(kwargs["tools"], [])

    async def test_retries_up_to_three_then_returns_raw(self):
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        agent = fake_agent(["garbage one", "garbage two", "garbage three"])
        with patch.object(ChatHandler, "_make_agent", return_value=agent), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(agent.invoke_async.await_count, 3)
        # Never raises; last transformed raw string is returned.
        self.assertIsInstance(out, str)
        self.assertIn("garbage", out)

    async def test_second_attempt_success(self):
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        agent = fake_agent(
            ["not json", '{"user": "SpinnyBoi", "content": "second try works"}']
        )
        with patch.object(ChatHandler, "_make_agent", return_value=agent):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "second try works")
        self.assertEqual(agent.invoke_async.await_count, 2)


class WorkingOnItTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_text(self):
        agent = fake_agent(["Patience, mortal."])
        with patch.object(ChatHandler, "_make_agent", return_value=agent):
            out = await ChatHandler.working_on_it()
        self.assertEqual(out, "Patience, mortal.")
        self.assertEqual(agent.invoke_async.await_count, 1)

    async def test_reasoning_blocks_excluded(self):
        agent = MagicMock()
        result = MagicMock()
        result.message = {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "thinking..."}}},
                {"text": "Hold your horses."},
            ],
        }
        agent.invoke_async = AsyncMock(return_value=result)
        with patch.object(ChatHandler, "_make_agent", return_value=agent):
            out = await ChatHandler.working_on_it()
        self.assertEqual(out, "Hold your horses.")


class ExtractTextTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(ChatHandler._extract_text("abc"), "abc")

    def test_dict_result(self):
        r = {"message": {"content": [{"text": "x"}, {"text": "y"}]}}
        self.assertEqual(ChatHandler._extract_text(r), "xy")

    def test_reasoning_excluded(self):
        r = FakeAgentResult("final")
        r.message["content"].insert(0, {"reasoningContent": {}})
        self.assertEqual(ChatHandler._extract_text(r), "final")


class SummarizeThreadTests(unittest.IsolatedAsyncioTestCase):
    def _thread(self, messages):
        class HistIter:
            def __init__(self, items):
                self.items = items

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.items:
                    return self.items.pop(0)
                raise StopAsyncIteration

        thread = MagicMock()
        thread.history = MagicMock(return_value=HistIter(list(messages)))
        return thread

    async def test_empty_history(self):
        thread = self._thread([])
        out = await ChatHandler.summarize_thread(thread, "incident at T1")
        self.assertEqual(out, "No messages found in the thread to summarize.")

    async def test_success_returns_text(self):
        msg = make_discord_message("he spun me out")
        msg.author.bot = False
        thread = self._thread([msg])
        agent = fake_agent(["Drivers: A\nDecision: none"])
        with patch.object(ChatHandler, "_make_agent", return_value=agent):
            out = await ChatHandler.summarize_thread(thread, "incident at T1")
        self.assertIn("Decision", out)
        self.assertEqual(agent.invoke_async.await_count, 1)

    async def test_never_raises(self):
        thread = self._thread([])
        thread.history = MagicMock(side_effect=RuntimeError("boom"))
        out = await ChatHandler.summarize_thread(thread, "incident")
        self.assertTrue(out.startswith("Failed to summarize thread:"))


if __name__ == "__main__":
    unittest.main()
