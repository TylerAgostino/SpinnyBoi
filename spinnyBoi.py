# pyright: basic
import discord
from discord.ext import commands
import logging
import os
from modules import ChatHandler
from modules.incidentCog import IncidentCog  # noqa: F401
from modules.wheelCog import WheelCog  # noqa: F401`
from modules.reactionsCog import ReactionsCog  # noqa: F401


intents = discord.Intents.default()
intents.message_content = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)

bot = commands.Bot(
    debug_guilds=[420037391454044171, 981935710514839572],
    intents=intents,
)


# @bot.listen()
# async def on_message(message):
#     if message.author == bot.user:
#         return

#     if message.channel.id == 1362287075142930442:  # Complaining
#         await message.delete()
#         return

#     if bot.user.mentioned_in(message):
#         async with message.channel.typing():
#             msg_channel = message.channel
#             history = [m async for m in msg_channel.history(limit=80, before=message)]
#             history.reverse()
#             response = await ChatHandler.respond_in_chat(message, bot)
#             await message.channel.send(response)


bot.add_cog(IncidentCog(bot))
bot.add_cog(WheelCog(bot))
bot.add_cog(ReactionsCog(bot))
bot.run(os.getenv("BOT_TOKEN"))
