# pyright: basic
"""SimRacerHub edge branches: numeric coercion, lap times, retry delay,
status payloads, default fetcher, singleton."""

import io
import unittest
import urllib.error
from unittest import mock

import modules.simracerhub as srh
from modules.simracerhub import (
    MAX_RETRY_WAIT_SECONDS,
    SRHRetryableFetchError,
    SimRacerHubClient,
    SimRacerHubConfig,
    _default_fetch_json,
    _fmt_lap_time,
    _sched_driver_entries,
    _to_number,
    get_simracerhub,
)


def http_error(status=503, headers=None):
    return urllib.error.HTTPError(
        "https://simracerhub.test/x",
        status,
        "nope",
        headers or {},  # pyright: ignore
        io.BytesIO(b""),
    )


class ToNumberTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(_to_number(None))

    def test_non_numeric_passes_through_unchanged(self):
        self.assertEqual(_to_number("not a number"), "not a number")

    def test_integer_strings_become_ints(self):
        self.assertEqual(_to_number("42"), 42)
        self.assertEqual(_to_number(3.0), 3)

    def test_floats_rounded_to_four_places(self):
        self.assertEqual(_to_number("12.3456789"), 12.3457)


class LapTimeTests(unittest.TestCase):
    def test_blank_and_zero_forms_are_none(self):
        for blank in (None, "", "0", 0):
            with self.subTest(blank=blank):
                self.assertIsNone(_fmt_lap_time(blank))

    def test_micros_convert_to_whole_seconds(self):
        self.assertEqual(_fmt_lap_time("91500000"), 91)

    def test_garbage_is_none(self):
        self.assertIsNone(_fmt_lap_time("garbage"))
        self.assertIsNone(_fmt_lap_time([]))


class RetryDelayTests(unittest.TestCase):
    def test_retry_after_header_honoured(self):
        delay = srh._retry_delay(http_error(headers={"Retry-After": "5"}), 2.0, 0)
        self.assertEqual(delay, 5.0)

    def test_retry_after_capped_at_module_max(self):
        delay = srh._retry_delay(http_error(headers={"Retry-After": "99999"}), 2.0, 0)
        self.assertEqual(delay, MAX_RETRY_WAIT_SECONDS)

    def test_retry_after_garbage_falls_back_to_backoff(self):
        delay = srh._retry_delay(
            http_error(headers={"Retry-After": "Fri, 19 Oct 2026"}), 2.0, 3
        )
        self.assertEqual(delay, min(2.0 * 2**3, MAX_RETRY_WAIT_SECONDS))

    def test_non_http_error_uses_exponential_backoff(self):
        self.assertEqual(srh._retry_delay(TimeoutError(), 2.0, 2), 8.0)


class SchedDriverEntryTests(unittest.TestCase):
    def test_flat_nested_and_non_dict_mix(self):
        sched = {
            "drivers": {
                "flat": {"finish_pos": 1},
                "byid": {"driver_id": 9},
                "race42": {"7": {"finish_pos": 2}, "junk": "skip me"},
                "count": 5,
            }
        }
        pairs = dict(_sched_driver_entries(sched))
        self.assertEqual(
            pairs,
            {
                "flat": {"finish_pos": 1},
                "byid": {"driver_id": 9},
                "7": {"finish_pos": 2},
            },
        )


class ResultsCutoffTests(unittest.TestCase):
    NOW = 1_800_000_000

    def sched(self, age_days, driver_id):
        return {
            "race_date": str(self.NOW - age_days * 86400),
            "drivers": {driver_id: {"finish_pos": 1}},
        }

    def run_days(self, days, schedules):
        rows = srh.normalize_results(
            {"schedules": schedules}, driver="", days=days, now_epoch=self.NOW
        )
        return {str(row.get("driver_id")) for row in rows}

    def test_race_older_than_days_excluded_newer_kept(self):
        ids = self.run_days(
            7, [self.sched(40, "1"), self.sched(2, "2")]
        )
        self.assertEqual(ids, {"2"})

    def test_future_and_undated_races_survive_cutoff(self):
        future = {
            "race_date": str(self.NOW + 86400),
            "drivers": {"3": {"finish_pos": 1}},
        }
        undated = {"drivers": {"4": {"finish_pos": 1}}}
        ids = self.run_days(7, [self.sched(40, "1"), future, undated])
        self.assertEqual(ids, {"4"})  # future races skipped; undated kept

    def test_days_none_applies_no_cutoff(self):
        ids = self.run_days(None, [self.sched(400, "1")])
        self.assertEqual(ids, {"1"})


