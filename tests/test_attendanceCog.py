# pyright: basic
"""Command-path tests for AttendanceCog (/mark_attendance).

The whole iRacing-results -> attendance-sheet flow is driven offline: a fake
handler feeds the subsession results payload (the api seam the cog calls), the
fake Google service from tests/test_sheets_client plays the points sheet, and a
tiny ctx records what the bot answered. tests/test_sheets_client.py already
pins the row-matching/column-writing of `_mark_attendance_in_sheet`; what is
pinned here is the command around it — option defaults from LeagueConfig, the
S{season}R{round} column it builds, the completion clamp, the summary/warning
embeds, and the fact that a failed results fetch is logged and reported instead
of crashing the command.
"""
import asyncio
import contextlib
import logging
import os
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import discord
from discord.ext import commands as ext_commands

from modules.attendanceCog import AttendanceCog
from modules.league_config import LeagueConfig, STAFF_ROLE_ID
from modules.sheets_client import SheetsError
from tests.test_sheets_client import _client

SHEET_ID = "attendance-sheet-id"
SUBSESSION_ID = 88771234


@contextlib.contextmanager
def _env(**pairs: Optional[str]):
    """Set (or, for None values, remove) environment variables for a block."""
    with patch.dict(
        os.environ, {k: v for k, v in pairs.items() if v is not None}, clear=False
    ):
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
        yield


def _driver(cust_id: Optional[int], laps: int, finish_position: int = 0, **extra):
    row: Dict[str, Any] = {
        "laps_complete": laps,
        "finish_position": finish_position,
        "display_name": f"Driver {cust_id}",
    }
    if cust_id is not None:
        row["cust_id"] = cust_id
    row.update(extra)
    return row


def _race_payload(race_results: List[Dict[str, Any]], total_laps: Optional[int] = None,
                  practice: Optional[List[Dict[str, Any]]] = None):
    """A results payload: practice session first, then the race (number 0)."""
    sessions: List[Dict[str, Any]] = []
    if practice is not None:
        sessions.append({"simsession_number": 2, "results": practice})
    sessions.append({"simsession_number": 0, "results": race_results})
    payload: Dict[str, Any] = {"session_results": sessions}
    if total_laps is not None:
        payload["race_summary"] = {"laps_complete": total_laps}
    return payload


class FakeIRacing:
    """Answers the one api call the cog makes: paged results over a URL."""

    def __init__(self, payload: Any = None, error: Optional[Exception] = None):
        self.payload = payload
        self.error = error
        self.urls: List[str] = []

    def _get_paged_data(self, url: str, *args, **kwargs):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


class _Ctx:
    """The slice of discord.ApplicationContext the attendance command uses."""

    def __init__(self, author_name: str = "tyler", staff: bool = False):
        author = MagicMock()
        author.name = author_name
        staff_role = MagicMock()
        staff_role.id = STAFF_ROLE_ID
        author.roles = [staff_role] if staff else []
        self.author = author
        self.channel = MagicMock()
        self.channel.type = discord.ChannelType.text
        self.channel.permissions_for = MagicMock(return_value=discord.Permissions.none())
        self.guild = MagicMock()
        self.deferred = False
        self.responses: List[Any] = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))


def _texts(ctx: _Ctx) -> List[str]:
    return [args[0] for args, kwargs in ctx.responses if args]


def _embeds(ctx: _Ctx) -> List[discord.Embed]:
    return [kwargs["embed"] for args, kwargs in ctx.responses if "embed" in kwargs]


def _fields(embed: discord.Embed) -> Dict[str, str]:
    return {field.name: field.value for field in embed.fields}


