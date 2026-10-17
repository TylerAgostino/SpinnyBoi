# pyright: basic
"""Command-path tests for RegistrationCog.

Everything here drives the cog's own command wrappers — /register and the
user-command (modal pre-fill), register_driver's Discord side effects (driver
role + league nickname), the modal callback's success/failure embeds, the
cached user list, and add_user/update_user's error handling — through the fake
Google service from tests/test_sheets_client and a fake iRacing handler. The
roster's own rules (id validation, taken-number bookkeeping, the league sweep)
live in tests/test_registration_roster.py and are exercised here only where the
cog has to translate them into Discord-visible outcomes. No network, no
Discord connection.
"""
import asyncio
import contextlib
import logging
import os
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from modules.league_config import LeagueConfig
from modules.registrationCog import RegistrationCog
from modules.sheets_client import SheetsError
from tests.test_sheets_client import _client

GUILD_ID = 420037391454044171
DISCORD_ID = 501948507645378560
IRACING_ID = 1234567

# The registration tab, as the production sheet orders its columns.
HEADERS = [
    "DiscordID",
    "iRacingID",
    "DesiredName",
    "NumRaces",
    "iRacingName",
    "CarNumber",
    "RegistrationDate",
    "DesiredButTakenCarNums",
]


def _store(rows: Optional[List[List[str]]] = None) -> Dict[str, List[List[str]]]:
    """A registration tab (sheet name 'Users') holding the given data rows."""
    return {"Users": [list(HEADERS)] + (rows or [])}


def _record(store: Dict[str, List[List[str]]], row_index: int = 1) -> Dict[str, str]:
    row = store["Users"][row_index]
    padded = list(row) + [""] * (len(HEADERS) - len(row))
    return dict(zip(HEADERS, padded))


class FakeIRacing:
    """Stand-in for iRacingAPIHandler: name lookup plus league membership."""

    def __init__(
        self,
        name: Optional[str] = "Driver Of Record",
        lookup_error: Optional[Exception] = None,
        members: Optional[List[Dict[str, Any]]] = None,
        pending: Optional[List[Dict[str, Any]]] = None,
    ):
        self.name = name
        self.lookup_error = lookup_error
        self.members = members or []
        self.pending = pending or []
        self.lookup_calls: List[int] = []
        self.member_calls: List[Any] = []

    def get_driver_name(self, cust_id: int) -> Optional[str]:
        self.lookup_calls.append(cust_id)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.name

    def get_league_members(self, league_id: int, pending: bool = False):
        self.member_calls.append((league_id, pending))
        return self.pending if pending else self.members


class _Ctx:
    """The slice of discord.ApplicationContext the registration cogs use."""

    def __init__(self, author_id: int = DISCORD_ID, author_name: str = "tyler"):
        self.author = MagicMock()
        self.author.id = author_id
        self.author.name = author_name
        self.author.nick = author_name
        self.deferred = False
        self.responses: List[Any] = []
        self.modals: List[Any] = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)


def _texts(ctx: _Ctx) -> List[str]:
    return [args[0] for args, kwargs in ctx.responses if args]


def _embeds(ctx: _Ctx) -> List[discord.Embed]:
    return [kwargs["embed"] for args, kwargs in ctx.responses if "embed" in kwargs]


@contextlib.contextmanager
def _env(**pairs: Optional[str]):
    """Set (or, for None values, remove) environment variables for a block."""
    with patch.dict(os.environ, {k: v for k, v in pairs.items() if v is not None}, clear=False):
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
        yield


