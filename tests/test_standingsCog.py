# pyright: basic
"""Command-path tests for StandingsCog's /standings view and capture seam.

Drives the slash command's ephemeral config view (its table options, the
channel picker, and the Fetch Standings button), the select callbacks' state
transitions, the submit callback's selection-to-capture mapping (URLs, element
selectors, tab clicks, target channel) and its results embed, the
_capture_and_send_table seam's channel post (text + PNG filename), and the
check-failure error handler. The Selenium/browser capture is replaced at the
_capture_table_png seam — these tests assert WHAT gets captured for which
selection and where it gets posted; they never render pixels or write files.
No network, no browser, no Discord connection.
"""
import asyncio
import io
import unittest
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

from modules.league_config import STAFF_ROLE_ID, LeagueConfig
from modules.standingsCog import StandingsCog, StandingsView

SOURCE_CHANNEL_ID = 5550001
OTHER_CHANNEL_ID = 5550002


class _Ctx:
    """The slice of discord.ApplicationContext the standings command uses."""

    def __init__(self, channel_id: int = SOURCE_CHANNEL_ID):
        self.responses: List[Any] = []
        self.channel = MagicMock()
        self.channel.id = channel_id

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))


class _Interaction:
    """The slice of discord.Interaction the view callbacks use."""

    def __init__(self):
        self.response = MagicMock()
        self.response.defer = AsyncMock()
        self.followups: List[Any] = []

        async def send(*args, **kwargs):
            self.followups.append((args, kwargs))

        self.followup = MagicMock()
        self.followup.send = AsyncMock(side_effect=send)


class _Select:
    """A select component as delivered mid-callback: values already chosen."""

    def __init__(self, values):
        self.values = values


def component_callback(view, component_attr: str):
    """The raw callback behind a view's decorated component, unwrapped."""
    return getattr(view, component_attr).callback.func


class _Fixture(unittest.TestCase):
    """Cog + view construction shared by the command-path tests."""

    def setUp(self):
        self.config = LeagueConfig()
        super().setUp()

    def make_cog(self) -> StandingsCog:
        self.bot = MagicMock()
        return StandingsCog(self.bot, self.config)

    def make_view(self, cog: StandingsCog, description: str = "",
                  channel_id: int = SOURCE_CHANNEL_ID) -> StandingsView:
        return StandingsView(cog, _Ctx(channel_id), description)

    def url(self, path: str, key: str, value: int) -> str:
        return f"https://www.simracerhub.com/scoring/{path}?{key}={value}"


class StandingsCommandTests(_Fixture):
    def test_standings_opens_the_config_view_ephemerally(self):
        cog = self.make_cog()
        ctx = _Ctx()
        asyncio.run(StandingsCog.standings.callback(cog, ctx, "race 4 recap"))

        (args, kwargs), = ctx.responses
        self.assertEqual(args, ("**Configure your standings fetch:**",))
        self.assertTrue(kwargs["ephemeral"])
        view = kwargs["view"]
        self.assertIsInstance(view, StandingsView)
        self.assertIs(view.cog, cog)
        self.assertEqual(view.description, "race 4 recap")
        self.assertEqual(view.selected_tables, [])
        self.assertIsNone(view.selected_channel)

    def test_view_offers_four_tables_then_a_channel_picker_then_submit(self):
        cog = self.make_cog()
        ctx = _Ctx()
        asyncio.run(StandingsCog.standings.callback(cog, ctx))
        view = ctx.responses[0][1]["view"]
        table_select, channel_select, button = view.children

        self.assertEqual(table_select.type, discord.ComponentType.string_select)
        self.assertEqual(table_select.placeholder, "Select standings tables to fetch...")
        self.assertEqual((table_select.min_values, table_select.max_values), (1, 4))
        self.assertEqual(
            [(o.label, o.value, o.default) for o in table_select.options],
            [
                ("Driver Standings", "driver_standings", True),
                ("Team Standings", "team_standings", True),
                ("League Stats", "league_stats", False),
                ("AM Standings", "am_standings", False),
            ],
        )

        self.assertEqual(channel_select.type, discord.ComponentType.channel_select)
        self.assertEqual(
            channel_select.placeholder, "Select channel (leave blank for this channel)"
        )
        self.assertEqual((channel_select.min_values, channel_select.max_values), (0, 1))
        self.assertIn(discord.ChannelType.text, channel_select.channel_types)
        self.assertIn(discord.ChannelType.news, channel_select.channel_types)

        self.assertEqual(button.label, "Fetch Standings")
        self.assertEqual(button.style, discord.ButtonStyle.primary)


