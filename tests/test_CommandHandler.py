import unittest
from modules import CommandHandler


class TestCommandHandler(unittest.TestCase):
    def async_info_test(self):
        command_with_args = str("/spinfo").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await CommandHandler.CommandHandler().run_command(command)
        else:
            response = await CommandHandler.CommandHandler().run_command(command, args)
        print(response)

    async def test_custom(self):
        command_with_args = str("/spin custom one, two, three").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await  CommandHandler.CommandHandler().run_command(command)
        else:
            response = await CommandHandler.CommandHandler().run_command(command, args)
        print(response)


    async def test_preset(self):
        command_with_args = str("/spin preset any week").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await CommandHandler.CommandHandler().run_command(command)
        else:
            response = await CommandHandler.CommandHandler().run_command(command, args)
        print(response)

