# pyright: basic
import discord
from discord.ext import commands, tasks
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
import datetime
import json
from functools import wraps
from modules.scheduler.scheduler import (
    get_pending_events,
    mark_event_completed,
    schedule_event,
    get_all_scheduled_events,
    cancel_event,
)


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


def wheel_command(needs_driver=True, is_interaction=True):
    """
    Decorator for wheel commands that handles common operations:
    - Defer the response
    - Set up and tear down webdriver (if needed)
    - Handle file attachments and responses

    The decorated function will be wrapped to handle common setup/teardown tasks.
    Original command functions don't need to call defer() or create/quit webdriver.

    Args:
        needs_driver: Whether the command needs a webdriver instance (default: True)
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(self, ctx, *args, **kwargs):
            # First defer the interaction
            if is_interaction:
                await ctx.defer()
                bot_response = await ctx.respond(ChatHandler.working_on_it())
            else:
                bot_response = await ctx.send(ChatHandler.working_on_it())

            driver = None
            try:
                # Create a driver if needed
                if needs_driver:
                    driver = webdriver.Firefox(options=self.driver_options)

                try:
                    # Call the original function with or without driver
                    if needs_driver:
                        result = await func(
                            self,
                            ctx=ctx,
                            bot_response=bot_response,
                            driver=driver,
                            *args,
                            **kwargs,
                        )
                    else:
                        result = await func(
                            self, ctx=ctx, bot_response=bot_response, *args, **kwargs
                        )
                except Exception as e:
                    result = (f"An error occurred:\n```{str(e)}```", None, None)
                    logging.error(f"Error in wheel command {func.__name__}: {str(e)}")

                # Process the result if returned
                if result:
                    message, files, filename = result
                    if files is None:
                        await bot_response.edit(content=message)
                    else:
                        if isinstance(files, list):
                            response_attachment = [
                                discord.File(fp=f, filename=filename) for f in files
                            ]
                        else:
                            response_attachment = [
                                discord.File(fp=files, filename=filename)
                            ]
                        try:
                            content = f"{ctx.author.mention} {message}"
                        except Exception:
                            content = message
                        await bot_response.edit(
                            content=content,
                            files=response_attachment,
                        )
            finally:
                # Clean up the driver if created
                if driver:
                    driver.quit()

        return wrapper

    return decorator


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

        # Days of week for schedule command
        self.days_of_week = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }

        self.presets_df = pd.read_csv(self.ghseet_url("presets"))
        options = webdriver.FirefoxOptions()
        options.add_argument("--headless")
        options.add_argument("--height=1100")
        options.add_argument("--width=1000")

        self.driver_options = options

        self.bot = bot
        self.check_scheduled_events.start()

    spin = discord.SlashCommandGroup("spin", "Spin commands")
    schedule = discord.SlashCommandGroup("schedule", "Schedule commands")

    @tasks.loop(minutes=1)
    async def check_scheduled_events(self):
        """Check for pending scheduled events and execute them."""
        pending_events = get_pending_events()
        message, files, filename = "No matching command.", None, None
        for event in pending_events:
            try:
                logging.info(
                    f"Processing scheduled event {event.id}: {event.function_name}"
                )

                # Get the channel where this event should be executed
                channel = await self.bot.fetch_channel(int(event.channel_id))
                if not channel:
                    logging.error(
                        f"Channel {event.channel_id} not found for event {event.id}"
                    )
                    if event.id is not None:
                        mark_event_completed(event.id)
                    continue

                # Parse the event data
                data = json.loads(event.data) if event.data else {}

                if event.function_name == "spin_preset":
                    preset_name = data.get("preset_name")
                    await self.spin_preset_new_message(
                        ctx=channel, preset_name=preset_name
                    )

                    # Mark the event as completed
                    if event.id is not None:
                        mark_event_completed(event.id)
            except Exception as e:
                logging.error(f"Error executing scheduled event {event.id}: {str(e)}")

            return message, files, filename

    @staticmethod
    def get_message(messages_file="messages.txt"):
        roll = random.random()
        fp = open(messages_file)
        messages = [message for message in fp.readlines()]
        lines = len(messages)
        return messages[int(int(roll * 100) % lines)].strip("\n")

    @spin.command(name="custom")
    @discord.option(
        "custom_options",
        str,
        required=True,
        description="Comma-separated custom options",
    )
    async def wrap_custom(self, ctx, custom_options):
        """Spins a wheel with the provided custom options."""
        await self.spin_custom(ctx=ctx, custom_options=custom_options)

    @wheel_command()
    async def spin_custom(
        self, ctx, custom_options: str, driver=None, bot_response=None
    ):
        opts_list = [_WheelOption(opt.strip()) for opt in custom_options.split(",")]
        wheel = WheelSpinner.WheelSpinner(opts_list)
        file = wheel.return_gif(driver)
        return self.get_message(), file, "wheel.gif"

    @spin.command(name="preset")
    @discord.option(
        "preset_name",
        str,
        required=True,
        autocomplete=discord.utils.basic_autocomplete(get_presets),
    )
    async def wrap_preset(self, ctx, preset_name):
        """Spins a preset wheel or colleciton of wheels based on the provided preset name"""
        await self.spin_preset(ctx=ctx, preset_name=preset_name)

    @wheel_command()
    async def spin_preset(self, *args, **kwargs):
        """Context based wrapper to call generic_spin_preset"""
        return await self.generic_spin_preset(*args, **kwargs)

    @wheel_command(is_interaction=False)
    async def spin_preset_new_message(self, *args, **kwargs):
        """Message based wrapper to call generic_spin_preset"""
        return await self.generic_spin_preset(*args, **kwargs)

    async def generic_spin_preset(
        self, ctx, preset_name, driver=None, bot_response=None
    ):
        try:
            filters_df = self.presets_df.query(
                f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'"
            ).to_dict("records")[0]
        except IndexError:
            return f"I can't find a preset named {preset_name}.", None, None

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
                    return (
                        f"The preset {preset_name} ran into an error: {str(e)}",
                        None,
                        None,
                    )

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

        message = "{} {}".format(self.get_message(), " ".join(responses))
        return message, gifs, "wheel.gif"

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
    async def wrap_spinfo(self, ctx, preset_name, tab_name):
        """Get information about the weights for a particular tab (wheel) of a preset"""
        await self.spinfo(ctx=ctx, preset_name=preset_name, tab_name=tab_name)

    @wheel_command()
    async def spinfo(self, ctx, preset_name, tab_name, driver=None, bot_response=None):
        try:
            filters_df = self.presets_df.query(
                f"Fullname.astype('string').str.lower()=='{preset_name.lower()}'"
            ).to_dict("records")[0]
        except IndexError:
            return f"I can't find a preset named {preset_name}.", None, None

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
            return f"The preset {preset_name} ran into an error: {str(e)}", None, None
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
        return (
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
    async def wrap_intermix(self, ctx, custom_options):
        """Returns a random ordering of the provided options."""
        await self.spintermix(ctx=ctx, custom_options=custom_options)

    @wheel_command()
    async def spintermix(
        self, ctx, custom_options: Optional[str] = None, driver=None, bot_response=None
    ):
        if custom_options:
            opts_list = [opt.strip() for opt in custom_options.split(",")]
            wheel = WheelSpinner.WheelSpinner.create_spindex(opts_list)
            file = wheel.return_gif(driver)
            return self.get_message("spindex_messages.txt"), file, "wheel.gif"
        else:
            return "Please provide comma-separated options to mix.", None, None

    @spin.command(name="auditor")
    @discord.default_permissions(administrator=True)
    async def wrap_auditor(self, ctx):
        """Restart the session auditor to check for new or changed sessions and run validation."""
        await self.spin_auditor(ctx=ctx)

    @wheel_command(needs_driver=False)
    async def spin_auditor(self, ctx, bot_response=None):
        webhook_url = "http://192.168.1.125:9996/api/stacks/webhooks/b1fb8123-6c54-439d-839f-11c2ad01a011?pullimage=true"
        req = requests.post(webhook_url)
        logging.info("Request sent to webhook: %s", req.status_code)
        return (
            "I've sent a request to restart the auditor. It should post shortly.",
            None,
            None,
        )

    @schedule.command(name="spin")
    @discord.option(
        "day_of_week",
        description="The day of the week to schedule the spin",
        choices=[
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ],
        required=True,
    )
    @discord.option(
        "hour",
        description="The hour to schedule the spin (0-23)",
        min_value=0,
        max_value=23,
        required=True,
    )
    @discord.option(
        "minute",
        description="The minute to schedule the spin (0-59)",
        min_value=0,
        max_value=59,
        required=True,
    )
    @discord.option(
        "preset_name",
        description="The preset to use for the spin",
        autocomplete=get_presets,
        required=True,
    )
    async def schedule_spin(
        self, ctx, day_of_week: str, hour: int, minute: int, preset_name: str
    ):
        await ctx.defer()

        # Get the current date
        now = datetime.datetime.now()

        # Calculate days until next occurrence of the specified day
        current_weekday = now.weekday()
        target_weekday = self.days_of_week[day_of_week.lower()]
        days_until = (target_weekday - current_weekday) % 7

        # If it's the same day and the time has already passed, schedule for next week
        if days_until == 0 and (
            now.hour > hour or (now.hour == hour and now.minute >= minute)
        ):
            days_until = 7

        # Create the scheduled datetime
        target_date = now + datetime.timedelta(days=days_until)
        scheduled_time = datetime.datetime(
            year=target_date.year,
            month=target_date.month,
            day=target_date.day,
            hour=hour,
            minute=minute,
        )

        # Convert to timestamp
        timestamp = scheduled_time.timestamp()

        # Store data needed for the spin
        data = json.dumps(
            {
                "preset_name": preset_name,
            }
        )

        try:
            channel = await self.bot.fetch_channel(ctx.channel.id)
        except Exception as e:
            await ctx.respond("I don't have permission to post in this channel.")
            return "I don't have permission to post in this channel.", None, None

        # Schedule the event
        schedule_event(
            timestamp=timestamp,
            function_name="spin_preset",
            message_id=ctx.interaction.id,
            channel_id=ctx.channel.id,
            data=data,
        )

        # Format the response
        formatted_time = scheduled_time.strftime("%A, %B %d at %I:%M %p")
        # Use ctx.author.nick directly in the respond message below
        try:
            nick = ctx.author.nick if ctx.author.nick else ctx.author.name
        except Exception:
            nick = ctx.author.name

        await ctx.respond(
            f"{nick} scheduled spin of '{preset_name}' for <t:{int(timestamp)}:R>. I'll post the results here when it's time!"
        )
        return f"Scheduled spin of '{preset_name}' for {formatted_time}", None, None

    @schedule.command(name="list")
    async def list_scheduled_spins(self, ctx):
        """List all scheduled spins that haven't been executed yet."""
        await ctx.defer()

        # Get all scheduled events
        scheduled_events = get_all_scheduled_events()

        # Filter for spin events
        spin_events = [
            event for event in scheduled_events if event.function_name == "spin_preset"
        ]

        if not spin_events:
            await ctx.respond("There are no scheduled spins.")
            return "No scheduled spins found.", None, None

        # Build the response
        response = "# Scheduled Spins\n\n"

        for event in spin_events:
            # Use the timestamp directly for Discord timestamp formatting
            data = json.loads(event.data) if event.data else {}
            preset_name = data.get("preset_name", "Unknown preset")

            response += (
                f"**ID:** {event.id}\n"
                f"**Preset:** {preset_name}\n"
                f"**Scheduled Time:** <t:{int(event.timestamp)}:F> (<t:{int(event.timestamp)}:R>)\n\n"
            )

        await ctx.respond(response)
        return "Listed scheduled spins.", None, None

    @schedule.command(name="cancel")
    @discord.default_permissions(administrator=True)
    @discord.option(
        "event_id",
        description="The ID of the scheduled spin to cancel",
        type=int,
        required=True,
    )
    async def cancel_scheduled_spin(self, ctx, event_id: int):
        """Cancel a scheduled spin."""
        await ctx.defer()

        # Get the specific event
        all_events = get_all_scheduled_events()
        event = next(
            (
                e
                for e in all_events
                if e.id == event_id and e.function_name == "spin_preset"
            ),
            None,
        )

        if not event:
            await ctx.respond(f"No scheduled spin found with ID {event_id}.")
            return f"No scheduled spin with ID {event_id}.", None, None

        # Cancel the event
        if cancel_event(event_id):
            # Get the event details for the response
            data = json.loads(event.data) if event.data else {}
            preset_name = data.get("preset_name", "Unknown preset")

            await ctx.respond(
                f"Successfully canceled the scheduled spin of '{preset_name}' that was set for <t:{int(event.timestamp)}:F>."
            )
            return f"Canceled scheduled spin with ID {event_id}.", None, None
        else:
            await ctx.respond(
                f"Failed to cancel the scheduled spin with ID {event_id}."
            )
            return f"Failed to cancel scheduled spin with ID {event_id}.", None, None
