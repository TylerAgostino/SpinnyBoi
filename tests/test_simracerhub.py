"""Unit tests for the SimRacerHub stats client (modules/simracerhub.py).

No network: the client takes an injected ``fetch_json`` callable, and the
parsers/URL builders are pure functions tested against a trimmed fixture that
mirrors the real get_standings.php payload shape (string numerics, dict-keyed
maps).

Run: python -m unittest tests.test_simracerhub
"""
import socket
import unittest
from unittest.mock import MagicMock, patch

from modules.simracerhub import (
    SimRacerHubClient,
    SimRacerHubConfig,
    build_suggest_url,
    build_standings_url,
    clean_driver_filter,
    driver_matches,
    normalize_days,
    normalize_results,
    normalize_standings,
)

NOW = 1_789_600_000  # fixed clock for time-filter tests
DAY = 86400

SAMPLE_PAYLOAD = {
    "drivers": {
        "48019": {"driver_id": "48019", "name": "Austin Tucker2", "last_first": "Tucker2, Austin", "is_ai": False},
        "16262": {"driver_id": "16262", "name": "Jordan Babcock", "last_first": "Babcock, Jordan", "is_ai": False},
        "99999": {"driver_id": "99999", "name": "Robot Racer", "last_first": "Racer, Robot", "is_ai": True},
    },
    "rps": {
        "48019": {
            "drid": "48019", "name": "Tucker2, Austin", "starts": "9", "counted": "7",
            "tpts": "443", "rpts": "405", "bpts": "38", "wins": "1", "poles": "1",
            "t3": "6", "t5": "6", "t10": "8", "laps": "266", "inc": "141",
            "rat": "1003.7587", "races": {"379770": {"race_points": "56"}},
        },
        "16262": {
            "drid": "16262", "name": "Babcock, Jordan", "starts": "8", "counted": "7",
            "tpts": "500", "wins": "3", "poles": "0", "t3": "7", "t5": "7", "t10": "8",
            "laps": "260", "inc": "40", "rat": "990.5",
        },
        "99999": {
            "drid": "99999", "name": "Racer, Robot", "starts": "2", "counted": "2",
            "tpts": "10", "wins": "0", "poles": "0", "t3": "0", "t5": "0", "t10": "1",
            "laps": "50", "inc": "90", "rat": "850",
        },
    },
    "tracks": {
        "17": {"track_name": "Homestead Miami Speedway", "name": "Homestead Miami Speedway Road Course B", "config_name": "Road Course B"},
        "55": {"track_name": "Virginia International Raceway", "name": " VIR Full Course", "config_name": "Full Course"},
    },
    "schedules": [
        {
            "schedule_id": "367869", "season_id": "29913", "race_date": str(NOW - 120 * DAY),
            "config_id": "55", "chase": "N",
            "drivers": {
                "48019": {"driver_id": "48019", "race_id": "366881", "qualify_pos": "3", "num_laps": "20", "laps_led": "5", "finish_pos": "2", "race_points": "48", "incidents": "12", "status": "Running", "rating": "110.2"},
            },
        },
        {
            "schedule_id": "375051", "season_id": "29913", "race_date": str(NOW - 7 * DAY),
            "config_id": "17", "chase": "N",
            "drivers": {
                "48019": {"driver_id": "48019", "race_id": "378500", "qualify_pos": "11", "num_laps": "7", "laps_led": "2", "finish_pos": "1", "race_points": "56", "incidents": "38", "status": "Running", "rating": "128.722"},
                "16262": {"driver_id": "16262", "race_id": "378500", "qualify_pos": "1", "num_laps": "7", "laps_led": "0", "finish_pos": "4", "race_points": "33", "incidents": "14", "status": "Running", "rating": "99"},
            },
        },
        {
            "schedule_id": "375811", "season_id": "29913", "race_date": str(NOW + 7 * DAY),
            "config_id": "17", "chase": "N",
            "drivers": {},  # scheduled, not yet run
        },
    ],
}


def make_client(fetch=None, config=None, clock_values=None):
    fetch = fetch if fetch is not None else MagicMock(return_value=SAMPLE_PAYLOAD)
    if clock_values:
        values = list(clock_values)
        clock = MagicMock(side_effect=lambda: values.pop(0) if values else 0.0)
    else:
        ticks = {"n": 0}

        def clock():
            now = ticks["n"] * 1000.0  # generous spacing; rate-limit tests pass explicit clocks
            ticks["n"] += 1
            return now
    client = SimRacerHubClient(config or SimRacerHubConfig(), fetch_json=fetch, clock=clock)
    return client


