# pyright: basic
import asyncio
import io
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .league_config import STAFF_ROLE_ID, LeagueConfig
from .simracerhub import SimRacerHubClient, SimRacerHubConfig

from dataclasses import replace


# Table option values are "series:<id>:driver|team" — one pair per series the
# league runs, discovered from the league id at command time. The league id
# is the only SRH id ever configured; nothing here bakes a series in.
SERIES_VALUE_PREFIX = "series"
LEAGUE_STATS_VALUE = "league_stats"
MAX_SERIES_TABLES = 12  # 2 options/series stays under Discord's 25-option limit


def series_display_names(catalog: Dict[str, Any]) -> Dict[int, str]:
    """``series_id -> display name`` from a season-discovery catalog."""
    names: Dict[int, str] = {}
    for row in catalog.get("series") or []:
        sid = row.get("series_id")
        if sid is not None:
            names[int(sid)] = str(row.get("series_name") or f"Series {sid}")
    return names


def primary_series_id(catalog: Dict[str, Any]) -> Optional[int]:
    """The series owning the league's active season (first series otherwise)."""
    active = catalog.get("active_season_id")
    rows = catalog.get("series") or []
    for row in rows:
        if active is not None and row.get("current_season_id") == active:
            return int(row["series_id"])
    return int(rows[0]["series_id"]) if rows else None


def build_table_options(catalog: Dict[str, Any]) -> List[discord.SelectOption]:
    """Driver + team option per discovered series, plus the league-wide stats
    option; the primary series' pair comes pre-selected."""
    options: List[discord.SelectOption] = []
    primary = primary_series_id(catalog)
    for sid, name in list(series_display_names(catalog).items())[:MAX_SERIES_TABLES]:
        default = sid == primary
        options.append(
            discord.SelectOption(
                label=f"{name} Standings",
                value=f"{SERIES_VALUE_PREFIX}:{sid}:driver",
                description="Individual driver championship standings",
                default=default,
            )
        )
        options.append(
            discord.SelectOption(
                label=f"{name} Team Standings",
                value=f"{SERIES_VALUE_PREFIX}:{sid}:team",
                description="Team championship standings",
                default=default,
            )
        )
    options.append(
        discord.SelectOption(
            label="League Stats",
            value=LEAGUE_STATS_VALUE,
            description="League statistics and performance data",
            default=False,
        )
    )
    return options


def default_table_values(catalog: Dict[str, Any]) -> List[str]:
    """Pre-selected option values: the primary series' driver + team pair."""
    primary = primary_series_id(catalog)
    if primary is None:
        return []
    return [
        f"{SERIES_VALUE_PREFIX}:{primary}:driver",
        f"{SERIES_VALUE_PREFIX}:{primary}:team",
    ]


def table_specs(
    values: List[str], catalog: Dict[str, Any], league_id: int
) -> List[Tuple[str, str, bool, str, str]]:
    """Selected option values -> (name, selector, use_class, url, tab_to_click);
    values for unknown series are dropped."""
    names = series_display_names(catalog)
    specs: List[Tuple[str, str, bool, str, str]] = []
    for value in values:
        if value == LEAGUE_STATS_VALUE:
            specs.append(
                (
                    "League Stats",
                    "react-table",
                    True,
                    f"https://www.simracerhub.com/scoring/league_stats.php?league_id={league_id}",
                    "",
                )
            )
            continue
        parts = str(value).split(":")
        if (
            len(parts) != 3
            or parts[0] != SERIES_VALUE_PREFIX
            or parts[2] not in ("driver", "team")
        ):
            continue
        try:
            sid = int(parts[1])
        except ValueError:
            continue
        if sid not in names:
            continue
        url = f"https://www.simracerhub.com/scoring/season_standings.php?series_id={sid}"
        if parts[2] == "driver":
            specs.append(
                (f"{names[sid]} Standings", "standings-class-section", True, url, "")
            )
        else:
            specs.append(
                (
                    f"{names[sid]} Team Standings",
                    "standings-class-section",
                    True,
                    url,
                    "Team Standings",
                )
            )
    return specs


