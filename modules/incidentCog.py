# pyright: basic

import discord
from discord.ext import commands

MAX_PENALTY_POINTS = 6

class ReactionManager:
    def __init__(self) -> None:
        self.reactions = 0

    def yield_reactions(self):
        for i in range(1, 27):
            self.reactions += 1
            yield "regional_indicator_" + chr(96 + i)

    async def apply_reactions(self, message):
        for i in range(0, self.reactions):
            await message.add_reaction(chr(127462 + i))

class IncidentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="spincident")
    @discord.option(
        "subject",
        str,
        required=False,
        default=None,
        description="Subject of the incident poll",
    )
    @commands.check_any(
        commands.has_permissions(administrator=True),
        commands.has_role(1146907150229192747),
    )
    async def spincident(self, ctx, subject: str = ""):
        """Create an incident poll."""
        await ctx.defer()
        try:
            msg = f"""
            >>> # Incident Poll: {subject}
            """
            reaction_manager = ReactionManager()
            for r in ["No Action", *[f"{n} Point{'s' if n > 1 else ''}" for n in range(1, MAX_PENALTY_POINTS + 1)], "Other"]:
                msg += f"\n{next(reaction_manager.yield_reactions())}: {r}"
            bot_response = await ctx.respond(msg)
            await reaction_manager.apply_reactions(bot_response)
        except Exception as ex:
            print(str(ex))
            await ctx.respond("Something went wrong.")
