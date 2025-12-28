# pyright: basic
import io
import logging
import os

import discord
from discord.ext import commands
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class StandingsView(discord.ui.View):
    def __init__(self, cog, ctx, description=""):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.description = description
        self.selected_tables = []
        self.selected_channel = None

    @discord.ui.select(
        placeholder="Select standings tables to fetch...",
        min_values=1,
        max_values=7,
        options=[
            discord.SelectOption(
                label="Driver Standings",
                value="driver_standings",
                description="Individual driver championship standings",
                default=True,
            ),
            discord.SelectOption(
                label="Race-by-Race Driver Standings",
                value="rbr_driver_standings",
                description="Race-by-Race Driver Standings",
                default=False,
            ),
            discord.SelectOption(
                label="Team Standings",
                value="team_standings",
                description="Team championship standings",
                default=True,
            ),
            discord.SelectOption(
                label="Race-by-Race Team Standings",
                value="rbr_team_standings",
                description="Race-by-Race Team Standings",
                default=False,
            ),
            discord.SelectOption(
                label="League Stats",
                value="league_stats",
                description="League statistics and performance data",
                default=False,
            ),
            discord.SelectOption(
                label="AM Standings",
                value="am_standings",
                description="AM championship standings",
                default=False,
            ),
            discord.SelectOption(
                label="Race-by-Race AM Standings",
                value="rbr_am_standings",
                description="Race-by-Race AM Standings",
                default=False,
            ),
        ],
        row=0,
    )
    async def table_select_callback(self, select, interaction: discord.Interaction):
        self.selected_tables = select.values
        await interaction.response.defer()

    @discord.ui.select(
        select_type=discord.ComponentType.channel_select,
        channel_types=[
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
            discord.ChannelType.forum,
            discord.ChannelType.news_thread,
            discord.ChannelType.news,
            discord.ChannelType.private_thread,
        ],
        placeholder="Select channel (leave blank for this channel)",
        min_values=0,
        max_values=1,
        row=1,
    )
    async def channel_select_callback(self, select, interaction: discord.Interaction):
        if select.values:
            self.selected_channel = select.values[0].id
        else:
            self.selected_channel = None
        await interaction.response.defer()

    @discord.ui.button(
        label="Fetch Standings", style=discord.ButtonStyle.primary, row=2
    )
    async def submit_callback(self, button, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Get selected tables
        if not self.selected_tables:
            # If nothing selected yet, default to driver and team
            self.selected_tables = ["driver_standings", "team_standings"]

        selected_tables = []
        for table_value in self.selected_tables:
            if table_value == "driver_standings":
                selected_tables.append(
                    (
                        "Driver Standings",
                        "driver_table",
                        False,
                        "https://www.simracerhub.com/scoring/season_standings.php?series_id=8812",
                    )
                )
            elif table_value == "team_standings":
                selected_tables.append(
                    (
                        "Team Standings",
                        "team_table",
                        False,
                        "https://www.simracerhub.com/scoring/season_standings.php?series_id=8812",
                    )
                )
            elif table_value == "league_stats":
                selected_tables.append(
                    (
                        "League Stats",
                        "react-table",
                        True,
                        "https://www.simracerhub.com/scoring/league_stats.php?season_id=27512",
                    )
                )
            elif table_value == "rbr_driver_standings":
                selected_tables.append(
                    (
                        "Race-by-Race Driver Standings",
                        "driver_grid",
                        False,
                        "https://www.simracerhub.com/scoring/season_standings.php?series_id=8812&grid=y",
                    )
                )
            elif table_value == "rbr_team_standings":
                selected_tables.append(
                    (
                        "Race-by-Race Team Standings",
                        "team_grid",
                        False,
                        "https://www.simracerhub.com/scoring/season_standings.php?series_id=8812&grid=y",
                    )
                )
            elif table_value == "am_standings":
                selected_tables.append(
                    (
                        "AM Standings",
                        "driver_table",
                        False,
                        "https://www.simracerhub.com/scoring/season_standings.php?series_id=13428",
                    )
                )
            elif table_value == "rbr_am_standings":
                selected_tables.append(
                    (
                        "Race-by-Race AM Standings",
                        "driver_grid",
                        False,
                        "https://www.simracerhub.com/scoring/season_standings.php?series_id=13428&grid=y",
                    )
                )

        if not selected_tables:
            await interaction.followup.send(
                "No tables selected. Please select at least one table.", ephemeral=True
            )
            return

        # Determine which channel to use
        if self.selected_channel:
            target_channel_id = int(self.selected_channel)
        else:
            # Use the channel where the command was invoked
            target_channel_id = self.ctx.channel.id

        # Send initial response
        await interaction.followup.send(
            "Fetching standings... This may take a moment.", ephemeral=True
        )

        # Process each selected table
        results = []
        for table_name, table_selector, use_class, url in selected_tables:
            success = await self.cog._capture_and_send_table(
                url=url,
                table_selector=table_selector,
                channel_id=target_channel_id,
                description=table_name,
                additional_description=self.description,
                use_class=use_class,
            )
            results.append((table_name, success))

        # Build embed response
        embed = discord.Embed(
            title="Standings Update Results",
            color=discord.Color.green()
            if all(r[1] for r in results)
            else discord.Color.orange(),
        )

        for name, success in results:
            status = "✅ Success" if success else "❌ Failed"
            embed.add_field(name=name, value=status, inline=False)

        embed.add_field(
            name="Posted to Channel", value=f"<#{target_channel_id}>", inline=False
        )

        if self.description:
            embed.add_field(name="Description", value=self.description, inline=False)

        await interaction.followup.send(embed=embed)


class StandingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _setup_driver(self):
        """Setup and return a configured Firefox WebDriver."""
        firefox_options = Options()
        firefox_options.add_argument("--headless")
        firefox_options.add_argument("--width=1920")
        firefox_options.add_argument("--height=4000")  # Larger viewport for tall tables

        driver = webdriver.Firefox(options=firefox_options)
        return driver

    async def _capture_and_send_table(
        self,
        url: str,
        table_selector: str,
        channel_id: int,
        description: str,
        additional_description: str = "",
        use_class: bool = False,
    ):
        """
        Navigate to a URL, capture a screenshot of a table element, and send it to a Discord channel.

        Args:
            url: The URL to navigate to
            table_selector: The ID or class of the table element to screenshot
            channel_id: The Discord channel ID to send the image to
            description: A description for logging and the Discord message
            additional_description: Optional additional description to append to the message
            use_class: If True, use class selector instead of ID
        """
        if channel_id == 0:
            logging.warning(f"Channel ID not configured for {description}")
            return False

        driver = None
        try:
            # Setup driver
            driver = self._setup_driver()

            # Navigate to the URL
            logging.info(f"Navigating to {url} for {description}")
            driver.get(url)

            # Wait for the table to be present
            wait = WebDriverWait(driver, 10)
            if use_class:
                table_element = wait.until(
                    EC.presence_of_element_located((By.CLASS_NAME, table_selector))
                )
            else:
                table_element = wait.until(
                    EC.presence_of_element_located((By.ID, table_selector))
                )

            # Give it a moment to fully render
            import time

            time.sleep(2)

            # Scroll the element into view
            driver.execute_script("arguments[0].scrollIntoView(true);", table_element)
            time.sleep(1)

            # Take a screenshot directly of the element
            element_screenshot = table_element.screenshot_as_png

            # Open the screenshot with PIL
            cropped_image = Image.open(io.BytesIO(element_screenshot))

            # Save to a BytesIO object
            img_byte_arr = io.BytesIO()
            cropped_image.save(img_byte_arr, format="PNG")
            img_byte_arr.seek(0)

            # Get the Discord channel
            channel = await self.bot.fetch_channel(channel_id)
            if not channel:
                logging.error(f"Channel {channel_id} not found for {description}")
                return False

            # Send the image to Discord
            file = discord.File(
                img_byte_arr, filename=f"{description.replace(' ', '_')}.png"
            )
            message_text = f"**{description}**"
            if additional_description:
                message_text += f"\n{additional_description}"
            await channel.send(message_text, file=file)

            logging.info(f"Successfully sent {description} to channel {channel_id}")
            return True

        except Exception as ex:
            logging.error(f"Error capturing/sending {description}: {str(ex)}")
            return False
        finally:
            if driver:
                driver.quit()

    @commands.slash_command(name="standings")
    @discord.default_permissions(administrator=True)
    @discord.option(
        "description",
        str,
        required=False,
        default="",
        description="Optional description to append to each standings post",
    )
    async def standings(self, ctx, description: str = ""):
        """Fetch and post standings tables to configured channels."""
        view = StandingsView(self, ctx, description)
        await ctx.respond(
            "**Configure your standings fetch:**", view=view, ephemeral=True
        )
