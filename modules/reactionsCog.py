# pyright: basic
from discord.ext import commands
import logging
import random


class ReactionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        reactions_file = open("reactions.txt", "r", encoding="utf-8")
        reactions = reactions_file.readlines()
        reaction_dict = {}
        for reaction in reactions:
            try:
                tup = reaction.split(",")
                reaction_key = tup[0].lower()
                reaction_dict[reaction_key] = []
                for emote_id in tup[1:]:
                    reaction_dict[reaction_key].append(emote_id.strip())
            except IndexError:
                logging.error("Invalid reaction line: " + reaction)
                logging.error("Its probably Brian's fault")
            except Exception as e:
                logging.error("Something went wrong: " + str(e))
        reactions_file.close()
        self.reaction_dict = reaction_dict

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