class _CogFixture(unittest.TestCase):
    """Cog construction shared by the command-path tests."""

    def make_cog(
        self,
        store: Dict[str, List[List[str]]],
        iracing: Optional[FakeIRacing] = None,
        spreadsheet_id: Optional[str] = "sid",
        driver_role_id: Optional[int] = LeagueConfig().driver_role_id,
    ) -> RegistrationCog:
        self.bot = MagicMock()
        self.bot.get_guild = MagicMock(return_value=None)
        config = LeagueConfig(registration_sheet_name="Users",
                              driver_role_id=driver_role_id or 0)
        with _env(REGISTRATION_SHEET_ID=spreadsheet_id):
            cog = RegistrationCog(
                self.bot, config, sheets=_client(store)
            )
        self.assertEqual(cog.roster.spreadsheet_id, spreadsheet_id)
        # Inject the iRacing seam so no credentials are read from the env.
        cog._iracing_api = iracing if iracing is not None else FakeIRacing()
        return cog

    def attach_member(self, member_id: int = DISCORD_ID, role_id: Optional[int] = None):
        """Give the fake bot a guild that resolves member + driver role."""
        role_id = role_id or LeagueConfig().driver_role_id
        role = MagicMock()
        role.id = role_id
        member = MagicMock()
        member.id = member_id
        member.add_roles = AsyncMock()
        member.edit = AsyncMock()
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=member)
        guild.get_role = MagicMock(return_value=role)
        self.bot.get_guild = MagicMock(return_value=guild)
        return guild, member, role

    def cached(self, cog: RegistrationCog, users: List[Dict[str, Any]]):
        """Pretend the user-list cache is warm so find_user reads no sheet."""
        import time as _time

        cog._users_cache = users
        cog._users_cache_ts = _time.monotonic()


class RegisterCommandTests(_CogFixture):
    def test_register_sends_modal_prefilled_from_cached_row(self):
        cog = self.make_cog(_store())
        self.cached(
            cog,
            [
                {
                    "DiscordID": str(DISCORD_ID),
                    "iRacingID": "55",
                    "DesiredName": "Old Name",
                    "CarNumber": "7",
                    "NumRaces": "12",
                }
            ],
        )
        ctx = _Ctx()
        asyncio.run(RegistrationCog.register.callback(cog, ctx))

        self.assertEqual(len(ctx.modals), 1)
        modal = ctx.modals[0]
        self.assertEqual(modal.title, "Driver Registration")
        self.assertEqual(modal.discord_user_id, DISCORD_ID)
        self.assertEqual(
            [child.value for child in modal.children], ["55", "Old Name", "7", "12"]
        )
        self.assertEqual(ctx.responses, [])

    def test_register_modal_shows_example_values_for_new_driver(self):
        # No row to pre-fill from, so the modal falls back to its own examples.
        cog = self.make_cog(_store())
        ctx = _Ctx()
        asyncio.run(RegistrationCog.register.callback(cog, ctx))
        self.assertEqual(len(ctx.modals), 1)
        self.assertEqual(
            [child.value for child in ctx.modals[0].children],
            ["123456", "Spinny Boi", "Spinny Boi", "9"],
        )

    def test_register_reports_lookup_failure_and_creates_no_modal(self):
        cog = self.make_cog(_store())
        cog.find_user = AsyncMock(side_effect=SheetsError("sheets down"))
        ctx = _Ctx()
        asyncio.run(RegistrationCog.register.callback(cog, ctx))

        self.assertEqual(ctx.modals, [])
        self.assertEqual(
            _texts(ctx),
            ["An error occurred while trying to register. Please try again later."],
        )
        self.assertTrue(ctx.responses[0][1]["ephemeral"])

    def test_user_command_prefills_modal_for_the_target_user(self):
        cog = self.make_cog(_store())
        self.cached(
            cog,
            [{"DiscordID": "99", "iRacingID": "55", "DesiredName": "Bo", "CarNumber": "8", "NumRaces": "9"}],
        )
        ctx = _Ctx()
        target = MagicMock()
        target.id = 99
        target.name = "Bo"
        asyncio.run(RegistrationCog.register_user_command.callback(cog, ctx, target))

        self.assertEqual(len(ctx.modals), 1)
        modal = ctx.modals[0]
        self.assertEqual(modal.title, "Driver Registration for Bo")
        self.assertEqual(modal.discord_user_id, 99)
        self.assertEqual([child.value for child in modal.children][1:3], ["Bo", "8"])

    def test_check_registrations_gate_is_administrator_only(self):
        perms = RegistrationCog.check_registrations.default_member_permissions
        self.assertEqual(perms, discord.Permissions(administrator=True))