class PermissionGateTests(_Fixture):
    def _gate_ctx(self, admin: bool = False, staff: bool = False) -> MagicMock:
        ctx = MagicMock()
        ctx.author = MagicMock()
        ctx.author.id = 111
        staff_role = MagicMock()
        staff_role.id = STAFF_ROLE_ID
        ctx.author.roles = [staff_role] if staff else []
        ctx.channel.permissions_for = MagicMock(
            return_value=discord.Permissions(administrator=True) if admin
            else discord.Permissions.none()
        )
        return ctx

    def test_non_staff_non_admin_is_rejected_by_the_command_check(self):
        with self.assertRaises(commands.CheckAnyFailure):
            asyncio.run(StandingsCog.standings.checks[0](self._gate_ctx()))

    def test_staff_role_satisfies_the_gate(self):
        self.assertTrue(
            asyncio.run(StandingsCog.standings.checks[0](self._gate_ctx(staff=True)))
        )

    def test_administrator_satisfies_the_gate(self):
        self.assertTrue(
            asyncio.run(StandingsCog.standings.checks[0](self._gate_ctx(admin=True)))
        )


class SelectCallbackTests(_Fixture):
    def test_table_select_stores_the_chosen_tables(self):
        async def run():
            cog = self.make_cog()
            view = self.make_view(cog)
            interaction = _Interaction()
            await component_callback(view, "table_select_callback")(
                view, _Select(["league_stats", "am_standings"]), interaction
            )
            self.assertEqual(view.selected_tables, ["league_stats", "am_standings"])
            interaction.response.defer.assert_awaited_once()

        asyncio.run(run())

    def test_channel_select_stores_the_picked_channel_id(self):
        picked = MagicMock()
        picked.id = OTHER_CHANNEL_ID

        async def run():
            cog = self.make_cog()
            view = self.make_view(cog)
            view.selected_channel = SOURCE_CHANNEL_ID
            interaction = _Interaction()
            await component_callback(view, "channel_select_callback")(
                view, _Select([picked]), interaction
            )
            self.assertEqual(view.selected_channel, OTHER_CHANNEL_ID)
            interaction.response.defer.assert_awaited_once()

        asyncio.run(run())

    def test_empty_channel_select_falls_back_to_no_selection(self):
        async def run():
            cog = self.make_cog()
            view = self.make_view(cog)
            view.selected_channel = OTHER_CHANNEL_ID
            await component_callback(view, "channel_select_callback")(
                view, _Select([]), _Interaction()
            )
            self.assertIsNone(view.selected_channel)

        asyncio.run(run())


