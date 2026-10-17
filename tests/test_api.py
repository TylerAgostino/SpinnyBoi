from time import time
import unittest
from unittest import mock

import requests

from modules.api import iRacingAPIHandler


def make_handler(use_oauth=False):
    # __init__ auto-logins; keep the unit offline by stubbing the entry point.
    with mock.patch.object(iRacingAPIHandler, "login", return_value=True):
        return iRacingAPIHandler(
            email="tester@example.com",
            password="pw",
            client_id="cid",
            client_secret="csecret",
            use_oauth=use_oauth,
        )


class RequestTimeoutTests(unittest.TestCase):
    def test_default_timeout_injected(self):
        handler = make_handler()
        with mock.patch.object(
            requests.Session, "request", return_value=mock.Mock()
        ) as base:
            handler.get("https://members-ng.iracing.com/api/x")
        self.assertEqual(base.call_args.kwargs["timeout"], iRacingAPIHandler.DEFAULT_TIMEOUT)

    def test_caller_timeout_not_overwritten(self):
        handler = make_handler()
        with mock.patch.object(
            requests.Session, "request", return_value=mock.Mock()
        ) as base:
            handler.get("https://members-ng.iracing.com/api/x", timeout=3)
        self.assertEqual(base.call_args.kwargs["timeout"], 3)


class OAuthLoginTimeoutTests(unittest.TestCase):
    def token_json(self):
        return {
            "access_token": "tok",
            "refresh_token": "rtok",
            "expires_in": 600,
            "refresh_token_expires_in": 604800,
        }

    def test_password_limited_post_carries_timeout(self):
        handler = make_handler(use_oauth=True)
        handler.logged_in = False
        resp = mock.Mock(status_code=200)
        resp.json.return_value = self.token_json()
        with mock.patch(
            "modules.api.requests.post", return_value=resp
        ) as posted:
            handler._login_password_limited_flow()
        self.assertEqual(
            posted.call_args.kwargs["timeout"], iRacingAPIHandler.DEFAULT_TIMEOUT
        )

    def test_token_refresh_post_carries_timeout(self):
        handler = make_handler(use_oauth=True)
        handler.refresh_token = "rtok"
        handler.refresh_token_expires_at = time() + 3600
        resp = mock.Mock(status_code=200)
        resp.json.return_value = self.token_json()
        with mock.patch(
            "modules.api.requests.post", return_value=resp
        ) as posted:
            handler._refresh_access_token()
        self.assertEqual(
            posted.call_args.kwargs["timeout"], iRacingAPIHandler.DEFAULT_TIMEOUT
        )


class SharedHandlerTests(unittest.TestCase):
    """api.shared_iracing_handler is a process-wide lazy singleton."""

    def setUp(self):
        import modules.api as api

        self.api = api
        self._saved = api._SHARED_HANDLER
        api._SHARED_HANDLER = None

    def tearDown(self):
        self.api._SHARED_HANDLER = self._saved

    def test_handler_built_once_across_calls(self):
        sentinel = object()
        with mock.patch.object(
            self.api, "iracing_handler_from_env", return_value=sentinel
        ) as build:
            self.assertIs(self.api.shared_iracing_handler(), sentinel)
            self.assertIs(self.api.shared_iracing_handler(), sentinel)
        self.assertEqual(build.call_count, 1)

    def test_failed_build_is_not_cached(self):
        with mock.patch.object(
            self.api, "iracing_handler_from_env", return_value=None
        ) as build:
            self.assertIsNone(self.api.shared_iracing_handler())
            self.assertIsNone(self.api.shared_iracing_handler())
        self.assertEqual(build.call_count, 2)

    def test_both_cogs_share_one_construction(self):
        from types import SimpleNamespace

        from modules.attendanceCog import AttendanceCog
        from modules.registrationCog import RegistrationCog

        built = [object(), object()]
        with mock.patch.object(
            self.api, "iracing_handler_from_env", side_effect=built
        ) as build:
            attendance = AttendanceCog._get_iracing_api(
                SimpleNamespace(_iracing_api=None)
            )
            registration = RegistrationCog._get_iracing_api(
                SimpleNamespace(_iracing_api=None)
            )
        self.assertEqual(build.call_count, 1)
        self.assertIs(attendance, registration)
        self.assertIs(attendance, built[0])

    def test_concurrent_first_calls_build_once(self):
        import threading
        from time import sleep

        def slow_build():
            sleep(0.05)
            return object()

        results = []
        start = threading.Barrier(4)

        def worker():
            start.wait()
            results.append(self.api.shared_iracing_handler())

        with mock.patch.object(
            self.api, "iracing_handler_from_env", side_effect=slow_build
        ) as build:
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(build.call_count, 1)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r is results[0] for r in results))

    def test_injected_handlers_bypass_the_singleton(self):
        from modules.attendanceCog import AttendanceCog
        from modules.league_config import LeagueConfig
        from modules.registrationCog import RegistrationCog

        fake_a, fake_r = object(), object()
        config = LeagueConfig.from_env()
        attendance = AttendanceCog(bot=None, config=config, iracing_api=fake_a)
        registration = RegistrationCog(
            bot=None, config=config, iracing_api=fake_r
        )
        with mock.patch.object(self.api, "iracing_handler_from_env") as build:
            self.assertIs(attendance._get_iracing_api(), fake_a)
            self.assertIs(registration._get_iracing_api(), fake_r)
        build.assert_not_called()
        self.assertIsNone(self.api._SHARED_HANDLER)


if __name__ == "__main__":
    unittest.main()