class _Fixture(unittest.TestCase):
    def make_cog(
        self,
        store: Dict[str, List[List[str]]],
        iracing: Any = None,
        sheet_id: Optional[str] = SHEET_ID,
        config: Optional[LeagueConfig] = None,
    ) -> AttendanceCog:
        config = config or LeagueConfig(
            season=16,
            points_sheet_name="Points",
            points_iracing_id_column="iRacing ID",
        )
        self.config = config
        with _env(ATTENDANCE_SHEET_ID=sheet_id):
            cog = AttendanceCog(MagicMock(), config, sheets=_client(store))
        self.assertEqual(cog.spreadsheet_id, sheet_id)
        cog._iracing_api = iracing if iracing is not None else FakeIRacing()
        return cog

    def run_command(self, cog: AttendanceCog, ctx: _Ctx, **kwargs):
        return AttendanceCog.markattendance.callback(cog, ctx, **kwargs)


class FetchRaceFinishersTests(_Fixture):
    def test_race_session_drives_the_completion_threshold(self):
        payload = _race_payload(
            race_results=[
                _driver(10, 8, finish_position=0),   # 80% of 10 -> qualifies
                _driver(11, 7, finish_position=1),   # 70% of 10 -> does not
                _driver(None, 10),                   # no cust id -> ignored
            ],
            total_laps=10,
            # A driver who only "won" the practice session must not count.
            practice=[_driver(99, 10, finish_position=0)],
        )
        cog = self.make_cog({}, iracing=FakeIRacing(payload))
        finishers = cog._fetch_race_finishers(SUBSESSION_ID)
        self.assertEqual(sorted(finishers), [10])
        self.assertEqual(finishers[10]["laps_complete"], 8)

    def test_threshold_is_configurable_per_call(self):
        payload = _race_payload(
            race_results=[_driver(10, 8), _driver(11, 5)], total_laps=10
        )
        cog = self.make_cog({}, iracing=FakeIRacing(payload))
        self.assertEqual(sorted(cog._fetch_race_finishers(1, 0.75)), [10])
        self.assertEqual(sorted(cog._fetch_race_finishers(1, 0.5)), [10, 11])
        self.assertEqual(cog._fetch_race_finishers(1, 0.95), {})

    def test_total_laps_falls_back_to_the_race_winner(self):
        # No race_summary: the leader's laps_complete becomes 100%.
        payload = _race_payload(
            race_results=[
                _driver(10, 6, finish_position=2),
                _driver(11, 8, finish_position=0),
            ]
        )
        cog = self.make_cog({}, iracing=FakeIRacing(payload))
        self.assertEqual(sorted(cog._fetch_race_finishers(1)), [10, 11])  # 6/8 = 75%

    def test_results_url_names_the_subsession_only(self):
        cog = self.make_cog({}, iracing=FakeIRacing(_race_payload([_driver(10, 5)], 5)))
        cog._fetch_race_finishers(SUBSESSION_ID)
        self.assertEqual(
            cog._iracing_api.urls,
            [
                "https://members-ng.iracing.com/data/results/get"
                f"?subsession_id={SUBSESSION_ID}&include_licenses=false"
            ],
        )

    def test_missing_credentials_are_reported(self):
        cog = self.make_cog({}, sheet_id=SHEET_ID)
        cog._iracing_api = None
        with patch("modules.attendanceCog.shared_iracing_handler", return_value=None):
            with self.assertRaisesRegex(Exception, "iRacing API is not available"):
                cog._fetch_race_finishers(1)

    def test_empty_payload_and_missing_race_session_are_reported(self):
        cases = [
            (None, "No data returned for subsession"),
            ({"session_results": [{"simsession_number": 2, "results": []}]},
             "No race session found"),
            (_race_payload([]), "has no driver results"),
            (_race_payload([_driver(10, 0)], total_laps=0), "Could not determine total race laps"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                cog = self.make_cog({}, iracing=FakeIRacing(payload))
                with self.assertRaisesRegex(Exception, expected):
                    cog._fetch_race_finishers(42)


class MarkAttendanceCommandTests(_Fixture):
    def _store(self):
        return {
            "Points": [
                ["iRacing ID", "S16R1", "S16R2", "Points"],
                ["10", "", "", "40"],
                ["'11", "", "", "38"],
                ["junk", "", "", ""],
                ["12", "", "", "30"],
            ]
        }

    def test_happy_path_marks_rows_and_reports_a_green_summary(self):
        store = self._store()
        payload = _race_payload(
            race_results=[_driver(10, 8), _driver(11, 10), _driver(12, 1)],
            total_laps=10,
        )
        cog = self.make_cog(store, iracing=FakeIRacing(payload))
        ctx = _Ctx()
        asyncio.run(
            self.run_command(
                cog, ctx, subsession_id=SUBSESSION_ID, round=1, season=16
            )
        )

        self.assertTrue(ctx.deferred)
        self.assertEqual(store["Points"][1][:3], ["10", "0", ""])
        self.assertEqual(store["Points"][2][:3], ["'11", "0", ""])  # quoted id matches
        self.assertEqual(store["Points"][3], ["junk", "", "", ""])  # unparsable id kept
        self.assertEqual(store["Points"][4][:3], ["12", "", ""])    # below threshold

        embed = _embeds(ctx)[0]
        self.assertEqual(embed.title, "Attendance Marked")
        self.assertEqual(embed.color, discord.Color.green())
        fields = _fields(embed)
        self.assertEqual(fields["Subsession ID"], str(SUBSESSION_ID))
        self.assertEqual(fields["Season"], "16")
        self.assertEqual(fields["Round"], "1")
        self.assertEqual(fields["Column Updated"], "`S16R1`")
        self.assertEqual(fields["Min Completion"], "75%")
        self.assertEqual(fields["Qualifying Finishers (iRacing)"], "2")
        self.assertEqual(fields["Sheet Rows Updated"], "2")
        self.assertEqual(fields["Sheet"], "`Points`")
        self.assertNotIn("ephemeral", ctx.responses[0][1])

    def test_finishers_missing_from_the_sheet_become_a_warning(self):
        store = self._store()
        payload = _race_payload(
            race_results=[_driver(10, 10), _driver(777, 10), _driver(778, 10)],
            total_laps=10,
        )
        cog = self.make_cog(store, iracing=FakeIRacing(payload))
        ctx = _Ctx()
        asyncio.run(self.run_command(cog, ctx, subsession_id=SUBSESSION_ID, round=2, season=16))

        embed = _embeds(ctx)[0]
        self.assertEqual(embed.title, "Attendance Marked (with warnings)")
        self.assertEqual(embed.color, discord.Color.orange())
        fields = _fields(embed)
        self.assertEqual(fields["Column Updated"], "`S16R2`")
        self.assertEqual(fields["Qualifying Finishers (iRacing)"], "3")
        self.assertEqual(fields["Sheet Rows Updated"], "1")
        warning = fields["2 Finisher(s) Not Found in Sheet"]
        self.assertIn("`777, 778`", warning)
        self.assertIn("`Points`", warning)

    def test_defaults_come_from_the_league_config(self):
        store = {
            "League Points": [
                ["Driver ID", "S17R9"],
                ["10", ""],
            ]
        }
        config = LeagueConfig(
            season=17, points_sheet_name="League Points", points_iracing_id_column="Driver ID"
        )
        payload = _race_payload(race_results=[_driver(10, 10)], total_laps=10)
        cog = self.make_cog(store, iracing=FakeIRacing(payload), config=config)
        ctx = _Ctx()
        # season / sheet_name / iracing_id_column all omitted.
        asyncio.run(self.run_command(cog, ctx, subsession_id=5, round=9))

        self.assertEqual(store["League Points"][1], ["10", "0"])
        fields = _fields(_embeds(ctx)[0])
        self.assertEqual(fields["Column Updated"], "`S17R9`")
        self.assertEqual(fields["Sheet"], "`League Points`")
        # The Season field echoes the raw argument, so it reads "None" here —
        # a display wart in attendanceCog, see report; the column is correct.

    def test_explicit_sheet_and_id_column_override_the_config(self):
        store = {
            "Backup": [["Cust ID", "S16R4"], ["10", ""], ["11", ""]],
            "Points": [["iRacing ID", "S16R4"], ["10", "keep"]],
        }
        payload = _race_payload(race_results=[_driver(10, 10)], total_laps=10)
        cog = self.make_cog(store, iracing=FakeIRacing(payload))
        ctx = _Ctx()
        asyncio.run(
            self.run_command(
                cog,
                ctx,
                subsession_id=5,
                round=4,
                season=16,
                sheet_name="Backup",
                iracing_id_column="Cust ID",
            )
        )
        self.assertEqual(store["Backup"][1], ["10", "0"])
        self.assertEqual(store["Points"][1], ["10", "keep"])  # config sheet untouched
        self.assertEqual(_fields(_embeds(ctx)[0])["Sheet"], "`Backup`")

    def test_completion_percentage_is_clamped_to_a_sensible_range(self):
        store = self._store()
        api = FakeIRacing(_race_payload([_driver(10, 10)], 10))
        for bad in (0, -5, 101):
            with self.subTest(min_completion_pct=bad):
                cog = self.make_cog(store, iracing=api)
                ctx = _Ctx()
                asyncio.run(
                    self.run_command(
                        cog, ctx, subsession_id=5, round=1, season=16,
                        min_completion_pct=bad,
                    )
                )
                self.assertEqual(
                    _texts(ctx), ["min_completion_pct must be between 1 and 100."]
                )
                self.assertTrue(ctx.responses[0][1]["ephemeral"])
        self.assertEqual(api.urls, [])            # iRacing never contacted
        self.assertEqual(store["Points"][1][:2], ["10", ""])  # sheet untouched

    def test_no_qualifying_finishers_reports_without_touching_the_sheet(self):
        store = self._store()
        payload = _race_payload(race_results=[_driver(10, 2)], total_laps=10)
        cog = self.make_cog(store, iracing=FakeIRacing(payload))
        ctx = _Ctx()
        asyncio.run(self.run_command(cog, ctx, subsession_id=42, round=1, season=16))

        self.assertEqual(
            _texts(ctx),
            [
                "No drivers in subsession **42** completed at least **75%** of the race distance."
            ],
        )
        self.assertTrue(ctx.responses[0][1]["ephemeral"])
        self.assertEqual(store["Points"][1][:2], ["10", ""])

    def test_results_fetch_failure_is_logged_and_reported_not_raised(self):
        store = self._store()
        api = FakeIRacing(error=RuntimeError("iRacing login failed"))
        cog = self.make_cog(store, iracing=api)
        ctx = _Ctx()
        with self.assertLogs(level=logging.ERROR) as logs:
            asyncio.run(self.run_command(cog, ctx, subsession_id=42, round=1, season=16))

        self.assertIn("Error in markattendance command", "\n".join(logs.output))
        embed = _embeds(ctx)[0]
        self.assertEqual(embed.title, "Error Marking Attendance")
        self.assertEqual(embed.description, "iRacing login failed")
        self.assertEqual(embed.color, discord.Color.red())
        self.assertTrue(ctx.responses[0][1]["ephemeral"])
        self.assertEqual(store["Points"][1][:2], ["10", ""])  # nothing written

    def test_sheet_configuration_failure_is_reported_as_an_embed(self):
        payload = _race_payload(race_results=[_driver(10, 10)], total_laps=10)
        store = self._store()
        cog = self.make_cog(store, iracing=FakeIRacing(payload), sheet_id=None)
        ctx = _Ctx()
        asyncio.run(self.run_command(cog, ctx, subsession_id=42, round=1, season=16))

        embed = _embeds(ctx)[0]
        self.assertEqual(embed.title, "Error Marking Attendance")
        self.assertIn("ATTENDANCE_SHEET_ID is not configured", embed.description)
        self.assertEqual(store["Points"][1][:2], ["10", ""])

    def test_unknown_target_column_is_reported_as_an_embed(self):
        store = self._store()
        payload = _race_payload(race_results=[_driver(10, 10)], total_laps=10)
        cog = self.make_cog(store, iracing=FakeIRacing(payload))
        ctx = _Ctx()
        asyncio.run(self.run_command(cog, ctx, subsession_id=42, round=99, season=16))

        embed = _embeds(ctx)[0]
        self.assertEqual(embed.title, "Error Marking Attendance")
        self.assertIn("Column 'S16R99' was not found", embed.description)
        self.assertEqual(store["Points"][1][:2], ["10", ""])

    def test_more_than_twenty_unmatched_ids_are_truncated_in_the_embed(self):
        store = self._store()
        finishers = [_driver(10, 10)] + [
            _driver(cid, 10) for cid in range(900, 921)
        ]  # 21 finishers the sheet has never heard of
        payload = _race_payload(race_results=finishers, total_laps=10)
        cog = self.make_cog(store, iracing=FakeIRacing(payload))
        ctx = _Ctx()
        asyncio.run(
            self.run_command(cog, ctx, subsession_id=SUBSESSION_ID, round=1, season=16)
        )

        warning = _fields(_embeds(ctx)[0])["21 Finisher(s) Not Found in Sheet"]
        self.assertIn("900", warning)
        self.assertIn(" ... (+1 more)`", warning)
        self.assertIn("919", warning)  # the 20th id is shown; the cap is exactly 20
        self.assertNotIn("920", warning)  # the 21st id stays behind the cap


class SheetMarkingHelperTests(_Fixture):
    """_mark_attendance_in_sheet direct: header validation and odd rows."""

    def test_missing_iracing_id_column_lists_available_headers(self):
        store = {"Points": [["iRacing ID", "S16R1"], ["10", ""]]}
        cog = self.make_cog(store)
        with self.assertRaises(SheetsError) as caught:
            cog._mark_attendance_in_sheet("Points", "Cust ID", "S16R1", {10})
        message = str(caught.exception)
        self.assertIn("Column 'Cust ID' was not found in sheet headers", message)
        self.assertIn("Available headers: ['iRacing ID', 'S16R1']", message)
        self.assertEqual(store["Points"][1], ["10", ""])  # nothing written

    def test_rows_shorter_than_the_id_column_are_skipped(self):
        store = {
            "Points": [
                ["Driver", "iRacing ID", "S16R1"],
                ["Al", "10", ""],
                ["Buddy"],  # shorter than the id column index
                ["Cy", "", ""],
            ]
        }
        cog = self.make_cog(store)
        stats = cog._mark_attendance_in_sheet(
            "Points", "iRacing ID", "S16R1", {10, 11}
        )
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["unmatched_ids"], [11])
        self.assertEqual(store["Points"][2], ["Buddy"])  # short row untouched
        self.assertEqual(store["Points"][1], ["Al", "10", "0"])


class PermissionGateTests(_Fixture):
    def _check(self, ctx: _Ctx):
        predicate = AttendanceCog.markattendance.checks[0]
        return asyncio.run(predicate(ctx))

    def test_staff_role_holder_passes_the_gate(self):
        self.assertTrue(self._check(_Ctx(staff=True)))

    def test_administrator_passes_the_gate(self):
        ctx = _Ctx()
        ctx.channel.permissions_for = MagicMock(return_value=discord.Permissions.all())
        self.assertTrue(self._check(ctx))

    def test_plain_driver_is_rejected_with_a_check_failure(self):
        ctx = _Ctx()
        ctx.channel.permissions_for = MagicMock(
            return_value=discord.Permissions(send_messages=True)
        )
        with self.assertRaises(ext_commands.CheckFailure):
            self._check(ctx)

    def test_error_handler_distinguishes_permission_and_unexpected_failures(self):
        cog = self.make_cog({})
        ctx = _Ctx()
        asyncio.run(
            cog.markattendance_error(ctx, ext_commands.CheckFailure("no role"))
        )
        self.assertEqual(
            _texts(ctx), ["You do not have permission to use this command."]
        )
        self.assertTrue(ctx.responses[0][1]["ephemeral"])

        other = _Ctx()
        with self.assertLogs(level=logging.ERROR) as logs:
            asyncio.run(cog.markattendance_error(other, RuntimeError("boom")))
        self.assertEqual(
            _texts(other), ["An unexpected error occurred. Please check the logs."]
        )
        self.assertIn("Unhandled error in markattendance", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
