from modules import WheelSpinner
from io import StringIO
import discord

class CommandHandler:
    def __init__(self, command, **kwargs):
        output = self.handle(command, **kwargs)
        if isinstance(output, tuple):
            self.response_text = output[0]
            self.response_attachment = output[1]
        else:
            self.response_text = output
            self.response_attachment = None

    def handle(self, command, **kwargs):
        command = getattr(self, command)
        return command(**kwargs)

    def fo(self):
        return """Testing the spinfo command"""

    def tester(self):
        wheel = WheelSpinner.WheelSpinner()
        file = wheel.return_gif()
        discord_file = discord.File(file, filename="wheel.gif")
        return "Here's your file", discord_file
