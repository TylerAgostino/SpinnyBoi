# pyright: basic
from discord.ext import commands
import logging
import random


def parse_reaction_lines(reactions):
    """Build trigger-key -> emote ids from reactions.txt lines.

    A line must have a non-empty key and at least one non-empty emote id;
    malformed lines are skipped with a warning. An empty key would match
    every message via str.find(), which is exactly what this prevents.
    For duplicate keys, the last line wins (as before).
    """
    reaction_dict = {}
    for line_number, reaction in enumerate(reactions, start=1):
        try:
            tup = [field.strip() for field in reaction.split(",")]
            reaction_key = tup[0].lower()
            emote_ids = [emote for emote in tup[1:] if emote]
            if not reaction_key or not emote_ids:
                logging.warning(
                    f"Skipping malformed reaction line {line_number}: {reaction!r}"
                )
                continue
            reaction_dict[reaction_key] = emote_ids
        except Exception as e:
            logging.error("Something went wrong: " + str(e))
    return reaction_dict


class ReactionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        with open("reactions.txt", "r", encoding="utf-8") as reactions_file:
            reactions = reactions_file.readlines()
        self.reaction_dict = parse_reaction_lines(reactions)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        for key in self.reaction_dict.keys():
            if str(message.content).lower().find(key) != -1:
                try:
                    e_id = random.choice(self.reaction_dict[key])
                    await message.add_reaction(e_id)
                except Exception as ex:
                    logging.error(
                        f"Error adding reaction {self.reaction_dict[key]} to message {message.id}: {str(ex)}"
                    )

        if message.author.id == 292447304395522048 and random.randint(0, 100) < 20:
            await message.add_reaction("<a:wheel:1096138684786544883>")

        if message.author.id == 267830473328295937 and random.randint(0, 100) < 10:
            random_reaction = random.choice(list(self.reaction_dict.values()))
            e_id = random.choice(random_reaction)
            try:
                await message.add_reaction(e_id)
            except Exception as ex:
                logging.error(
                    f"Error adding reaction {e_id} to message {message.id}: {str(ex)}"
                )
