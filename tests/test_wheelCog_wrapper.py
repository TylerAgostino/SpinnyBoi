# pyright: basic
"""Regression tests for the wheel_command wrapper's error handling."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.wheelCog import NoTabError, WheelUserError, WheelCog, wheel_command


class _BoomCog:
    @wheel_command(needs_driver=False, is_interaction=False)
    async def boom(self, ctx, bot_response):
        raise ValueError("kaboom secret internals")

    @wheel_command(needs_driver=False, is_interaction=False)
    async def badsheet(self, ctx, bot_response):
        raise WheelUserError("Your filter team:zzz returned no results")

    @wheel_command(needs_driver=False, is_interaction=False)
    async def missing_tab(self, ctx, bot_response):
        raise NoTabError("Tab drivers not found")


class WheelCommandUserErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, method_name, level="WARNING"):
        cog = _BoomCog()
        ctx = MagicMock()
        bot_response = AsyncMock()
        ctx.send = AsyncMock(return_value=bot_response)
        with patch(
            "modules.wheelCog.ChatHandler.working_on_it",
            new=AsyncMock(return_value="working..."),
        ):
            with self.assertLogs(level=level) as captured:
                await getattr(cog, method_name)(ctx)
        content = bot_response.edit.await_args.kwargs["content"]
        return content, captured

    async def test_user_error_message_is_surfaced_no_traceback(self):
        content, captured = await self._run("badsheet")
        self.assertIn("Your filter team:zzz returned no results", content)
        self.assertIn("That didn't work", content)
        self.assertNotIn("Traceback", content)
        self.assertEqual([r.exc_info for r in captured.records if r.exc_info], [])

    async def test_no_tab_error_is_surfaced_too(self):
        content, _ = await self._run("missing_tab")
        self.assertIn("Tab drivers not found", content)

    async def test_unknown_error_stays_generic(self):
        content, _ = await self._run("boom", level="ERROR")
        self.assertNotIn("secret internals", content)
        self.assertIn("check the bot logs", content)


class ReadFailureSanitizationTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, boom):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock as A

        cog = SimpleNamespace(presets=SimpleNamespace(read_tab=A(side_effect=boom)))
        return cog

    async def test_read_tab_failure_message_is_fixed_and_chained(self):
        cog = self._cog(RuntimeError("gapis request to secrets-project at 10.0.0.5"))
        with self.assertRaises(WheelUserError) as caught:
            await WheelCog.generate_option_set(cog, "drivers", "")
        message = str(caught.exception)
        self.assertIn("drivers", message)
        self.assertNotIn("secrets-project", message)
        self.assertNotIn("10.0.0.5", message)
        self.assertIs(type(caught.exception.__cause__), RuntimeError)

    async def test_spinfo_surfaces_user_detail_but_not_internals(self):
        from types import SimpleNamespace

        preset = MagicMock()
        preset.resolve_tab.return_value = ("t", "")
        cog = SimpleNamespace(
            presets=SimpleNamespace(load=lambda: None, preset=lambda name: preset),
            generate_option_set=AsyncMock(
                side_effect=RuntimeError("client secret-token blew up")
            ),
        )
        message, images, name = await WheelCog.spinfo.__wrapped__(
            cog, None, "p", "t"
        )
        self.assertNotIn("secret-token", message)
        self.assertIn("bot logs", message)

        cog.generate_option_set = AsyncMock(
            side_effect=WheelUserError("Your filter x returned no results")
        )
        message, images, name = await WheelCog.spinfo.__wrapped__(
            cog, None, "p", "t"
        )
        self.assertIn("Your filter x returned no results", message)


class WheelCommandErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_reply_has_no_traceback_and_logs_with_exc_info(self):
        cog = _BoomCog()
        ctx = MagicMock()
        bot_response = AsyncMock()
        ctx.send = AsyncMock(return_value=bot_response)

        with patch(
            "modules.wheelCog.ChatHandler.working_on_it",
            new=AsyncMock(return_value="working..."),
        ):
            with self.assertLogs(level="ERROR") as captured:
                await cog.boom(ctx)

        bot_response.edit.assert_awaited_once()
        content = bot_response.edit.await_args.kwargs["content"]
        self.assertNotIn("Traceback", content)
        self.assertNotIn("kaboom", content)
        self.assertIn("check the bot logs", content)

        log_records = [record for record in captured.records if record.exc_info]
        self.assertEqual(len(log_records), 1)
        log_record = log_records[0]
        self.assertIs(log_record.exc_info[0], ValueError)
        self.assertIn("boom", log_record.getMessage())


if __name__ == "__main__":
    unittest.main()