class StandingsView(discord.ui.View):
    def __init__(
        self,
        cog,
        ctx,
        description="",
        catalog: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.description = description
        self.catalog = catalog or {"series": []}
        self.selected_tables: List[str] = []
        self.selected_channel = None
        self.table_select = discord.ui.Select(
            placeholder="Select standings tables to fetch...",
            min_values=1,
            max_values=4,
            options=build_table_options(self.catalog),
            row=0,
        )
        # Dynamically assigned item callbacks receive ONLY the interaction
        # (py-cord view.py calls `item.callback(interaction)`); decorated ones
        # get (item, interaction) via the decorator's partial wrapper.
        self.table_select.callback = self._table_select_callback
        self.add_item(self.table_select)
        # Decorated items register during super().__init__, so the dynamic
        # select lands after them; keep children in their visual row order.
        self.children.sort(key=lambda item: item.row if item.row is not None else 0)

    async def _table_select_callback(self, interaction: discord.Interaction):
        self.selected_tables = list(self.table_select.values or [])
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
            # If nothing selected yet, default to the primary series' pair
            self.selected_tables = default_table_values(self.catalog)

        selected_tables = table_specs(
            self.selected_tables, self.catalog, self.cog.config.srh_league_id
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
        for table_name, table_selector, use_class, url, tab_to_click in selected_tables:
            success = await self.cog._capture_and_send_table(
                url=url,
                table_selector=table_selector,
                channel_id=target_channel_id,
                description=table_name,
                additional_description=self.description,
                use_class=use_class,
                tab_to_click=tab_to_click,
            )
            results.append((table_name, success))

        # Build embed response
        embed = discord.Embed(
            title="Standings Update Results",
            color=(
                discord.Color.green()
                if all(r[1] for r in results)
                else discord.Color.orange()
            ),
        )

        for name, success in results:
            status = "✅ Success" if success else "❌ Failed"
            embed.add_field(name=name, value=status, inline=False)

        embed.add_field(
            name="Posted to Channel", value=f"<#{target_channel_id}>", inline=False
        )

        if self.description:
            embed.add_field(name="Description", value=self.description, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


class StandingsCog(commands.Cog):
    def __init__(self, bot, config: LeagueConfig, srh: Optional[SimRacerHubClient] = None):
        self.bot = bot
        self.config = config
        self._srh = srh

    def _season_index(self) -> SimRacerHubClient:
        """The SRH client that discovers the league's series (built lazily so
        cog wiring stays offline; tests inject their own)."""
        if self._srh is None:
            self._srh = SimRacerHubClient(
                config=replace(
                    SimRacerHubConfig.from_env(), league_id=self.config.srh_league_id
                )
            )
        return self._srh

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
        tab_to_click: str = "",
    ):
        """
        Navigate to a URL, capture a screenshot of a table element, and send it to a Discord channel.

        The Selenium capture runs in a worker thread so the event loop stays
        free to ack other interactions during the multi-second capture.

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

        try:
            img_byte_arr = await asyncio.to_thread(
                self._capture_table_png,
                url,
                table_selector,
                use_class,
                tab_to_click,
                description,
            )
        except Exception as ex:
            logging.error(f"Error capturing/sending {description}: {str(ex)}")
            return False

        try:
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

    def _capture_table_png(
        self,
        url: str,
        table_selector: str,
        use_class: bool,
        tab_to_click: str,
        description: str,
    ) -> io.BytesIO:
        """Blocking: drive Firefox, wait for the table element, screenshot it."""
        driver = None
        try:
            # Setup driver
            driver = self._setup_driver()

            # Navigate to the URL
            logging.info(f"Navigating to {url} for {description}")
            driver.get(url)

            # Wait for the initial page content to be present
            wait = WebDriverWait(driver, 15)
            if use_class:
                wait.until(
                    EC.presence_of_element_located((By.CLASS_NAME, table_selector))
                )
            else:
                wait.until(EC.presence_of_element_located((By.ID, table_selector)))

            # Give it a moment to fully render
            time.sleep(2)

            # If a specific tab needs to be clicked, do so now
            if tab_to_click:
                logging.info(f"Clicking tab '{tab_to_click}' for {description}")
                tab_element = wait.until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            "//button[contains(@class,'nav-link') and normalize-space(text())='"
                            + tab_to_click
                            + "']",
                        )
                    )
                )
                driver.execute_script("arguments[0].click();", tab_element)
                time.sleep(2)

            # Re-locate the element after any tab click
            if use_class:
                table_element = wait.until(
                    EC.presence_of_element_located((By.CLASS_NAME, table_selector))
                )
            else:
                table_element = wait.until(
                    EC.presence_of_element_located((By.ID, table_selector))
                )

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
            return img_byte_arr
        finally:
            if driver:
                driver.quit()

    @commands.slash_command(name="standings")
    @commands.check_any(
        commands.has_permissions(administrator=True),
        commands.has_role(STAFF_ROLE_ID),
    )
    @discord.option(
        "description",
        str,
        required=False,
        default="",
        description="Optional description to append to each standings post",
    )
    async def standings(self, ctx, description: str = ""):
        """Fetch and post standings tables to configured channels."""
        catalog = await asyncio.to_thread(self._season_index().seasons)
        if catalog.get("status") != "ok" or not catalog.get("series"):
            logging.warning(
                f"standings series discovery failed: {catalog.get('status')}"
            )
            await ctx.respond(
                "Couldn't reach SimRacerHub to list the league's series; "
                "try again shortly.",
                ephemeral=True,
            )
            return
        view = StandingsView(self, ctx, description, catalog)
        await ctx.respond(
            "**Configure your standings fetch:**", view=view, ephemeral=True
        )

    @standings.error
    async def info_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.respond(
                "You do not have permission to use this command.", ephemeral=True
            )
