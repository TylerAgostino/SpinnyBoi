# pyright: basic
import discord
from discord.ext import commands
import os
import logging
from typing import Optional
from modules import ChatHandler
from selenium import webdriver
import io
import pandas as pd
from modules import WheelSpinner
import typing  # For type hinting
import functools
import asyncio
import random
import dataframe_image as dfi
import uuid
import requests


async def get_presets(a):
    base_url = f'https://docs.google.com/spreadsheets/d/{os.getenv("GSHEET_ID")}'
    presets_df = pd.read_csv(f"{base_url}/gviz/tq?tqx=out:csv&sheet={'presets'}")
    return [x["Fullname"] for x in presets_df.to_dict("records")]


async def get_preset_tabs(ctx):
    base_url = f'https://docs.google.com/spreadsheets/d/{os.getenv("GSHEET_ID")}'
    presets_df = pd.read_csv(f"{base_url}/gviz/tq?tqx=out:csv&sheet={'presets'}")
    preset_name = ctx.options.get("preset_name", "")
    if not preset_name:
        return []
    try:
        filters_df = presets_df.query(
            f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'"
        ).to_dict("records")[0]
        tabs = [
            k for k, v in filters_df.items() if isinstance(v, str) and k != "Fullname"
        ]
        return tabs
    except Exception as e:
        logging.error(f"Error getting tabs for preset {preset_name}: {str(e)}")
        return []


def to_thread(func: typing.Callable):
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
        self.include_text = include_text if include_text is not None else ""


