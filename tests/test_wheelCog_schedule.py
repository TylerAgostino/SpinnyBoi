# pyright: basic
"""Command-path tests for WheelCog's /schedule spin / list / cancel group.

The commands run against the real Scheduler on a temp SQLite file (the setup
proven offline in tests/test_scheduler.py), with a frozen clock so the
day-of-week arithmetic has one right answer, a fake bot for channel lookups,
and a local presets file so WheelCog's constructor never reaches the network.
What is pinned here is the cog's half of scheduling: the event row it writes
(channel, interaction id, JSON payload, next-occurrence timestamp), the
channel message it answers with, the listing/filtering of active spins, cancel's
success/absence/failure branches, and the fact that a canceled spin is kept
distinguishably (cancelled=1, completed=0) and never dispatched.

Permissions: the group carries only Discord-side gates
(`default_permissions(administrator=True)` on /schedule cancel). Nothing inside
the callbacks inspects the invoking member, so these tests pin the advertised
gate and record that any non-admin rejection happens in Discord, not here.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from modules import wheelCog as wheelCogModule
from modules.preset_source import PresetSource
from modules.scheduler.scheduler import ScheduledEvent, Scheduler
from modules.wheelCog import WheelCog

# A Wednesday, so "friday" is two days away and "wednesday" needs the
# same-day/next-week branch under test.
FROZEN_NOW = datetime.datetime(2026, 10, 14, 12, 0)
CHANNEL_ID = 123456789012345678
INTERACTION_ID = 987654321098765432
SPIN = "spin_preset"


@contextlib.contextmanager
def _frozen_clock(now: datetime.datetime):
    """Replace wheelCog's `datetime` module with one whose now() is fixed."""
    fake_datetime = MagicMock(
        side_effect=lambda *args, **kwargs: datetime.datetime(*args, **kwargs)
    )
    fake_datetime.now = MagicMock(return_value=now)
    fake_module = MagicMock(spec=("datetime", "timedelta"))
    fake_module.datetime = fake_datetime
    fake_module.timedelta = datetime.timedelta
    with patch.object(wheelCogModule, "datetime", fake_module):
        yield


class _Ctx:
    """The slice of discord.ApplicationContext the schedule commands use."""

    def __init__(
        self,
        channel_id: int = CHANNEL_ID,
        interaction_id: int = INTERACTION_ID,
        nick: Optional[str] = "Ty",
        name: str = "tyler",
    ):
        if nick is None:
            author = _MemberWithoutNick(name)
        else:
            author = MagicMock()
            author.nick = nick
            author.name = name
        self.author = author
        self.channel = MagicMock()
        self.channel.id = channel_id
        self.interaction = MagicMock()
        self.interaction.id = interaction_id
        self.guild = MagicMock()
        self.guild.get_role = MagicMock(return_value=None)
        self.deferred = False
        self.responses: List[Any] = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))


class _MemberWithoutNick:
    """A member whose `nick` blows up, as some Discord members do."""

    def __init__(self, name: str):
        self.name = name

    @property
    def nick(self):
        raise AttributeError("nick is not available")


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sched", "test.db")
        self.scheduler = Scheduler(self.db_path)
        self.preset_csv = os.path.join(self.temp_dir.name, "presets.csv")
        with open(self.preset_csv, "w", encoding="utf-8", newline="") as fp:
            fp.write('Fullname,Rotation\n"Rotational","driver_last_name == \'A\'"\n')
        self.channel = MagicMock()
        self.bot = MagicMock()
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_cog(self) -> WheelCog:
        presets = PresetSource(csv_url=lambda sheet: self.preset_csv)
        # Keep the cog's background loops off: the tests drive run_due() by hand.
        with patch.object(WheelCog, "check_scheduled_events", MagicMock()), patch.object(
            WheelCog, "refresh_presets", MagicMock()
        ):
            return WheelCog(self.bot, scheduler=self.scheduler, presets=presets)

    # --- command drivers (each pins the callback's real signature) ---------

    def schedule_spin(self, cog: WheelCog, ctx: _Ctx, now=FROZEN_NOW, **kwargs):
        async def run():
            return await WheelCog.schedule_spin.callback(cog, ctx, **kwargs)

        with _frozen_clock(now):
            return asyncio.run(run())

    def list_spins(self, cog: WheelCog, ctx: _Ctx):
        return asyncio.run(WheelCog.list_scheduled_spins.callback(cog, ctx))

    def cancel_spin(self, cog: WheelCog, ctx: _Ctx, event_id: int):
        return asyncio.run(
            WheelCog.cancel_scheduled_spin.callback(cog, ctx, event_id=event_id)
        )

    # --- storage probes ---------------------------------------------------

    def row(self, event_id: int):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM scheduled_events WHERE id = ?", (event_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def spin_events(self) -> List[ScheduledEvent]:
        return [e for e in self.scheduler.all_active() if e.function_name == SPIN]


