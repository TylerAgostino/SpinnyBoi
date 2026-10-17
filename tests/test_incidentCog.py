# pyright: basic
"""Command-path tests for IncidentCog's /spincident poll.

Drives the command through its own callback: the poll body it posts (one
labelled line per penalty option, tagged with its regional-indicator
shortcode), the matching 🇦..🇭 reactions applied to the posted message, the
fallback answer when reaction application blows up, and the staff-or-
administrator check_any gate (invoked through the command's registered
check predicate, raising CheckAnyFailure for an under-permitted context).
The ReactionManager primitives themselves live in tests/test_reaction_manager.py;
here the manager is exercised only through the command. No network, no
Discord connection.
"""
import asyncio
import unittest
from typing import Any, List
from unittest.mock import MagicMock

import discord
from discord.ext import commands

from modules.incidentCog import IncidentCog
from modules.league_config import STAFF_ROLE_ID

# The eight options the poll must list, in order, letters a..h.
OPTIONS = [
    "No Action",
    "1 Point",
    "2 Points",
    "3 Points",
    "4 Points",
    "5 Points",
    "6 Points",
    "Other (Please explain below)",
]
LETTERS = ["a", "b", "c", "d", "e", "f", "g", "h"]


class _Message:
    """The slice of discord.Message the poll uses: a recording add_reaction."""

    def __init__(self):
        self.reactions: List[str] = []

    async def add_reaction(self, emoji: str):
        self.reactions.append(emoji)


class _ExplodingMessage(_Message):
    """A posted message whose reactions fail to apply, as on a locked thread."""

    async def add_reaction(self, emoji: str):
        raise RuntimeError("Missing Permissions")


class _Ctx:
    """The slice of discord.ApplicationContext spincident uses."""

    def __init__(self, message_factory=_Message):
        self.deferred = False
        self.responses: List[Any] = []
        self.messages: List[_Message] = []
        self._message_factory = message_factory

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))
        message = self._message_factory()
        self.messages.append(message)
        return message


def _texts(ctx: _Ctx) -> List[str]:
    return [args[0] for args, kwargs in ctx.responses if args]


def _spincident(cog: IncidentCog, ctx: _Ctx, **kwargs):
    asyncio.run(IncidentCog.spincident.callback(cog, ctx, **kwargs))


class _Fixture(unittest.TestCase):
    """Cog construction shared by the command-path tests."""

    def make_cog(self) -> IncidentCog:
        return IncidentCog(MagicMock())


class PollBodyTests(_Fixture):
    def test_poll_names_the_subject_and_lists_every_option_with_its_letter(self):
        cog = self.make_cog()
        ctx = _Ctx()
        _spincident(cog, ctx, subject="Pit lane speeding")

        self.assertTrue(ctx.deferred)
        body = _texts(ctx)[0]
        self.assertIn("Incident Poll: Pit lane speeding", body)
        for letter, option in zip(LETTERS, OPTIONS):
            self.assertIn(f":regional_indicator_{letter}: {option}", body)
        # Exactly eight option lines — nothing beyond the Other escape hatch.
        self.assertEqual(body.count(":regional_indicator_"), 8)
        # Singular for one point, plural above it.
        self.assertIn(":regional_indicator_b: 1 Point\n", body)
        self.assertIn(":regional_indicator_c: 2 Points", body)

    def test_poll_works_without_a_subject(self):
        cog = self.make_cog()
        ctx = _Ctx()
        _spincident(cog, ctx)
        self.assertIn("Incident Poll:", _texts(ctx)[0])
        self.assertEqual(ctx.messages[0].reactions, list("🇦🇧🇨🇩🇪🇫🇬🇭"))


class PollReactionTests(_Fixture):
    def test_reactions_applied_match_the_listed_options(self):
        cog = self.make_cog()
        ctx = _Ctx()
        _spincident(cog, ctx, subject="Blue flag incident")

        self.assertEqual(len(ctx.messages), 1)
        applied = ctx.messages[0].reactions
        self.assertEqual(applied, ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭"])

    def test_failed_reaction_application_falls_back_to_an_error_reply(self):
        cog = self.make_cog()
        ctx = _Ctx(message_factory=_ExplodingMessage)
        _spincident(cog, ctx, subject="Bus stop crash")

        self.assertEqual(ctx.messages[0].reactions, [])
        self.assertEqual(_texts(ctx)[-1], "Something went wrong.")


def _gate_ctx(admin: bool = False, staff: bool = False) -> MagicMock:
    """A context whose author is neither staff nor (unless asked) permitted."""
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


class PermissionGateTests(_Fixture):
    def test_non_staff_non_admin_is_rejected_by_the_command_check(self):
        with self.assertRaises(commands.CheckAnyFailure):
            asyncio.run(IncidentCog.spincident.checks[0](_gate_ctx()))

    def test_staff_role_satisfies_the_gate(self):
        self.assertTrue(
            asyncio.run(IncidentCog.spincident.checks[0](_gate_ctx(staff=True)))
        )

    def test_administrator_satisfies_the_gate(self):
        self.assertTrue(
            asyncio.run(IncidentCog.spincident.checks[0](_gate_ctx(admin=True)))
        )


if __name__ == "__main__":
    unittest.main()
