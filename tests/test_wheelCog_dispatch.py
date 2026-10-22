# pyright: basic
"""Scheduled-spin dispatch handlers, autocomplete hooks, and _quit_driver.

The scheduler calls WheelCog._run_scheduled_spin/_run_scheduled_custom_spin
with a stored ScheduledEvent; these tests drive the handlers through cog
stubs (same SimpleNamespace style as test_wheelCog_option_filter) and
assert the recorded calls to the *_new_message entry points. Also covers
the get_presets/get_preset_tabs autocomplete hooks and _quit_driver's
promise that a failing driver.quit never escapes the quit thread.

Run: .venv\\Scripts\\python -m unittest tests.test_wheelCog_dispatch
"""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from modules import wheelCog as wheelCogModule
from modules.scheduler.scheduler import ScheduledEvent
from modules.wheelCog import WheelCog, _quit_driver

CHANNEL_ID = 123456789012345678


def scheduled_event(function_name, data=None, event_id=9):
    """A real ScheduledEvent row as the scheduler hands it to a handler."""
    return ScheduledEvent(
        id=event_id,
        timestamp=0.0,
        function_name=function_name,
        message_id=0,
        channel_id=CHANNEL_ID,
        data=json.dumps(data) if data is not None else None,
    )


class FakeChannel:
    """A fetched channel: one guild whose get_role answers with `role`."""

    def __init__(self, role=None):
        self.guild = SimpleNamespace(get_role=mock.Mock(return_value=role))


class GuildlessChannel:
    """A channel whose guild access raises, as unavailable guilds do."""

    @property
    def guild(self):
        raise RuntimeError("guild unavailable")


def dispatch_cog(channel):
    """The slice of WheelCog the dispatch handlers touch."""
    return SimpleNamespace(
        bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=channel)),
        spin_preset_new_message=AsyncMock(),
        spin_custom_new_message=AsyncMock(),
    )


class ScheduledPresetSpinDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_preset_and_role_reach_spin_preset_new_message(self):
        role = object()
        channel = FakeChannel(role=role)
        cog = dispatch_cog(channel)
        event = scheduled_event(
            "spin_preset", {"preset_name": "Rotational", "role_id": 55}
        )

        await WheelCog._run_scheduled_spin(cog, event)

        cog.bot.fetch_channel.assert_awaited_once_with(CHANNEL_ID)
        channel.guild.get_role.assert_called_once_with(55)
        cog.spin_preset_new_message.assert_awaited_once_with(
            ctx=channel, preset_name="Rotational", role=role
        )
        cog.spin_custom_new_message.assert_not_awaited()

    async def test_no_role_id_skips_the_guild_lookup_entirely(self):
        channel = FakeChannel()
        cog = dispatch_cog(channel)

        await WheelCog._run_scheduled_spin(
            cog, scheduled_event("spin_preset", {"preset_name": "S16"})
        )

        channel.guild.get_role.assert_not_called()
        cog.spin_preset_new_message.assert_awaited_once_with(
            ctx=channel, preset_name="S16", role=None
        )

    async def test_event_without_data_spins_with_no_preset_or_role(self):
        cog = dispatch_cog(FakeChannel())

        await WheelCog._run_scheduled_spin(cog, scheduled_event("spin_preset"))

        cog.spin_preset_new_message.assert_awaited_once_with(
            ctx=mock.ANY, preset_name=None, role=None
        )

    async def test_missing_channel_logs_and_aborts_before_spinning(self):
        cog = dispatch_cog(None)
        event = scheduled_event("spin_preset", {"preset_name": "S16"}, event_id=11)

        with self.assertLogs(level="ERROR") as logs:
            await WheelCog._run_scheduled_spin(cog, event)

        self.assertIn(
            "Channel 123456789012345678 not found for event 11",
            "\n".join(logs.output),
        )
        cog.spin_preset_new_message.assert_not_awaited()

    async def test_role_lookup_failure_still_spins_with_role_none(self):
        cog = dispatch_cog(GuildlessChannel())
        event = scheduled_event(
            "spin_preset", {"preset_name": "Rotational", "role_id": 55}
        )

        with self.assertLogs(level="ERROR") as logs:
            await WheelCog._run_scheduled_spin(cog, event)

        self.assertIn(
            "Error fetching role 55: guild unavailable", "\n".join(logs.output)
        )
        cog.spin_preset_new_message.assert_awaited_once_with(
            ctx=mock.ANY, preset_name="Rotational", role=None
        )


class ScheduledCustomSpinDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_options_and_role_reach_spin_custom_new_message(self):
        role = object()
        channel = FakeChannel(role=role)
        cog = dispatch_cog(channel)
        event = scheduled_event(
            "spin_custom", {"custom_options": "sorry=3, not sorry=7", "role_id": 66}
        )

        await WheelCog._run_scheduled_custom_spin(cog, event)

        cog.bot.fetch_channel.assert_awaited_once_with(CHANNEL_ID)
        channel.guild.get_role.assert_called_once_with(66)
        cog.spin_custom_new_message.assert_awaited_once_with(
            ctx=channel, custom_options="sorry=3, not sorry=7", role=role
        )
        cog.spin_preset_new_message.assert_not_awaited()

    async def test_missing_channel_logs_and_aborts_before_spinning(self):
        cog = dispatch_cog(None)
        event = scheduled_event(
            "spin_custom", {"custom_options": "a,b"}, event_id=12
        )

        with self.assertLogs(level="ERROR") as logs:
            await WheelCog._run_scheduled_custom_spin(cog, event)

        self.assertIn("not found for event 12", "\n".join(logs.output))
        cog.spin_custom_new_message.assert_not_awaited()

    async def test_role_lookup_failure_still_spins_with_role_none(self):
        cog = dispatch_cog(GuildlessChannel())
        event = scheduled_event(
            "spin_custom", {"custom_options": "a,b", "role_id": 66}
        )

        with self.assertLogs(level="ERROR") as logs:
            await WheelCog._run_scheduled_custom_spin(cog, event)

        self.assertIn(
            "Error fetching role 66: guild unavailable", "\n".join(logs.output)
        )
        cog.spin_custom_new_message.assert_awaited_once_with(
            ctx=mock.ANY, custom_options="a,b", role=None
        )


class AutocompleteTests(unittest.IsolatedAsyncioTestCase):
    """get_presets/get_preset_tabs proxy PresetSource through a worker
    thread so the event loop never blocks on the sheet read."""

    def stub_source(self, names=None, tabs=None):
        state = {}

        def _names():
            state["names_thread"] = threading.get_ident()
            return names() if names else ["S16", "S20"]

        def _tabs(preset_name):
            state["tabs_thread"] = threading.get_ident()
            state["tabs_arg"] = preset_name
            return tabs(preset_name) if tabs else [f"{preset_name} driver sheet"]

        source = SimpleNamespace(names=_names, tabs=_tabs)
        patcher = mock.patch.object(
            wheelCogModule, "PresetSource", mock.MagicMock()
        )
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        patched.default.return_value = source
        return state

    async def test_get_presets_returns_the_preset_names(self):
        state = self.stub_source(names=lambda: ["S16", "S20", "Rotational"])

        options = await wheelCogModule.get_presets(None)

        self.assertEqual(options, ["S16", "S20", "Rotational"])
        self.assertNotEqual(state["names_thread"], threading.get_ident())

    async def test_get_preset_tabs_asks_the_source_about_the_chosen_preset(self):
        state = self.stub_source()
        ctx = SimpleNamespace(options={"preset_name": "S16"})

        options = await wheelCogModule.get_preset_tabs(ctx)

        self.assertEqual(options, ["S16 driver sheet"])
        self.assertEqual(state["tabs_arg"], "S16")
        self.assertNotEqual(state["tabs_thread"], threading.get_ident())

    async def test_get_preset_tabs_without_a_preset_name_returns_nothing(self):
        state = self.stub_source()

        self.assertEqual(
            await wheelCogModule.get_preset_tabs(SimpleNamespace(options={})), []
        )
        self.assertEqual(
            await wheelCogModule.get_preset_tabs(
                SimpleNamespace(options={"preset_name": ""})
            ),
            [],
        )
        self.assertNotIn("tabs_thread", state)


class QuitDriverTests(unittest.TestCase):
    """_quit_driver runs on a bare quit thread: success and failure both
    end quietly, with the failure logged."""

    def test_quit_is_called_through(self):
        driver = mock.Mock()
        _quit_driver(driver)
        driver.quit.assert_called_once_with()

    def test_quit_failure_is_logged_and_swallowed(self):
        driver = mock.Mock()
        driver.quit.side_effect = RuntimeError("geckodriver went away")
        with self.assertLogs(level="ERROR") as logs:
            _quit_driver(driver)
        self.assertIn("Failed to quit webdriver cleanly", "\n".join(logs.output))
        self.assertIn("geckodriver went away", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