class ScheduleSpinTests(_Fixture):
    def test_persists_the_next_occurrence_and_confirms_it_in_channel(self):
        cog = self.make_cog()
        ctx = _Ctx()
        message, files, filename = self.schedule_spin(
            cog,
            ctx,
            day_of_week="friday",
            hour=20,
            minute=30,
            preset_name="Rotational",
        )

        expected = datetime.datetime(2026, 10, 16, 20, 30)
        events = self.spin_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.timestamp, expected.timestamp())
        self.assertEqual(event.channel_id, CHANNEL_ID)
        self.assertEqual(event.message_id, INTERACTION_ID)  # the interaction id
        self.assertEqual(event.completed, False)
        self.assertEqual(event.cancelled, False)
        self.assertEqual(
            json.loads(event.data), {"preset_name": "Rotational", "role_id": None}
        )

        self.assertTrue(ctx.deferred)
        self.assertEqual(ctx.responses[0][0][0], (
            f"Ty scheduled spin of 'Rotational' for <t:{int(event.timestamp)}:R>. "
            "I'll post the results here when it's time!"
        ))
        self.assertIsNone(files)
        self.assertIsNone(filename)
        self.assertIn("Friday, October 16 at 08:30 PM", message)

    def test_tag_role_is_recorded_for_the_event(self):
        cog = self.make_cog()
        role = MagicMock()
        role.id = 1146907150229192747
        self.schedule_spin(
            cog,
            _Ctx(),
            day_of_week="saturday",
            hour=18,
            minute=0,
            preset_name="Rotational",
            role=role,
        )
        self.assertEqual(
            json.loads(self.spin_events()[0].data),
            {"preset_name": "Rotational", "role_id": 1146907150229192747},
        )

    def test_username_is_used_when_a_member_has_no_nick(self):
        cog = self.make_cog()
        ctx = _Ctx(nick=None)
        self.schedule_spin(
            cog,
            ctx,
            day_of_week="friday",
            hour=20,
            minute=0,
            preset_name="Rotational",
        )
        self.assertIn("tyler scheduled spin of 'Rotational'", _last_text(ctx))

    def test_same_weekday_already_past_rolls_forward_a_week(self):
        cog = self.make_cog()
        self.schedule_spin(
            cog,
            _Ctx(),
            day_of_week="wednesday",  # FROZEN_NOW is a Wednesday 12:00
            hour=9,
            minute=0,
            preset_name="Rotational",
        )
        expected = datetime.datetime(2026, 10, 21, 9, 0)
        self.assertEqual(self.spin_events()[0].timestamp, expected.timestamp())

    def test_same_weekday_still_to_come_stays_this_week(self):
        cog = self.make_cog()
        self.schedule_spin(
            cog, _Ctx(), day_of_week="wednesday", hour=18, minute=0, preset_name="Rotational"
        )
        expected = datetime.datetime(2026, 10, 14, 18, 0)
        self.assertEqual(self.spin_events()[0].timestamp, expected.timestamp())

    def test_day_names_are_case_insensitive(self):
        cog = self.make_cog()
        self.schedule_spin(
            cog, _Ctx(), day_of_week="Sunday", hour=7, minute=15, preset_name="Rotational"
        )
        expected = datetime.datetime(2026, 10, 18, 7, 15)
        self.assertEqual(self.spin_events()[0].timestamp, expected.timestamp())

    def test_unpostable_channel_is_reported_and_nothing_is_saved(self):
        cog = self.make_cog()
        self.bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no access"))
        ctx = _Ctx()
        message, files, filename = self.schedule_spin(
            cog, ctx, day_of_week="friday", hour=20, minute=0, preset_name="Rotational"
        )
        self.assertEqual(_last_text(ctx), "I don't have permission to post in this channel.")
        self.assertEqual(self.scheduler.all_active(), [])
        self.assertEqual(message, "I don't have permission to post in this channel.")

    def test_a_due_spin_fires_once_through_the_registered_handler(self):
        cog = self.make_cog()
        cog.spin_preset_new_message = AsyncMock()
        event_id = self.scheduler.schedule(
            time.time() - 10,
            SPIN,
            INTERACTION_ID,
            CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": None}),
        )
        executed = asyncio.run(self.scheduler.run_due())
        self.assertEqual(executed, 1)
        cog.spin_preset_new_message.assert_awaited_once_with(
            ctx=self.channel, preset_name="Rotational", role=None
        )
        self.bot.fetch_channel.assert_awaited_once_with(int(CHANNEL_ID))
        self.assertEqual(self.row(event_id)["completed"], 1)
        # A completed event is never dispatched again.
        self.assertEqual(asyncio.run(self.scheduler.run_due()), 0)
        self.assertEqual(cog.spin_preset_new_message.await_count, 1)

    def test_tag_role_is_resolved_when_the_spin_fires(self):
        cog = self.make_cog()
        cog.spin_preset_new_message = AsyncMock()
        role = MagicMock()
        self.channel.guild.get_role = MagicMock(return_value=role)
        self.scheduler.schedule(
            time.time() - 10,
            SPIN,
            INTERACTION_ID,
            CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": 55}),
        )
        asyncio.run(self.scheduler.run_due())
        cog.spin_preset_new_message.assert_awaited_once_with(
            ctx=self.channel, preset_name="Rotational", role=role
        )
        self.channel.guild.get_role.assert_called_once_with(55)

    def test_unpostable_channel_leaves_the_spin_pending_for_retry(self):
        cog = self.make_cog()
        cog.spin_preset_new_message = AsyncMock()
        self.bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
        self.scheduler.schedule(
            time.time() - 10,
            SPIN,
            INTERACTION_ID,
            CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": None}),
        )
        with self.assertLogs(level=logging.ERROR) as logs:
            executed = asyncio.run(self.scheduler.run_due())
        self.assertEqual(executed, 0)
        cog.spin_preset_new_message.assert_not_awaited()
        self.assertIn("Error executing scheduled event", "\n".join(logs.output))
        self.assertEqual(len(self.scheduler.pending()), 1)  # retried on the next tick


