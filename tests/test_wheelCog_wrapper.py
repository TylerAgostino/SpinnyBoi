# pyright: basic
"""Regression tests for the wheel_command wrapper's error handling."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.wheelCog import wheel_command


class _BoomCog:
    @wheel_command(needs_driver=False, is_interaction=False)
    async def boom(self, ctx, bot_response):
        raise ValueError("kaboom")


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
