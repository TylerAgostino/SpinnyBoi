"""Unit tests for the SimRacerHub stats client (modules/simracerhub.py).

No network: the client takes injected ``fetch_json``/``fetch_text`` callables;
the parsers/URL builders are pure functions tested against trimmed fixtures
that mirror the real get_standings.php payload shape (string numerics,
dict-keyed maps) and the league_series/series_seasons discovery markup.

Run: python -m unittest tests.test_simracerhub
"""
import email.message
import os
import socket
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from modules.league_config import DEFAULT_SRH_LEAGUE_ID
from modules.simracerhub import (
    MAX_RETRY_WAIT_SECONDS,
    USER_AGENT,
    SRHRetryableFetchError,
    SimRacerHubClient,
    SimRacerHubConfig,
    assemble_season_catalog,
    build_seasons_index_url,
    build_series_index_url,
    build_suggest_url,
    build_standings_url,
    clean_driver_filter,
    driver_matches,
    match_season,
    normalize_days,
    normalize_results,
    normalize_standings,
    parse_seasons_page,
    parse_series_index,
    season_number,
    _default_fetch_text,
)

NOW = 1_789_600_000  # fixed clock for time-filter tests
DAY = 86400

SAMPLE_PAYLOAD = {
    "drivers": {
        "48019": {"driver_id": "48019", "name": "Speedy McFast2", "last_first": "McFast2, Speedy", "is_ai": False},
        "16262": {"driver_id": "16262", "name": "Danny Dummy", "last_first": "Dummy, Danny", "is_ai": False},
        "99999": {"driver_id": "99999", "name": "Robot Racer", "last_first": "Racer, Robot", "is_ai": True},
    },
    "rps": {
        "48019": {
            "drid": "48019", "name": "McFast2, Speedy", "starts": "9", "counted": "7",
            "tpts": "443", "rpts": "405", "bpts": "38", "wins": "1", "poles": "1",
            "t3": "6", "t5": "6", "t10": "8", "laps": "266", "inc": "141",
            "rat": "1003.7587", "races": {"379770": {"race_points": "56"}},
        },
        "16262": {
            "drid": "16262", "name": "Dummy, Danny", "starts": "8", "counted": "7",
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
    "teams": {
        "901": {"name": "Test Squad"},
        "902": {"name": "Merc Squad"},
    },
    # season-level teams; 16262 points at a team id that does not exist
    "team_drivers": {"48019": "901", "16262": "no-such-team"},
    # per-race assignment overrides the season-level team
    "race_team_assignments": {"P-2": "902"},
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
                "48019": {"driver_id": "48019", "race_id": "378500", "race_participant_id": "P-1", "qualify_pos": "11", "num_laps": "7", "laps_led": "2", "finish_pos": "1", "race_points": "56", "incidents": "38", "status": "Running", "rating": "128.722"},
                "16262": {"driver_id": "16262", "race_id": "378500", "race_participant_id": "P-2", "qualify_pos": "1", "num_laps": "7", "laps_led": "0", "finish_pos": "4", "race_points": "33", "incidents": "14", "status": "Running", "rating": "99"},
            },
        },
        {
            "schedule_id": "375811", "season_id": "29913", "race_date": str(NOW + 7 * DAY),
            "config_id": "17", "chase": "N",
            "drivers": {},  # scheduled, not yet run
        },
    ],
}

# What get_standings.php ACTUALLY returns for a schedule week: drivers keyed
# by race id, then by driver id (captured live from season 28156, 2026-10).
NESTED_PAYLOAD = {
    "drivers": {
        "48019": {"driver_id": "48019", "name": "Speedy McFast2", "is_ai": False},
        "16262": {"driver_id": "16262", "name": "Danny Dummy", "is_ai": False},
    },
    "tracks": {"55": {"track_name": "Daytona Road Course", "name": "Daytona"}},
    "teams": {"901": {"name": "Test Squad"}},
    "team_drivers": {"16262": "901"},
    "schedules": [
        {
            "schedule_id": "367869",
            "race_date": str(NOW - 5 * DAY),
            "config_id": "55",
            "drivers": {
                "330167": {
                    "48019": {"driver_id": "48019", "race_id": "330167", "qualify_pos": "3", "num_laps": "20", "laps_led": "5", "finish_pos": "2", "race_points": "48", "incidents": "12", "status": "Running", "rating": "110.2"},
                },
                "330166": {
                    "16262": {"driver_id": "16262", "race_id": "330166", "qualify_pos": "1", "num_laps": "20", "laps_led": "0", "finish_pos": "1", "race_points": "56", "incidents": "14", "status": "Running", "rating": "99"},
                },
            },
        },
    ],
}