def client_with(fetch_result=None, fetch_exc=None):
    config = SimRacerHubConfig(enabled=True, default_season_id=5, max_rows=10)
    fetcher = mock.MagicMock()
    if fetch_exc:
        fetcher.side_effect = fetch_exc
    else:
        fetcher.return_value = fetch_result
    client = SimRacerHubClient(config, fetch_json=fetcher)
    client._sleep = mock.MagicMock()
    return client


class StatusPayloadTests(unittest.TestCase):
    def test_missing_season_short_circuits_both_apis(self):
        config = SimRacerHubConfig(enabled=True, default_season_id=None, max_rows=10)
        client = SimRacerHubClient(config, fetch_json=mock.MagicMock())
        for call in (client.standings(), client.results()):
            self.assertEqual(call["status"], "no_season")
            self.assertIn("no season_id", call["error"])

    def test_non_dict_payload_is_error_not_crash(self):
        for kind in ("standings", "results"):
            client = client_with(fetch_result=[1, 2, 3])
            result = getattr(client, kind)()
            with self.subTest(kind=kind):
                self.assertEqual(result["status"], "error")

    def test_api_error_payload_becomes_no_data_with_message(self):
        for kind in ("standings", "results"):
            client = client_with(fetch_result={"error": " season closed "})
            result = getattr(client, kind)()
            with self.subTest(kind=kind):
                self.assertEqual(result["status"], "no_data")
                self.assertEqual(result["error"], "season closed")

    def test_empty_normalisation_becomes_no_data(self):
        for kind in ("standings", "results"):
            client = client_with(fetch_result={"standings": [], "schedules": []})
            result = getattr(client, kind)()
            with self.subTest(kind=kind):
                self.assertEqual(result["status"], "no_data")
                self.assertEqual(result["season_id"], 5)


class ResolveDriverEdgesTests(unittest.TestCase):
    def test_fetch_failure_surfaces_error_status(self):
        client = client_with(fetch_exc=TimeoutError("srh down"))
        result = client.resolve_driver("Max")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["drivers"], [])

    def test_non_dict_payload_is_error(self):
        client = client_with(fetch_result="not a dict")
        self.assertEqual(client.resolve_driver("Max")["status"], "error")


class DefaultFetcherTests(unittest.TestCase):
    def make_response(self, body):
        response = mock.MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        return response

    def test_json_body_parsed_and_request_shaped(self):
        response = self.make_response(b'{"a": 1}')
        with mock.patch.object(
            srh.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(
                _default_fetch_json("https://simracerhub.test/x", 5), {"a": 1}
            )
        request = urlopen.call_args.args[0]
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["user-agent"], srh.USER_AGENT)

    def test_html_teapage_becomes_retryable_error(self):
        response = self.make_response(b"<html>rate limited</html>")
        with mock.patch.object(srh.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(
                SRHRetryableFetchError, "non-JSON response"
            ):
                _default_fetch_json("https://simracerhub.test/x", 5)


class SingletonTests(unittest.TestCase):
    def setUp(self):
        self._saved = srh._simracerhub
        srh._simracerhub = None

    def tearDown(self):
        srh._simracerhub = self._saved

    def test_shared_instance_cached_across_calls(self):
        config = SimRacerHubConfig(enabled=True, default_season_id=5, max_rows=10)
        with mock.patch.object(
            SimRacerHubConfig, "from_env", classmethod(lambda cls: config)
        ):
            first = get_simracerhub()
            second = get_simracerhub()
        self.assertIs(first, second)
        self.assertIsInstance(first, SimRacerHubClient)

    def test_disabled_returns_none_and_leaves_no_singleton(self):
        config = SimRacerHubConfig(enabled=False)
        with mock.patch.object(
            SimRacerHubConfig, "from_env", classmethod(lambda cls: config)
        ):
            self.assertIsNone(get_simracerhub())
        self.assertIsNone(srh._simracerhub)


if __name__ == "__main__":
    unittest.main()
