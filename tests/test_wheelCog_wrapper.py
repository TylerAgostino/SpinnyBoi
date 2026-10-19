# pyright: basic
"""wheel_command wrapper: defer/respond, result shapes, attachments, driver teardown."""

import asyncio
import io
import unittest
from unittest import mock

import modules.wheelCog as wheelCogModule
from modules.wheelCog import wheel_command


class FakeRole:
    def __init__(self, mention):
        self.mention = mention


class FakeCtx:
    def __init__(self):
        self.author = FakeRole("<@1>")
        self.deferred = False
        self.order = []

    async def defer(self):
        self.deferred = True
        self.order.append("defer")


class FakeResponse:
    def __init__(self, sink):
        self.sink = sink

    async def edit(self, content=None, files=None):
        self.sink.append({"content": content, "files": files})


class WrapperHarness:
    """Minimal `self` for the decorator: it only touches self.driver_options."""

    def __init__(self):
        self.driver_options = "fake-options"


class WheelCommandWrapperTests(unittest.TestCase):
    def setUp(self):
        self.firefox = mock.MagicMock()
        self.thread_cls = mock.MagicMock()
        self.quit = mock.MagicMock()
        patches = [
            mock.patch.object(
                wheelCogModule.ChatHandler,
                "working_on_it",
                mock.AsyncMock(return_value="working…"),
            ),
            mock.patch.object(wheelCogModule.webdriver, "Firefox", self.firefox),
            # Rebind the module's `threading` global, NOT threading.Thread
            # itself: patching the real Thread deadlocks asyncio.to_thread's
            # executor (its worker threads never start).
            mock.patch.object(
                wheelCogModule,
                "threading",
                mock.MagicMock(Thread=self.thread_cls),
            ),
            mock.patch.object(wheelCogModule, "_quit_driver", self.quit),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.edits = []
        self.response = FakeResponse(self.edits)

    def command_ctx(self, interaction=True):
        self.ctx = FakeCtx()

        def record(*args, interaction=interaction, **kwargs):
            self.ctx.order.append("respond" if interaction else "send")
            return mock.DEFAULT  # keep return_value (the response object)

        self.placeholder = mock.AsyncMock(return_value=self.response)
        self.placeholder.side_effect = record
        if interaction:
            self.ctx.respond = self.placeholder
        else:
            self.ctx.send = self.placeholder
        return self.ctx

    def test_interaction_defers_then_edits_plain_message(self):
        target = WrapperHarness()

        captured = []

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            captured.append(bot_response)
            return "done", None, None, None

        ctx = self.command_ctx(interaction=True)
        asyncio.run(cmd(target, ctx))
        self.assertIs(captured[0], self.response)
        self.assertTrue(ctx.deferred)
        self.assertEqual(ctx.order, ["defer", "respond"])
        self.placeholder.assert_called_once_with("working…")
        self.assertEqual(self.edits[0]["content"], "done")
        self.assertIsNone(self.edits[0]["files"])

    def test_message_channel_sends_then_edits(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=False)
        async def cmd(self, ctx, bot_response):
            return "msg", None, None

        ctx = self.command_ctx(interaction=False)
        asyncio.run(cmd(target, ctx))
        self.assertEqual(ctx.order, ["send"])
        self.assertEqual(self.edits[0]["content"], "msg")
        self.thread_cls.assert_not_called()  # no driver -> no teardown thread

    def test_files_list_each_wrapped_with_filename_and_author_mention(self):
        target = WrapperHarness()
        buf = io.BytesIO(b"gif-bytes")

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            return "spun", [buf, buf], "out.gif"

        asyncio.run(cmd(target, self.command_ctx()))
        files = self.edits[0]["files"]
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.filename == "out.gif" for f in files))
        self.assertTrue(all(f.fp is buf for f in files))
        self.assertEqual(self.edits[0]["content"], "<@1> spun")

    def test_single_stream_files_become_one_attachment(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            return "spun", io.BytesIO(b"one"), "solo.gif"

        asyncio.run(cmd(target, self.command_ctx()))
        self.assertEqual(len(self.edits[0]["files"]), 1)
        self.assertEqual(self.edits[0]["files"][0].filename, "solo.gif")
        self.assertIn("<@1>", self.edits[0]["content"])

    def test_role_mention_replaces_author_mention(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            return "hey", [io.BytesIO(b"x")], "f.gif", FakeRole("<@staff>")

        asyncio.run(cmd(target, self.command_ctx()))
        content = self.edits[0]["content"]
        self.assertTrue(content.startswith("<@staff>"))
        self.assertNotIn("<@1>", content)

    def test_mention_failure_falls_back_to_plain_message(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            return "plain", [io.BytesIO(b"x")], "f.gif"

        ctx = self.command_ctx()
        ctx.author = None  # ctx.author.mention -> AttributeError -> fallback
        asyncio.run(cmd(target, ctx))
        self.assertEqual(self.edits[0]["content"], "plain")

    def test_driver_made_with_options_and_quit_dispatched_on_thread(self):
        target = WrapperHarness()
        captured = []

        @wheel_command(needs_driver=True, is_interaction=True)
        async def cmd(self, ctx, bot_response, driver):
            captured.append(driver)
            return "ok", None, None

        asyncio.run(cmd(target, self.command_ctx()))
        self.assertIs(captured[0], self.firefox.return_value)
        self.assertEqual(self.edits[0]["content"], "ok")
        self.firefox.assert_called_once_with(options="fake-options")
        self.thread_cls.assert_called_once()
        kwargs = self.thread_cls.call_args.kwargs
        self.assertIs(kwargs["target"], self.quit)
        self.assertEqual(kwargs["args"], (self.firefox.return_value,))
        self.thread_cls.return_value.start.assert_called_once_with()

    def test_quit_dispatched_on_thread_even_when_command_raises(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=True, is_interaction=True)
        async def cmd(self, ctx, bot_response, driver):
            raise RuntimeError("boom")

        asyncio.run(cmd(target, self.command_ctx()))
        self.thread_cls.assert_called_once()
        kwargs = self.thread_cls.call_args.kwargs
        self.assertIs(kwargs["target"], self.quit)
        self.assertEqual(kwargs["args"], (self.firefox.return_value,))
        self.thread_cls.return_value.start.assert_called_once_with()
        self.assertIn("boom", self.edits[0]["content"])

    def test_role_without_files_is_ignored(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            return "plain", None, None, FakeRole("<@staff>")

        asyncio.run(cmd(target, self.command_ctx()))
        self.assertEqual(self.edits[0]["content"], "plain")
        self.assertIsNone(self.edits[0]["files"])

    def test_webdriver_creation_failure_propagates_without_thread(self):
        target = WrapperHarness()
        self.firefox.side_effect = RuntimeError("no browser")

        @wheel_command(needs_driver=True, is_interaction=True)
        async def cmd(self, ctx, bot_response, driver):
            raise AssertionError("must not run")

        # Creation is outside the inner try: the error escapes, but the
        # finally must NOT spawn a teardown thread for a driver that
        # doesn't exist.
        ctx = self.command_ctx()
        with self.assertRaisesRegex(RuntimeError, "no browser"):
            asyncio.run(cmd(target, ctx))
        self.thread_cls.assert_not_called()
        self.assertEqual(self.edits, [])

    def test_errors_are_swallowed_into_an_edit(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            raise ValueError("kaboom")

        asyncio.run(cmd(target, self.command_ctx()))  # must not raise
        self.assertEqual(len(self.edits), 1)
        self.assertIn("kaboom", self.edits[0]["content"])

    def test_no_result_no_edit(self):
        target = WrapperHarness()

        @wheel_command(needs_driver=False, is_interaction=True)
        async def cmd(self, ctx, bot_response):
            return None

        asyncio.run(cmd(target, self.command_ctx()))
        self.assertEqual(self.edits, [])


if __name__ == "__main__":
    unittest.main()