# Discovery fixtures: the league_series.php / series_seasons.php markup in
# the shape captured live from SRH (2026-10) - jsTableRow rows with a Yes/No
# Active cell, and the ``seasons=[{id,sname,scrt,...}]`` JS literal array
# plus the ``curr_season_id`` pointer.
SERIES_INDEX_HTML = """
<table><tr class='jsTableHdr'><th>Active</th></tr>
<tr class='jsTableRow' id='ser_9'><td class='ctr'><b>Yes</b></td> <td class='wrap'><a href='series_seasons.php?series_id=9' title='View Seasons in this Series'>Test Series</a></td> </tr>
<tr class='jsTableRow' id='ser_10'><td class='ctr'><b>No</b></td> <td class='wrap'><a href='series_seasons.php?series_id=10' title='View Seasons in this Series'>Retired Series</a></td> </tr>
</table>"""

SEASONS_HTML_9 = """
<script language='javascript'>
series_id=9;
curr_season_id=2;
seasons=[{id:1,sname:"Season 1",scrt:100,kw:3,ps:"pos",scrt_str:"Jan 1, 2024",ns:9,nr:9},
{id:2,sname:"Season 2",scrt:200,kw:4,ps:"scheme",scrt_str:"Jan 1, 2025",ns:9,nr:9}];
</script>"""

SEASONS_HTML_10 = """
series_id=10;
curr_season_id=3;
seasons=[{id:3,sname:"Vintage Cup",scrt:50,scrt_str:"Jan 1, 2023",ns:9,nr:9}];
"""

# Rows that cannot drive discovery: one whose link points at a different
# series id than its ``ser_<id>`` row, one with no seasons link at all.
MISMATCHED_SERIES_HTML = """
<table>
<tr class='jsTableRow' id='ser_9'><td class='ctr'><b>Yes</b></td> <td class='wrap'><a href='series_seasons.php?series_id=99' title='View Seasons in this Series'>Wrong Link Series</a></td> </tr>
<tr class='jsTableRow' id='ser_10'><td class='ctr'><b>No</b></td> <td class='wrap'>No Link Series</td> </tr>
<tr class='jsTableRow' id='ser_11'><td class='ctr'><b>Yes</b></td> <td class='wrap'><a href='series_seasons.php?series_id=11' title='View Seasons in this Series'>Good Series</a></td> </tr>
</table>"""

# A season name whose JS-literal escapes are invalid JSON (``\\x`` is not a
# JSON escape); the parser must fall back to the raw text.
SEASONS_HTML_BAD_ESCAPE = """
series_id=11;
curr_season_id=4;
seasons=[{id:4,sname:"Bad\\x41 Escape",scrt:410,scrt_str:"Feb 1, 2026",ns:9,nr:9}];
"""

# A league whose only series page carries no ``seasons=[...]`` array.
GHOST_SERIES_INDEX_HTML = """
<table><tr class='jsTableRow' id='ser_12'><td class='ctr'><b>Yes</b></td> <td class='wrap'><a href='series_seasons.php?series_id=12' title='View Seasons in this Series'>Ghost Series</a></td> </tr>
</table>"""

NO_SEASONS_HTML = """
<script language='javascript'>
series_id=12;
curr_season_id=0;
</script>"""


def discovery_fetch_text(url, timeout):
    if "league_series" in url:
        return SERIES_INDEX_HTML
    if "series_id=10" in url:
        return SEASONS_HTML_10
    return SEASONS_HTML_9


class NestedScheduleParsingTests(unittest.TestCase):
    def test_race_keyed_schedules_flatten_to_driver_rows(self):
        rows = normalize_results(NESTED_PAYLOAD, now_epoch=NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["name"] for r in rows}, {"Speedy McFast2", "Danny Dummy"})
        for row in rows:
            self.assertEqual(row["track"], "Daytona Road Course")
            self.assertIsNotNone(row["finish_pos"])
            self.assertIsNotNone(row["race_points"])
        self.assertEqual(
            {r["name"]: r["team"] for r in rows},
            {"Speedy McFast2": None, "Danny Dummy": "Test Squad"},
        )

    def test_driver_filter_works_on_nested_shape(self):
        rows = normalize_results(NESTED_PAYLOAD, driver="dummy", now_epoch=NOW)
        self.assertEqual([r["name"] for r in rows], ["Danny Dummy"])


