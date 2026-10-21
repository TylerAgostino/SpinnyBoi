"""Tests for the LeagueConfig seam: one source for every season fact.

Covers env parsing (override, empty-as-unset, garbage fallback), the
frozen-by-design default, and that cogs actually read season facts from
the config they are handed instead of buried defaults.
"""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.league_config import (
    DEFAULT_DEBUG_GUILD_IDS,
    DEFAULT_DRIVER_ROLE_ID,
    DEFAULT_SEASON,
    DEFAULT_SRH_LEAGUE_ID,
    LeagueConfig,
)

CONFIG_ENV = [
    "LEAGUE_SEASON",
    "LEAGUE_DRIVER_ROLE_ID",
    "LEAGUE_REGISTRATION_SHEET",
    "LEAGUE_POINTS_SHEET",
    "LEAGUE_POINTS_ID_COLUMN",
    "IRACING_LEAGUE_ID",
    "SRH_LEAGUE_ID",
    "LEAGUE_DEBUG_GUILD_IDS",
    "LEAGUE_AUTO_DELETE_CHANNEL_ID",
]


def _clear_env(test):
    old = {name: os.environ.get(name) for name in CONFIG_ENV}
    for name in CONFIG_ENV:
        os.environ.pop(name, None)

    def restore():
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    return restore


class LeagueConfigDefaultsTests(unittest.TestCase):
    def test_defaults_match_current_production_values(self):
        cfg = LeagueConfig.from_env()
        self.assertEqual(cfg.season, 16)
        self.assertEqual(cfg.driver_role_id, 1551759071533858838)
        self.assertEqual(cfg.registration_sheet_name, "SpinnyBoiRegistrations")
        self.assertEqual(cfg.points_sheet_name, "Points Entry S15-17")
        self.assertEqual(cfg.points_iracing_id_column, "iRacing ID")
        self.assertEqual(cfg.iracing_league_id, 8579)
        self.assertEqual(cfg.srh_league_id, DEFAULT_SRH_LEAGUE_ID)
        self.assertEqual(cfg.debug_guild_ids, (420037391454044171, 981935710514839572))
        self.assertEqual(cfg.auto_delete_channel_id, 1362287075142930442)


class LeagueConfigEnvTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_clear_env(self))

    def test_env_overrides(self):
        os.environ["LEAGUE_SEASON"] = "17"
        os.environ["SRH_LEAGUE_ID"] = "9999"
        os.environ["LEAGUE_POINTS_SHEET"] = "Points Entry S17-19"
        cfg = LeagueConfig.from_env()
        self.assertEqual(cfg.season, 17)
        self.assertEqual(cfg.srh_league_id, 9999)
        self.assertEqual(cfg.points_sheet_name, "Points Entry S17-19")

    def test_empty_string_means_unset(self):
        # docker compose passes unset host vars through as empty strings
        os.environ["LEAGUE_SEASON"] = ""
        os.environ["SRH_LEAGUE_ID"] = "  "
        cfg = LeagueConfig.from_env()
        self.assertEqual(cfg.season, DEFAULT_SEASON)
        self.assertEqual(cfg.srh_league_id, DEFAULT_SRH_LEAGUE_ID)

    def test_garbage_int_falls_back_to_default(self):
        os.environ["LEAGUE_SEASON"] = "sixteen"
        os.environ["LEAGUE_DRIVER_ROLE_ID"] = "12ab"
        cfg = LeagueConfig.from_env()
        self.assertEqual(cfg.season, DEFAULT_SEASON)
        self.assertEqual(cfg.driver_role_id, DEFAULT_DRIVER_ROLE_ID)

    def test_debug_guild_ids_parse_comma_list(self):
        os.environ["LEAGUE_DEBUG_GUILD_IDS"] = "111, 222 ,333"
        cfg = LeagueConfig.from_env()
        self.assertEqual(cfg.debug_guild_ids, (111, 222, 333))

    def test_debug_guild_ids_garbage_falls_back(self):
        os.environ["LEAGUE_DEBUG_GUILD_IDS"] = "111,abc"
        cfg = LeagueConfig.from_env()
        self.assertEqual(cfg.debug_guild_ids, DEFAULT_DEBUG_GUILD_IDS)

    def test_config_is_frozen(self):
        cfg = LeagueConfig()
        with self.assertRaises(AttributeError):
            cfg.season = 99


class CogConsumesConfigTests(unittest.TestCase):
    """The attendance cog must derive its sheet/season/column facts from the
    config it is handed — that is the whole point of the seam."""

    def _make_cog(self, config):
        from modules.attendanceCog import AttendanceCog

        return AttendanceCog(MagicMock(), config, sheets=MagicMock())

    def _callback(self, cog, name):
        fn = getattr(cog.__class__, name)
        callback = getattr(fn, "callback", fn)
        return lambda *args, **kwargs: callback(cog, *args, **kwargs)

    def test_season_sheet_and_id_column_default_from_config(self):
        import asyncio

        cfg = LeagueConfig(
            season=17,
            points_sheet_name="My Points",
            points_iracing_id_column="Player ID",
        )
        cog = self._make_cog(cfg)
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.respond = AsyncMock()
        marked = MagicMock(
            return_value={"updated": 0, "matched": 0, "unmatched": 0, "unmatched_ids": []}
        )
        fetch = MagicMock(return_value={123: {"cust_id": 123}})
        markattendance = self._callback(cog, "markattendance")
        with patch.object(cog, "_fetch_race_finishers", fetch), patch.object(
            cog, "_mark_attendance_in_sheet", marked
        ):
            asyncio.run(markattendance(ctx, subsession_id=1, round=2))
        kwargs = marked.call_args.kwargs
        self.assertEqual(kwargs["target_column"], "S17R2")
        self.assertEqual(kwargs["sheet_name"], "My Points")
        self.assertEqual(kwargs["iracing_id_column"], "Player ID")

    def test_explicit_season_option_wins_over_config(self):
        import asyncio

        cfg = LeagueConfig(season=17)
        cog = self._make_cog(cfg)
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.respond = AsyncMock()
        marked = MagicMock(
            return_value={"updated": 0, "matched": 0, "unmatched": 0, "unmatched_ids": []}
        )
        fetch = MagicMock(return_value={123: {"cust_id": 123}})
        markattendance = self._callback(cog, "markattendance")
        with patch.object(cog, "_fetch_race_finishers", fetch), patch.object(
            cog, "_mark_attendance_in_sheet", marked
        ):
            asyncio.run(markattendance(ctx, subsession_id=1, round=2, season=15))
        self.assertEqual(marked.call_args.kwargs["target_column"], "S15R2")

    def test_default_sheet_attr_is_gone(self):
        cog = self._make_cog(LeagueConfig())
        self.assertFalse(hasattr(cog, "default_sheet_name"))


if __name__ == "__main__":
    unittest.main()
