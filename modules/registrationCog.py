# pyright: basic
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from .api import iRacingAPIHandler, shared_iracing_handler
from .embed_utils import status_embed
from .league_config import LeagueConfig
from .registration_roster import RegistrationRoster, match_user, registration_status
from .sheets_client import SheetsClient, SheetsError


# How long the cached registration user list is considered fresh. Stale
# entries are served immediately while a background task refreshes them.
_USERS_CACHE_TTL = 300.0
class RegistrationCog(commands.Cog):
    """
    A cog for managing user registration with Google Sheets integration.
    This cog handles user onboarding and stores information in a Google Sheet.
    """

    def __init__(
        self,
        bot,
        config: LeagueConfig,
        sheets: Optional[SheetsClient] = None,
        iracing_api: Optional[iRacingAPIHandler] = None,
    ):
        self.bot = bot
        self.config = config
        self.driver_role_id = config.driver_role_id
        self.sheets = sheets or SheetsClient.from_env()
        self.roster = RegistrationRoster(
            self.sheets,
            os.getenv("REGISTRATION_SHEET_ID"),
            config.registration_sheet_name,
        )
        # Injected handlers bypass the process-wide singleton entirely.
        self._iracing_api: Optional[iRacingAPIHandler] = iracing_api
        # Cached user list so /register can answer with its modal within
        # Discord's 3-second interaction window instead of waiting on the
        # (blocking) Sheets API. Writes invalidate it; reads refresh it.
        self._users_cache: Optional[List[Dict[str, Any]]] = None
        self._users_cache_ts: float = 0.0
        self._users_refreshing: bool = False

    async def cog_load(self) -> None:
        # Warm the user cache off any interaction deadline so the first
        # /register can pre-fill its modal without blocking on Sheets.
        asyncio.create_task(self._refresh_users_cache())

    class RegistrationModal(discord.ui.Modal):
        def __init__(self, cog, *args, **kwargs) -> None:
            self.cog = cog
            self.discord_user_id = kwargs.pop("discord_user_id", None)
            placeholders = kwargs.pop("placeholders", {})
            super().__init__(*args, **kwargs)
            self.add_item(
                discord.ui.InputText(
                    label="iRacing Customer ID",
                    value=placeholders.get("iRacingID", "123456"),
                )
            )
            self.add_item(
                discord.ui.InputText(
                    label="Desired League Name",
                    value=placeholders.get("DesiredName", "Spinny Boi"),
                )
            )
            self.add_item(
                discord.ui.InputText(
                    label="Desired Car Number",
                    value=placeholders.get("CarNumber", "Spinny Boi"),
                )
            )
            self.add_item(
                discord.ui.InputText(
                    label="Expected Number of Races",
                    value=placeholders.get("NumRaces", "9"),
                )
            )

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            iracing_id = self.children[0].value
            desired_league_name = self.children[1].value
            desired_league_numbers = self.children[2].value
            num_races = self.children[3].value

            try:
                await self.cog.register_driver(
                    interaction.guild.id,
                    self.discord_user_id,
                    iracing_id,
                    desired_league_name,
                    desired_league_numbers,
                    num_races,
                )
                embed = status_embed(
                    "Registration Successful",
                    "You have been successfully registered and assigned the driver role.",
                )
            except ValueError as nat:
                logging.warning(f"Registration failed: {str(nat)}")
                embed = status_embed("Registration Failed", str(nat), ok=False)
                await interaction.followup.send(embed=embed)
                return
            except Exception as ex:
                logging.error(f"Error during registration: {str(ex)}")
                embed = status_embed(
                    "Registration Error",
                    "An error occurred during registration. Please try again later.",
                    ok=False,
                )
            await interaction.followup.send(embed=embed)

    @commands.slash_command(name="register")
    async def register(self, ctx: discord.ApplicationContext):
        """Register as a driver for the league."""
        # await ctx.defer(ephemeral=True)

        try:
            existing_user = await self.find_user(ctx.author.id)
            placeholders = {}
            if existing_user:
                placeholders = {
                    "iRacingID": existing_user.get("iRacingID", ""),
                    "DesiredName": existing_user.get("DesiredName", ""),
                    "CarNumber": existing_user.get("CarNumber", ""),
                    "NumRaces": existing_user.get("NumRaces", ""),
                }

            modal = self.RegistrationModal(
                cog=self,
                title="Driver Registration",
                discord_user_id=ctx.author.id,
                placeholders=placeholders,
            )
            await ctx.send_modal(modal)
        except Exception as ex:
            logging.error(f"Error initiating registration: {str(ex)}")
            await ctx.respond(
                "An error occurred while trying to register. Please try again later.",
                ephemeral=True,
            )

    @discord.default_permissions(administrator=True)
    @commands.user_command(name="register")
    async def register_user_command(
        self, ctx: discord.ApplicationContext, user: discord.User
    ):
        try:
            existing_user = await self.find_user(user.id)
            placeholders = {}
            if existing_user:
                placeholders = {
                    "iRacingID": existing_user.get("iRacingID", ""),
                    "DesiredName": existing_user.get("DesiredName", ""),
                    "CarNumber": existing_user.get("CarNumber", ""),
                    "NumRaces": existing_user.get("NumRaces", ""),
                }

            modal = self.RegistrationModal(
                cog=self,
                title=f"Driver Registration for {user.name}",
                discord_user_id=user.id,
                placeholders=placeholders,
            )
            await ctx.send_modal(modal)
        except Exception as ex:
            logging.error(f"Error initiating registration: {str(ex)}")
            await ctx.respond(
                "An error occurred while trying to register. Please try again later.",
                ephemeral=True,
            )


    async def read_users(self) -> List[Dict[str, Any]]:
        """
        Read all users from the registration spreadsheet.

        The blocking Sheets API calls run in a worker thread so the event
        loop stays free to ack interactions; fresh rows also refresh the
        user-list cache.

        Returns:
            List of user dictionaries with their registration data
        """
        users = await asyncio.to_thread(self.roster.read_users)
        self._users_cache = users
        self._users_cache_ts = time.monotonic()
        return users


    async def find_user(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """
        Find a user by Discord ID using the cached registration list.

        Serves from cache so /register can answer with its modal within
        Discord's 3-second interaction window; stale caches are refreshed
        by a background task. Callers that need fresh rows (e.g. the
        add-vs-update decision in register_driver) go through read_users().

        Args:
            discord_id: The Discord user ID to search for

        Returns:
            User data dictionary if found in the cached list, None otherwise
        """
        users = self._users_cache
        if users is None or time.monotonic() - self._users_cache_ts > _USERS_CACHE_TTL:
            asyncio.create_task(self._refresh_users_cache())
        return match_user(users or [], discord_id)

    async def _refresh_users_cache(self) -> None:
        """Refresh the user-list cache off any interaction deadline."""
        if self._users_refreshing:
            return
        self._users_refreshing = True
        try:
            await self.read_users()
        finally:
            self._users_refreshing = False

    async def add_user(self, user_data: Dict[str, Any]) -> bool:
        """Append a row to the registration sheet; invalidate the user cache.

        Returns False (logged) when the sheet write fails."""
        try:
            await asyncio.to_thread(self.roster.add, user_data)
        except SheetsError as err:
            logging.error(f"Error adding user to spreadsheet: {err}")
            return False
        self._users_cache = None  # force reload before the next pre-fill
        logging.info(
            f"Added user {user_data.get('DiscordID')} to registration spreadsheet"
        )
        return True

    async def update_user(self, discord_id: int, user_data: Dict[str, Any]) -> bool:
        """Update an existing registration row; invalidate the user cache.

        Returns False (logged) when the sheet write fails."""
        try:
            await asyncio.to_thread(self.roster.update, discord_id, user_data)
        except SheetsError as err:
            logging.error(f"Error updating user in spreadsheet: {err}")
            return False
        self._users_cache = None
        logging.info(f"Updated user {discord_id} in registration spreadsheet")
        return True

    def _get_iracing_api(self) -> Optional[iRacingAPIHandler]:
        """The process-wide iRacing handler (see api.shared_iracing_handler)."""
        if self._iracing_api is None:
            self._iracing_api = shared_iracing_handler()
        return self._iracing_api

    def _driver_name_sync(self, cust_id: int) -> Optional[str]:
        """Sync driver-name lookup for the roster, run on the worker thread."""
        api = self._get_iracing_api()
        if api is None:
            logging.warning("iRacing API unavailable; skipping driver name lookup.")
            return None
        try:
            return api.get_driver_name(cust_id)
        except Exception as ex:
            logging.error(f"Error looking up iRacing name for cust_id {cust_id}: {ex}")
            return None

    async def register_driver(
        self,
        guild_id: int,
        discord_id: int,
        iracing_id: int,
        desired_league_name: str,
        desired_league_number: str,
        num_races: str,
    ):
        """
        Register a new driver through the roster, then assign the Discord
        driver role and league nickname.

        Raises ValueError with a user-facing message when a registration
        rule fails; SheetsError propagates on sheet-infrastructure failure.
        """
        await asyncio.to_thread(
            self.roster.register_driver,
            discord_id,
            iracing_id,
            desired_league_name,
            desired_league_number,
            num_races,
            self._driver_name_sync,
        )

        member = None
        if self.driver_role_id:
            guild = self.bot.get_guild(guild_id)
            if guild:
                member = guild.get_member(discord_id)
                if member:
                    role = guild.get_role(self.driver_role_id)
                    if role:
                        await member.add_roles(role, reason="Driver Registration")
                        logging.info(
                            f"Assigned driver role to user {discord_id} in guild {guild_id}"
                        )
                    else:
                        logging.error(
                            f"Driver role ID {self.driver_role_id} not found in guild {guild_id}"
                        )
                else:
                    logging.error(
                        f"Member with ID {discord_id} not found in guild {guild_id}"
                    )
            else:
                logging.error(f"Guild with ID {guild_id} not found")

        if member:
            discord_name = f"{desired_league_name} | {desired_league_number}"
            try:
                await member.edit(nick=discord_name, reason="Registration Update")
                logging.info(
                    f"Updated nickname for user {discord_id} to {discord_name}"
                )
            except Exception as ex:
                logging.error(
                    f"Error updating nickname for user {discord_id}: {str(ex)}"
                )

    @commands.slash_command(name="check_registrations")
    @discord.default_permissions(administrator=True)
    async def check_registrations(self, ctx: discord.ApplicationContext):
        """
        Check all registrations and log their status.
        This can be expanded to include additional validation logic.
        """
        await ctx.defer()
        registered_drivers = await self.read_users()
        handler = self._get_iracing_api()
        if handler is None:
            await ctx.respond(
                "iRacing API is not configured. Check the IRACING_* credentials.",
                ephemeral=True,
            )
            return
        league_members = handler.get_league_members(self.config.iracing_league_id)
        pending_invites = handler.get_league_members(
            self.config.iracing_league_id, pending=True
        )
        status = registration_status(registered_drivers, league_members, pending_invites)

        embed = discord.Embed(
            title="Registration Check Results",
            color=discord.Color.blue(),
        )
        if status.name_changes:
            embed.add_field(
                name="Name Changes Required",
                value="\n".join(
                    [
                        f"Rename '{current}' => '{name}' in iRacing"
                        for current, name in status.name_changes
                    ]
                ),
                inline=False,
            )
        if status.number_changes:
            embed.add_field(
                name="Number Changes Required",
                value="\n".join(
                    [
                        f"{name} Requests #{requested} (currently {current})"
                        for name, requested, current in status.number_changes
                    ]
                ),
                inline=False,
            )
        if status.invites:
            embed.add_field(
                name="Invites Required",
                value="\n".join(
                    [
                        f"{name} needs to be invited to the league"
                        for name in status.invites
                    ]
                ),
                inline=False,
            )
        if status.accept_invites:
            embed.add_field(
                name="Accept Invites Required",
                value="\n".join(
                    [
                        f"{name} needs to accept their league invite"
                        for name in status.accept_invites
                    ]
                ),
                inline=False,
            )
        if status.penalty_prefix_changes:
            embed.add_field(
                name="Penalty Prefix Updates Required",
                value="\n".join(
                    [
                        f"'{name}': rename '{current}' => '{expected}' in iRacing"
                        for name, current, expected in status.penalty_prefix_changes
                    ]
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Penalty Prefixes",
                value="✅ All penalty prefixes are correct.",
                inline=False,
            )
        if not embed.fields:
            embed.description = "All registrations are valid."

        await ctx.respond(embed=embed)