class TeamAwarenessTests(unittest.TestCase):
    def test_standings_rows_carry_season_team(self):
        rows = {r["name"]: r for r in normalize_standings(SAMPLE_PAYLOAD)}
        self.assertEqual(rows["Speedy McFast2"]["team"], "Test Squad")
        # dangling team id drops the team rather than inventing one
        self.assertIsNone(rows["Danny Dummy"]["team"])
        self.assertIsNone(rows["Robot Racer"]["team"])

    def test_standings_filter_matches_team_name(self):
        rows = normalize_standings(SAMPLE_PAYLOAD, driver="test squad")
        self.assertEqual([r["name"] for r in rows], ["Speedy McFast2"])

    def test_results_prefer_per_race_team_assignment(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW)
        dummy = [r for r in rows if r["name"] == "Danny Dummy"]
        self.assertEqual(len(dummy), 1)
        self.assertEqual(dummy[0]["team"], "Merc Squad")  # via P-2
        speedy = [r for r in rows if r["name"] == "Speedy McFast2"]
        self.assertTrue(speedy)
        self.assertTrue(
            all(r["team"] == "Test Squad" for r in speedy),
            "P-1 has no assignment; season-level team must be used",
        )

    def test_results_filter_matches_team_name(self):
        rows = normalize_results(SAMPLE_PAYLOAD, driver="Merc Squad", now_epoch=NOW)
        self.assertEqual({r["name"] for r in rows}, {"Danny Dummy"})


class FakeClock:
    """Hand-driven monotonic clock for pacing/cache tests: recorded sleeps
    advance the value, so paced waits resolve; tests set gaps by hand."""

    def __init__(self, value=0.0):
        self.value = value
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def make_client(fetch=None, config=None, clock=None, sleep=None, fetch_text=None):
    fetch = fetch if fetch is not None else MagicMock(return_value=SAMPLE_PAYLOAD)
    if clock is None:
        ticks = {"n": 0}

        def clock():
            now = ticks["n"] * 1000.0  # generous spacing; pacing tests drive FakeClock
            ticks["n"] += 1
            return now

    return SimRacerHubClient(
        config or SimRacerHubConfig(league_id=7),
        fetch_json=fetch,
        fetch_text=fetch_text or discovery_fetch_text,
        clock=clock,
        sleep=sleep,
    )

def never_sleep(seconds):  # keep tests from really sleeping
    pass


def http_error(code, retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://www.simracerhub.com/scoring/get_standings.php?season_id=1",
        code,
        "Too Many Requests",
        headers,
        None,
    )


class EmptyAttemptsInt(int):
    """int subclass that makes ``max(1, v)`` pick a *negative* value so the
    retry loop's ``range`` is empty — the only way to reach the loop's
    terminal fallthrough guard, which must raise, never return None."""

    def __add__(self, other):
        return EmptyAttemptsInt(int(self) + int(other))

    def __radd__(self, other):
        return EmptyAttemptsInt(int(other) + int(self))

    def __gt__(self, other):
        return True


class QueryFormattingTests(unittest.TestCase):
    def test_clean_driver_filter(self):
        self.assertEqual(clean_driver_filter("  speedy   mcfast  "), "speedy mcfast")
        self.assertEqual(clean_driver_filter(None), "")
        self.assertEqual(len(clean_driver_filter("a" * 200)), 60)

    def test_driver_matches(self):
        self.assertTrue(driver_matches("Speedy McFast2", "mcfast"))
        self.assertTrue(driver_matches("Wally Widgetton", "WALLY WIDGETTON"))
        self.assertTrue(driver_matches("Speedy McFast2", "speedymcfast2"))
        self.assertFalse(driver_matches("Danny Dummy", "mcfast"))
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
        url = build_suggest_url("Speedy McFast")
        self.assertIn("search_suggest.php", url)
        self.assertIn("q=Speedy+McFast", url)

    def test_series_index_url(self):
        url = build_series_index_url(4007)
        self.assertIn("league_series.php", url)
        self.assertIn("league_id=4007", url)

    def test_seasons_index_url(self):
        url = build_seasons_index_url(8812)
        self.assertIn("series_seasons.php", url)
        self.assertIn("series_id=8812", url)


