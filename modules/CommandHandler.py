import os
import pandas as pd
from modules import WheelSpinner
import discord
import logging
from selenium import webdriver
import typing  # For type hinting
import functools
import asyncio
import random


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


class CommandHandler:
    def __init__(self):
        self.ghseet_url = lambda x: f'https://docs.google.com/spreadsheets/d/{os.getenv("GSHEET_ID")}/gviz/tq?tqx=out:csv&sheet={x}'
        self.presets_df = pd.read_csv(self.ghseet_url('presets'))
        options = webdriver.FirefoxOptions()
        options.add_argument('--headless')
        options.add_argument("--height=1100")
        options.add_argument("--width=1000")

        logging.info('Starting browser')
        self.driver = webdriver.Firefox(options=options)
        pass

    async def run_command(self, command, *args):
        logging.info(f'Spinning {command} with args: {args}')
        if command == '':
            output = await self.handle('preset', 'DEFAULT')
        else:
            output = await self.handle(command, *args)
        if isinstance(output, tuple):
            self.response_text = output[0]
            if isinstance(output[1], list):
                self.response_attachment = [discord.File(gif, filename='wheel.gif') for gif in output[1]]
            else:
                self.response_attachment = [discord.File(output[1], filename='wheel.gif')]
        else:
            self.response_text = output
            self.response_attachment = []
        logging.info('Done spinning')
        return self.response_text, self.response_attachment

    @to_thread
    def handle(self, command, *args, **kwargs):
        try:
            command = getattr(self, command)
            return command(*args, **kwargs)
        except Exception as e:
            try:
                return self.spin_single_sheet(command, *args, **kwargs)
            except Exception as e:
                print(str(e))
                return "I don't know that command"

    @staticmethod
    def fo():
        return """*SpinnyBoi*
        `/spin` - Spins the default wheel for The Beer League
        `/spin custom A,B,C` - Spins a wheel with the options A, B, and C
        `/spin preset <preset_name>` - Spins a wheel with the options from the preset <preset_name>. 
        `/spin <tab> <filter>` - Spins a wheel with the options from the tab <tab> filtered by <filter>
        You can use `<`, `>`, `<=`, `>=`, `=`, and `<>` to filter numeric columns, or `<>` (not equal), `:` (contains), and `=` (exactly equal) to filter text columns. Use `|` to give an OR condition. Use `!weight=<column_name>` to weight the options by the values in <column_name>.
        Presets, tabs, and their columns and values are defined in the GSheet. If you don't know where that is, ask an admin, like Koffard. 
        """

    def tester(self):
        wheel = WheelSpinner.WheelSpinner()
        file = wheel.return_gif(self.driver)
        return self.get_message(), file

    def custom(self, options):
        opts_list = [opt for opt in options.split(',')]
        wheel = WheelSpinner.WheelSpinner(opts_list)
        file = wheel.return_gif(self.driver)
        return self.get_message(), file

    def preset(self, preset_name):
        filters_df = self.presets_df.query(f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'").to_dict('records')[0]
        wheels = []
        for tab in filters_df.keys():
            if isinstance(filters_df[tab], str) and tab != 'Fullname':
                filter_string = str(filters_df[tab]).lower()
                opt_set = self.generate_option_set(tab, filter_string)
                wheels.append(WheelSpinner.WheelSpinner(opt_set[0]))
        gifs = []
        for wheel in wheels:
            gifs.append(wheel.return_gif(self.driver))
        return self.get_message(), gifs

    def generate_option_set(self, tab, filter_string=''):
        df = pd.read_csv(self.ghseet_url(tab))
        df.columns = df.columns.str.lower()
        filter_string = filter_string.strip(' ')
        filters = filter_string.split(',')
        filter_queries = []
        weighting = None
        for filter in filters:
            if filter.find('|') > 0:
                or_query = []
                for option in filter.split('|'):
                    if option.find('<>') > 0:
                        a = option.split('<>')
                        or_query.append(f"not `{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')")
                    elif option.find('>=') > 0:
                        a = option.split('>=')
                        or_query.append(f"`{a[0].strip(' ')}`>={a[1].strip(' ')}")
                    elif option.find('<=') > 0:
                        a = option.split('<=')
                        or_query.append(f"`{a[0].strip(' ')}`<={a[1].strip(' ')}")
                    elif option.find('<') > 0:
                        a = option.split('<')
                        or_query.append(f"`{a[0].strip(' ')}`<{a[1].strip(' ')}")
                    elif option.find('>') > 0:
                        a = option.split('>')
                        or_query.append(f"`{a[0].strip(' ')}`>{a[1].strip(' ')}")
                    elif option.find(':') > 0:
                        a = option.split(':')
                        or_query.append(f"`{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')")
                    elif option.find('=') > 0:
                        a = option.split('=')
                        or_query.append(f"`{a[0].strip(' ')}`.astype('string').str.lower()=='{a[1].strip(' ')}'")
                filter_queries.append(" | ".join(or_query))
            else:
                if filter.find('!weight=') > 0:
                    weighting = filter.split('=')[1]
                elif filter.find('<>') > 0:
                    a = filter.split('<>')
                    filter_queries.append(f"not `{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')")
                elif filter.find('>=') > 0:
                    a = filter.split('>=')
                    filter_queries.append(f"`{a[0].strip(' ')}`>={a[1].strip(' ')}")
                elif filter.find('<=') > 0:
                    a = filter.split('<=')
                    filter_queries.append(f"`{a[0].strip(' ')}`<={a[1].strip(' ')}")
                elif filter.find('<') > 0:
                    a = filter.split('<')
                    filter_queries.append(f"`{a[0].strip(' ')}`<{a[1].strip(' ')}")
                elif filter.find('>') > 0:
                    a = filter.split('>')
                    filter_queries.append(f"`{a[0].strip(' ')}`>{a[1].strip(' ')}")
                elif filter.find(':') > 0:
                    a = filter.split(':')
                    filter_queries.append(f"`{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')")
                elif filter.find('=') > 0:
                    a = filter.split('=')
                    filter_queries.append(f"`{str(a[0].strip(' '))}`.astype('string').str.lower()=='{a[1].strip(' ')}'")

        filtereddf = df
        for query_string in filter_queries:
            filtereddf = filtereddf.query(query_string)
        selections = filtereddf.to_dict('records')
        if weighting is None:
            option_set = [[(selection['fullname'], 1) for selection in selections]]
        else:
            option_set = [[(selection['fullname'], int(selection[weighting])) for selection in selections]]
        return option_set

    def spin_single_sheet(self, tab, filter_string=''):
        opt_set = self.generate_option_set(tab, filter_string)
        wheel = WheelSpinner.WheelSpinner(opt_set[0])
        gif = wheel.return_gif(self.driver)
        return self.get_message(), gif

    @staticmethod
    def get_message(messages_file='messages.txt'):
        roll = random.random()
        fp = open(messages_file)
        messages = [message for message in fp.readlines()]
        lines = len(messages)
        return messages[int(int(roll * 100) % lines)]