class ListScheduledSpinsTests(_Fixture):
    def test_empty_schedule_says_so(self):
        cog = self.make_cog()
        ctx = _Ctx()
        message, files, filename = self.list_spins(cog, ctx)
        self.assertEqual(_last_text(ctx), "There are no scheduled spins.")
        self.assertEqual(message, "No scheduled spins found.")

    def test_lists_active_spins_with_preset_time_and_tag_role(self):
        cog = self.make_cog()
        first = self.scheduler.schedule(
            datetime.datetime(2026, 10, 16, 20, 30).timestamp(),
            SPIN,
            1,
            CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": 55}),
        )
        second = self.scheduler.schedule(
            datetime.datetime(2026, 10, 18, 7, 0).timestamp(),
            SPIN,
            2,
            CHANNEL_ID,
            data=json.dumps({"preset_name": "Endurance", "role_id": None}),
        )
        role = MagicMock()
        role.name = "Spinners"
        ctx = _Ctx()
        ctx.guild.get_role = MagicMock(return_value=role)

        self.list_spins(cog, ctx)
        body = _last_text(ctx)
        self.assertTrue(body.startswith("# Scheduled Spins"))
        for expected in (
            f"**ID:** {first}",
            "**Preset:** Rotational",
            f"**ID:** {second}",
            "**Preset:** Endurance",
            f"<t:{int(datetime.datetime(2026, 10, 16, 20, 30).timestamp())}:F>",
            "**Tag Role:** Spinners",
        ):
            self.assertIn(expected, body)
        self.assertEqual(body.count("**Preset:**"), 2)
        ctx.guild.get_role.assert_called_once_with(55)

    def test_non_spin_and_canceled_events_are_left_out(self):
        cog = self.make_cog()
        spin_id = self.scheduler.schedule(time.time() + 600, SPIN, 1, CHANNEL_ID,
                                          data=json.dumps({"preset_name": "Rotational"}))
        self.scheduler.schedule(time.time() + 300, "cleanup", 2, CHANNEL_ID)
        canceled = self.scheduler.schedule(time.time() + 120, SPIN, 3, CHANNEL_ID,
                                           data=json.dumps({"preset_name": "Gone"}))
        self.assertTrue(self.scheduler.cancel(canceled))

        ctx = _Ctx()
        self.list_spins(cog, ctx)
        body = _last_text(ctx)
        self.assertIn(f"**ID:** {spin_id}", body)
        self.assertNotIn("**Preset:** Gone", body)
        self.assertNotIn(f"**ID:** {canceled}", body)

    def test_event_without_a_payload_lists_unknown_preset(self):
        cog = self.make_cog()
        event_id = self.scheduler.schedule(time.time() + 600, SPIN, 1, CHANNEL_ID, data=None)
        ctx = _Ctx()
        self.list_spins(cog, ctx)
        body = _last_text(ctx)
        self.assertIn("**Preset:** Unknown preset", body)
        self.assertIn(f"**ID:** {event_id}", body)