class QueryFormattingTests(unittest.TestCase):
    def test_clean_driver_filter(self):
        self.assertEqual(clean_driver_filter("  tucker   bragg  "), "tucker bragg")
        self.assertEqual(clean_driver_filter(None), "")
        self.assertEqual(len(clean_driver_filter("a" * 200)), 60)

    def test_driver_matches(self):
        self.assertTrue(driver_matches("Austin Tucker2", "tucker"))
        self.assertTrue(driver_matches("Tucker Bragg", "TUCKER BRAGG"))
        self.assertTrue(driver_matches("Austin Tucker2", "austint2".replace("t2", "tucker2")))
        self.assertFalse(driver_matches("Jordan Babcock", "tucker"))
        self.assertTrue(driver_matches("Anyone", ""))

    def test_normalize_days(self):
        self.assertEqual(normalize_days(None, 14), 14)
        self.assertEqual(normalize_days("7", 14), 7)
        self.assertEqual(normalize_days("30d", 14), 30)
        self.assertEqual(normalize_days(True, 14), 14)
        self.assertEqual(normalize_days(-5, 14), 0)
        self.assertEqual(normalize_days(99999, 14), 3650)
        self.assertEqual(normalize_days("junk", 14), 14)
        self.assertEqual(normalize_days(7, 14), 7)


class UrlBuilderTests(unittest.TestCase):
    def test_standings_url(self):
        url = build_standings_url(29913)
        self.assertTrue(url.startswith("https://www.simracerhub.com/scoring/get_standings.php?"))
        self.assertIn("season_id=29913", url)
        self.assertNotIn("class_id", url)

    def test_standings_url_with_class(self):
        self.assertIn("class_id=7", build_standings_url(29913, 7))

    def test_suggest_url(self):
        url = build_suggest_url("Tucker Bragg")
        self.assertIn("search_suggest.php", url)
        self.assertIn("q=Tucker+Bragg", url)


class StandingsParsingTests(unittest.TestCase):
    def test_sorted_by_points_and_positioned(self):
        rows = normalize_standings(SAMPLE_PAYLOAD)
        self.assertEqual([r["name"] for r in rows], ["Jordan Babcock", "Austin Tucker2", "Robot Racer"])
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3])

    def test_numeric_coercion_and_inc_rate(self):
        row = next(r for r in normalize_standings(SAMPLE_PAYLOAD) if r["name"] == "Austin Tucker2")
        self.assertEqual(row["points"], 443)
        self.assertEqual(row["wins"], 1)
        self.assertEqual(row["rating"], 1003.7587)
        self.assertEqual(row["inc_per_race"], round(141 / 9, 2))
        self.assertIs(row["is_ai"], False)

    def test_driver_filter(self):
        rows = normalize_standings(SAMPLE_PAYLOAD, driver="tucker")
        self.assertEqual([r["name"] for r in rows], ["Austin Tucker2"])

    def test_max_rows(self):
        self.assertEqual(len(normalize_standings(SAMPLE_PAYLOAD, max_rows=2)), 2)

    def test_empty_and_malformed(self):
        self.assertEqual(normalize_standings({}), [])
        self.assertEqual(normalize_standings({"rps": {"x": None}, "drivers": {}}), [])


class ResultsParsingTests(unittest.TestCase):
    def test_future_weeks_excluded_newest_first(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW)
        # the +7d week has no drivers anyway; last week (Homestead) has 2 finishers
        self.assertEqual(len(rows), 3)
        self.assertGreater(rows[0]["race_date"], rows[-1]["race_date"] - 1)
        first = rows[0]
        self.assertEqual(first["track"], "Homestead Miami Speedway")
        self.assertEqual(first["finish_pos"], 1)  # sorted by finish within week
        self.assertEqual(first["name"], "Austin Tucker2")

    def test_days_filter_excludes_old_weeks(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW, days=30)
        self.assertTrue(all(r["race_date"] >= NOW - 30 * DAY for r in rows))
        self.assertEqual([r["track"] for r in rows].count("Virginia International Raceway"), 0)
        self.assertEqual(len(rows), 2)

    def test_days_filter_includes_old_weeks_when_wide(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW, days=365)
        self.assertEqual(len(rows), 3)

    def test_driver_filter(self):
        rows = normalize_results(SAMPLE_PAYLOAD, driver="babcock", now_epoch=NOW)
        self.assertEqual([r["name"] for r in rows], ["Jordan Babcock"])

    def test_max_weeks_bounds_scan(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW, days=365, max_weeks=1)
        self.assertTrue(all(r["track"] == "Homestead Miami Speedway" for r in rows))

    def test_empty(self):
        self.assertEqual(normalize_results({}, now_epoch=NOW), [])


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = SimRacerHubConfig.from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.timeout_seconds, 8.0)
        self.assertEqual(cfg.max_rows, 15)
        self.assertIsNone(cfg.default_season_id)

    def test_disabled_flag(self):
        for value in ("0", "false", "OFF"):
            self.assertFalse(SimRacerHubConfig.from_env({"SIMRACERHUB_ENABLED": value}).enabled)

    def test_clamping_and_bad_values(self):
        cfg = SimRacerHubConfig.from_env(
            {"SIMRACERHUB_MAX_ROWS": "999", "SIMRACERHUB_TIMEOUT_SECONDS": "junk", "SIMRACERHUB_SEASON_ID": "29913"}
        )
        self.assertEqual(cfg.max_rows, 50)
        self.assertEqual(cfg.timeout_seconds, 8.0)
        self.assertEqual(cfg.default_season_id, 29913)


