# pyright: basic
"""Background task-loop bodies, slash-command shells, the spin_*/generic_*
forwarding entry points (incl. the scheduler-facing new_message ones), the
filter-builder failure chain, and the defensive role lookup while listing."""

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from modules.scheduler.scheduler import ScheduledEvent
from modules.wheelCog import WheelCog, WheelUserError

CHANNEL_ID = 123456789012345678


def run_task_once(loop, cog):
    """Run exactly one iteration of a tasks.loop body against `cog`, the
    way the scheduled task itself does it, without starting the task."""
    return loop.__get__(cog, WheelCog)()


class TaskLoopCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_presets_tick_awaits_presets_refresh(self):
        presets = SimpleNamespace(refresh=AsyncMock())
        cog = SimpleNamespace(presets=presets)

        await run_task_once(WheelCog.refresh_presets, cog)

        presets.refresh.assert_awaited_once_with()

    async def test_check_scheduled_events_tick_awaits_scheduler_run_due(self):
        scheduler = SimpleNamespace(run_due=AsyncMock())
        cog = SimpleNamespace(scheduler=scheduler)

        await run_task_once(WheelCog.check_scheduled_events, cog)

        scheduler.run_due.assert_awaited_once_with()


class SlashCommandShellTests(unittest.IsolatedAsyncioTestCase):
    """Each /spin command shell hands its parsed options to the decorated
    entry point as exact keywords; nothing else."""

    async def test_wrap_custom_forwards_ctx_options_and_role_to_spin_custom(self):
        spin_custom = AsyncMock()
        cog = SimpleNamespace(spin_custom=spin_custom)
        ctx, options, role = object(), "sorry=3, not sorry=7", object()

        await WheelCog.wrap_custom.callback(cog, ctx, options, role)

        spin_custom.assert_awaited_once_with(
            ctx=ctx, custom_options=options, role=role
        )

    async def test_wrap_custom_defaults_role_to_none(self):
        spin_custom = AsyncMock()
        cog = SimpleNamespace(spin_custom=spin_custom)
        ctx, options = object(), "a,b"

        await WheelCog.wrap_custom.callback(cog, ctx, options)

        spin_custom.assert_awaited_once_with(
            ctx=ctx, custom_options=options, role=None
        )

    async def test_wrap_preset_forwards_preset_name_and_role_to_spin_preset(self):
        spin_preset = AsyncMock()
        cog = SimpleNamespace(spin_preset=spin_preset)
        ctx, role = object(), object()

        await WheelCog.wrap_preset.callback(cog, ctx, "Rotational", role)

        spin_preset.assert_awaited_once_with(
            ctx=ctx, preset_name="Rotational", role=role
        )

    async def test_wrap_spinfo_forwards_preset_and_tab_to_spinfo(self):
        spinfo = AsyncMock()
        cog = SimpleNamespace(spinfo=spinfo)
        ctx = object()

        await WheelCog.wrap_spinfo.callback(cog, ctx, "Rotational", "drivers")

        spinfo.assert_awaited_once_with(
            ctx=ctx, preset_name="Rotational", tab_name="drivers"
        )

    async def test_wrap_intermix_forwards_options_to_spintermix(self):
        spintermix = AsyncMock()
        cog = SimpleNamespace(spintermix=spintermix)
        ctx = object()

        await WheelCog.wrap_intermix.callback(cog, ctx, "a,b,c")

        spintermix.assert_awaited_once_with(ctx=ctx, custom_options="a,b,c")