class RegisterDriverDiscordEffectsTests(_CogFixture):
    def test_success_writes_row_then_assigns_role_and_nickname(self):
        store = _store()
        cog = self.make_cog(store)
        guild, member, role = self.attach_member()

        asyncio.run(
            cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Spinny Boi", "7", "9")
        )

        record = _record(store)
        self.assertEqual(record["DiscordID"], str(DISCORD_ID))
        self.assertEqual(record["iRacingID"], str(IRACING_ID))
        self.assertEqual(record["DesiredName"], "Spinny Boi")
        self.assertEqual(record["iRacingName"], "Driver Of Record")
        self.assertEqual(record["CarNumber"], "7")
        self.assertEqual(record["NumRaces"], "9")
        self.assertTrue(record["RegistrationDate"])  # stamped by the roster
        self.assertEqual(cog.roster.sheets._service.store, store)
        # Discord side effects, in the documented order.
        self.bot.get_guild.assert_called_once_with(GUILD_ID)
        member.add_roles.assert_awaited_once_with(
            role, reason="Driver Registration"
        )
        member.edit.assert_awaited_once_with(
            nick="Spinny Boi | 7", reason="Registration Update"
        )

    def test_second_registration_updates_the_same_row_not_appends(self):
        store = _store()
        cog = self.make_cog(store)
        self.attach_member()
        asyncio.run(cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9"))
        asyncio.run(
            cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al Renamed", "7", "12")
        )
        self.assertEqual(len(store["Users"]), 2)  # header + one driver row
        self.assertEqual(store["Users"][1][2], "Al Renamed")

    def test_missing_guild_or_member_still_registers_without_role(self):
        store = _store()
        cog = self.make_cog(store)  # bot.get_guild -> None
        asyncio.run(cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9"))
        self.assertEqual(len(store["Users"]), 2)  # row written despite no guild

        memberless = MagicMock()
        memberless.get_member = MagicMock(return_value=None)
        memberless.get_role = MagicMock(return_value=MagicMock())
        self.bot.get_guild = MagicMock(return_value=memberless)
        asyncio.run(
            cog.register_driver(GUILD_ID, DISCORD_ID + 1, IRACING_ID + 1, "Bo", "8", "9")
        )
        self.assertEqual(len(store["Users"]), 3)
        memberless.get_role.assert_not_called()

    def test_nickname_failure_is_logged_not_raised(self):
        store = _store()
        cog = self.make_cog(store)
        _, member, _ = self.attach_member()
        member.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "missing permissions"))
        with self.assertLogs(level=logging.ERROR) as logs:
            asyncio.run(cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9"))
        self.assertTrue(len(store["Users"]) == 2)
        self.assertTrue(any("Error updating nickname" in line for line in logs.output))


class RegistrationRulePathTests(_CogFixture):
    def test_taken_car_number_raises_without_role_or_nickname(self):
        store = _store()
        cog = self.make_cog(store)
        asyncio.run(cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9"))
        guild, member, _ = self.attach_member(member_id=DISCORD_ID + 1)

        with self.assertRaises(ValueError) as caught:
            asyncio.run(
                cog.register_driver(GUILD_ID, DISCORD_ID + 1, IRACING_ID + 1, "Bo", "7", "9")
            )
        self.assertEqual(
            str(caught.exception), "Desired league number 7 is already taken."
        )
        # The conflict is recorded on the sheet, and no role/nick is assigned.
        self.assertEqual(_record(store, 2)["DesiredButTakenCarNums"], "7")
        self.assertEqual(_record(store, 2)["CarNumber"], "")
        member.add_roles.assert_not_called()
        member.edit.assert_not_called()

    def test_modal_callback_turns_taken_number_into_failed_embed(self):
        store = _store()
        cog = self.make_cog(store)
        asyncio.run(cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9"))

        interaction = self._interaction(guild_id=GUILD_ID)
        self._submit(cog, DISCORD_ID + 1, ["1234568", "Bo", "7", "9"], interaction)

        embeds = _embeds_from(interaction)
        self.assertEqual(embeds[-1].title, "Registration Failed")
        self.assertIn("already taken", embeds[-1].description)
        self.assertEqual(_record(store, 2)["CarNumber"], "")

    def test_modal_callback_reports_success_and_writes_row(self):
        store = _store()
        cog = self.make_cog(store)
        self.attach_member(member_id=DISCORD_ID + 1)

        interaction = self._interaction(guild_id=GUILD_ID)
        self._submit(cog, DISCORD_ID + 1, ["1234568", "Bo", "8", "9"], interaction)

        embeds = _embeds_from(interaction)
        self.assertEqual(embeds[-1].title, "Registration Successful")
        self.assertEqual(_record(store)["DesiredName"], "Bo")
        self.assertEqual(_record(store)["CarNumber"], "8")

    def test_modal_callback_reports_unexpected_error_generically(self):
        store = _store()
        cog = self.make_cog(store)
        cog.register_driver = AsyncMock(side_effect=SheetsError("quota exceeded"))
        interaction = self._interaction(guild_id=GUILD_ID)
        self._submit(cog, DISCORD_ID, ["1234567", "Al", "7", "9"], interaction)

        embeds = _embeds_from(interaction)
        self.assertEqual(embeds[-1].title, "Registration Error")
        self.assertEqual(
            embeds[-1].description,
            "An error occurred during registration. Please try again later.",
        )
        self.assertEqual(store["Users"], [list(HEADERS)])  # nothing written

    def _submit(
        self,
        cog: RegistrationCog,
        discord_user_id: int,
        values: List[str],
        interaction,
    ):
        """Build (Modal needs a running loop) and submit the registration modal."""

        async def run():
            modal = cog.RegistrationModal(
                cog=cog, title="Driver Registration", discord_user_id=discord_user_id
            )
            for child, value in zip(modal.children, values):
                child.value = value
            await modal.callback(interaction)

        asyncio.run(run())

    def _interaction(self, guild_id: int):
        interaction = MagicMock()
        interaction.response = MagicMock(defer=AsyncMock())
        interaction.followup = MagicMock(send=AsyncMock())
        interaction.guild.id = guild_id
        return interaction


def _embeds_from(interaction) -> List[discord.Embed]:
    return [
        call.kwargs["embed"]
        for call in interaction.followup.send.call_args_list
        if "embed" in call.kwargs
    ]


class ConfigurationErrorTests(_CogFixture):
    def test_missing_sheet_id_surfaces_configuration_error_on_register(self):
        store = _store()
        cog = self.make_cog(store, spreadsheet_id=None)
        with self.assertRaises(SheetsError) as caught:
            asyncio.run(
                cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9")
            )
        self.assertEqual(
            str(caught.exception), "REGISTRATION_SHEET_ID is not configured."
        )
        self.assertEqual(store["Users"], [list(HEADERS)])

    def test_missing_sheet_id_makes_writers_report_failure(self):
        store = _store()
        cog = self.make_cog(store, spreadsheet_id=None)
        with self.assertLogs(level=logging.ERROR) as logs:
            self.assertFalse(asyncio.run(cog.add_user({"DiscordID": "1"})))
            self.assertFalse(asyncio.run(cog.update_user(1, {"CarNumber": "7"})))
        messages = "\n".join(logs.output)
        self.assertIn("Error adding user to spreadsheet", messages)
        self.assertIn("REGISTRATION_SHEET_ID is not configured.", messages)
        self.assertEqual(store["Users"], [list(HEADERS)])

    def test_missing_sheet_id_still_opens_the_modal_with_example_values(self):
        # Reads answer empty without a sheet id, so /register is never blocked;
        # the modal just falls back to its example values.
        store = _store()
        cog = self.make_cog(store, spreadsheet_id=None)
        self.assertEqual(asyncio.run(cog.read_users()), [])
        ctx = _Ctx()
        asyncio.run(RegistrationCog.register.callback(cog, ctx))
        self.assertEqual(len(ctx.modals), 1)
        self.assertEqual(
            [child.value for child in ctx.modals[0].children],
            ["123456", "Spinny Boi", "Spinny Boi", "9"],
        )


class NameLookupTests(_CogFixture):
    def test_unknown_customer_blocks_write_with_actionable_message(self):
        store = _store()
        cog = self.make_cog(store, iracing=FakeIRacing(name=None))
        with self.assertRaises(ValueError) as caught:
            asyncio.run(
                cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9")
            )
        self.assertEqual(
            str(caught.exception),
            "iRacing ID not found. Please double-check your Customer ID and try again.",
        )
        self.assertEqual(store["Users"], [list(HEADERS)])

    def test_lookup_exception_is_contained_and_still_blocks_write(self):
        cog = self.make_cog(_store(), iracing=FakeIRacing(lookup_error=RuntimeError("429")))
        with self.assertLogs(level=logging.ERROR) as logs:
            self.assertIsNone(cog._driver_name_sync(IRACING_ID))
        self.assertIn("Error looking up iRacing name", "\n".join(logs.output))
        with self.assertRaises(ValueError):
            asyncio.run(
                cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9")
            )

    def test_driver_name_sync_without_credentials_returns_none(self):
        store = _store()
        cog = self.make_cog(store)
        cog._iracing_api = None  # force the lazy factory path
        factory = MagicMock(return_value=None)
        with patch("modules.registrationCog.shared_iracing_handler", factory):
            with self.assertLogs(level=logging.WARNING) as logs:
                self.assertIsNone(cog._driver_name_sync(IRACING_ID))
        factory.assert_called_once_with()
        self.assertIn("iRacing API unavailable", "\n".join(logs.output))
        self.assertEqual(store["Users"], [list(HEADERS)])

    def test_handler_is_built_once_from_the_environment(self):
        cog = self.make_cog(_store())
        cog._iracing_api = None
        handler = FakeIRacing(name="Once Only")
        factory = MagicMock(return_value=handler)
        with patch("modules.registrationCog.shared_iracing_handler", factory):
            self.assertEqual(cog._driver_name_sync(1), "Once Only")
            self.assertEqual(cog._driver_name_sync(2), "Once Only")
        factory.assert_called_once_with()


class CacheAndWriterTests(_CogFixture):
    def test_read_users_refreshes_cache_then_find_user_serves_it(self):
        store = _store([["99", "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""]])
        cog = self.make_cog(store)
        self.assertIsNone(cog._users_cache)
        users = asyncio.run(cog.read_users())
        self.assertEqual(len(users), 1)
        self.assertEqual(cog._users_cache, users)
        self.assertEqual(asyncio.run(cog.find_user(99))["DesiredName"], "Al")

        # Rows added straight to the sheet stay invisible until the next read.
        store["Users"].append(["98", "11", "Bo", "9", "Bo", "8", "2026-01-01 00:00:00", ""])
        self.assertIsNone(asyncio.run(cog.find_user(98)))
        asyncio.run(cog.read_users())
        self.assertEqual(asyncio.run(cog.find_user(98))["DesiredName"], "Bo")

    def test_add_user_appends_row_and_invalidates_cache(self):
        store = _store()
        cog = self.make_cog(store)
        self.cached(cog, [{"DiscordID": "1"}])
        self.assertTrue(
            asyncio.run(cog.add_user({"DiscordID": "77", "CarNumber": "7"}))
        )
        self.assertEqual(store["Users"][1][0], "77")
        self.assertEqual(store["Users"][1][5], "7")
        self.assertIsNone(cog._users_cache)

    def test_update_user_merges_into_existing_row(self):
        store = _store([["77", "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""]])
        cog = self.make_cog(store)
        self.cached(cog, [{"DiscordID": "77"}])
        self.assertTrue(asyncio.run(cog.update_user(77, {"CarNumber": "11"})))
        self.assertEqual(store["Users"][1][5], "11")
        self.assertEqual(store["Users"][1][2], "Al")  # untouched cells survive
        self.assertIsNone(cog._users_cache)

    def test_writers_report_sheet_failure_as_false(self):
        # The registration tab is absent, so every read (and write) fails.
        store = {"Other": [["DiscordID", "CarNumber"]]}
        cog = self.make_cog(store)
        with self.assertLogs(level=logging.ERROR) as logs:
            self.assertFalse(asyncio.run(cog.add_user({"DiscordID": "1"})))
            self.assertFalse(asyncio.run(cog.update_user(1, {"CarNumber": "7"})))
        messages = "\n".join(logs.output)
        self.assertIn("Error adding user to spreadsheet", messages)
        self.assertIn("Error updating user in spreadsheet", messages)
        self.assertEqual(store, {"Other": [["DiscordID", "CarNumber"]]})


class CheckRegistrationsTests(_CogFixture):
    def test_reports_missing_iracing_credentials(self):
        cog = self.make_cog(_store())
        cog._iracing_api = None  # force the lazy factory path
        ctx = _Ctx()
        with patch("modules.registrationCog.shared_iracing_handler", return_value=None):
            asyncio.run(RegistrationCog.check_registrations.callback(cog, ctx))
        self.assertTrue(ctx.deferred)
        self.assertEqual(
            _texts(ctx),
            ["iRacing API is not configured. Check the IRACING_* credentials."],
        )
        self.assertTrue(ctx.responses[0][1]["ephemeral"])

    def test_embed_lists_invite_accept_and_number_actions(self):
        rows = [
            [str(DISCORD_ID), "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""],
            ["99", "20", "Bo", "9", "Bo", "8", "2026-01-01 00:00:00", ""],
            ["98", "30", "Cy", "9", "Cy", "9", "2026-01-01 00:00:00", ""],
            ["97", "40", "Unregistered", "9", "Unregistered", "10", "", ""],
        ]
        handler = FakeIRacing(
            members=[{"cust_id": 10, "nick_name": "Al", "car_number": "8"}],
            pending=[{"cust_id": 30}],
        )
        cog = self.make_cog(_store(rows), iracing=handler)
        ctx = _Ctx()
        asyncio.run(RegistrationCog.check_registrations.callback(cog, ctx))

        self.assertTrue(ctx.deferred)
        self.assertEqual(
            handler.member_calls, [(LeagueConfig().iracing_league_id, False),
                                   (LeagueConfig().iracing_league_id, True)]
        )
        fields = {f.name: f.value for f in _embeds(ctx)[0].fields}
        self.assertIn("Number Changes Required", fields)
        self.assertIn("Al Requests #7 (currently 8)", fields["Number Changes Required"])
        self.assertEqual(fields["Invites Required"], "Bo needs to be invited to the league")
        self.assertEqual(
            fields["Accept Invites Required"], "Cy needs to accept their league invite"
        )
        self.assertEqual(fields["Penalty Prefixes"], "\u2705 All penalty prefixes are correct.")
        self.assertNotIn("Unregistered", "\n".join(fields.values()))

    def test_embed_with_nothing_to_do_shows_only_the_penalty_prefix_field(self):
        rows = [
            [str(DISCORD_ID), "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""],
        ]
        handler = FakeIRacing(
            members=[{"cust_id": 10, "nick_name": "Al", "car_number": "7"}]
        )
        cog = self.make_cog(_store(rows), iracing=handler)
        ctx = _Ctx()
        asyncio.run(RegistrationCog.check_registrations.callback(cog, ctx))
        embed = _embeds(ctx)[0]
        # A clean sweep leaves exactly one field; the "All registrations are
        # valid." fallback below the fields can never fire (see report).
        self.assertEqual([f.name for f in embed.fields], ["Penalty Prefixes"])
        self.assertIsNone(embed.description)


if __name__ == "__main__":
    unittest.main()
