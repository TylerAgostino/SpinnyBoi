# pyright: basic
"""Live integration tests: real HTTP against simracerhub.com scoring pages.

These exercise the actual endpoints (``league_series.php`` /
``series_seasons.php`` / ``search_suggest.php`` / ``get_standings.php``)
through the real client — default urllib fetch, real config, league id only.
Season discovery must yield the league's series with the active season
flagged; standings then run against the discovered active season by NAME.
They catch upstream shape changes that the offline fixtures
(tests/test_simracerhub.py) drift from.

Skipped unless ``SIMRACERHUB_LIVE=1`` so the default suite stays offline:

    SIMRACERHUB_LIVE=1 .venv\\Scripts\\python -m unittest tests.test_simracerhub_live

The client self-polices (paced fetches, backoff retries, payload cache), so
a full run is a handful of requests spaced by the configured min interval.
"""
import os
import unittest

import pytest

from modules.simracerhub import SimRacerHubClient, SimRacerHubConfig

LIVE = os.environ.get("SIMRACERHUB_LIVE", "") == "1"


@pytest.mark.live
@unittest.skipUnless(LIVE, "set SIMRACERHUB_LIVE=1 to hit the real endpoints")
class LiveSimRacerHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = SimRacerHubClient(config=SimRacerHubConfig.from_env({}))
        catalog = cls.client.seasons()
        cls.catalog = catalog
        active = [s for s in catalog.get("series", []) if s["active"]]
        cls.active_series = active[0] if active else None

    def test_season_discovery(self):
        out = self.catalog
        self.assertEqual(out["status"], "ok", out.get("error"))
        self.assertTrue(out["series"], "league has no series on SRH?")
        for entry in out["series"]:
            self.assertTrue(entry["series_id"] and entry["series_name"])
        active = [s for s in out["series"] if s["active"]]
        self.assertTrue(active, "no series flagged active on SRH?")
        for entry in active:
            self.assertTrue(
                entry["current_season_id"],
                f"active series {entry['series_name']} has no current season",
            )
        current_ids = {s["current_season_id"] for s in active}
        if len(current_ids) == 1:
            self.assertEqual(out["active_season_id"], current_ids.pop())
        else:
            # main + AM running different seasons: no single default, by design
            self.assertIsNone(out["active_season_id"])
        for season in out["seasons"]:
            self.assertTrue(season["season_id"] and season["season_name"])

    def test_default_standings_resolves_or_asks_via_active_seasons(self):
        # Unqualified standings must either land on the one active season or
        # answer ambiguous_season listing every active (series, season) —
        # never silently guess between two live championships.
        out = self.client.standings()
        active = [s for s in self.catalog["series"] if s["active"]]
        if len(active) == 1:
            self.assertEqual(out["status"], "ok", out.get("error"))
            self.assertTrue(out["rows"])
            return
        self.assertEqual(out["status"], "ambiguous_season")
        self.assertEqual(out["rows"], [])
        listed = {o["series_name"] for o in out["seasons"]}
        for series in active:
            self.assertIn(series["series_name"], listed)

    def test_standings_of_a_named_series_active_season(self):
        # Select a specific series' current season BY SEASON ID: names like
        # "Season 17" can be ambiguous across main/AM, ids never are.
        if not self.active_series:
            self.skipTest("league has no active series on SRH")
        out = self.client.standings(season=self.active_series["current_season_id"])
        self.assertEqual(out["status"], "ok", out.get("error"))
        rows = out["rows"]
        self.assertTrue(rows, "active season returned zero standings rows")
        self.assertEqual(rows[0]["pos"], 1)
        for row in rows:
            self.assertTrue(row["name"])
            self.assertIsInstance(row["points"], (int, float))
            self.assertIsInstance(row["rating"], (int, float))
        self.assertFalse(
            any(row["is_ai"] for row in rows),
            "standings should exclude AI entries",
        )
        self.assertTrue(
            any(row["team"] for row in rows),
            "league uses teams but no standings row carries one",
        )

    def test_results_active_season_races(self):
        # days=3650 keeps every past race in the window (the parser still
        # scans only the most recent scored weeks), so this can't rot as a
        # season ages out of a fixed 365-day window.
        if not self.active_series:
            self.skipTest("league has no active series on SRH")
        out = self.client.results(
            season=self.active_series["current_season_id"], days=3650
        )
        self.assertEqual(out["status"], "ok", out.get("error"))
        rows = out["rows"]
        self.assertTrue(rows, "no race results parsed from live payload")
        for row in rows:
            self.assertTrue(row["name"], "result row lost its driver name")
            self.assertIsNotNone(row["finish_pos"])
            self.assertTrue(row["track"], "result row lost its track name")
        self.assertTrue(
            any(row["team"] for row in rows),
            "league uses teams but no result row carries one",
        )

    def test_resolve_driver(self):
        # Resolve a driver that actually exists on the league sheet instead
        # of hardcoding anyone's real name into this repo.
        if not self.active_series:
            self.skipTest("league has no active series on SRH")
        season = self.client.standings(season=self.active_series["current_season_id"])
        self.assertEqual(season["status"], "ok", season.get("error"))
        out = self.client.resolve_driver(season["rows"][0]["name"])
        self.assertEqual(out["status"], "ok", out.get("error"))
        self.assertTrue(out["drivers"])
        self.assertTrue(
            all(d["driver_id"] and d["name"] for d in out["drivers"])
        )


if __name__ == "__main__":
    unittest.main()