class CancelScheduledSpinTests(_Fixture):
    def _scheduled(self, preset="Rotational") -> int:
        return self.scheduler.schedule(
            datetime.datetime(2026, 10, 16, 20, 30).timestamp(),
            SPIN,
            INTERACTION_ID,
            CHANNEL_ID,
            data=json.dumps({"preset_name": preset, "role_id": None}),
        )

    def test_cancel_reports_success_and_keeps_the_row_distinguishable(self):
        cog = self.make_cog()
        event_id = self._scheduled()
        ctx = _Ctx()
        message, files, filename = self.cancel_spin(cog, ctx, event_id)

        self.assertIn(
            f"Successfully canceled the scheduled spin of 'Rotational' that was set for "
            f"<t:{int(datetime.datetime(2026, 10, 16, 20, 30).timestamp())}:F>.",
            _last_text(ctx),
        )
        self.assertEqual(message, f"Canceled scheduled spin with ID {event_id}.")
        self.assertEqual(self.scheduler.all_active(), [])
        stored = self.row(event_id)
        self.assertEqual((stored["completed"], stored["cancelled"]), (0, 1))

    def test_cancel_of_an_unknown_id_is_reported_without_touching_storage(self):
        cog = self.make_cog()
        self._scheduled()
        ctx = _Ctx()
        message, _, _ = self.cancel_spin(cog, ctx, 4242)
        self.assertEqual(_last_text(ctx), "No scheduled spin found with ID 4242.")
        self.assertEqual(message, "No scheduled spin with ID 4242.")
        self.assertEqual(len(self.spin_events()), 1)  # the real spin survives

    def test_cancel_never_targets_another_function(self):
        cog = self.make_cog()
        other_id = self.scheduler.schedule(time.time() + 600, "cleanup", 1, CHANNEL_ID)
        ctx = _Ctx()
        self.cancel_spin(cog, ctx, other_id)
        self.assertEqual(_last_text(ctx), f"No scheduled spin found with ID {other_id}.")
        self.assertEqual(len(self.scheduler.all_active()), 1)  # still pending

    def test_already_canceled_spin_reports_absence_on_the_second_attempt(self):
        cog = self.make_cog()
        event_id = self._scheduled()
        self.cancel_spin(cog, _Ctx(), event_id)
        ctx = _Ctx()
        self.cancel_spin(cog, ctx, event_id)
        self.assertEqual(_last_text(ctx), f"No scheduled spin found with ID {event_id}.")

    def test_storage_failure_to_cancel_is_reported_as_failure(self):
        cog = self.make_cog()
        event_id = self._scheduled()
        cog.scheduler.cancel = MagicMock(return_value=False)
        ctx = _Ctx()
        message, _, _ = self.cancel_spin(cog, ctx, event_id)
        self.assertEqual(_last_text(ctx), f"Failed to cancel the scheduled spin with ID {event_id}.")
        self.assertEqual(message, f"Failed to cancel scheduled spin with ID {event_id}.")
        self.assertEqual(len(self.spin_events()), 1)  # still active


