import os
import discord
import logging
import random
from modules import CommandHandler, ChatHandler

status_message = "/spin"

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')

# Reactions

reactions_file = open('reactions.txt', 'r', encoding='utf-8')
reactions = reactions_file.readlines()
reaction_dict = {}
for reaction in reactions:
    try:
        tup = reaction.split(',')
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


class MyClient(discord.Client):
    async def on_ready(self):
        print('Logged on as ' + str(self.user))
        activity = discord.Activity(name=status_message, type=discord.ActivityType.playing)
        await self.change_presence(status=discord.Status.online, activity=activity)

    async def on_message(self, message):
        # don't respond to ourselves
        if message.author == self.user:
            return

        if message.channel.id == 1362287075142930442: # Complaining
            await message.delete()
            return

        if str(message.content).lower().startswith('/spin'):
            try:
                msg = ChatHandler.working_on_it()
                bot_response = await message.channel.send(msg)
            except Exception as e:
                logging.error(f"Error sending working on it message: {str(e)}")
                bot_response = await message.channel.send('Working on it. Go easy on me, I\'m still in beta.')
            try:
                command_with_args = str(message.content).lower().removeprefix('/spin').strip(' ')
                command = command_with_args.split(' ')[0]
                args = ' '.join(command_with_args.split(' ')[1:])
                if args == '':
                    response_text, response_attachment = await CommandHandler.CommandHandler().run_command(command)
                else:
                    response_text, response_attachment = await CommandHandler.CommandHandler().run_command(command, args)
                # await message.channel.send(response_text, files=response_attachment)
                await bot_response.edit(content=f'{message.author.mention} {response_text}', attachments=response_attachment)
            except Exception as e:
                print(str(e))
                await bot_response.edit(content="Something went wrong.")
            return

        for key in reaction_dict.keys():
            if str(message.content).lower().find(key) != -1:
                try:
                    emote_id = random.choice(reaction_dict[key])
                    await message.add_reaction(emote_id)
                except Exception as e:
                    logging.error(f"Error adding reaction {reaction_dict[key]} to message {message.id}: {str(e)}")

        if self.user.mentioned_in(message):
            async with message.channel.typing():
                msg_channel = message.channel
                history = [m async for m in msg_channel.history(limit=80, before=message)]
                history.reverse()
                response = await ChatHandler.respond_in_chat(message, self)
                await message.channel.send(response)

        if message.author.id == 292447304395522048 and random.randint(0, 100) < 20:
            await message.add_reaction("<a:wheel:1096138684786544883>")


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = MyClient(intents=intents)
    bot_token = os.getenv('BOT_TOKEN')
    client.run(bot_token)


if __name__ == '__main__':
    main()