class StandingsParsingTests(unittest.TestCase):
    def test_sorted_by_points_and_positioned(self):
        rows = normalize_standings(SAMPLE_PAYLOAD)
        self.assertEqual([r["name"] for r in rows], ["Danny Dummy", "Speedy McFast2", "Robot Racer"])
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3])

    def test_numeric_coercion_and_inc_rate(self):
        row = next(r for r in normalize_standings(SAMPLE_PAYLOAD) if r["name"] == "Speedy McFast2")
        self.assertEqual(row["points"], 443)
        self.assertEqual(row["wins"], 1)
        self.assertEqual(row["rating"], 1003.7587)
        self.assertEqual(row["inc_per_race"], round(141 / 9, 2))
        self.assertIs(row["is_ai"], False)

    def test_driver_filter(self):
        rows = normalize_standings(SAMPLE_PAYLOAD, driver="mcfast")
        self.assertEqual([r["name"] for r in rows], ["Speedy McFast2"])

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
        self.assertEqual(first["name"], "Speedy McFast2")

    def test_days_filter_excludes_old_weeks(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW, days=30)
        self.assertTrue(all(r["race_date"] >= NOW - 30 * DAY for r in rows))
        self.assertEqual([r["track"] for r in rows].count("Virginia International Raceway"), 0)
        self.assertEqual(len(rows), 2)

    def test_days_filter_includes_old_weeks_when_wide(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW, days=365)
        self.assertEqual(len(rows), 3)

    def test_driver_filter(self):
        rows = normalize_results(SAMPLE_PAYLOAD, driver="dummy", now_epoch=NOW)
        self.assertEqual([r["name"] for r in rows], ["Danny Dummy"])

    def test_max_weeks_bounds_scan(self):
        rows = normalize_results(SAMPLE_PAYLOAD, now_epoch=NOW, days=365, max_weeks=1)
        self.assertTrue(all(r["track"] == "Homestead Miami Speedway" for r in rows))

    def test_empty(self):
        self.assertEqual(normalize_results({}, now_epoch=NOW), [])


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = SimRacerHubConfig.from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.timeout_seconds, 8.0)
        self.assertEqual(cfg.max_rows, 15)
        self.assertEqual(cfg.max_retries, 2)
        self.assertEqual(cfg.retry_base_seconds, 2.0)
        self.assertEqual(cfg.cache_ttl_seconds, 300.0)
        # the only SRH id needed; discovery fills the seasons in from it
        self.assertEqual(cfg.league_id, DEFAULT_SRH_LEAGUE_ID)

    def test_env_mapping_is_isolated_from_process_env(self):
        # SRH_* values in the supplied mapping win; the real process
        # environment must not leak in through the LeagueConfig fallback.
        cfg = SimRacerHubConfig.from_env({"SRH_LEAGUE_ID": "111"})
        self.assertEqual(cfg.league_id, 111)
        cfg = SimRacerHubConfig.from_env({"SIMRACERHUB_LEAGUE_ID": "222"})
        self.assertEqual(cfg.league_id, 222)

    def test_disabled_flag(self):
        for value in ("0", "false", "OFF"):
            self.assertFalse(SimRacerHubConfig.from_env({"SIMRACERHUB_ENABLED": value}).enabled)

    def test_clamping_and_bad_values(self):
        cfg = SimRacerHubConfig.from_env(
            {"SIMRACERHUB_MAX_ROWS": "999", "SIMRACERHUB_TIMEOUT_SECONDS": "junk"}
        )
        self.assertEqual(cfg.max_rows, 50)
        self.assertEqual(cfg.timeout_seconds, 8.0)
        cfg = SimRacerHubConfig.from_env(
            {
                "SIMRACERHUB_MAX_RETRIES": "99",
                "SIMRACERHUB_RETRY_BASE_SECONDS": "junk",
                "SIMRACERHUB_CACHE_TTL_SECONDS": "-1",
            }
        )
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.retry_base_seconds, 2.0)
        self.assertEqual(cfg.cache_ttl_seconds, 0.0)


