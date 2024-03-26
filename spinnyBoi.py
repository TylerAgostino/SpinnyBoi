import os
import discord
import logging
from modules import CommandHandler

status_message = "/spin"

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')

# Reactions

reactions_file = open('reactions.txt', 'r')
reactions = reactions_file.readlines()
reaction_dict = {}
for reaction in reactions:
    tup = reaction.split(',')
    reaction_dict[tup[0].lower()] = tup[1].strip()
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

        if str(message.content).lower().startswith('/spin'):
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
                await message.add_reaction(reaction_dict[key])
                return
        if str(message.content).lower().find('spin') != -1 or str(message.content).lower().find('wheel') != -1:
            await message.add_reaction("")
            return


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    client = MyClient(intents=intents)
    bot_token = os.getenv('BOT_TOKEN')
    client.run(bot_token)


if __name__ == '__main__':
    main()
