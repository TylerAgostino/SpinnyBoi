# pyright: basic
"""Live integration tests: real HTTP against simracerhub.com scoring pages.

These exercise the actual endpoints (``search_suggest.php`` /
``get_standings.php``) through the real client — default urllib fetch, real
config, league default season. They catch upstream shape changes that the
offline fixtures (tests/test_simracerhub.py) drift from.

Skipped unless ``SIMRACERHUB_LIVE=1`` so the default suite stays offline:

    SIMRACERHUB_LIVE=1 .venv\\Scripts\\python -m unittest tests.test_simracerhub_live

The client self-polices (paced fetches, backoff retries, payload cache), so
a full run is a couple of requests spaced by the configured min interval.
"""
import os
import unittest

from modules.simracerhub import SimRacerHubClient, SimRacerHubConfig

LIVE = os.environ.get("SIMRACERHUB_LIVE", "") == "1"


@unittest.skipUnless(LIVE, "set SIMRACERHUB_LIVE=1 to hit the real endpoints")
class LiveSimRacerHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = SimRacerHubClient(config=SimRacerHubConfig.from_env({}))

    def test_standings_default_season(self):
        out = self.client.standings()
        self.assertEqual(out["status"], "ok", out.get("error"))
        rows = out["rows"]
        self.assertTrue(rows, "league season returned zero standings rows")
        self.assertEqual(rows[0]["pos"], 1)
        for row in rows:
            self.assertTrue(row["name"])
            self.assertIsInstance(row["points"], (int, float))
            self.assertIsInstance(row["rating"], (int, float))
        self.assertFalse(
            any(row["is_ai"] for row in rows),
            "default standings should exclude AI entries",
        )
        self.assertTrue(
            any(row["team"] for row in rows),
            "league uses teams but no standings row carries one",
        )

    def test_results_recent_season_races(self):
        # days=3650 keeps every past race in the window (the parser still
        # scans only the most recent scored weeks), so this can't rot as a
        # season ages out of a fixed 365-day window.
        out = self.client.results(days=3650)
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
        season = self.client.standings()
        self.assertEqual(season["status"], "ok", season.get("error"))
        out = self.client.resolve_driver(season["rows"][0]["name"])
        self.assertEqual(out["status"], "ok", out.get("error"))
        self.assertTrue(out["drivers"])
        self.assertTrue(
            all(d["driver_id"] and d["name"] for d in out["drivers"])
        )


if __name__ == "__main__":
    unittest.main()
