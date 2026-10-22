# pyright: basic
"""Lifecycle- and audit-path tests for RegistrationCog (issue #183 closure).

Complements tests/test_registrationCog.py by pinning the paths that file
leaves open: the cog_load background warm-up, the admin user-command's
modal-open failure (logged, then the fixed ephemeral reply), the refresh
re-entry guard (two triggers, one Sheets read), the missing-driver-role
branch (logged; registration still completes), and the check_registrations
audit embed's name-change and penalty-prefix fields plus the clean-sweep
shape. Fixtures are shared with tests/test_registrationCog.py. No network,
no Discord connection.
"""
import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from modules.registrationCog import RegistrationCog
from tests.test_registrationCog import (
    DISCORD_ID,
    GUILD_ID,
    HEADERS,
    IRACING_ID,
    FakeIRacing,
    _CogFixture,
    _Ctx,
    _embeds,
    _store,
    _texts,
)

REGISTERED_ROW = ["99", "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""]


class CogLifecycleTests(_CogFixture):
    def test_cog_load_warms_the_user_cache_in_the_background(self):
        # cog_load must not wait on the sheet itself; it detaches a warm-up
        # task that lands the first page of users for the next /register.
        cog = self.make_cog(_store([REGISTERED_ROW]))
        self.assertIsNone(cog._users_cache)

        async def run():
            await cog.cog_load()
            for _ in range(200):  # detached task; wait for it to land
                if cog._users_cache is not None:
                    break
                await asyncio.sleep(0.01)

        asyncio.run(run())
        self.assertIsNotNone(cog._users_cache)
        self.assertEqual(cog._users_cache[0]["DesiredName"], "Al")
        self.assertFalse(cog._users_refreshing)  # warm-up released the guard

    def test_user_command_modal_open_failure_logs_and_sends_fixed_reply(self):
        # The admin user-command shares /register's failure contract: when
        # the modal never opens, the officer gets the fixed ephemeral
        # message (never an exception back into the event loop).
        cog = self.make_cog(_store())
        self.cached(cog, [])
        ctx = _Ctx()
        ctx.send_modal = AsyncMock(side_effect=RuntimeError("interaction expired"))
        target = MagicMock()
        target.id = 99
        target.name = "Bo"

        with self.assertLogs(level=logging.ERROR) as logs:
            asyncio.run(
                RegistrationCog.register_user_command.callback(cog, ctx, target)
            )

        self.assertEqual(ctx.modals, [])  # the modal never got out
        self.assertEqual(
            _texts(ctx),
            ["An error occurred while trying to register. Please try again later."],
        )
        self.assertTrue(ctx.responses[0][1]["ephemeral"])
        self.assertIn("Error initiating registration", "\n".join(logs.output))

    def test_refresh_guard_collapses_concurrent_refreshes_into_one_read(self):
        # Two staleness triggers hitting at once must cost exactly one
        # Sheets read; the guard releases so later triggers can refresh.
        cog = self.make_cog(_store([REGISTERED_ROW]))
        reads = []
        original = cog.read_users

        async def counted_read():
            reads.append(1)
            await asyncio.sleep(0.01)  # hold the guard open across the seam
            return await original()

        cog.read_users = counted_read

        async def run():
            await asyncio.gather(
                cog._refresh_users_cache(), cog._refresh_users_cache()
            )
            await cog._refresh_users_cache()  # guard released, not latched

        asyncio.run(run())
        self.assertEqual(len(reads), 2)  # one shared + one after release
        self.assertEqual(len(cog._users_cache), 1)
        self.assertFalse(cog._users_refreshing)


class DriverRoleMissTests(_CogFixture):
    def test_missing_driver_role_is_logged_and_registration_still_completes(self):
        # A dangling driver_role_id must not undo the sheet write or the
        # league nickname; it is reported and the flow continues.
        store = _store()
        cog = self.make_cog(store)
        member = MagicMock()
        member.add_roles = AsyncMock()
        member.edit = AsyncMock()
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=member)
        guild.get_role = MagicMock(return_value=None)  # role deleted/misfiled
        self.bot.get_guild = MagicMock(return_value=guild)

        with self.assertLogs(level=logging.ERROR) as logs:
            asyncio.run(
                cog.register_driver(GUILD_ID, DISCORD_ID, IRACING_ID, "Al", "7", "9")
            )

        self.assertEqual(len(store["Users"]), 2)  # row survived
        member.add_roles.assert_not_called()
        member.edit.assert_awaited_once_with(
            nick="Al | 7", reason="Registration Update"
        )
        message = "\n".join(logs.output)
        self.assertIn("Driver role ID", message)
        self.assertIn(f"not found in guild {GUILD_ID}", message)


