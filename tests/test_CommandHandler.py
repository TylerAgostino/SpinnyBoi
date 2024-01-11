import unittest
import os
from modules import CommandHandler


class TestCommandHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.command_handler = CommandHandler.CommandHandler()
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # Change working directory to project root

    async def async_info_test(self):
        command_with_args = str("/spinfo").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await self.command_handler.run_command(command)
        else:
            response = await self.command_handler.run_command(command, args)
        print(response)

    async def test_single_sheet(self):
        command_with_args = str("/spin tracks times run=0").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await self.command_handler.run_command(command)
        else:
            response = await self.command_handler.run_command(command, args)
        print(response)

    async def test_custom(self):
        command_with_args = str("/spin custom a,b,c").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await self.command_handler.run_command(command)
        else:
            response = await self.command_handler.run_command(command, args)
        print(response)

    async def test_preset(self):
        command_with_args = str("/spin preset chain").lower().removeprefix('/spin').strip(' ')
        command = command_with_args.split(' ')[0]
        args = ' '.join(command_with_args.split(' ')[1:])
        if args == '':
            response = await self.command_handler.run_command(command)
        else:
            response = await self.command_handler.run_command(command, args)
        print(response)

    def tearDown(self):
        self.command_handler.driver.close()

