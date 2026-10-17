"""Unit tests for the strands-agents invocation paths in modules/ChatHandler.

These mock at the ``_make_agent`` boundary (the only place a strands Agent is
constructed), so no LLM or Discord connection is required.

Run: python -m unittest tests.test_ChatHandler_unit
"""
import asyncio
import datetime
import io
import json
import os
import struct
import unittest
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

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



def make_attachment(content_type="image/png", data=b"PNGBYTES", size=None):
    a = MagicMock()
    a.content_type = content_type
    a.filename = "shot.png"
    a.size = len(data) if size is None else size
    a.read = AsyncMock(return_value=data)
    return a


def make_discord_message(content="hello", nick="tyler", name="tyler_l", attachments=None):
    m = MagicMock()
    m.author.nick = nick
    m.author.name = name
    m.author.id = 123
    m.content = content
    m.created_at = datetime.datetime(2026, 1, 2, 3, 4, 5)
    m.attachments = list(attachments or [])
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

    async def test_agent_failures_without_images_burn_attempts(self):
        agent = MagicMock()
        agent.invoke_async = AsyncMock(side_effect=RuntimeError("model down"))
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        with patch.object(ChatHandler, "_make_agent", return_value=agent):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "")
        self.assertEqual(agent.invoke_async.await_count, 3)

    async def test_invocation_failure_rebuilds_agent_and_reasks_trigger(self):
        """A failed invocation answered nothing, so a reformat directive has
        no bad reply to point at and a canceled strands agent may sit
        mid-exchange: a fresh agent must get the real prompt."""
        bad = MagicMock()
        bad.invoke_async = AsyncMock(side_effect=RuntimeError("connection reset"))
        good = fake_agent(['{"user": "SpinnyBoi", "content": "second try"}'])
        trigger = make_discord_message("hey there")
        bot = make_bot_user([])
        with patch.object(ChatHandler, "_make_agent", side_effect=[bad, good]) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "second try")
        self.assertEqual(make_agent.call_count, 2)
        retry_prompt = good.invoke_async.call_args.args[0]
        self.assertIn("hey there", retry_prompt)
        # reformat directives are for parse retries only
        self.assertNotIn("JSON", retry_prompt)

    def test_make_agent_copies_history_list(self):
        history = [{"role": "user", "content": [{"text": "hi"}]}]
        agent = ChatHandler._make_agent(MagicMock(), "sys", history)
        self.assertIsNot(agent.messages, history)
        agent.messages.append({"role": "user", "content": [{"text": "trigger"}]})
        self.assertEqual(len(history), 1)


class DecompressionBombTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _png_chunk(tag, payload):
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    @staticmethod
    def _png_declaring(w, h):
        # well-formed chunk structure so Pillow parses the header for real
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00" * 30)
        return (
            b"\x89PNG\r\n\x1a\n"
            + DecompressionBombTests._png_chunk(b"IHDR", ihdr)
            + DecompressionBombTests._png_chunk(b"IDAT", idat)
            + DecompressionBombTests._png_chunk(b"IEND", b"")
        )

    async def test_decompression_bomb_is_skipped(self):
        # a tiny file declaring a 10 gigapixel PNG: Pillow raises
        # DecompressionBombError at open(); the image must be skipped, never
        # passed through to the inference server
        bomb = make_attachment(
            content_type="image/png",
            data=self._png_declaring(100000, 100000),
        )
        self.assertIsNone(await ChatHandler._attachment_image_block(bomb))

    async def test_megapixel_outlier_is_skipped(self):
        # 45.5 MP real decodable PNG: below Pillow's bomb limit but above
        # this module's pixel ceiling; decoding-and-resizing it is not worth
        # the memory, so it is skipped
        buf = io.BytesIO()
        Image.new("RGB", (7000, 6500), "red").save(buf, "PNG")
        att = make_attachment(content_type="image/png", data=buf.getvalue())
        self.assertIsNone(await ChatHandler._attachment_image_block(att))





class AttachmentBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_image_and_oversize_skipped_without_download(self):
        pdf = make_attachment(content_type="application/pdf")
        big = make_attachment(size=ChatHandler.MAX_VISION_IMAGE_BYTES + 1)
        self.assertIsNone(await ChatHandler._attachment_image_block(pdf))
        self.assertIsNone(await ChatHandler._attachment_image_block(big))
        pdf.read.assert_not_called()
        big.read.assert_not_called()

    async def test_gif_maps_to_gif_format(self):
        gif = make_attachment(content_type="image/gif", data=b"GIF89a")
        block = await ChatHandler._attachment_image_block(gif)
        self.assertEqual(block["image"]["format"], "gif")
        self.assertEqual(block["image"]["source"]["bytes"], b"GIF89a")

    async def test_webp_attachment_formats_as_data_image_webp(self):
        # regression for the live 400: ChatHandler registers .webp explicitly,
        # so the real strands OpenAI formatter must emit a data:image/webp URL
        # instead of the octet-stream fallback llama.cpp rejects
        from strands.models.openai import OpenAIModel

        webp = make_attachment(content_type="image/webp", data=b"RIFF\x00\x00\x00WEBP")
        block = await ChatHandler._attachment_image_block(webp)
        self.assertIsNotNone(block, ".webp must be a registered mime")
        formatted = OpenAIModel.format_request_message_content(block)
        url = formatted["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/webp;base64,"))
        self.assertTrue(url.split(",", 1)[1], "base64 payload must not be empty")

    async def test_download_failure_yields_none(self):
        bad = make_attachment()
        bad.read = AsyncMock(side_effect=RuntimeError("cdn down"))
        self.assertIsNone(await ChatHandler._attachment_image_block(bad))

    async def test_format_without_resolvable_mime_is_skipped(self):
        # the OpenAI adapter derives the data-URL mime from mimetypes.types_map;
        # a format it misses must never ship as an octet-stream data URL (llama.cpp
        # answers those with 400 Invalid uri format)
        gif = make_attachment(content_type="image/gif", data=b"GIF89a")
        with patch.dict("mimetypes.types_map", {".gif": None}):
            self.assertIsNone(await ChatHandler._attachment_image_block(gif))
        gif.read.assert_not_called()

class VisionPassThroughTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, history_msgs, trigger_attachments, env=None):
        agent = fake_agent(['{"user": "SpinnyBoi", "content": "nice shot"}'])
        trigger = make_discord_message("look at this", attachments=trigger_attachments)
        bot = make_bot_user(history_msgs)
        with patch.dict(os.environ, env or {}, clear=False), \
             patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "nice shot")
        return agent, make_agent

    @staticmethod
    def _images(blocks):
        return [b["image"] for b in blocks if isinstance(b, dict) and "image" in b]

    async def test_history_and_trigger_images_reach_the_model(self):
        history = [make_discord_message("before pic", attachments=[make_attachment(data=b"HIST")])]
        agent, make_agent = await self._run(history, [make_attachment(data=b"TRIG")])
        entry = make_agent.call_args.args[2][0]
        images = self._images(entry["content"])
        self.assertEqual([img["source"]["bytes"] for img in images], [b"HIST"])
        self.assertEqual(images[0]["format"], "png")
        prompt = agent.invoke_async.call_args.args[0]
        self.assertIsInstance(prompt, list)
        self.assertIn("look at this", prompt[0]["text"])
        self.assertEqual([img["source"]["bytes"] for img in self._images(prompt)], [b"TRIG"])

    async def test_text_only_trigger_stays_a_string_prompt(self):
        agent, _ = await self._run([], [])
        self.assertIsInstance(agent.invoke_async.call_args.args[0], str)

    async def test_vision_disabled_skips_attachments(self):
        hist_att = make_attachment()
        history = [make_discord_message("pic", attachments=[hist_att])]
        agent, make_agent = await self._run(
            history, [make_attachment()], env={"CHAT_VISION_ENABLED": "off"}
        )
        entry = make_agent.call_args.args[2][0]
        self.assertEqual(self._images(entry["content"]), [])
        self.assertIsInstance(agent.invoke_async.call_args.args[0], str)
        hist_att.read.assert_not_called()

    async def test_only_the_newest_images_are_downloaded(self):
        atts = [make_attachment(data=bytes([i])) for i in range(6)]
        await self._run([], atts)
        downloaded = [a for a in atts if a.read.await_count]
        self.assertEqual(len(downloaded), ChatHandler.MAX_VISION_IMAGES)
        self.assertEqual(atts[0].read.await_count, 0)

    async def test_image_rejection_degrades_the_turn_to_text_only(self):
        bad = MagicMock()
        bad.invoke_async = AsyncMock(side_effect=RuntimeError("400 Invalid uri format"))
        good = fake_agent(['{"user": "SpinnyBoi", "content": "text worked"}'])
        history = [make_discord_message("pic", attachments=[make_attachment(data=b"HIST")])]
        trigger = make_discord_message("look at this", attachments=[make_attachment(data=b"TRIG")])
        bot = make_bot_user(history)
        with patch.object(ChatHandler, "_make_agent", side_effect=[bad, good]) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "text worked")
        self.assertEqual(make_agent.call_count, 2)
        first = make_agent.call_args_list[0].args[2]
        second = make_agent.call_args_list[1].args[2]
        self.assertTrue(any(any("image" in b for b in e["content"]) for e in first))
        self.assertFalse(any(any("image" in b for b in e["content"]) for e in second))
        # a rebuilt text-only turn gets the plain trigger, not the reformat
        # directive - an image failure is not a format failure
        self.assertIsInstance(good.invoke_async.call_args.args[0], str)
        self.assertNotIn("rejected", good.invoke_async.call_args.args[0])

    async def test_byte_budget_keeps_newest_images(self):
        atts = [make_attachment(data=ch.encode() * 100) for ch in "ABC"]
        agent = fake_agent(['{"user": "SpinnyBoi", "content": "nice"}'])
        trigger = make_discord_message("three pics", attachments=atts)
        bot = make_bot_user([])
        with patch.object(ChatHandler, "_vision_total_budget_bytes", return_value=250), \
             patch.object(ChatHandler, "_make_agent", return_value=agent), \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_web_lookup", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "nice")
        prompt = agent.invoke_async.call_args.args[0]
        # the newest images claim the budget first; A is dropped
        self.assertEqual(
            [img["source"]["bytes"] for img in self._images(prompt)],
            [b"B" * 100, b"C" * 100],
        )

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


