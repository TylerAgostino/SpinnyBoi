"""Offline auth-lifecycle tests for iRacingAPIHandler (issue #109).

No network is ever touched: the OAuth token endpoint is faked via
modules.api.requests.post, the base transport via requests.Session.request,
the clock via modules.api.time (api.py does `from time import time`), and
paged fetches via an instance-level handler.get fake. Assertions encode the
*real* contract of the current code, including its boundary semantics.
"""

import unittest
from unittest import mock

import requests

from modules.api import iRacingAPIHandler

# Fixed fake-clock reading; every patched time() call returns this.
NOW = 1_700_000_000.0
API = "https://members-ng.iracing.com"


def make_handler(use_oauth=True):
    # __init__ auto-logins; keep construction offline by stubbing the entry point.
    with mock.patch.object(iRacingAPIHandler, "login", return_value=True):
        return iRacingAPIHandler(
            email="tester@example.com",
            password="pw",
            client_id="cid",
            client_secret="csecret",
            use_oauth=use_oauth,
        )


def token_json(
    access="tok",
    refresh="rtok",
    expires_in=600,
    refresh_expires_in=604800,
):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "refresh_token_expires_in": refresh_expires_in,
    }


def resp(status_code, payload=None, text=""):
    response = mock.Mock(status_code=status_code, text=text)
    response.json.return_value = payload if payload is not None else {}
    return response


