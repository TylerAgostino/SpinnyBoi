import io
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
import dataframe_image as dfi
import uuid


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


class NoTabError(Exception):
    pass


class _WheelOption:
    def __init__(self, option: str, weight=1, on_select=None, include_text=None):
        self.option = option
        self.weight = weight
        self.on_select = on_select
        self.include_text = include_text if include_text is not None else ''


class CommandHandler:
    def __init__(self):
        base_url = f'https://docs.google.com/spreadsheets/d/{os.getenv("GSHEET_ID")}'

        self.ghseet_url = lambda x: f'{base_url}/gviz/tq?tqx=out:csv&sheet={x}'

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
            try:
                filename = output[2]
            except IndexError:
                filename = 'wheel.gif'
            if isinstance(output[1], list):
                self.response_attachment = [discord.File(gif, filename=filename) for gif in output[1]]
            else:
                self.response_attachment = [discord.File(output[1], filename=filename)]
        else:
            self.response_text = output
            self.response_attachment = []
        logging.info('Done spinning')
        self.driver.quit()
        return self.response_text, self.response_attachment

    @to_thread
    def handle(self, command, *args, **kwargs):
        try:
            command = getattr(self, command)
            return command(*args, **kwargs)
        except AttributeError as e:
            try:
                return self.spin_single_sheet(command, *args, **kwargs)
            except NoTabError as e:
                logging.warning(f'No tab found: {str(e)}')
                return f"I don't have a command called {command}, and there's no tab in the gsheet with that name."
        except Exception as e:
            logging.debug(f'Command {command} failed with args {" ".join(*args)}: {str(e)}')
            logging.error(e)
            return "You're using a valid command, but something went wrong."

    def fo(self, *args):
        # if no arguments are passed, return the help message
        if not args:
            return """*SpinnyBoi*
            `/spin` - Spins the default wheel for The Beer League
            `/spin custom A,B,C` - Spins a wheel with the options A, B, and C
            `/spin preset <preset_name>` - Spins a wheel with the options from the preset <preset_name>. 
            `/spin <tab> <filter>` - Spins a wheel with the options from the tab <tab> filtered by <filter>. Filter is optional.
            `/spinfo` - Returns this help message
            `/spinfo <preset> <tab>` - Returns the odds of each option for the given tab and preset. Both a tab and preset are required, and the tab must be used in the preset.
            You can use `<`, `>`, `<=`, `>=`, `=`, and `<>` to filter numeric columns, or `<>` (not equal), `:` (contains), and `=` (exactly equal) to filter text columns. Use `|` to give an OR condition. Use `!weight=<column_name>` to weight the options by the values in <column_name>.
            Presets, tabs, and their columns and values are defined in the GSheet. If you don't know where that is, ask an admin, like Koffard. 
            """
        else:
            args = args[0].split(' ')
            try:
                preset_name = args[0]
                tab_name = args[1]
            except IndexError:
                return "You need to provide a preset and a tab to get the odds for."
            try:
                filters_df = self.presets_df.query(f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'").to_dict('records')[0]
            except IndexError:
                return f"I can't find a preset named {preset_name}."

            for tab in filters_df.keys():
                if tab.lower() == tab_name.lower():
                    tab_name = tab
            filter_string = str(filters_df[tab_name]).lower()
            try:
                opt_set = self.generate_option_set(tab_name, filter_string)
            except Exception as e:
                logging.error(f'Error processing tab {tab_name}, filter string {filter_string}: {str(e)}')
                return f"The preset {preset_name} ran into an error: {str(e)}"
            options_df = pd.DataFrame([[option.option, option.weight, option.on_select] for option in opt_set], columns=['Option', 'Weight', 'OnSelect'])
            # Get the percentage of each option
            total_weight = options_df['Weight'].sum()
            options_df['Percentage'] = options_df['Weight'] / total_weight
            # Get rid of extra columns
            outputdf = options_df[['Option', 'Percentage']]
            # Get rid of options with 0% chance
            outputdf = outputdf[outputdf['Percentage'] > 0]
            # Sort by percentage
            outputdf = outputdf.sort_values(by='Percentage', ascending=False)
            # Format the percentages
            outputdf['Percentage'] = [f"{round(100 * p, 2)}%" for p in outputdf['Percentage'] ]
            # Reset the index
            outputdf = outputdf.reset_index(drop=True)
            # Make the index 1-based
            outputdf.index = outputdf.index + 1
            # Get a unique filename
            run_id = f'{os.getcwd()}/{str(uuid.uuid4())[0:8]}.png'
            # Export the dataframe to a series of images
            rows = len(outputdf)
            images = []
            for i in range(0, rows, 30):
                dfi.export(outputdf[i:i+30], run_id, max_rows=-1, dpi=100, fontsize=20, table_conversion='matplotlib')
                fh = io.BytesIO()
                fh.write(open(run_id, 'rb').read())
                fh.seek(0)
                os.remove(run_id)
                images.append(fh)
            return "", images, 'odds.png'

    def custom(self, options):
        opts_list = [_WheelOption(opt) for opt in options.split(',')]
        wheel = WheelSpinner.WheelSpinner(opts_list)
        file = wheel.return_gif(self.driver)
        return self.get_message(), file

    def preset(self, preset_name):
        try:
            filters_df = self.presets_df.query(f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'").to_dict('records')[0]
        except IndexError:
            return f"I can't find a preset named {preset_name}."
        wheels = []
        for tab in filters_df.keys():
            if isinstance(filters_df[tab], str) and tab != 'Fullname':
                filter_string = str(filters_df[tab]).lower()
                try:
                    opt_set = self.generate_option_set(tab, filter_string)
                except Exception as e:
                    logging.error(f'Error processing tab {tab}, filter string {filter_string}: {str(e)}')
                    return f"The preset {preset_name} ran into an error: {str(e)}"

                wheel = WheelSpinner.WheelSpinner(opt_set)
                wheels.append(wheel)
                depth = 0
                next_spin = wheel.next_spin
                while next_spin is not None and depth < 10:
                    next_tab, next_filter_string = next_spin
                    next_opt_set = self.generate_option_set(next_tab, next_filter_string)
                    next_wheel = WheelSpinner.WheelSpinner(next_opt_set)
                    wheels.append(next_wheel)
                    next_spin = next_wheel.next_spin
                    depth += 1
                    pass

        gifs = []
        responses = []
        for wheel in wheels:
            gifs.append(wheel.return_gif(self.driver))
            responses.append(wheel.response)

        message = f'{self.get_message()} {" ".join(responses)}'
        return message, gifs

    def generate_option_set(self, tab, filter_string=''):
        try:
            df = pd.read_csv(self.ghseet_url(tab))
            if list(df.columns.values) == ['FALSE']:
                raise NoTabError(f'Tab {tab} not found')
            pass
        except NoTabError as e:
            raise e
        except Exception as e:
            logging.error(f'Error processing tab {tab}: {str(e)}')
            raise e
        df.columns = df.columns.str.lower()
        filter_string = filter_string.strip(' ').lower()
        filters = filter_string.split(',')
        filter_queries = []
        weighting = None
        on_select = None
        response_text = None
        for filter in filters:
            try:
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
                    if filter.find('!weight=') >= 0:
                        weighting = filter.split('=')[1]
                    elif filter.find('!onselect=') >= 0:
                        on_select = filter.split('=')[1]
                    elif filter.find('!response=') >= 0:
                        response_text = filter.split('=')[1]
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
            except Exception as e:
                logging.error(f'Error processing filter {filter}: {str(e)}')
                raise Exception(f'One of your filters is not properly formatted') from e

        filtereddf = df
        for query_string in filter_queries:
            try:
                filtereddf = filtereddf.query(query_string)
            except Exception as e:
                logging.error(f'Error processing filter {query_string}: {str(e)}')
                raise Exception(f'Something is wrong with one of your filters.') from e
        if filtereddf.empty:
            raise Exception(f'Your filter {filter} returned no results')
        selections = filtereddf.to_dict('records')
        if weighting is None:
            for selection in selections:
                selection['nullweight'] = 1
                weighting = 'nullweight'
        if on_select is None:
            for selection in selections:
                selection['nullonSelect'] = None
                on_select = 'nullonSelect'

        option_set = [_WheelOption(selection['fullname'], int(selection[weighting]), selection[on_select], None if response_text is None else selection[response_text]) for selection in selections]
        return option_set

    def spin_single_sheet(self, tab, filter_string=''):
        try:
            opt_set = self.generate_option_set(tab, filter_string)
        except Exception as e:
            logging.error(f"Error processing tab {tab}: {str(e)}")
            return str(e)
        wheel = WheelSpinner.WheelSpinner(opt_set)
        gif = wheel.return_gif(self.driver)
        message = f'{self.get_message()} {wheel.response}'
        return message, gif

    @staticmethod
    def get_message(messages_file='messages.txt'):
        roll = random.random()
        fp = open(messages_file)
        messages = [message for message in fp.readlines()]
        lines = len(messages)
        return messages[int(int(roll * 100) % lines)].strip('\n')