class AuditEmbedFieldTests(_CogFixture):
    def test_name_change_shows_the_rename_field_without_other_actions(self):
        # Right car number, wrong iRacing handle: only the rename field.
        store = _store(
            [[str(DISCORD_ID), "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""]]
        )
        handler = FakeIRacing(
            members=[{"cust_id": 10, "nick_name": "Mystery Racer", "car_number": "7"}]
        )
        cog = self.make_cog(store, iracing=handler)
        ctx = _Ctx()
        asyncio.run(RegistrationCog.check_registrations.callback(cog, ctx))

        fields = {f.name: f.value for f in _embeds(ctx)[0].fields}
        self.assertEqual(
            fields["Name Changes Required"], "Rename 'Mystery Racer' => 'Al' in iRacing"
        )
        self.assertNotIn("Number Changes Required", fields)
        self.assertNotIn("Penalty Prefix Updates Required", fields)
        self.assertEqual(
            fields["Penalty Prefixes"], "\u2705 All penalty prefixes are correct."
        )

    def test_penalty_prefix_drift_shows_the_rename_and_drops_the_clean_note(self):
        # 6 penalty points => "(XX) Al" is expected; the bare "Al" handle
        # gets flagged with the exact rename, and the all-clear field is
        # replaced by the action field.
        headers = list(HEADERS) + ["PenaltyPoints"]
        store = {
            "Users": [
                headers,
                [str(DISCORD_ID), "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", "", "6"],
            ]
        }
        handler = FakeIRacing(
            members=[{"cust_id": 10, "nick_name": "Al", "car_number": "7"}]
        )
        cog = self.make_cog(store, iracing=handler)
        ctx = _Ctx()
        asyncio.run(RegistrationCog.check_registrations.callback(cog, ctx))

        fields = {f.name: f.value for f in _embeds(ctx)[0].fields}
        self.assertEqual(
            fields["Penalty Prefix Updates Required"],
            "'Al': rename 'Al' => '(XX) Al' in iRacing",
        )
        self.assertNotIn("Penalty Prefixes", fields)
        self.assertNotIn("Name Changes Required", fields)

    def test_all_clean_sweep_sends_only_the_prefix_confirmation(self):
        # Nothing to do means exactly one field. The "All registrations
        # are valid." fallback under `if not embed.fields` is unreachable
        # by construction: the penalty-prefix else-branch always adds a
        # field, so that condition can never hold.
        store = _store(
            [[str(DISCORD_ID), "10", "Al", "9", "Al", "7", "2026-01-01 00:00:00", ""]]
        )
        handler = FakeIRacing(
            members=[{"cust_id": 10, "nick_name": "Al", "car_number": "7"}]
        )
        cog = self.make_cog(store, iracing=handler)
        ctx = _Ctx()
        asyncio.run(RegistrationCog.check_registrations.callback(cog, ctx))

        embed = _embeds(ctx)[0]
        self.assertEqual([f.name for f in embed.fields], ["Penalty Prefixes"])
        self.assertIsNone(embed.description)


if __name__ == "__main__":
    unittest.main()
