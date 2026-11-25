# pyright: basic
"""Edge-case tests for StandingsCog's pure catalog helpers and lazy SRH seam.

Pins the season-discovery catalog's degenerate shapes: primary_series_id
falling back to the first catalog row when no series owns the active season
(and None on an empty catalog), default_table_values returning no pre-selected
values when there is no primary series, table_specs skipping stored value
strings whose series id is non-numeric or unknown without dropping the good
entries around them, and _season_index building the SimRacerHubClient exactly
once with the league id from LeagueConfig while injected clients bypass
construction entirely. No network, no browser, no Discord connection.
"""
import unittest
from unittest import mock

from modules.league_config import LeagueConfig
from modules.simracerhub import SimRacerHubConfig
from modules.standingsCog import (
    LEAGUE_STATS_VALUE,
    StandingsCog,
    default_table_values,
    primary_series_id,
    table_specs,
)

# Main series owns the active season, so it is the primary without fallback.
CATALOG = {
    "status": "ok",
    "league_id": 7,
    "active_season_id": 29913,
    "series": [
        {"series_id": 8812, "series_name": "Beer League", "current_season_id": 29913},
        {"series_id": 13428, "series_name": "Beer League Am", "current_season_id": 28000},
    ],
}

# No series owns the active season: the first row becomes the primary.
NO_ACTIVE_MATCH = {
    "status": "ok",
    "league_id": 7,
    "active_season_id": 424242,
    "series": [
        {"series_id": 13428, "series_name": "Beer League Am", "current_season_id": 28000},
        {"series_id": 8812, "series_name": "Beer League", "current_season_id": 29913},
    ],
}

EMPTY_CATALOG = {"status": "ok", "league_id": 7, "active_season_id": None, "series": []}


class PrimarySeriesIdTests(unittest.TestCase):
    def test_active_season_owner_is_primary(self):
        self.assertEqual(primary_series_id(CATALOG), 8812)

    def test_falls_back_to_first_row_without_active_match(self):
        self.assertEqual(primary_series_id(NO_ACTIVE_MATCH), 13428)

    def test_falls_back_to_first_row_without_active_season(self):
        single = {"series": [CATALOG["series"][1]]}
        self.assertEqual(primary_series_id(single), 13428)

    def test_empty_catalog_has_no_primary(self):
        self.assertIsNone(primary_series_id(EMPTY_CATALOG))
        self.assertIsNone(primary_series_id({}))


class DefaultTableValuesTests(unittest.TestCase):
    def test_active_primary_pair_preselected(self):
        self.assertEqual(
            default_table_values(CATALOG),
            ["series:8812:driver", "series:8812:team"],
        )

    def test_fallback_primary_pair_preselected(self):
        self.assertEqual(
            default_table_values(NO_ACTIVE_MATCH),
            ["series:13428:driver", "series:13428:team"],
        )

    def test_no_primary_preselects_nothing(self):
        self.assertEqual(default_table_values(EMPTY_CATALOG), [])
        self.assertEqual(default_table_values({}), [])


class TableSpecsSkipTests(unittest.TestCase):
    def test_bad_series_ids_skipped_without_poisoning_good_entries(self):
        values = [
            "series:8812:driver",
            "series:abc:driver",  # non-int series id
            "",  # malformed stored value
            "bogus:8812:driver",  # right shape, wrong prefix
            "series:8812:foo",  # right shape, unknown suffix (else-branch trap)
            "series:8812",  # too few parts
            "series:8812:driver:x",  # too many parts
            "series:13428:team",
            "series:99999:driver",  # series unknown to the catalog
            LEAGUE_STATS_VALUE,
        ]
        specs = table_specs(values, CATALOG, 7)
        self.assertEqual(
            [spec[0] for spec in specs],
            [
                "Beer League Standings",
                "Beer League Am Team Standings",
                "League Stats",
            ],
        )
        team = specs[1]
        self.assertEqual(
            team[3],
            "https://www.simracerhub.com/scoring/season_standings.php?series_id=13428",
        )
        self.assertEqual(team[4], "Team Standings")

    def test_all_bad_values_yield_no_specs(self):
        self.assertEqual(table_specs(["series:x:driver", "series:99999:team"], CATALOG, 7), [])


class LazySeasonIndexTests(unittest.TestCase):
    def test_client_built_once_with_league_config_id(self):
        config = LeagueConfig.from_env({"SRH_LEAGUE_ID": "9999"})
        cog = StandingsCog(bot=mock.MagicMock(), config=config, srh=None)
        env_config = SimRacerHubConfig(league_id=42, max_rows=3)
        client_cls = mock.MagicMock()
        with mock.patch.object(
            SimRacerHubConfig, "from_env", classmethod(lambda cls, env=None: env_config)
        ), mock.patch("modules.standingsCog.SimRacerHubClient", client_cls):
            first = cog._season_index()
            second = cog._season_index()
        self.assertIs(first, client_cls.return_value)
        self.assertIs(second, first)
        client_cls.assert_called_once()
        built = client_cls.call_args.kwargs["config"]
        self.assertEqual(built.league_id, 9999)  # LeagueConfig overrides the env default
        self.assertEqual(built.max_rows, 3)  # the rest of the env config survives the replace

    def test_injected_client_never_triggers_construction(self):
        srh = mock.MagicMock()
        cog = StandingsCog(bot=mock.MagicMock(), config=LeagueConfig(), srh=srh)
        with mock.patch("modules.standingsCog.SimRacerHubClient") as client_cls:
            self.assertIs(cog._season_index(), srh)
        client_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