class ScheduleRoundTripTests(_Fixture):
    def test_schedule_then_list_then_cancel(self):
        cog = self.make_cog()
        first_ctx = _Ctx(interaction_id=11)
        self.schedule_spin(
            cog, first_ctx, day_of_week="friday", hour=20, minute=30, preset_name="Rotational"
        )
        second_ctx = _Ctx(interaction_id=22)
        self.schedule_spin(
            cog, second_ctx, day_of_week="sunday", hour=7, minute=15, preset_name="Endurance"
        )
        events = self.spin_events()
        self.assertEqual([e.message_id for e in events], [11, 22])

        list_ctx = _Ctx()
        self.list_spins(cog, list_ctx)
        body = _last_text(list_ctx)
        self.assertIn("**Preset:** Rotational", body)
        self.assertIn("**Preset:** Endurance", body)

        canceled_id = events[0].id
        cancel_ctx = _Ctx()
        self.cancel_spin(cog, cancel_ctx, canceled_id)
        self.assertIn(
            "Successfully canceled the scheduled spin of 'Rotational'",
            _last_text(cancel_ctx),
        )

        after_ctx = _Ctx()
        self.list_spins(cog, after_ctx)
        after = _last_text(after_ctx)
        self.assertNotIn("**Preset:** Rotational", after)
        self.assertIn("**Preset:** Endurance", after)
        self.assertIn(f"**ID:** {events[1].id}", after)
        remaining = self.spin_events()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(self.row(canceled_id)["cancelled"], 1)

    def test_canceled_spin_survives_a_due_tick_unexecuted(self):
        cog = self.make_cog()
        cog.spin_preset_new_message = AsyncMock()
        keep = self.scheduler.schedule(
            time.time() - 10, SPIN, 1, CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": None}),
        )
        drop = self.scheduler.schedule(
            time.time() - 20, SPIN, 2, CHANNEL_ID,
            data=json.dumps({"preset_name": "Endurance", "role_id": None}),
        )
        self.cancel_spin(cog, _Ctx(), drop)
        self.assertEqual(asyncio.run(self.scheduler.run_due()), 1)
        kwargs = cog.spin_preset_new_message.await_args.kwargs
        self.assertEqual(kwargs["preset_name"], "Rotational")
        self.assertEqual(self.row(keep)["completed"], 1)
        self.assertEqual((self.row(drop)["completed"], self.row(drop)["cancelled"]), (0, 1))


class PermissionGateTests(_Fixture):
    def test_cancel_advertises_an_administrator_gate(self):
        self.assertEqual(
            WheelCog.cancel_scheduled_spin.default_member_permissions,
            discord.Permissions(administrator=True),
        )

    def test_schedule_and_list_advertise_no_default_gate(self):
        self.assertIsNone(WheelCog.schedule_spin.default_member_permissions)
        self.assertIsNone(WheelCog.list_scheduled_spins.default_member_permissions)

    def test_callbacks_do_no_author_check_of_their_own(self):
        # Nothing in the callback inspects the member: an under-permitted
        # context still cancels, so the gate is enforced by Discord alone.
        cog = self.make_cog()
        event_id = self.scheduler.schedule(
            time.time() + 600, SPIN, 1, CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": None}),
        )
        ctx = _Ctx()
        ctx.channel.permissions_for = MagicMock(return_value=discord.Permissions.none())
        self.cancel_spin(cog, ctx, event_id)
        self.assertIn("Successfully canceled", _last_text(ctx))
        self.assertEqual(self.scheduler.all_active(), [])


def _last_text(ctx: _Ctx) -> str:
    texts = [args[0] for args, kwargs in ctx.responses if args]
    assert texts, "the command answered no text"
    return texts[-1]


if __name__ == "__main__":
    unittest.main()