class ClientTests(unittest.TestCase):
    def test_standings_success(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        client = make_client(fetch)
        out = client.standings(season=2)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["season_id"], 2)
        self.assertEqual(out["rows"][0]["name"], "Danny Dummy")
        url, timeout = fetch.call_args[0]
        self.assertIn("get_standings.php", url)
        self.assertIn("season_id=2", url)
        self.assertEqual(timeout, 8.0)

    def test_results_success_passes_days(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        client = make_client(fetch)
        out = client.results(season=2, driver="mcfast", days=365)
        self.assertEqual(out["status"], "ok")
        self.assertEqual({r["name"] for r in out["rows"]}, {"Speedy McFast2"})

    def test_default_season_is_discovered_active(self):
        out = make_client().standings()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["season_id"], 2)  # the series' curr_season_id
        self.assertTrue(out["season"]["active"])

    def test_no_league_id_configured(self):
        out = make_client(config=SimRacerHubConfig(league_id=None)).standings()
        self.assertEqual(out["status"], "no_league")
        self.assertEqual(out["rows"], [])

    def test_api_error_payload_handled(self):
        fetch = MagicMock(return_value={"error": "Season ID 1 not found "})
        out = make_client(fetch).standings(season=1)
        self.assertEqual(out["status"], "no_data")
        self.assertIn("not found", out["error"])

    def test_transient_network_errors_retried_then_surfaced(self):
        fetch = MagicMock(side_effect=ConnectionError("boom"))
        sleeps = []
        out = make_client(fetch, sleep=sleeps.append).results(season=1)
        self.assertEqual(out, {"status": "error", "rows": []})
        self.assertEqual(fetch.call_count, 3)  # 1 try + 2 retries (default budget)
        self.assertEqual(sleeps, [2.0, 4.0])  # exponential from retry_base_seconds

    def test_timeout_retried_then_surfaced(self):
        fetch = MagicMock(side_effect=socket.timeout("timed out"))
        out = make_client(fetch, sleep=never_sleep).standings(season=1)
        self.assertEqual(out["status"], "error")
        self.assertEqual(fetch.call_count, 3)

    def test_non_dict_payload(self):
        out = make_client(MagicMock(return_value=[])).standings(season=1)
        self.assertEqual(out["status"], "error")

    def test_pacing_waits_instead_of_dropping_calls(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        # FakeClock sleeps advance the clock, so discovery pacing resolves at
        # min_interval; the standings->results gap is then set to 1s (< 3s
        # min) and the client waits out the remainder instead of answering
        # "rate_limited". different seasons keep the payload cache out of it.
        clock = FakeClock(100.0)
        client = make_client(
            fetch,
            config=SimRacerHubConfig(league_id=7, min_interval_seconds=3.0),
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertEqual(client.standings(season=1)["status"], "ok")
        clock.value += 1.0
        second = client.results(season=2, days=3650)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(clock.sleeps[-1], 2.0)
        self.assertEqual(fetch.call_count, 2)

    def test_disabled(self):
        fetch = MagicMock()
        client = SimRacerHubClient(SimRacerHubConfig(enabled=False), fetch_json=fetch)
        self.assertEqual(client.standings(season=1)["status"], "disabled")
        fetch.assert_not_called()

    def test_disabled_envelopes_for_every_entry_point(self):
        fetch = MagicMock()
        text = MagicMock()
        client = SimRacerHubClient(
            SimRacerHubConfig(enabled=False),
            fetch_json=fetch,
            fetch_text=text,
        )
        self.assertEqual(client.seasons(), {"status": "disabled", "seasons": []})
        self.assertEqual(client.seasons(league_id=9), {"status": "disabled", "seasons": []})
        self.assertEqual(client.results(season=1), {"status": "disabled", "rows": []})
        self.assertEqual(client.resolve_driver("Speedy"), {"status": "disabled", "drivers": []})
        fetch.assert_not_called()
        text.assert_not_called()

    def test_resolve_driver(self):
        fetch = MagicMock(return_value={"drivers": [{"id": 71940, "name": "Speedy McFast", "is_ai": False}], "series": []})
        out = make_client(fetch).resolve_driver("Speedy")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["drivers"], [{"driver_id": 71940, "name": "Speedy McFast"}])

    def test_resolve_driver_empty_query(self):
        fetch = MagicMock()
        out = make_client(fetch).resolve_driver("   ")
        self.assertEqual(out["status"], "empty_query")
        fetch.assert_not_called()


class FetchResilienceTests(unittest.TestCase):
    """simracerhub.com aggressively rate-limits: fetches retry with backoff
    (honouring Retry-After) and repeat URLs are served from a short cache."""

    def test_429_retried_after_retry_after_seconds(self):
        fetch = MagicMock(side_effect=[http_error(429, 7), SAMPLE_PAYLOAD])
        sleeps = []
        out = make_client(fetch, sleep=sleeps.append).standings(season=1)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(sleeps, [7.0])

    def test_retry_after_capped_at_max_wait(self):
        fetch = MagicMock(side_effect=[http_error(429, 9999), SAMPLE_PAYLOAD])
        sleeps = []
        out = make_client(fetch, sleep=sleeps.append).standings(season=1)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(sleeps, [MAX_RETRY_WAIT_SECONDS])

    def test_503_retries_exponentially(self):
        fetch = MagicMock(side_effect=[http_error(503), http_error(503), SAMPLE_PAYLOAD])
        sleeps = []
        out = make_client(fetch, sleep=sleeps.append).results(season=1, days=3650)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_permanent_http_status_not_retried(self):
        fetch = MagicMock(side_effect=http_error(404))
        out = make_client(fetch, sleep=never_sleep).standings(season=1)
        self.assertEqual(out["status"], "error")
        self.assertEqual(fetch.call_count, 1)

    def test_retry_budget_exhausted_surfaces_error(self):
        fetch = MagicMock(side_effect=http_error(429))
        cfg = SimRacerHubConfig(max_retries=1, league_id=7)
        out = make_client(fetch, config=cfg, sleep=never_sleep).standings()
        self.assertEqual(out["status"], "error")
        self.assertEqual(fetch.call_count, 2)

    def test_non_json_body_retried_as_rate_limit(self):
        fetch = MagicMock(
            side_effect=[SRHRetryableFetchError("<html>slow down</html>"), SAMPLE_PAYLOAD]
        )
        sleeps = []
        out = make_client(fetch, sleep=sleeps.append).standings(season=1)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(sleeps, [2.0])

    def test_unrelated_error_not_retried(self):
        fetch = MagicMock(side_effect=ValueError("bad"))
        out = make_client(fetch).standings(season=1)
        self.assertEqual(out["status"], "error")
        self.assertEqual(fetch.call_count, 1)

    def test_retry_loop_fallthrough_guard_raises(self):
        # every attempt normally returns or re-raises; the loop's terminal
        # AssertionError is only reachable when the attempts range is empty.
        # The fake transport records that it (and the patched sleep seam)
        # never ran.
        fetch = MagicMock()
        cfg = SimRacerHubConfig(league_id=7, max_retries=EmptyAttemptsInt(-2))
        client = make_client(fetch, config=cfg, sleep=never_sleep)
        with self.assertRaisesRegex(AssertionError, "unreachable"):
            client._fetch_with_retry(build_standings_url(1), fetch)
        fetch.assert_not_called()

    def test_cache_collapses_repeat_requests_same_url(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        cfg = SimRacerHubConfig(league_id=7, min_interval_seconds=0.0)
        client = make_client(fetch, config=cfg, clock=FakeClock(10.0), sleep=lambda s: None)
        first = client.standings(season=1)
        second = client.results(season=1, days=365)
        self.assertEqual((first["status"], second["status"]), ("ok", "ok"))
        self.assertEqual(fetch.call_count, 1)

    def test_cache_expires_after_ttl(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        cfg = SimRacerHubConfig(league_id=7, cache_ttl_seconds=5.0, min_interval_seconds=0.0)
        clock = FakeClock(0.0)
        client = make_client(fetch, config=cfg, clock=clock, sleep=clock.sleep)
        self.assertEqual(client.standings(season=1)["status"], "ok")
        clock.value = 6.0
        self.assertEqual(client.results(season=1, days=3650)["status"], "ok")
        self.assertEqual(fetch.call_count, 2)

    def test_zero_ttl_disables_cache(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        cfg = SimRacerHubConfig(league_id=7, cache_ttl_seconds=0.0, min_interval_seconds=0.0)
        clock = FakeClock(0.0)
        client = make_client(fetch, config=cfg, clock=clock, sleep=clock.sleep)
        self.assertEqual(client.standings(season=1)["status"], "ok")
        self.assertEqual(client.results(season=1, days=3650)["status"], "ok")
        self.assertEqual(fetch.call_count, 2)


class SeasonDiscoveryTests(unittest.TestCase):
    """Pure parsing/assembly/matching of the league season catalog."""

    def test_parse_series_index(self):
        rows = parse_series_index(SERIES_INDEX_HTML)
        self.assertEqual([r["series_id"] for r in rows], [9, 10])
        self.assertEqual(rows[0]["series_name"], "Test Series")
        self.assertTrue(rows[0]["active"])
        self.assertFalse(rows[1]["active"])

    def test_parse_series_index_skips_rows_without_matching_link(self):
        # a row linking to a *different* series id would misroute season
        # discovery, and a row with no link cannot be expanded at all
        rows = parse_series_index(MISMATCHED_SERIES_HTML)
        self.assertEqual([r["series_id"] for r in rows], [11])
        self.assertEqual(rows[0]["series_name"], "Good Series")

    def test_parse_seasons_page(self):
        parsed = parse_seasons_page(SEASONS_HTML_9)
        self.assertEqual(parsed["current_season_id"], 2)
        self.assertEqual([s["season_id"] for s in parsed["seasons"]], [1, 2])
        self.assertEqual(parsed["seasons"][0]["season_name"], "Season 1")
        self.assertEqual(parsed["seasons"][0]["created"], 100)

    def test_parse_seasons_page_bad_json_escape_keeps_raw_text(self):
        parsed = parse_seasons_page(SEASONS_HTML_BAD_ESCAPE)
        season = parsed["seasons"][0]
        self.assertEqual(parsed["current_season_id"], 4)
        # invalid JSON escapes do not drop the season; the title falls back
        # to the raw regex-group text
        self.assertEqual(season["season_id"], 4)
        self.assertEqual(season["season_name"], "Bad\\x41 Escape")
        self.assertEqual(season["created"], 410)

    def test_assemble_orders_newest_first_and_flags_active(self):
        catalog = assemble_season_catalog(
            7,
            [
                ({"series_id": 9, "series_name": "Test Series", "active": True}, parse_seasons_page(SEASONS_HTML_9)),
                ({"series_id": 10, "series_name": "Retired Series", "active": False}, parse_seasons_page(SEASONS_HTML_10)),
            ],
        )
        self.assertEqual(catalog["active_season_id"], 2)
        self.assertEqual([s["season_id"] for s in catalog["seasons"]], [2, 1, 3])
        entry = next(s for s in catalog["seasons"] if s["season_id"] == 2)
        self.assertTrue(entry["active"])
        self.assertEqual(entry["series_name"], "Test Series")

    def test_active_season_ambiguous_when_active_series_disagree(self):
        # main + AM both Active, each with its own current season: picking
        # one silently could route to the wrong table, so there is no default.
        am_page = {
            "current_season_id": 3,
            "seasons": [{"season_id": 3, "season_name": "AM Season 1", "created": 300}],
        }
        catalog = assemble_season_catalog(
            7,
            [
                ({"series_id": 9, "series_name": "Main", "active": True}, parse_seasons_page(SEASONS_HTML_9)),
                ({"series_id": 10, "series_name": "AM", "active": True}, am_page),
            ],
        )
        self.assertIsNone(catalog["active_season_id"])
        entry, err = match_season(None, catalog)
        self.assertIsNone(entry)
        status, options = err
        self.assertEqual(status, "ambiguous_season")
        self.assertEqual({o["season_id"] for o in options}, {2, 3})

    def test_active_season_ref_without_any_active_entry_lists_all_seasons(self):
        # Retired-only league: no entry matches the (absent) active season and
        # there is not a crowd of competing current seasons, so the
        # unqualified ref degrades to unknown_season with the whole catalog.
        catalog = assemble_season_catalog(
            7,
            [({"series_id": 10, "series_name": "Retired Series", "active": False}, parse_seasons_page(SEASONS_HTML_10))],
        )
        self.assertIsNone(catalog["active_season_id"])
        for ref in (None, "active"):
            entry, err = match_season(ref, catalog)
            self.assertIsNone(entry)
            status, options = err
            self.assertEqual(status, "unknown_season")
            self.assertEqual({o["season_id"] for o in options}, {3})
            self.assertEqual(set(options[0]), {"season_id", "season_name", "series_name", "active"})

    def test_ambiguous_season_ref_lists_the_matching_tier(self):
        catalog = {
            "seasons": [
                {"season_id": 5, "season_name": "Twin Cup", "series_name": "Main", "active": False},
                {"season_id": 6, "season_name": "Twin Cup", "series_name": "AM", "active": False},
                {"season_id": 90, "season_name": "Season 9", "series_name": "Main", "active": False},
                {"season_id": 91, "season_name": "Season 9", "series_name": "AM", "active": False},
            ],
            "active_season_id": None,
        }
        entry, err = match_season("Twin Cup", catalog)
        self.assertIsNone(entry)
        status, options = err
        self.assertEqual(status, "ambiguous_season")
        self.assertEqual([o["season_id"] for o in options], [5, 6])
        # a bare number matching several seasons by season number is likewise
        # never guessed
        entry, err = match_season(9, catalog)
        self.assertIsNone(entry)
        self.assertEqual(err[0], "ambiguous_season")
        self.assertEqual([o["season_id"] for o in err[1]], [90, 91])

    def test_match_season_tiers(self):
        catalog = assemble_season_catalog(
            7,
            [({"series_id": 9, "series_name": "Test Series", "active": True}, parse_seasons_page(SEASONS_HTML_9))],
        )
        self.assertEqual(match_season(2, catalog)[0]["season_id"], 2)
        self.assertEqual(match_season("S2", catalog)[0]["season_id"], 2)
        self.assertEqual(match_season("Season 1", catalog)[0]["season_id"], 1)
        self.assertEqual(match_season("active", catalog)[0]["season_id"], 2)
        entry, err = match_season("Season 99", catalog)
        self.assertIsNone(entry)
        self.assertEqual(err[0], "unknown_season")
        self.assertEqual({o["season_id"] for o in err[1]}, {1, 2})

    def test_season_number_helper(self):
        self.assertEqual(season_number({"season_name": "Season 16"}), 16)
        self.assertEqual(season_number({"season_name": "S16"}), 16)
        self.assertIsNone(season_number({"season_name": "Season 16 Am"}))


class SeasonDiscoveryClientTests(unittest.TestCase):
    def test_seasons_lists_catalog(self):
        out = make_client().seasons()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["league_id"], 7)
        self.assertEqual(out["active_season_id"], 2)
        self.assertEqual([s["season_id"] for s in out["seasons"]], [2, 1, 3])

    def test_seasons_empty_catalog_reports_no_data(self):
        def ghost_fetch_text(url, timeout):
            if "league_series" in url:
                return GHOST_SERIES_INDEX_HTML
            return NO_SEASONS_HTML

        out = make_client(fetch_text=ghost_fetch_text).seasons()
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["seasons"], [])
        self.assertEqual(out["league_id"], 7)

    def test_standings_season_name_resolves_to_discovered_id(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        make_client(fetch).standings(season="Season 1")
        url, _ = fetch.call_args[0]
        self.assertIn("season_id=1", url)

    def test_discovery_failure_degrades(self):
        text = MagicMock(side_effect=ValueError("gone"))
        out = make_client(fetch_text=text).standings()
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["rows"], [])

    def test_unknown_season_lists_options(self):
        out = make_client().standings(season="Season 99")
        self.assertEqual(out["status"], "unknown_season")
        self.assertEqual(out["season_query"], "Season 99")
        self.assertEqual({s["season_id"] for s in out["seasons"]}, {1, 2, 3})


class DefaultFetcherTests(unittest.TestCase):
    """``_default_fetch_text`` is the season-discovery HTML transport; run it
    with ``urlopen`` patched (no network) to pin request + utf-8 decoding."""

    def test_default_fetch_text_requests_and_decodes(self):
        captured = {}

        class _Response:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response("<p>Saison 16 — Sakhir</p>".encode("utf-8"))

        url = build_seasons_index_url(9)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = _default_fetch_text(url, 4.25)
        self.assertEqual(out, "<p>Saison 16 — Sakhir</p>")
        self.assertEqual(captured["timeout"], 4.25)
        self.assertEqual(captured["request"].full_url, url)
        self.assertEqual(captured["request"].get_header("User-agent"), USER_AGENT)


class ToolRegistrationTests(unittest.TestCase):
    def test_tool_has_spec(self):
        tool = make_client().stats_tool()
        spec = tool.tool_spec
        self.assertEqual(spec["name"], "get_simracerhub_stats")
        self.assertIn("kind", str(spec["inputSchema"]))

    def test_tool_kind_routing(self):
        tool = make_client().stats_tool()
        self.assertEqual(tool(kind="Standings", season="Season 2")["status"], "ok")
        self.assertEqual(tool(kind="races", season="Season 2", days=3650)["status"], "ok")
        self.assertEqual(tool(kind="seasons")["status"], "ok")

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
