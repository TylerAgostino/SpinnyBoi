import unittest
import os
from unittest.mock import AsyncMock, MagicMock, patch
import json
import datetime
import discord
from modules.ChatHandler import respond_in_chat

class DiscordBotChatMessageTest(discord.Client):
    async def on_ready(self):
        # Get IDs from config
        channel_id = 1292642327566745601
        message_id = 1396615919765164164
        # Get the channel and message
        channel = self.get_channel(channel_id)
        if not channel:
            channel = await self.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        # Call the respond_in_chat function with real objects
        response = await respond_in_chat(message, self)
        # Basic validation that we got a response
        print(response)
        await self.close()


if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = DiscordBotChatMessageTest(intents=intents)

    # Start the bot
    bot_token = os.getenv('BOT_TOKEN')
    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable is not set")

    client.run(bot_token)