class ClientTests(unittest.TestCase):
    def test_standings_success(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        client = make_client(fetch)
        out = client.standings(season_id=29913)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["season_id"], 29913)
        self.assertEqual(out["rows"][0]["name"], "Jordan Babcock")
        url, timeout = fetch.call_args[0]
        self.assertIn("get_standings.php", url)
        self.assertEqual(timeout, 8.0)

    def test_results_success_passes_days(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        client = make_client(fetch)
        out = client.results(season_id=29913, driver="tucker", days=365)
        self.assertEqual(out["status"], "ok")
        self.assertEqual({r["name"] for r in out["rows"]}, {"Austin Tucker2"})

    def test_default_season_from_config(self):
        client = make_client(config=SimRacerHubConfig(default_season_id=123))
        out = client.standings()
        self.assertEqual(out["season_id"], 123)

    def test_no_season_configured(self):
        out = make_client().standings()
        self.assertEqual(out["status"], "no_season")
        self.assertEqual(out["rows"], [])

    def test_api_error_payload_handled(self):
        fetch = MagicMock(return_value={"error": "Season ID 999999999 not found "})
        out = make_client(fetch).standings(season_id=999999999)
        self.assertEqual(out["status"], "no_data")
        self.assertIn("not found", out["error"])

    def test_network_error_handled(self):
        fetch = MagicMock(side_effect=ConnectionError("boom"))
        out = make_client(fetch).results(season_id=1)
        self.assertEqual(out, {"status": "error", "rows": []})

    def test_timeout_handled(self):
        fetch = MagicMock(side_effect=socket.timeout("timed out"))
        out = make_client(fetch).standings(season_id=1)
        self.assertEqual(out["status"], "error")

    def test_non_dict_payload(self):
        out = make_client(MagicMock(return_value=[])).standings(season_id=1)
        self.assertEqual(out["status"], "error")

    def test_rate_limiting(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        client = make_client(fetch, clock_values=[100.0, 101.0])
        self.assertEqual(client.standings(season_id=1)["status"], "ok")
        second = client.results(season_id=1)
        self.assertEqual(second["status"], "rate_limited")
        self.assertEqual(fetch.call_count, 1)

    def test_disabled(self):
        fetch = MagicMock()
        client = SimRacerHubClient(SimRacerHubConfig(enabled=False), fetch_json=fetch)
        self.assertEqual(client.standings(season_id=1)["status"], "disabled")
        fetch.assert_not_called()

    def test_resolve_driver(self):
        fetch = MagicMock(return_value={"drivers": [{"id": 71940, "name": "Tucker Bragg", "is_ai": False}], "series": []})
        out = make_client(fetch).resolve_driver("Tucker")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["drivers"], [{"driver_id": 71940, "name": "Tucker Bragg"}])

    def test_resolve_driver_empty_query(self):
        fetch = MagicMock()
        out = make_client(fetch).resolve_driver("   ")
        self.assertEqual(out["status"], "empty_query")
        fetch.assert_not_called()


class ToolRegistrationTests(unittest.TestCase):
    def test_tool_has_spec(self):
        tool = make_client().stats_tool()
        spec = tool.tool_spec
        self.assertEqual(spec["name"], "get_simracerhub_stats")
        self.assertIn("kind", str(spec["inputSchema"]))

    def test_tool_kind_routing(self):
        client = make_client()
        tool = client.stats_tool()
        self.assertEqual(tool(kind="Standings", season_id=1)["status"], "ok")
        self.assertEqual(tool(kind="races", season_id=1, days=3650)["status"], "ok")

    def test_tool_bad_kind(self):
        tool = make_client().stats_tool()
        out = tool(kind="vibes")
        self.assertEqual(out["status"], "bad_kind")


class SingletonTests(unittest.TestCase):
    def test_singleton_disabled_returns_none(self):
        import modules.simracerhub as srh

        with patch.dict("modules.simracerhub.os.environ", {"SIMRACERHUB_ENABLED": "0"}, clear=False):
            original = srh._simracerhub
            srh._simracerhub = None
            try:
                self.assertIsNone(srh.get_simracerhub())
            finally:
                srh._simracerhub = original


if __name__ == "__main__":
    unittest.main()
