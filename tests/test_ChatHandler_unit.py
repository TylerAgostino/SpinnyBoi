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

    channel.history = MagicMock(side_effect=lambda **kw: HistIter(list(history_messages)))
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

    def test_missing_content_key_is_parse_failure(self):
        # The seam owns the failure: no KeyError escapes, content is None.
        msg, content = ChatHandler._parse_chat_reply('{"user": "SpinnyBoi"}')
        self.assertIsNone(content)
        self.assertIsInstance(msg, str)


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
             patch.object(ChatHandler, "get_simracerhub", return_value=srh), \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "tucker")
        _, kwargs = make_agent.call_args
        self.assertEqual(kwargs["tools"], ["the-tool"])

    async def test_all_tools_registered_in_order(self):
        trigger = make_discord_message("admin stuff and news and stats")
        bot = make_bot_user([])
        agent = fake_agent(['{"content": "ok"}'])
        memory = MagicMock()
        memory.lookup_tool.return_value = "memory-tool"
        web = MagicMock()
        web.lookup_tool.return_value = "web-tool"
        srh = MagicMock()
        srh.stats_tool.return_value = "srh-tool"
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", return_value=memory), \
             patch.object(ChatHandler, "get_web_lookup", return_value=web), \
             patch.object(ChatHandler, "get_simracerhub", return_value=srh):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "ok")
        _, kwargs = make_agent.call_args
        self.assertEqual(
            kwargs["tools"], ["memory-tool", "web-tool", "srh-tool"]
        )

    async def test_broken_tool_skipped_gracefully(self):
        """A getter raising must not kill the reply - other tools still register."""
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        agent = fake_agent(['{"content": "yo"}'])
        web = MagicMock()
        web.lookup_tool.return_value = "web-tool"
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", side_effect=RuntimeError("boom")), \
             patch.object(ChatHandler, "get_web_lookup", return_value=web), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "yo")
        _, kwargs = make_agent.call_args
        self.assertEqual(kwargs["tools"], ["web-tool"])

    async def test_no_tools_when_all_disabled(self):
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        agent = fake_agent(['{"content": "yo"}'])
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None), \
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

    async def test_history_fetched_once(self):
        trigger = make_discord_message("hey")
        bot = make_bot_user([make_discord_message("earlier")])
        agent = fake_agent(['{"content": "yo"}'])
        with patch.object(ChatHandler, "_make_agent", return_value=agent), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            await ChatHandler.respond_in_chat(trigger, bot)
        channel = bot.fetch_channel.return_value
        self.assertEqual(bot.fetch_channel.await_count, 1)
        self.assertEqual(channel.history.call_count, 1)

    async def test_model_injected_into_agent(self):
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        agent = fake_agent(['{"content": "yo"}'])
        marker = object()
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent:
            await ChatHandler.respond_in_chat(trigger, bot, model=marker)
        self.assertIs(make_agent.call_args.args[0], marker)



    async def test_parse_retries_continue_the_same_conversation(self):
        """A failed parse must not rebuild the agent from scratch: retries
        continue the same conversation with the reformat directive, so the
        model sees its own bad reply and any tool results stay in context
        instead of another full tool cycle."""
        trigger = make_discord_message("hey bot")
        bot = make_bot_user([make_discord_message("past note", nick="someone")])
        agent = fake_agent(
            [
                "garbage not json",
                '{"user": "SpinnyBoi"}',
                '{"user": "SpinnyBoi", "content": "finally"}',
            ]
        )
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "finally")
        self.assertEqual(make_agent.call_count, 1)
        self.assertEqual(agent.invoke_async.await_count, 3)
        first_prompt = agent.invoke_async.call_args_list[0].args[0]
        self.assertIn("hey bot", first_prompt)
        for retry_call in agent.invoke_async.call_args_list[1:]:
            directive = retry_call.args[0]
            self.assertNotIn("hey bot", directive)
            self.assertIn("JSON", directive)
        # the caller's history list stayed pristine (one history message,
        # never the trigger nor any reply appended to it)
        history = make_agent.call_args.args[2]
        self.assertEqual(len(history), 1)

    def test_make_agent_copies_history_list(self):
        history = [{"role": "user", "content": [{"text": "hi"}]}]
        agent = ChatHandler._make_agent(MagicMock(), "sys", history)
        self.assertIsNot(agent.messages, history)
        agent.messages.append({"role": "user", "content": [{"text": "trigger"}]})
        self.assertEqual(len(history), 1)

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

    async def test_model_injected(self):
        agent = fake_agent(["go go go"])
        marker = object()
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent:
            out = await ChatHandler.working_on_it(model=marker)
        self.assertEqual(out, "go go go")
        self.assertIs(make_agent.call_args.args[0], marker)


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


if __name__ == "__main__":
    unittest.main()