class SubmitCallbackTests(_Fixture):
    def _prepared(self, selected_tables, selected_channel=None, description=""):
        """Run submit with the capture seam mocked; return view + interaction."""
        async def run():
            cog = self.make_cog()
            cog._capture_and_send_table = AsyncMock(return_value=True)
            view = self.make_view(cog, description)
            view.selected_tables = list(selected_tables)
            view.selected_channel = selected_channel
            interaction = _Interaction()
            await component_callback(view, "submit_callback")(
                view, None, interaction
            )
            return cog, view, interaction

        return asyncio.run(run())

    def test_defaults_capture_driver_and_team_into_the_source_channel(self):
        cog, view, interaction = self._prepared([])
        series = self.url("season_standings.php", "series_id", self.config.srh_series_id)
        calls = cog._capture_and_send_table.await_args_list
        self.assertEqual(len(calls), 2)
        driver, team = (c.kwargs for c in calls)
        self.assertEqual(driver["description"], "Driver Standings")
        self.assertEqual(driver["url"], series)
        self.assertEqual(driver["table_selector"], "standings-class-section")
        self.assertTrue(driver["use_class"])
        self.assertEqual(driver["tab_to_click"], "")
        self.assertEqual(driver["channel_id"], SOURCE_CHANNEL_ID)
        self.assertEqual(team["description"], "Team Standings")
        self.assertEqual(team["url"], series)
        self.assertEqual(team["tab_to_click"], "Team Standings")
        self.assertEqual(team["channel_id"], SOURCE_CHANNEL_ID)

        first_followup, embed_followup = interaction.followups
        self.assertEqual(
            first_followup[0], ("Fetching standings... This may take a moment.",)
        )
        self.assertTrue(first_followup[1]["ephemeral"])
        embed = embed_followup[1]["embed"]
        self.assertTrue(embed_followup[1]["ephemeral"])
        self.assertEqual(embed.title, "Standings Update Results")
        self.assertEqual(embed.colour, discord.Color.green())
        self.assertEqual(
            [(f.name, f.value) for f in embed.fields],
            [
                ("Driver Standings", "✅ Success"),
                ("Team Standings", "✅ Success"),
                ("Posted to Channel", f"<#{SOURCE_CHANNEL_ID}>"),
            ],
        )

    def test_selection_maps_each_table_to_its_url_and_the_chosen_channel(self):
        cog, view, interaction = self._prepared(
            ["league_stats", "am_standings"], selected_channel=OTHER_CHANNEL_ID
        )
        calls = cog._capture_and_send_table.await_args_list
        self.assertEqual(len(calls), 2)
        league, am = (c.kwargs for c in calls)
        self.assertEqual(
            league["url"],
            self.url("league_stats.php", "season_id", self.config.srh_season_id),
        )
        self.assertEqual(league["table_selector"], "react-table")
        self.assertEqual(
            am["url"],
            self.url("season_standings.php", "series_id", self.config.srh_am_series_id),
        )
        self.assertEqual(league["channel_id"], OTHER_CHANNEL_ID)
        self.assertEqual(am["channel_id"], OTHER_CHANNEL_ID)
        embed = interaction.followups[-1][1]["embed"]
        self.assertIn(
            ("Posted to Channel", f"<#{OTHER_CHANNEL_ID}>"),
            [(f.name, f.value) for f in embed.fields],
        )

    def test_description_rides_along_to_the_capture_and_the_embed(self):
        cog, view, interaction = self._prepared(
            ["driver_standings"], description="race 4 recap"
        )
        call = cog._capture_and_send_table.await_args_list[0].kwargs
        self.assertEqual(call["additional_description"], "race 4 recap")
        embed = interaction.followups[-1][1]["embed"]
        self.assertIn(
            ("Description", "race 4 recap"),
            [(f.name, f.value) for f in embed.fields],
        )

    def test_failed_capture_is_reported_as_failed_in_the_embed(self):
        async def run():
            cog = self.make_cog()
            cog._capture_and_send_table = AsyncMock(side_effect=[True, False])
            view = self.make_view(cog)
            interaction = _Interaction()
            await component_callback(view, "submit_callback")(view, None, interaction)
            return interaction

        interaction = asyncio.run(run())
        embed = interaction.followups[-1][1]["embed"]
        self.assertEqual(embed.colour, discord.Color.orange())
        self.assertEqual(
            [(f.name, f.value) for f in embed.fields],
            [
                ("Driver Standings", "✅ Success"),
                ("Team Standings", "❌ Failed"),
                ("Posted to Channel", f"<#{SOURCE_CHANNEL_ID}>"),
            ],
        )

    def test_unmapped_selection_answers_without_any_capture(self):
        cog, view, interaction = self._prepared(["bogus_table"])
        self.assertEqual(cog._capture_and_send_table.await_count, 0)
        self.assertEqual(len(interaction.followups), 1)
        (args, kwargs), = interaction.followups
        self.assertEqual(
            args, ("No tables selected. Please select at least one table.",)
        )
        self.assertTrue(kwargs["ephemeral"])


