# pyright: basic
import logging
import os

import discord
from discord.ext import commands

from modules.league_config import LeagueConfig
from modules import ChatHandler
from modules.league_memory import get_league_memory
from modules.attendanceCog import AttendanceCog  # noqa: F401
from modules.incidentCog import IncidentCog  # noqa: F401
from modules.reactionsCog import ReactionsCog  # noqa: F401
from modules.registrationCog import RegistrationCog  # noqa: F401
from modules.standingsCog import StandingsCog  # noqa: F401
from modules.wheelCog import WheelCog  # noqa: F401

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)

config = LeagueConfig.from_env()

bot = commands.Bot(
    debug_guilds=list(config.debug_guild_ids),
    intents=intents,
)


@bot.listen()
async def on_ready():
    # Build the league vector store in the background so message handling
    # is never blocked by startup indexing.
    memory = get_league_memory()
    if memory is not None:
        memory.start_indexing(bot)


@bot.listen()
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id == config.auto_delete_channel_id:  # Complaining
        await message.delete()
        return

    if bot.user.mentioned_in(message):
        async with message.channel.typing():
            response = await ChatHandler.respond_in_chat(message, bot)
            await message.channel.send(response)


bot.add_cog(IncidentCog(bot))
bot.add_cog(WheelCog(bot))
bot.add_cog(ReactionsCog(bot))
bot.add_cog(RegistrationCog(bot, config))
bot.add_cog(StandingsCog(bot, config))
bot.add_cog(AttendanceCog(bot, config))
bot.run(os.getenv("BOT_TOKEN"))
