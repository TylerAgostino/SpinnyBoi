# pyright: basic
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from .api import iRacingAPIHandler

# Google API imports - install these with pip
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_APIS_AVAILABLE = True
except ImportError:
    logging.error(
        "Google API libraries not installed. Registration features will be limited."
    )
    GOOGLE_APIS_AVAILABLE = False


class RegistrationCog(commands.Cog):
    """
    A cog for managing user registration with Google Sheets integration.
    This cog handles user onboarding and stores information in a Google Sheet.
    """

    def __init__(self, bot, sheet_name: str = "Registration", role_id: int = None):
        self.bot = bot
        self.credentials = None
        self.spreadsheet_id = os.getenv("REGISTRATION_SHEET_ID")
        self.users_sheet_name = sheet_name
        self.driver_role_id = role_id
        # Init Google Sheets API
        self._init_google_sheets()

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
                embed = discord.Embed(
                    title="Registration Successful",
                    description="You have been successfully registered and assigned the driver role.",
                    color=discord.Color.green(),
                )
            except ValueError as nat:
                logging.warning(f"Registration failed: {str(nat)}")
                embed = discord.Embed(
                    title="Registration Failed",
                    description=str(nat),
                    color=discord.Color.red(),
                )
                await interaction.followup.send(embed=embed)
                return
            except Exception as ex:
                logging.error(f"Error during registration: {str(ex)}")
                embed = discord.Embed(
                    title="Registration Error",
                    description="An error occurred during registration. Please try again later.",
                    color=discord.Color.red(),
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

    def _init_google_sheets(self) -> None:
        """
        Initialize Google Sheets API credentials.
        Loads credentials from service account file or environment variable.
        """
        if not GOOGLE_APIS_AVAILABLE:
            logging.warning(
                "Google APIs not available. Please install required packages."
            )
            return

        try:
            # Try to load credentials from a file first
            creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
            if os.path.exists(creds_file):
                self.credentials = Credentials.from_service_account_file(
                    creds_file, scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
                logging.info("Google Sheets credentials loaded from file")
            # If no file, try to load from environment variable
            elif os.getenv("GOOGLE_CREDENTIALS_JSON"):
                creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
                if creds_json_str:
                    creds_json = json.loads(creds_json_str)
                    self.credentials = Credentials.from_service_account_info(
                        creds_json,
                        scopes=["https://www.googleapis.com/auth/spreadsheets"],
                    )
                    logging.info("Google Sheets credentials loaded from environment")
            else:
                logging.error("No Google Sheets credentials found")
        except Exception as ex:
            logging.error(f"Error initializing Google Sheets API: {str(ex)}")

    def _get_service(self):
        """
        Get Google Sheets API service instance.

        Returns:
            Google Sheets API service or None if credentials are not available
        """
        if not self.credentials:
            logging.error("No Google credentials available")
            return None

        try:
            return build("sheets", "v4", credentials=self.credentials)
        except Exception as ex:
            logging.error(f"Error building Google Sheets service: {str(ex)}")
            return None

    async def read_users(self) -> List[Dict[str, Any]]:
        """
        Read all users from the registration spreadsheet.

        Returns:
            List of user dictionaries with their registration data
        """
        try:
            service = self._get_service()
            if not service or not self.spreadsheet_id:
                return []

            # Get the first row to determine column headers
            range_name = f"{self.users_sheet_name}!A1:Z1"
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=range_name)
                .execute()
            )

            if "values" not in result or not result["values"]:
                logging.warning("No header row found in spreadsheet")
                return []

            headers = result["values"][0]

            # Get all data rows
            range_name = f"{self.users_sheet_name}!A2:Z"
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=range_name)
                .execute()
            )

            if "values" not in result:
                return []

            # Convert rows to dictionaries using headers
            users = []
            for row in result["values"]:
                user_data = {}
                # Pad row if needed to match header length
                padded_row = row + [""] * (len(headers) - len(row))
                for i, header in enumerate(headers):
                    if i < len(padded_row):
                        user_data[header] = padded_row[i]
                    else:
                        user_data[header] = ""
                users.append(user_data)

            return users

        except HttpError as error:
            logging.error(f"Google Sheets API error: {error}")
            return []
        except Exception as ex:
            logging.error(f"Error reading users from spreadsheet: {str(ex)}")
            return []

    async def find_user(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """
        Find a user by Discord ID in the registration spreadsheet.

        Args:
            discord_id: The Discord user ID to search for

        Returns:
            User data dictionary if found, None otherwise
        """
        try:
            users = await self.read_users()
            for user in users:
                # Convert the stored Discord ID to int for comparison
                stored_id = user.get("DiscordID", "")
                if stored_id and str(stored_id) == str(discord_id):
                    return user
            return None
        except Exception as ex:
            logging.error(f"Error finding user: {str(ex)}")
            return None

    async def add_user(self, user_data: Dict[str, Any]) -> bool:
        """
        Add a new user to the registration spreadsheet.

        Args:
            user_data: Dictionary containing user data to add

        Returns:
            True if successful, False otherwise
        """
        try:
            service = self._get_service()
            if not service or not self.spreadsheet_id:
                return False

            # Get headers first to maintain column order
            range_name = f"{self.users_sheet_name}!A1:Z1"
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=range_name)
                .execute()
            )

            if "values" not in result or not result["values"]:
                logging.error("No headers found in spreadsheet")
                return False

            headers = result["values"][0]

            # Create row data in the same order as headers
            row_data = []
            for header in headers:
                row_data.append(user_data.get(header, ""))

            # Append the new row
            range_name = f"{self.users_sheet_name}!A:Z"
            body = {"values": [row_data]}

            service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            ).execute()

            logging.info(
                f"Added user {user_data.get('DiscordID')} to registration spreadsheet"
            )
            return True

        except HttpError as error:
            logging.error(f"Google Sheets API error: {error}")
            return False
        except Exception as ex:
            logging.error(f"Error adding user to spreadsheet: {str(ex)}")
            return False

    async def update_user(self, discord_id: int, user_data: Dict[str, Any]) -> bool:
        """
        Update an existing user in the registration spreadsheet.

        Args:
            discord_id: Discord ID of the user to update
            user_data: Dictionary containing updated user data

        Returns:
            True if successful, False otherwise
        """
        try:
            service = self._get_service()
            if not service or not self.spreadsheet_id:
                return False

            # Find the user's row
            range_name = f"{self.users_sheet_name}!A:Z"
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=range_name)
                .execute()
            )

            if "values" not in result:
                logging.error("No data found in spreadsheet")
                return False

            values = result["values"]
            headers = values[0]

            # Find Discord ID column index
            discord_id_index = -1
            for i, header in enumerate(headers):
                if header == "DiscordID":
                    discord_id_index = i
                    break

            if discord_id_index == -1:
                logging.error("DiscordID column not found in spreadsheet")
                return False

            # Find the user's row
            row_index = -1
            for i, row in enumerate(values):
                if i > 0 and len(row) > discord_id_index:
                    try:
                        if int(row[discord_id_index]) == discord_id:
                            row_index = i
                            break
                    except (ValueError, TypeError):
                        continue

            if row_index == -1:
                logging.error(f"User with Discord ID {discord_id} not found")
                return False

            # Update user data
            update_row = []
            for header in headers:
                update_row.append(
                    user_data.get(
                        header,
                        (
                            values[row_index][headers.index(header)]
                            if header in headers
                            and headers.index(header) < len(values[row_index])
                            else ""
                        ),
                    )
                )

            # Update the row
            update_range = f"{self.users_sheet_name}!A{row_index + 1}"
            body = {"values": [update_row]}

            service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=update_range,
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()

            logging.info(f"Updated user {discord_id} in registration spreadsheet")
            return True

        except HttpError as error:
            logging.error(f"Google Sheets API error: {error}")
            return False
        except Exception as ex:
            logging.error(f"Error updating user in spreadsheet: {str(ex)}")
            return False

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
        Register a new driver and assign appropriate Discord role.

        Args:
            discord_id: Discord user ID
            iracing_id: iRacing ID
            iracing_name: iRacing display name
            desired_league_name: League name preference
            num_races: Number of races completed
        """
        try:
            members = await self.read_users()
            existing_user = await self.find_user(discord_id)
            other_members = [
                m for m in members if str(m.get("DiscordID")) != str(f"{discord_id}")
            ]
            user_data = {
                "DiscordID": f"'{discord_id}",
                "iRacingID": f"'{iracing_id}",
                "DesiredName": desired_league_name,
                "NumRaces": num_races,
            }

            if not re.fullmatch(r"\d{1,8}", str(iracing_id)):
                raise ValueError("iRacing ID not valid.")
            if not re.fullmatch(r"\d{1,3}", desired_league_number):
                raise ValueError("Car Number not valid.")

            if desired_league_number in [m.get("CarNumber", "") for m in other_members]:
                if existing_user:
                    user_data["DesiredButTakenCarNums"] = (
                        f"'{desired_league_number}, {existing_user.get('DesiredButTakenCarNums', '')}".strip(
                            ", "
                        )
                    )
                    await self.update_user(discord_id, user_data)
                else:
                    user_data["DesiredButTakenCarNums"] = f"'{desired_league_number}"
                    await self.add_user(user_data)
                raise ValueError(
                    f"Desired league number {desired_league_number} is already taken."
                )
            user_data["CarNumber"] = f"'{desired_league_number}"
            user_data["RegistrationDate"] = discord.utils.utcnow().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            if existing_user:
                await self.update_user(discord_id, user_data)
            else:
                await self.add_user(user_data)

            # Assign Discord role
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

            # Set Discord Nickname
            discord_name = f"{desired_league_name} | {desired_league_number}"
            if member:
                try:
                    await member.edit(nick=discord_name, reason="Registration Update")
                    logging.info(
                        f"Updated nickname for user {discord_id} to {discord_name}"
                    )
                except Exception as ex:
                    logging.error(
                        f"Error updating nickname for user {discord_id}: {str(ex)}"
                    )

        except ValueError:
            raise

        except Exception as ex:
            logging.error(f"Error in register_driver: {str(ex)}")

    @commands.slash_command(name="check_registrations")
    @discord.default_permissions(administrator=True)
    async def check_registrations(self, ctx: discord.ApplicationContext):
        """
        Check all registrations and log their status.
        This can be expanded to include additional validation logic.
        """
        await ctx.defer()
        required_name_changes = []
        required_number_changes = []
        required_invites = []
        required_accept_invites = []

        registered_drivers = await self.read_users()
        handler = iRacingAPIHandler(
            os.getenv("IRACING_EMAIL"),
            os.getenv("IRACING_PASSWORD"),
            os.getenv("IRACING_CLIENT_ID"),
            os.getenv("IRACING_CLIENT_SECRET"),
            use_oauth=True,
        )
        league_members = handler.get_league_members(8579)
        pending_invites = handler.get_league_members(8579, pending=True)

        for driver in registered_drivers:
            if not driver.get("RegistrationDate"):
                continue  # skip unregistered drivers
            iracing_id = driver.get("iRacingID", "").lstrip("'")
            desired_name = driver.get("DesiredName", "")
            desired_number = driver.get("CarNumber", "")
            member = next(
                (m for m in league_members if str(m.get("cust_id")) == iracing_id), None
            )
            pending_member = next(
                (m for m in pending_invites if str(m.get("cust_id")) == iracing_id),
                None,
            )

            if iracing_id:
                if not member and not pending_member:
                    required_invites.append(desired_name)
                elif pending_member:
                    required_accept_invites.append(desired_name)
                else:
                    current_name = member.get("nick_name") or member.get(
                        "display_name", ""
                    )
                    current_number = member.get("car_number", "")

                    if current_name != desired_name:
                        required_name_changes.append((current_name, desired_name))

                    if current_number != desired_number:
                        required_number_changes.append(
                            (current_name, desired_number, current_number)
                        )

        embed = discord.Embed(
            title="Registration Check Results",
            color=discord.Color.blue(),
        )
        if required_name_changes:
            embed.add_field(
                name="Name Changes Required",
                value="\n".join(
                    [
                        f"Rename '{iracing_id}' => '{name}' in iRacing"
                        for iracing_id, name in required_name_changes
                    ]
                ),
                inline=False,
            )
        if required_number_changes:
            embed.add_field(
                name="Number Changes Required",
                value="\n".join(
                    [
                        f"{iracing_id} Requests #{numbers} (currently {current})"
                        for iracing_id, numbers, current in required_number_changes
                    ]
                ),
                inline=False,
            )
        if required_invites:
            embed.add_field(
                name="Invites Required",
                value="\n".join(
                    [
                        f"{iracing_id} needs to be invited to the league"
                        for iracing_id in required_invites
                    ]
                ),
                inline=False,
            )
        if required_accept_invites:
            embed.add_field(
                name="Accept Invites Required",
                value="\n".join(
                    [
                        f"{iracing_id} needs to accept their league invite"
                        for iracing_id in required_accept_invites
                    ]
                ),
                inline=False,
            )
        if not embed.fields:
            embed.description = "All registrations are valid."

        await ctx.respond(embed=embed)
