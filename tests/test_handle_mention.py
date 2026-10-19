"""Offline unit tests for ChatHandler.handle_mention.

The on_message listener must never raise into discord.py: the success path
replies exactly as the old inline branch did (typing indicator + channel.send),
any failure is logged via logging.exception and answered with a best-effort
apology, and even a failing apology stays silent.

Run: python -m pytest tests/test_handle_mention.py
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules import ChatHandler


def make_mention_message():
    message = MagicMock()
    message.id = 42
    message.channel.id = 999
    message.channel.send = AsyncMock()
    message.reply = AsyncMock()
    return message


class HandleMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_sends_response_once(self):
        message = make_mention_message()
        bot = MagicMock()
        respond = AsyncMock(return_value="hello there")
        with patch.object(ChatHandler, "respond_in_chat", respond), \
             patch.object(ChatHandler.logging, "exception") as log_exception, \
             patch.object(ChatHandler.logging, "debug") as log_debug:
            await ChatHandler.handle_mention(message, bot)
        respond.assert_awaited_once_with(message, bot)
        message.channel.typing.return_value.__aenter__.assert_awaited_once()
        message.channel.send.assert_awaited_once_with("hello there")
        message.reply.assert_not_awaited()
        log_exception.assert_not_called()
        log_debug.assert_not_called()

    async def test_failure_logs_and_attempts_fallback_reply(self):
        message = make_mention_message()
        bot = MagicMock()
        with patch.object(ChatHandler, "respond_in_chat",
                          AsyncMock(side_effect=RuntimeError("model down"))), \
             patch.object(ChatHandler.logging, "exception") as log_exception, \
             patch.object(ChatHandler.logging, "debug") as log_debug:
            await ChatHandler.handle_mention(message, bot)  # must not raise
        log_exception.assert_called_once()
        self.assertEqual(log_exception.call_args.args[1:], (999,))
        message.channel.send.assert_not_awaited()
        message.reply.assert_awaited_once_with("Sorry, I couldn't process that.")
        log_debug.assert_not_called()

    async def test_fallback_reply_failure_stays_silent(self):
        message = make_mention_message()
        message.reply = AsyncMock(side_effect=RuntimeError("missing perms"))
        bot = MagicMock()
        with patch.object(ChatHandler, "respond_in_chat",
                          AsyncMock(side_effect=RuntimeError("model down"))), \
             patch.object(ChatHandler.logging, "exception") as log_exception, \
             patch.object(ChatHandler.logging, "debug") as log_debug:
            await ChatHandler.handle_mention(message, bot)  # must not raise
        log_exception.assert_called_once()
        message.reply.assert_awaited_once_with("Sorry, I couldn't process that.")
        log_debug.assert_called_once()


if __name__ == "__main__":
    unittest.main()