class CaptureSeamTests(_Fixture):
    """The real _capture_and_send_table with only the browser capture faked."""

    def test_capture_posts_named_png_and_message_to_the_target_channel(self):
        async def run():
            cog = self.make_cog()
            captured = io.BytesIO(b"fake-png")
            cog._capture_table_png = MagicMock(return_value=captured)
            channel = MagicMock()
            channel.send = AsyncMock()
            self.bot.fetch_channel = AsyncMock(return_value=channel)
            ok = await cog._capture_and_send_table(
                url="http://example/table",
                table_selector="standings-class-section",
                channel_id=OTHER_CHANNEL_ID,
                description="Driver Standings",
                additional_description="race 4 recap",
                use_class=True,
                tab_to_click="Team Standings",
            )
            return ok, cog, channel, captured

        ok, cog, channel, captured = asyncio.run(run())
        self.assertTrue(ok)
        self.bot.fetch_channel.assert_awaited_once_with(OTHER_CHANNEL_ID)
        channel.send.assert_awaited_once()
        (text,), file_kwarg = channel.send.await_args
        self.assertEqual(text, "**Driver Standings**\nrace 4 recap")
        self.assertIs(file_kwarg["file"].fp, captured)
        self.assertEqual(file_kwarg["file"].filename, "Driver_Standings.png")
        # The browser seam is asked for exactly this page/selector/tab.
        cog._capture_table_png.assert_called_once_with(
            "http://example/table", "standings-class-section", True,
            "Team Standings", "Driver Standings",
        )

    def test_unconfigured_channel_short_circuits_before_any_capture(self):
        async def run():
            cog = self.make_cog()
            cog._capture_table_png = MagicMock()
            return await cog._capture_and_send_table(
                url="http://example/table",
                table_selector="t",
                channel_id=0,
                description="Team Standings",
            )

        self.assertFalse(asyncio.run(run()))
        self.bot.fetch_channel.assert_not_called()

    def test_capture_failure_never_reaches_the_channel(self):
        async def run():
            cog = self.make_cog()
            cog._capture_table_png = MagicMock(side_effect=RuntimeError("driver died"))
            ok = await cog._capture_and_send_table(
                url="http://example/table",
                table_selector="t",
                channel_id=OTHER_CHANNEL_ID,
                description="League Stats",
            )
            return ok

        self.assertFalse(asyncio.run(run()))
        self.bot.fetch_channel.assert_not_called()


class ErrorPresenterTests(_Fixture):
    def test_check_failure_is_answered_privately(self):
        async def run():
            cog = self.make_cog()
            ctx = _Ctx()
            await StandingsCog.info_error(cog, ctx, commands.CheckFailure())
            return ctx

        ctx = asyncio.run(run())
        (args, kwargs), = ctx.responses
        self.assertEqual(args, ("You do not have permission to use this command.",))
        self.assertTrue(kwargs["ephemeral"])

    def test_other_errors_are_left_unanswered(self):
        async def run():
            cog = self.make_cog()
            ctx = _Ctx()
            await StandingsCog.info_error(
                cog, ctx, commands.CommandInvokeError(ValueError("boom"))
            )
            return ctx

        ctx = asyncio.run(run())
        self.assertEqual(ctx.responses, [])


if __name__ == "__main__":
    unittest.main()