class RequestRefreshTests(unittest.TestCase):
    """request() proactively refreshes near expiry and carries the new bearer."""

    def setUp(self):
        self.handler = make_handler(use_oauth=True)
        self.handler.logged_in = True
        self.handler.access_token = "old-tok"
        self.handler.refresh_token_expires_at = NOW + 86400  # keep refresh viable
        self.handler.refresh_token = "rtok"

    def run_get(self, posted):
        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch(
            "modules.api.requests.post", side_effect=posted
        ), mock.patch.object(
            requests.Session, "request", return_value=resp(200, {})
        ) as base:
            self.handler.get(API + "/data/x")
        return base

    def test_past_buffer_refreshes_and_sends_new_bearer(self):
        # token_expires_at 29s out is inside the 30s refresh buffer.
        self.handler.token_expires_at = NOW + 29
        base = self.run_get([resp(200, token_json(access="new-tok", refresh="rtok2"))])
        self.assertEqual(base.call_args.kwargs["headers"]["Authorization"], "Bearer new-tok")
        self.assertEqual(self.handler.access_token, "new-tok")
        self.assertEqual(self.handler.refresh_token, "rtok2")
        self.assertEqual(self.handler.token_expires_at, NOW + 600)

    def test_exactly_at_buffer_boundary_refreshes(self):
        # Code uses `time() >= token_expires_at - 30`, so NOW+30 is in-buffer.
        self.handler.token_expires_at = NOW + 30
        refreshed = resp(200, token_json(access="new-tok"))
        base = self.run_get([refreshed])
        self.assertEqual(base.call_args.kwargs["headers"]["Authorization"], "Bearer new-tok")

    def test_outside_buffer_skips_refresh_and_keeps_current_bearer(self):
        self.handler.token_expires_at = NOW + 31
        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch("modules.api.requests.post") as posted, mock.patch.object(
            requests.Session, "request", return_value=resp(200, {})
        ) as base:
            self.handler.get(API + "/data/x")
        posted.assert_not_called()
        self.assertEqual(base.call_args.kwargs["headers"]["Authorization"], "Bearer old-tok")


    def test_logged_out_skips_refresh_even_when_expired(self):
        # Pins the `self.logged_in` conjunct in request()'s refresh guard:
        # an expired token while logged out must not trigger a refresh POST.
        self.handler.logged_in = False
        self.handler.token_expires_at = NOW - 1
        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch("modules.api.requests.post") as posted, mock.patch.object(
            requests.Session, "request", return_value=resp(200, {})
        ):
            self.handler.get(API + "/data/x")
        posted.assert_not_called()
    def test_refresh_failure_falls_back_to_full_relogin(self):
        self.handler.token_expires_at = NOW - 1  # already expired

        seen_logged_in_at_login = []
        original_login = self.handler.login

        def spy_login():
            seen_logged_in_at_login.append(self.handler.logged_in)
            return original_login()

        self.handler.login = spy_login

        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch(
            "modules.api.requests.post",
            side_effect=[
                resp(400, None, text="invalid_grant"),  # refresh attempt fails
                resp(200, token_json(access="relogin-tok")),  # login() succeeds
            ],
        ) as posted, mock.patch.object(
            requests.Session, "request", return_value=resp(200, {})
        ) as base:
            self.handler.get(API + "/data/x")

        self.assertEqual(posted.call_count, 2)
        self.assertEqual(posted.call_args_list[0].kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(posted.call_args_list[1].kwargs["data"]["grant_type"], "password_limited")
        self.assertEqual(seen_logged_in_at_login, [False])  # request() cleared it first
        self.assertTrue(self.handler.logged_in)
        self.assertEqual(self.handler.access_token, "relogin-tok")
        self.assertEqual(base.call_args.kwargs["headers"]["Authorization"], "Bearer relogin-tok")

    def test_oauth_off_skips_refresh_even_when_expired(self):
        # use_oauth gates proactive refresh AND bearer attachment; login is
        # still OAuth but request() stays hands-off when the flag is False.
        handler = make_handler(use_oauth=False)
        handler.logged_in = True
        handler.access_token = "old-tok"
        handler.token_expires_at = NOW - 1
        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch("modules.api.requests.post") as posted, mock.patch.object(
            requests.Session, "request", return_value=resp(200, {})
        ) as base:
            handler.get(API + "/data/x")
        posted.assert_not_called()
        self.assertNotIn("headers", base.call_args.kwargs)


class TokenResponseProcessingTests(unittest.TestCase):
    """_process_token_response expiry math, including missing-field defaults."""

    def test_expiry_times_are_clock_plus_intervals(self):
        handler = make_handler()
        with mock.patch("modules.api.time", return_value=NOW):
            result = handler._process_token_response(
                {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_in": 120,
                    "refresh_token_expires_in": 3600,
                }
            )
        self.assertTrue(result)
        self.assertEqual(handler.access_token, "a")
        self.assertEqual(handler.refresh_token, "r")
        self.assertEqual(handler.token_expires_at, NOW + 120)
        self.assertEqual(handler.refresh_token_expires_at, NOW + 3600)
        self.assertTrue(handler.logged_in)

    def test_missing_expiry_fields_use_documented_defaults(self):
        # Real defaults: expires_in=600, refresh_token_expires_in=604800,
        # refresh_token defaults to None; logged_in flips regardless.
        handler = make_handler()
        handler.logged_in = False
        with mock.patch("modules.api.time", return_value=NOW):
            handler._process_token_response({"access_token": "a"})
        self.assertIsNone(handler.refresh_token)
        self.assertEqual(handler.token_expires_at, NOW + 600)
        self.assertEqual(handler.refresh_token_expires_at, NOW + 604800)
        self.assertTrue(handler.logged_in)


class RefreshTokenExpiryTests(unittest.TestCase):
    """_refresh_access_token refuses expired/absent refresh tokens pre-network."""

    def setUp(self):
        self.handler = make_handler(use_oauth=True)

    def test_expired_refresh_token_raises_before_any_post(self):
        # Boundary: `time() >= refresh_token_expires_at` treats NOW as expired.
        self.handler.refresh_token = "rtok"
        self.handler.refresh_token_expires_at = NOW
        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch("modules.api.requests.post") as posted:
            with self.assertRaisesRegex(Exception, "Refresh token has expired"):
                self.handler._refresh_access_token()
        posted.assert_not_called()

    def test_missing_refresh_token_raises(self):
        self.handler.refresh_token = None
        with mock.patch("modules.api.requests.post") as posted:
            with self.assertRaisesRegex(Exception, "No refresh token available"):
                self.handler._refresh_access_token()
        posted.assert_not_called()

    def test_refresh_allowed_just_before_expiry(self):
        self.handler.refresh_token = "rtok"
        self.handler.refresh_token_expires_at = NOW + 1  # still valid
        ok = resp(200, token_json(access="new-tok"))
        with mock.patch(
            "modules.api.time", return_value=NOW
        ), mock.patch("modules.api.requests.post", return_value=ok) as posted:
            self.assertTrue(self.handler._refresh_access_token())
        self.assertEqual(posted.call_count, 1)
        self.assertEqual(self.handler.access_token, "new-tok")


class PagedDataTests(unittest.TestCase):
    """_get_paged_data real contract: one-hop link follow, single-retry on 401."""

    def setUp(self):
        self.handler = make_handler(use_oauth=True)
        self.relogin_count = 0

        def relogin():
            self.relogin_count += 1
            # First relogin succeeds; any second one fails. Independently,
            # _get_paged_data's _auth_retry flag caps the cycle at one
            # re-attempt even when relogin keeps succeeding (issue #130).
            self.handler.logged_in = self.relogin_count == 1
            return self.handler.logged_in

        self.handler.login = mock.Mock(side_effect=relogin)

    def test_follows_link_one_hop_and_returns_that_page(self):
        # Real contract: when page one carries a "link" URL, it is fetched
        # exactly once and ITS payload is returned (page-one rows are not
        # merged); no further hops — the second page advertises another
        # link, which must NOT be followed (kills recursive-link regressions).
        self.handler.logged_in = True
        self.handler.get = mock.Mock(
            side_effect=[
                resp(200, {"rows": [1, 2], "link": API + "/next?offset=2"}),
                resp(200, {"rows": [3], "link": API + "/next?offset=3"}),
            ]
        )
        result = self.handler._get_paged_data(API + "/data/league/get?league_id=7")
        self.assertEqual(result, {"rows": [3], "link": API + "/next?offset=3"})
        self.assertEqual(self.handler.get.call_count, 2)
        self.assertEqual(
            self.handler.get.call_args_list[1].args, (API + "/next?offset=2",)
        )
        self.handler.login.assert_not_called()

    def test_stops_when_link_absent_and_returns_page_payload(self):
        self.handler.logged_in = True
        self.handler.get = mock.Mock(return_value=resp(200, {"roster": [1, 2, 3]}))
        result = self.handler._get_paged_data(API + "/data/league/get?league_id=7")
        self.assertEqual(result, {"roster": [1, 2, 3]})
        self.assertEqual(self.handler.get.call_count, 1)

    def test_link_fetch_failure_yields_empty_dict(self):
        self.handler.logged_in = True
        self.handler.get = mock.Mock(
            side_effect=[
                resp(200, {"rows": [1], "link": API + "/next"}),
                resp(500, text="boom"),
            ]
        )
        self.assertEqual(self.handler._get_paged_data(API + "/first"), {})

    def test_logs_in_first_when_not_logged_in(self):
        self.handler.logged_in = False
        self.handler.get = mock.Mock(return_value=resp(200, {"members": []}))
        result = self.handler._get_paged_data(API + "/data/member/get?cust_ids=1")
        self.assertEqual(result, {"members": []})
        self.assertEqual(self.handler.login.call_count, 1)
        self.assertEqual(self.handler.get.call_count, 1)

    def test_401_logs_in_once_then_retries_once(self):
        self.handler.logged_in = True
        self.handler.get = mock.Mock(
            side_effect=[resp(401), resp(200, {"members": [{"cust_id": 9}]})]
        )
        result = self.handler._get_paged_data(API + "/data/member/get?cust_ids=9")
        self.assertEqual(result, {"members": [{"cust_id": 9}]})
        self.assertEqual(self.handler.get.call_count, 2)
        self.assertEqual(self.handler.login.call_count, 1)

    def test_failed_relogin_raises_not_logged_in(self):
        # When the single re-attempt cannot restore the session, the top-of-
        # method guard raises rather than fetching with no auth.
        self.handler.logged_in = True

        def relogin_fails():
            self.handler.logged_in = False
            return False

        self.handler.login = mock.Mock(side_effect=relogin_fails)
        self.handler.get = mock.Mock(return_value=resp(401))
        with self.assertRaisesRegex(Exception, "Not logged in to iRacing API"):
            self.handler._get_paged_data(API + "/data/member/get?cust_ids=9")
        self.assertEqual(self.handler.get.call_count, 1)
        self.assertEqual(self.handler.login.call_count, 1)

    def test_second_401_after_successful_relogin_raises_http_error(self):
        # Issue #130: if re-login succeeds but the endpoint keeps answering
        # 401, the retry flag (not the login guard) must terminate with a
        # real HTTPError - not RecursionError.
        self.handler.logged_in = True

        def relogin_ok():
            self.handler.logged_in = True
            return True

        self.handler.login = mock.Mock(side_effect=relogin_ok)
        failure = resp(401)
        failure.raise_for_status.side_effect = requests.HTTPError("401 unauthorized")
        self.handler.get = mock.Mock(return_value=failure)
        with self.assertRaises(requests.HTTPError):
            self.handler._get_paged_data(API + "/data/member/get?cust_ids=9")
        self.assertEqual(self.handler.get.call_count, 2)
        self.assertEqual(self.handler.login.call_count, 1)

    def test_non_401_error_raises_for_status(self):
        self.handler.logged_in = True
        failure = resp(500, text="boom")
        failure.raise_for_status.side_effect = requests.HTTPError("500 boom")
        self.handler.get = mock.Mock(return_value=failure)
        with self.assertRaises(requests.HTTPError):
            self.handler._get_paged_data(API + "/data/member/get?cust_ids=9")
        self.assertEqual(self.handler.get.call_count, 1)
        self.handler.login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