class WheelCog(commands.Cog):
    def __init__(self, bot):
        base_url = f'https://docs.google.com/spreadsheets/d/{os.getenv("GSHEET_ID")}'

        self.ghseet_url = lambda x: f"{base_url}/gviz/tq?tqx=out:csv&sheet={x}"

        self.presets_df = pd.read_csv(self.ghseet_url("presets"))
        options = webdriver.FirefoxOptions()
        options.add_argument("--headless")
        options.add_argument("--height=1100")
        options.add_argument("--width=1000")

        self.driver_options = options

        self.bot = bot

    spin = discord.SlashCommandGroup("spin", "Spin commands")

    @staticmethod
    def get_message(messages_file="messages.txt"):
        roll = random.random()
        fp = open(messages_file)
        messages = [message for message in fp.readlines()]
        lines = len(messages)
        return messages[int(int(roll * 100) % lines)].strip("\n")

    async def process_reply(
        self, ctx, bot_response, response_text, response_attachment, response_filename
    ):
        try:
            if isinstance(response_attachment, list):
                response_attachment = [
                    discord.File(fp=f, filename=response_filename)
                    for f in response_attachment
                ]
            else:
                response_attachment = (
                    [discord.File(fp=response_attachment, filename=response_filename)]
                    if response_attachment
                    else None
                )
            await bot_response.edit(
                content=f"{ctx.author.mention} {response_text}",
                files=response_attachment,
            )
        except Exception as e:
            logging.error(f"Error in spin_handler: {str(e)}")
            await bot_response.edit(content=f"An error occurred: {str(e)}")

    @spin.command(name="custom")
    @discord.option(
        "custom_options",
        str,
        required=True,
        description="Comma-separated custom options",
    )
    async def spin_custom(self, ctx, custom_options: str):
        """Spins a wheel with the provided custom options."""
        await ctx.defer()
        driver = webdriver.Firefox(options=self.driver_options)
        bot_response = await ctx.respond(ChatHandler.working_on_it())
        opts_list = [_WheelOption(opt) for opt in custom_options.split(",")]
        wheel = WheelSpinner.WheelSpinner(opts_list)
        file = wheel.return_gif(driver)
        await self.process_reply(
            ctx, bot_response, self.get_message(), file, "wheel.gif"
        )
        driver.quit()

    @spin.command(name="preset")
    @discord.option(
        "preset_name",
        str,
        required=True,
        autocomplete=discord.utils.basic_autocomplete(get_presets),
    )
    async def spin_preset(self, ctx, preset_name):
        """Spins a preset wheel or colleciton of wheels based on the provided preset name"""
        await ctx.defer()
        driver = webdriver.Firefox(options=self.driver_options)
        bot_response = await ctx.respond(ChatHandler.working_on_it())
        try:
            filters_df = self.presets_df.query(
                f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'"
            ).to_dict("records")[0]
        except IndexError:
            return f"I can't find a preset named {preset_name}."
        wheels = []
        for tab in filters_df.keys():
            if isinstance(filters_df[tab], str) and tab != "Fullname":
                filter_string = str(filters_df[tab]).lower()
                try:
                    opt_set = self.generate_option_set(tab, filter_string)
                except Exception as e:
                    logging.error(
                        f"Error processing tab {tab}, filter string {filter_string}: {str(e)}"
                    )
                    return f"The preset {preset_name} ran into an error: {str(e)}"

                wheel = WheelSpinner.WheelSpinner(opt_set)
                wheels.append(wheel)
                depth = 0
                next_spin = wheel.next_spin
                while next_spin is not None and depth < 10:
                    next_tab, next_filter_string = next_spin
                    next_opt_set = self.generate_option_set(
                        next_tab, next_filter_string
                    )
                    next_wheel = WheelSpinner.WheelSpinner(next_opt_set)
                    wheels.append(next_wheel)
                    next_spin = next_wheel.next_spin
                    depth += 1
                    pass

        gifs = []
        responses = []
        for wheel in wheels:
            gifs.append(wheel.return_gif(driver))
            responses.append(wheel.response)

        message = f'{self.get_message()} {" ".join(responses)}'
        await self.process_reply(ctx, bot_response, message, gifs, "wheel.gif")
        driver.quit()

    @commands.slash_command(name="spinfo")
    @discord.option(
        "preset_name",
        str,
        required=True,
        autocomplete=discord.utils.basic_autocomplete(get_presets),
        description="The name of the preset to get info about",
    )
    @discord.option(
        "tab_name",
        str,
        required=True,
        description="The name of the tab to get info about (required if preset_name is provided)",
        autocomplete=discord.utils.basic_autocomplete(get_preset_tabs),
    )
    async def spinfo(
        self,
        ctx,
        preset_name,
        tab_name,
    ):
        """Get information about the weights for a particular tab (wheel) of a preset"""
        await ctx.defer()
        bot_response = await ctx.respond(ChatHandler.working_on_it())
        try:
            filters_df = self.presets_df.query(
                f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'"
            ).to_dict("records")[0]
        except IndexError:
            return f"I can't find a preset named {preset_name}."

        for tab in filters_df.keys():
            if tab.lower() == tab_name.lower():
                tab_name = tab
        filter_string = str(filters_df[tab_name]).lower()
        try:
            opt_set = self.generate_option_set(tab_name, filter_string)
        except Exception as e:
            logging.error(
                f"Error processing tab {tab_name}, filter string {filter_string}: {str(e)}"
            )
            return f"The preset {preset_name} ran into an error: {str(e)}"
        options_df = pd.DataFrame(
            [[option.option, option.weight, option.on_select] for option in opt_set],
            columns=["Option", "Weight", "OnSelect"],
        )
        # Get the percentage of each option
        total_weight = options_df["Weight"].sum()
        options_df["Percentage"] = options_df["Weight"] / total_weight
        # Get rid of extra columns
        outputdf = options_df[["Option", "Percentage"]]
        # Get rid of options with 0% chance
        outputdf = outputdf[outputdf["Percentage"] > 0]
        # Sort by percentage
        outputdf = outputdf.sort_values(by="Percentage", ascending=False)
        # Format the percentages
        outputdf["Percentage"] = [
            f"{round(100 * p, 2)}%" for p in outputdf["Percentage"]
        ]
        # Reset the index
        outputdf = outputdf.reset_index(drop=True)
        # Make the index 1-based
        outputdf.index = outputdf.index + 1
        # Get a unique filename
        run_id = f"{os.getcwd()}/{str(uuid.uuid4())[0:8]}.png"
        # Export the dataframe to a series of images
        rows = len(outputdf)
        images = []
        for i in range(0, rows, 30):
            dfi.export(
                outputdf[i : i + 30],
                run_id,
                max_rows=-1,
                dpi=100,
                fontsize=20,
                table_conversion="matplotlib",
            )
            fh = io.BytesIO()
            fh.write(open(run_id, "rb").read())
            fh.seek(0)
            os.remove(run_id)
            images.append(fh)
        await self.process_reply(
            ctx,
            bot_response,
            f"Here are the options for the preset {preset_name}, tab {tab_name}:",
            images,
            "options.png",
        )

    def generate_option_set(self, tab, filter_string=""):
        try:
            df = pd.read_csv(self.ghseet_url(tab))
            if list(df.columns.values) == ["FALSE"]:
                raise NoTabError(f"Tab {tab} not found")
            pass
        except NoTabError as e:
            raise e
        except Exception as e:
            logging.error(f"Error processing tab {tab}: {str(e)}")
            raise e
        df.columns = df.columns.str.lower()
        filter_string = filter_string.strip(" ").lower()
        filters = filter_string.split(",")
        filter_queries = []
        weighting = None
        on_select = None
        response_text = None
        for filter in filters:
            try:
                if filter.find("|") > 0:
                    or_query = []
                    for option in filter.split("|"):
                        if option.find("<>") > 0:
                            a = option.split("<>")
                            or_query.append(
                                f"not `{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')"
                            )
                        elif option.find(">=") > 0:
                            a = option.split(">=")
                            or_query.append(f"`{a[0].strip(' ')}`>={a[1].strip(' ')}")
                        elif option.find("<=") > 0:
                            a = option.split("<=")
                            or_query.append(f"`{a[0].strip(' ')}`<={a[1].strip(' ')}")
                        elif option.find("<") > 0:
                            a = option.split("<")
                            or_query.append(f"`{a[0].strip(' ')}`<{a[1].strip(' ')}")
                        elif option.find(">") > 0:
                            a = option.split(">")
                            or_query.append(f"`{a[0].strip(' ')}`>{a[1].strip(' ')}")
                        elif option.find(":") > 0:
                            a = option.split(":")
                            or_query.append(
                                f"`{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')"
                            )
                        elif option.find("=") > 0:
                            a = option.split("=")
                            or_query.append(
                                f"`{a[0].strip(' ')}`.astype('string').str.lower()=='{a[1].strip(' ')}'"
                            )
                    filter_queries.append(" | ".join(or_query))
                else:
                    if filter.find("!weight=") >= 0:
                        weighting = filter.split("=")[1]
                    elif filter.find("!onselect=") >= 0:
                        on_select = filter.split("=")[1]
                    elif filter.find("!response=") >= 0:
                        response_text = filter.split("=")[1]
                    elif filter.find("<>") > 0:
                        a = filter.split("<>")
                        filter_queries.append(
                            f"not `{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')"
                        )
                    elif filter.find(">=") > 0:
                        a = filter.split(">=")
                        filter_queries.append(f"`{a[0].strip(' ')}`>={a[1].strip(' ')}")
                    elif filter.find("<=") > 0:
                        a = filter.split("<=")
                        filter_queries.append(f"`{a[0].strip(' ')}`<={a[1].strip(' ')}")
                    elif filter.find("<") > 0:
                        a = filter.split("<")
                        filter_queries.append(f"`{a[0].strip(' ')}`<{a[1].strip(' ')}")
                    elif filter.find(">") > 0:
                        a = filter.split(">")
                        filter_queries.append(f"`{a[0].strip(' ')}`>{a[1].strip(' ')}")
                    elif filter.find(":") > 0:
                        a = filter.split(":")
                        filter_queries.append(
                            f"`{a[0].strip(' ')}`.astype('string').str.lower().str.contains('{a[1].strip(' ')}')"
                        )
                    elif filter.find("=") > 0:
                        a = filter.split("=")
                        filter_queries.append(
                            f"`{str(a[0].strip(' '))}`.astype('string').str.lower()=='{a[1].strip(' ')}'"
                        )
            except Exception as e:
                logging.error(f"Error processing filter {filter}: {str(e)}")
                raise Exception(f"One of your filters is not properly formatted") from e

        filtereddf = df
        for query_string in filter_queries:
            try:
                filtereddf = filtereddf.query(query_string)
            except Exception as e:
                logging.error(f"Error processing filter {query_string}: {str(e)}")
                raise Exception(f"Something is wrong with one of your filters.") from e
        if filtereddf.empty:
            raise Exception(f"Your filter {filter} returned no results")
        selections = filtereddf.to_dict("records")
        if weighting is None:
            for selection in selections:
                selection["nullweight"] = 1
                weighting = "nullweight"
        if on_select is None:
            for selection in selections:
                selection["nullonSelect"] = None
                on_select = "nullonSelect"

        option_set = [
            _WheelOption(
                selection["fullname"],
                int(selection[weighting]),
                selection[on_select],
                None if response_text is None else selection[response_text],
            )
            for selection in selections
        ]
        return option_set

    @spin.command(name="order")
    @discord.option(
        "custom_options",
        str,
        required=False,
        default=None,
        description="Comma-separated custom options",
    )
    async def spintermix(self, ctx, custom_options: Optional[str] = None):
        """Returns a random ordering of the provided options."""
        await ctx.defer()
        driver = webdriver.Firefox(options=self.driver_options)
        bot_response = await ctx.respond(ChatHandler.working_on_it())
        opts_list = [opt.strip() for opt in custom_options.split(",")]
        wheel = WheelSpinner.WheelSpinner.create_spindex(opts_list)
        file = wheel.return_gif(driver)
        await self.process_reply(
            ctx,
            bot_response,
            self.get_message("spindex_messages.txt"),
            file,
            "wheel.gif",
        )
        driver.quit()

    @spin.command(name="auditor")
    @discord.default_permissions(administrator=True)
    async def spin_auditor(self, ctx):
        """Restart the session auditor to check for new or changed sessions and run validation."""
        await ctx.defer()
        webhook_url = "http://192.168.1.125:9996/api/stacks/webhooks/b1fb8123-6c54-439d-839f-11c2ad01a011?pullimage=true"
        req = requests.post(webhook_url)
        logging.info(f"Request sent to webhook: {req}")
        await ctx.respond(
            "I've sent a request to restart the auditor. It should post shortly."
        )
