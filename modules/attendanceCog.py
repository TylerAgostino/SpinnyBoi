# pyright: basic
import logging
import os
from typing import Any, Dict, List, Optional, Set

import discord
from discord.ext import commands

from .api import iRacingAPIHandler

try:
    import json as _json

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_APIS_AVAILABLE = True
except ImportError:
    logging.error(
        "Google API libraries not installed. Attendance features will be limited."
    )
    GOOGLE_APIS_AVAILABLE = False


def _col_index_to_letter(n: int) -> str:
    """Convert a 0-based column index to an Excel-style column letter (A, B, ..., Z, AA, ...)."""
    result = ""
    while n >= 0:
        result = chr(n % 26 + ord("A")) + result
        n = n // 26 - 1
    return result


class AttendanceCog(commands.Cog):
    """
    A cog that marks attendance in a Google Sheet based on iRacing subsession results.

    For each driver who completed at least a configurable percentage of the race
    (default 75%), it finds their row in the sheet by iRacing customer ID and
    writes a 0 in the specified column.
    """

    def __init__(
        self,
        bot,
        default_sheet_name: str = "Points Entry S15-17",
        spreadsheet_id: Optional[str] = None,
    ):
        self.bot = bot
        self.credentials = None
        self.default_sheet_name = default_sheet_name
        self.spreadsheet_id = spreadsheet_id or os.getenv("ATTENDANCE_SHEET_ID")
        self._iracing_api: Optional[iRacingAPIHandler] = None
        self._init_google_sheets()

    # ------------------------------------------------------------------
    # Google Sheets helpers
    # ------------------------------------------------------------------

    def _init_google_sheets(self) -> None:
        """Initialize Google Sheets API credentials."""
        if not GOOGLE_APIS_AVAILABLE:
            logging.warning(
                "Google APIs not available. Please install required packages."
            )
            return

        try:
            creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
            if os.path.exists(creds_file):
                self.credentials = Credentials.from_service_account_file(
                    creds_file,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"],
                )
                logging.info("Google Sheets credentials loaded from file")
            elif os.getenv("GOOGLE_CREDENTIALS_JSON"):
                creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
                if creds_json_str:
                    creds_json = _json.loads(creds_json_str)
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
        """Return a Google Sheets API service instance, or None on failure."""
        if not self.credentials:
            logging.error("No Google credentials available")
            return None
        try:
            return build("sheets", "v4", credentials=self.credentials)
        except Exception as ex:
            logging.error(f"Error building Google Sheets service: {str(ex)}")
            return None

    # ------------------------------------------------------------------
    # iRacing API helpers
    # ------------------------------------------------------------------

    def _get_iracing_api(self) -> Optional[iRacingAPIHandler]:
        """Lazily initialize and return the iRacing API handler."""
        if self._iracing_api is None:
            email = os.getenv("IRACING_EMAIL")
            password = os.getenv("IRACING_PASSWORD")
            client_id = os.getenv("IRACING_CLIENT_ID")
            client_secret = os.getenv("IRACING_CLIENT_SECRET")
            if email and password and client_id and client_secret:
                try:
                    self._iracing_api = iRacingAPIHandler(
                        email=email,
                        password=password,
                        client_id=client_id,
                        client_secret=client_secret,
                        use_oauth=True,
                    )
                    logging.info("iRacing API handler initialized successfully")
                except Exception as ex:
                    logging.error(f"Failed to initialize iRacing API handler: {ex}")
            else:
                logging.warning(
                    "iRacing API credentials not fully configured. "
                    "Set IRACING_EMAIL, IRACING_PASSWORD, IRACING_CLIENT_ID, "
                    "and IRACING_CLIENT_SECRET."
                )
        return self._iracing_api

    def _fetch_race_finishers(
        self, subsession_id: int, min_completion_pct: float = 0.75
    ) -> Dict[int, Dict[str, Any]]:
        """
        Fetch subsession results from the iRacing API and return a dict of
        cust_id -> result data for every driver who completed at least
        `min_completion_pct` of the total race laps.

        Raises:
            Exception: if the API call fails or the subsession has no race session.
        """
        api = self._get_iracing_api()
        if not api:
            raise Exception("iRacing API is not available. Check your credentials.")

        url = (
            f"https://members-ng.iracing.com/data/results/get"
            f"?subsession_id={subsession_id}&include_licenses=false"
        )
        logging.info(f"Fetching iRacing results for subsession {subsession_id}")
        data = api._get_paged_data(url)

        if not data:
            raise Exception(
                f"No data returned for subsession {subsession_id}. "
                "Check that the subsession ID is correct and the race has finished."
            )

        # Locate the race session (simsession_type 6 == Race)
        session_results: List[Dict[str, Any]] = data.get("session_results", [])
        race_session: Optional[Dict[str, Any]] = None
        for session in session_results:
            if session.get("simsession_number") == 0:
                race_session = session
                break

        if race_session is None:
            raise Exception(
                f"No race session found in subsession {subsession_id}. "
                "The subsession may be a non-race event, or results may not yet be available."
            )

        results: List[Dict[str, Any]] = race_session.get("results", [])
        if not results:
            raise Exception(
                f"Race session in subsession {subsession_id} has no driver results."
            )

        # Determine total race laps:
        # 1. Prefer race_summary.laps_complete (total laps the race ran)
        # 2. Fall back to the winner's laps_complete (finish_position == 0)
        race_summary: Dict[str, Any] = data.get("race_summary", {})
        total_laps: int = race_summary.get("laps_complete", 0)

        if total_laps == 0:
            # Sort by finish position (0-indexed) and take the leader
            sorted_results = sorted(
                results, key=lambda r: r.get("finish_position", 9999)
            )
            if sorted_results:
                total_laps = sorted_results[0].get("laps_complete", 0)

        if total_laps == 0:
            raise Exception(
                f"Could not determine total race laps for subsession {subsession_id}."
            )

        min_laps_required: float = total_laps * min_completion_pct
        logging.info(
            f"Subsession {subsession_id}: total_laps={total_laps}, "
            f"min_laps_required={min_laps_required:.1f} ({min_completion_pct * 100:.0f}%)"
        )

        finishers: Dict[int, Dict[str, Any]] = {}
        for result in results:
            cust_id = result.get("cust_id")
            laps_complete: int = result.get("laps_complete", 0)
            if cust_id is None:
                continue
            if laps_complete >= min_laps_required:
                finishers[int(cust_id)] = result

        logging.info(
            f"Found {len(finishers)} finisher(s) meeting the completion threshold "
            f"out of {len(results)} total entries."
        )
        return finishers

    # ------------------------------------------------------------------
    # Sheet update logic
    # ------------------------------------------------------------------

    def _mark_attendance_in_sheet(
        self,
        sheet_name: str,
        iracing_id_column: str,
        target_column: str,
        finisher_ids: Set[int],
    ) -> Dict[str, Any]:
        """
        Read the Google Sheet, find rows whose iRacing ID matches a finisher,
        and write 0 into `target_column` for those rows using a batch update.

        Returns a dict with keys:
            'updated'       - number of rows updated
            'matched'       - number of finisher IDs found in the sheet
            'unmatched'     - number of finisher IDs NOT found in the sheet
            'unmatched_ids' - sorted list of unmatched iRacing IDs
        """
        service = self._get_service()
        if not service or not self.spreadsheet_id:
            raise Exception(
                "Google Sheets service is not available. "
                "Check your credentials and ATTENDANCE_SHEET_ID."
            )

        # Read the entire sheet (use a wide range to capture all columns)
        range_name = f"'{sheet_name}'!A1:ZZ"
        try:
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=range_name)
                .execute()
            )
        except HttpError as err:
            raise Exception(f"Google Sheets API error while reading sheet: {err}")

        if "values" not in result or not result["values"]:
            raise Exception(
                f"Sheet '{sheet_name}' appears to be empty or does not exist."
            )

        all_rows: List[List[str]] = result["values"]
        headers: List[str] = all_rows[0]

        # Resolve column indices
        if iracing_id_column not in headers:
            raise Exception(
                f"Column '{iracing_id_column}' was not found in sheet headers. "
                f"Available headers: {headers}"
            )
        if target_column not in headers:
            raise Exception(
                f"Column '{target_column}' was not found in sheet headers. "
                f"Available headers: {headers}"
            )

        iracing_col_idx = headers.index(iracing_id_column)
        target_col_idx = headers.index(target_column)
        target_col_letter = _col_index_to_letter(target_col_idx)

        logging.info(
            f"Sheet '{sheet_name}': iRacing ID col='{iracing_id_column}' (idx {iracing_col_idx}), "
            f"target col='{target_column}' (idx {target_col_idx}, letter '{target_col_letter}')"
        )

        # Build batch update data
        batch_data: List[Dict[str, Any]] = []
        matched_ids: Set[int] = set()

        for row_offset, row in enumerate(all_rows[1:], start=2):  # row 1 is header
            if len(row) <= iracing_col_idx:
                continue  # Row is too short to have an iRacing ID

            raw_id = row[iracing_col_idx]
            try:
                row_iracing_id = int(raw_id)
            except (ValueError, TypeError):
                continue  # Not a valid integer ID

            if row_iracing_id in finisher_ids:
                cell_ref = f"'{sheet_name}'!{target_col_letter}{row_offset}"
                batch_data.append(
                    {
                        "range": cell_ref,
                        "values": [[0]],
                    }
                )
                matched_ids.add(row_iracing_id)

        if batch_data:
            body = {
                "valueInputOption": "USER_ENTERED",
                "data": batch_data,
            }
            try:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body=body
                ).execute()
                logging.info(
                    f"Batch updated {len(batch_data)} cell(s) in sheet '{sheet_name}'."
                )
            except HttpError as err:
                raise Exception(f"Google Sheets API error while writing updates: {err}")

        unmatched_ids: List[int] = sorted(finisher_ids - matched_ids)

        return {
            "updated": len(batch_data),
            "matched": len(matched_ids),
            "unmatched": len(unmatched_ids),
            "unmatched_ids": unmatched_ids,
        }

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @commands.slash_command(name="mark_attendance")
    @commands.check_any(
        commands.has_permissions(administrator=True),
        commands.has_role(1146907150229192747),
    )
    @discord.option(
        "subsession_id",
        int,
        required=True,
        description="The iRacing subsession ID for the race.",
    )
    @discord.option(
        "round",
        int,
        required=True,
        description="The race round number (e.g. 1 for the first race). Column will be S{season}R{round}.",
    )
    @discord.option(
        "season",
        int,
        required=False,
        default=15,
        description="The season number (default: 15). Column will be S{season}R{round}.",
    )
    @discord.option(
        "sheet_name",
        str,
        required=False,
        default=None,
        description="Sheet tab name to update (defaults to Points Entry S15-17).",
    )
    @discord.option(
        "iracing_id_column",
        str,
        required=False,
        default="iRacing ID",
        description="Column header that contains iRacing customer IDs (default: iRacing ID).",
    )
    @discord.option(
        "min_completion_pct",
        float,
        required=False,
        default=75.0,
        description="Minimum race completion percentage required (default: 75).",
    )
    async def markattendance(
        self,
        ctx: discord.ApplicationContext,
        subsession_id: int,
        round: int,
        season: int = 15,
        sheet_name: Optional[str] = None,
        iracing_id_column: str = "iRacingID",
        min_completion_pct: float = 75.0,
    ):
        """
        Fetch iRacing subsession results and mark 0 in a sheet column for
        every driver who completed at least the specified percentage of the race.
        The target column is constructed as S{season}R{round} (e.g. S15R1).
        """
        await ctx.defer()

        effective_sheet = sheet_name or self.default_sheet_name
        column = f"S{season}R{round}"

        # Clamp completion percentage to a sensible range
        if min_completion_pct <= 0 or min_completion_pct > 100:
            await ctx.respond(
                "min_completion_pct must be between 1 and 100.", ephemeral=True
            )
            return

        try:
            # ── Step 1: fetch qualifying finishers from iRacing ──────────────
            logging.info(
                f"markattendance invoked by {ctx.author} for subsession {subsession_id}, "
                f"column='{column}' (season={season}, round={round}), "
                f"sheet='{effective_sheet}', min_completion={min_completion_pct}%"
            )

            finishers = self._fetch_race_finishers(
                subsession_id, min_completion_pct / 100.0
            )

            if not finishers:
                await ctx.respond(
                    f"No drivers in subsession **{subsession_id}** completed "
                    f"at least **{min_completion_pct:.0f}%** of the race distance.",
                    ephemeral=True,
                )
                return

            # ── Step 2: update the Google Sheet ──────────────────────────────
            stats = self._mark_attendance_in_sheet(
                sheet_name=effective_sheet,
                iracing_id_column=iracing_id_column,
                target_column=column,
                finisher_ids=set(finishers.keys()),
            )

            unmatched_count: int = stats["unmatched"]
            unmatched_ids: List[int] = stats["unmatched_ids"]

            # ── Step 3: build a summary embed ────────────────────────────────
            all_matched = unmatched_count == 0
            embed = discord.Embed(
                title=(
                    "Attendance Marked"
                    if all_matched
                    else "Attendance Marked (with warnings)"
                ),
                color=(
                    discord.Color.green() if all_matched else discord.Color.orange()
                ),
            )
            embed.add_field(name="Subsession ID", value=str(subsession_id), inline=True)
            embed.add_field(name="Season", value=str(season), inline=True)
            embed.add_field(name="Round", value=str(round), inline=True)
            embed.add_field(name="Column Updated", value=f"`{column}`", inline=True)
            embed.add_field(
                name="Min Completion",
                value=f"{min_completion_pct:.0f}%",
                inline=True,
            )
            embed.add_field(
                name="Qualifying Finishers (iRacing)",
                value=str(len(finishers)),
                inline=True,
            )
            embed.add_field(
                name="Sheet Rows Updated",
                value=str(stats["updated"]),
                inline=True,
            )
            embed.add_field(name="Sheet", value=f"`{effective_sheet}`", inline=True)

            if unmatched_count > 0:
                display_ids = unmatched_ids[:20]
                id_list = ", ".join(str(i) for i in display_ids)
                if unmatched_count > 20:
                    id_list += f" ... (+{unmatched_count - 20} more)"
                embed.add_field(
                    name=f"{unmatched_count} Finisher(s) Not Found in Sheet",
                    value=(
                        f"These iRacing IDs completed the race but had no matching "
                        f"row in `{effective_sheet}`:\n`{id_list}`"
                    ),
                    inline=False,
                )

            await ctx.respond(embed=embed)

        except Exception as ex:
            logging.error(f"Error in markattendance command: {str(ex)}", exc_info=True)
            error_embed = discord.Embed(
                title="Error Marking Attendance",
                description=str(ex),
                color=discord.Color.red(),
            )
            await ctx.respond(embed=error_embed, ephemeral=True)

    @markattendance.error
    async def markattendance_error(
        self, ctx: discord.ApplicationContext, error: Exception
    ):
        if isinstance(error, commands.CheckFailure):
            await ctx.respond(
                "You do not have permission to use this command.", ephemeral=True
            )
        else:
            logging.error(f"Unhandled error in markattendance: {error}", exc_info=True)
            await ctx.respond(
                "An unexpected error occurred. Please check the logs.",
                ephemeral=True,
            )