class ModelParamsTests(unittest.TestCase):
    def test_generation_and_client_are_bounded(self):
        # regression: max_tokens was unset, so llama.cpp ran with
        # num_predict=-1 and a degraded model streamed nonsense forever
        # ("stuck thinking forever"); both completion and client now capped
        model = ChatHandler._make_model(0.5)
        self.assertEqual(
            model.config["params"]["max_tokens"], ChatHandler.CHAT_MAX_TOKENS
        )
        self.assertGreaterEqual(model.config["params"]["max_tokens"], 64)
        self.assertLessEqual(model.client_args["timeout"], 900)


class NormalizeImageTests(unittest.TestCase):
    @staticmethod
    def _png(w, h):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "red").save(buf, "PNG")
        return buf.getvalue()

    @staticmethod
    def _dims(data):
        with Image.open(io.BytesIO(data)) as img:
            return img.size

    def test_oversized_image_downscaled_to_cap(self):
        # regression: a 1700x1200 screenshot sailed through the 8 MB byte cap
        # and blew the vision-token budget, garbling the llama.cpp context
        out, fmt = ChatHandler._normalize_image_bytes(self._png(1700, 1200), "png")
        self.assertEqual(fmt, "png")
        w, h = self._dims(out)
        self.assertEqual(max(w, h), ChatHandler._vision_max_dim())
        self.assertAlmostEqual(w / h, 1700 / 1200, places=2)

    def test_oversized_jpeg_reencoded_as_jpeg(self):
        buf = io.BytesIO()
        Image.new("RGB", (400, 1700), "blue").save(buf, "JPEG")
        out, fmt = ChatHandler._normalize_image_bytes(buf.getvalue(), "jpeg")
        self.assertEqual(fmt, "jpeg")
        self.assertLessEqual(max(self._dims(out)), ChatHandler._vision_max_dim())

    def test_small_image_passes_through_byte_identical(self):
        data = self._png(64, 50)
        out, fmt = ChatHandler._normalize_image_bytes(data, "png")
        self.assertEqual(out, data)
        self.assertEqual(fmt, "png")

    def test_undecodable_passes_through(self):
        # fake bytes are the server's business exactly as before normalization
        out, fmt = ChatHandler._normalize_image_bytes(b"GIF89a", "gif")
        self.assertEqual((out, fmt), (b"GIF89a", "gif"))


def stuck_agent():
    """Agent double whose invoke_async never returns (degraded stream or
    wedged inference server - the live 'stuck thinking forever' failure)."""
    agent = MagicMock()

    async def never(prompt):
        await asyncio.sleep(3600)

    agent.invoke_async = AsyncMock(side_effect=never)
    return agent


class InvocationTimeoutTests(unittest.IsolatedAsyncioTestCase):
    timeout_env = {"CHAT_TURN_TIMEOUT_SECONDS": "1"}

    @staticmethod
    def _tool_patches():
        return [
            patch.object(ChatHandler, "get_league_memory", return_value=None),
            patch.object(ChatHandler, "get_web_lookup", return_value=None),
            patch.object(ChatHandler, "get_simracerhub", return_value=None),
        ]

    async def test_timeout_burns_attempts_and_returns(self):
        agent = stuck_agent()
        trigger = make_discord_message("hey")
        bot = make_bot_user([])
        with patch.dict(os.environ, self.timeout_env), \
             patch.object(ChatHandler, "_make_agent", return_value=agent), \
             self._tool_patches()[0], self._tool_patches()[1], self._tool_patches()[2]:
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "")
        self.assertEqual(agent.invoke_async.await_count, 3)

    async def test_timeout_on_image_turn_degrades_to_text_only(self):
        stuck = stuck_agent()
        good = fake_agent(['{"user": "SpinnyBoi", "content": "alive"}'])
        history = [
            make_discord_message("pic", attachments=[make_attachment(data=b"HIST")])
        ]
        trigger = make_discord_message("look", attachments=[make_attachment(data=b"TRIG")])
        bot = make_bot_user(history)
        with patch.dict(os.environ, self.timeout_env), \
             patch.object(
                 ChatHandler, "_make_agent", side_effect=[stuck, good]
             ) as make_agent, \
             self._tool_patches()[0], self._tool_patches()[1], self._tool_patches()[2]:
            out = await ChatHandler.respond_in_chat(trigger, bot)
        self.assertEqual(out, "alive")
        self.assertEqual(make_agent.call_count, 2)
        second = make_agent.call_args_list[1].args[2]
        self.assertFalse(any(any("image" in b for b in e["content"]) for e in second))


if __name__ == "__main__":
    unittest.main()