class EntryForwardingTests(unittest.IsolatedAsyncioTestCase):
    """The wheel_command-decorated entry points hand the wrapper-provided
    ctx/bot_response/driver plus the user's own arguments straight to their
    generic_* implementation, interaction and new_message (scheduler) modes
    alike."""

    def _ctx(self):
        ctx = MagicMock(name="ctx")
        bot_response = AsyncMock(name="bot_response")
        ctx.defer = AsyncMock()
        ctx.respond = AsyncMock(return_value=bot_response)
        ctx.send = AsyncMock(return_value=bot_response)
        return ctx, bot_response

    async def _drive(self, entry_point, cog, ctx, *args, **kwargs):
        fake_webdriver = MagicMock(name="webdriver_module")
        with patch("modules.wheelCog.webdriver", fake_webdriver), patch(
            "modules.wheelCog.ChatHandler.working_on_it",
            new=AsyncMock(return_value="working..."),
        ):
            await entry_point(cog, ctx, *args, **kwargs)
            fake_webdriver.Firefox.assert_called_once_with(options=cog.driver_options)
        return fake_webdriver.Firefox.return_value

    async def test_spin_custom_entry_forwards_args_and_context_to_generic(self):
        cog = SimpleNamespace(
            driver_options="fake-options",
            generic_spin_custom=AsyncMock(return_value=("Spun.", None, None)),
        )
        ctx, bot_response = self._ctx()
        role = object()

        driver = await self._drive(
            WheelCog.spin_custom, cog, ctx, "sorry=3, not sorry", role=role
        )

        cog.generic_spin_custom.assert_awaited_once_with(
            "sorry=3, not sorry",
            role=role,
            ctx=ctx,
            bot_response=bot_response,
            driver=driver,
        )
        ctx.defer.assert_awaited_once_with()
        bot_response.edit.assert_awaited_once_with(content="Spun.")

    async def test_spin_custom_new_message_forwards_scheduler_kwargs_to_generic(self):
        cog = SimpleNamespace(
            driver_options="fake-options",
            generic_spin_custom=AsyncMock(return_value=("Spun.", None, None)),
        )
        ctx, bot_response = self._ctx()
        role = object()

        driver = await self._drive(
            WheelCog.spin_custom_new_message,
            cog,
            ctx,
            custom_options="a=2,b",
            role=role,
        )

        cog.generic_spin_custom.assert_awaited_once_with(
            custom_options="a=2,b",
            role=role,
            ctx=ctx,
            bot_response=bot_response,
            driver=driver,
        )
        ctx.send.assert_awaited_once_with("working...")
        ctx.defer.assert_not_awaited()
        bot_response.edit.assert_awaited_once_with(content="Spun.")

    async def test_spin_preset_entry_forwards_args_and_context_to_generic(self):
        cog = SimpleNamespace(
            driver_options="fake-options",
            generic_spin_preset=AsyncMock(return_value=("Spun.", None, None)),
        )
        ctx, bot_response = self._ctx()
        role = object()

        driver = await self._drive(
            WheelCog.spin_preset, cog, ctx, "Rotational", role=role
        )

        cog.generic_spin_preset.assert_awaited_once_with(
            "Rotational",
            role=role,
            ctx=ctx,
            bot_response=bot_response,
            driver=driver,
        )
        ctx.defer.assert_awaited_once_with()
        bot_response.edit.assert_awaited_once_with(content="Spun.")

    async def test_spin_preset_new_message_forwards_scheduler_kwargs_to_generic(self):
        cog = SimpleNamespace(
            driver_options="fake-options",
            generic_spin_preset=AsyncMock(return_value=("Spun.", None, None)),
        )
        ctx, bot_response = self._ctx()
        role = object()

        driver = await self._drive(
            WheelCog.spin_preset_new_message,
            cog,
            ctx,
            preset_name="Rotational",
            role=role,
        )

        cog.generic_spin_preset.assert_awaited_once_with(
            preset_name="Rotational",
            role=role,
            ctx=ctx,
            bot_response=bot_response,
            driver=driver,
        )
        ctx.send.assert_awaited_once_with("working...")
        ctx.defer.assert_not_awaited()
        bot_response.edit.assert_awaited_once_with(content="Spun.")


class _UnparseableFilterFragment:
    """A filter fragment the DSL cannot even start parsing: touching it in
    any way blows up."""

    def find(self, *needle):
        raise ValueError("filter fragment is not parseable")

    def strip(self, *args):
        raise ValueError("filter fragment is not parseable")

    def __contains__(self, item):
        raise ValueError("filter fragment is not parseable")

    def __getitem__(self, item):
        raise ValueError("filter fragment is not parseable")

    def __len__(self):
        raise ValueError("filter fragment is not parseable")

    def __str__(self):
        return "team=alpha="


class MalformedFilterString(str):
    """A filter string whose split hands the builder a fragment it cannot
    parse, standing in for genuinely malformed filter configuration."""

    def strip(self, chars=None):
        return self

    def lower(self):
        return self

    def split(self, *args, **kwargs):
        return [_UnparseableFilterFragment()]


class FilterBuilderFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unparseable_filter_becomes_chained_wheel_user_error(self):
        frame = pd.DataFrame({"fullname": ["Max"], "team": ["alpha"]})
        cog = SimpleNamespace(
            presets=SimpleNamespace(read_tab=AsyncMock(return_value=frame))
        )

        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(WheelUserError) as caught:
                await WheelCog.generate_option_set(
                    cog, "drivers", MalformedFilterString("team=alpha")
                )

        self.assertEqual(
            str(caught.exception), "One of your filters is not properly formatted"
        )
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertIn("Error processing filter", "\n".join(logs.output))


class ListingCtx:
    """The slice of discord.ApplicationContext list_scheduled_spins uses."""

    def __init__(self, get_role):
        self.guild = SimpleNamespace(get_role=get_role)
        self.responses = []

    async def defer(self):
        pass

    async def respond(self, content):
        self.responses.append(content)


class ListingRoleFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_lookup_failure_lists_the_spin_without_role_text(self):
        event = ScheduledEvent(
            id=9,
            timestamp=time.time() + 600,
            function_name="spin_preset",
            message_id=0,
            channel_id=CHANNEL_ID,
            data=json.dumps({"preset_name": "Rotational", "role_id": 55}),
        )
        cog = SimpleNamespace(
            scheduler=SimpleNamespace(all_active=lambda: [event])
        )
        get_role = MagicMock(side_effect=RuntimeError("guild unavailable"))
        ctx = ListingCtx(get_role=get_role)

        message, files, filename = await WheelCog.list_scheduled_spins.callback(
            cog, ctx
        )

        body = ctx.responses[-1]
        self.assertIn("**Preset:** Rotational", body)
        self.assertNotIn("Tag Role", body)
        get_role.assert_called_once_with(55)
        self.assertEqual((message, files, filename), ("Listed scheduled spins.", None, None))


if __name__ == "__main__":
    unittest.main()
