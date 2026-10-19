# pyright: basic
"""iRacingAPIHandler credential guards, login request shape, accessors, env factory."""

import unittest
from unittest import mock

import modules.api as apiModule
from modules.api import iRacingAPIHandler


def fake_token_response():
    response = mock.MagicMock()
    response.status_code = 200
    response.json.return_value = {"access_token": "tok", "expires_in": 600}
    return response


class LoginCredentialGuards(unittest.TestCase):
    def build(self, **kwargs):
        params = {
            "email": "a@b.c",
            "password": "pw",
            "client_id": "cid",
            "client_secret": "cs",
        }
        params.update(kwargs)
        with mock.patch.object(apiModule.requests, "post") as post:
            try:
                iRacingAPIHandler(**params)
            finally:
                post.assert_not_called()

    def test_each_single_missing_field_raises_before_network(self):
        for missing in ("client_id", "client_secret"):
            with self.subTest(missing=missing), self.assertRaisesRegex(
                Exception, "client_id and client_secret"
            ):
                self.build(**{missing: None})
        for missing in ("email", "password"):
            with self.subTest(missing=missing), self.assertRaisesRegex(
                Exception, "email and password"
            ):
                self.build(**{missing: ""})

    def test_client_guard_wins_when_everything_missing(self):
        # guard order is pinned: client pair is checked first
        with self.assertRaisesRegex(Exception, "client_id and client_secret"):
            self.build(email="", password="", client_id=None, client_secret=None)

    def test_non_200_login_raises_with_response_text(self):
        bad = mock.MagicMock()
        bad.status_code = 401
        bad.text = "creds rejected"
        with mock.patch.object(apiModule.requests, "post", return_value=bad) as post:
            with self.assertRaisesRegex(Exception, "creds rejected"):
                iRacingAPIHandler(
                    email="a@b.c",
                    password="pw",
                    client_id="cid",
                    client_secret="cs",
                )
        post.assert_called_once()

    def test_success_posts_password_limited_form_fields(self):
        with mock.patch.object(
            apiModule.requests, "post", return_value=fake_token_response()
        ) as post:
            handler = iRacingAPIHandler(
                email="Driver@Test.COM",
                password="pw",
                client_id="cid",
                client_secret="cs",
                use_oauth=True,
            )
        self.assertTrue(handler.logged_in)
        self.assertEqual(
            post.call_args.args[0],
            "https://oauth.iracing.com/oauth2/token",
        )
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 60))
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["grant_type"], "password_limited")
        self.assertEqual(data["client_id"], "cid")
        self.assertEqual(data["username"], "Driver@Test.COM")
        self.assertEqual(data["scope"], "iracing.auth")
        # exact expected masks (identifier-trim/lowercase included),
        # and plaintext secrets absent from every posted field
        self.assertEqual(data["client_secret"], handler._mask_secret("cs", "cid"))
        self.assertEqual(
            data["password"], handler._mask_secret("pw", "Driver@Test.COM")
        )
        self.assertNotIn("cs", data.values())
        self.assertNotIn("pw", data.values())


def bare_handler():
    return iRacingAPIHandler.__new__(iRacingAPIHandler)


class AccessorTests(unittest.TestCase):
    def test_get_driver_name_returns_first_display_name(self):
        handler = bare_handler()
        payload = {"members": [{"display_name": "Max"}, {"display_name": "2nd"}]}
        with mock.patch.object(
            handler, "_get_paged_data", return_value=payload
        ) as paged:
            self.assertEqual(handler.get_driver_name(7), "Max")
        self.assertIn("cust_ids=7", paged.call_args.args[0])

    def test_get_driver_name_empty_members_is_none(self):
        handler = bare_handler()
        with mock.patch.object(
            handler, "_get_paged_data", return_value={"members": []}
        ):
            self.assertIsNone(handler.get_driver_name(7))

    def test_get_driver_name_missing_key_is_none(self):
        handler = bare_handler()
        with mock.patch.object(handler, "_get_paged_data", return_value={}):
            self.assertIsNone(handler.get_driver_name(7))

    def test_league_members_roster_vs_pending(self):
        payload = {"roster": [{"r": 1}], "pending_requests": [{"p": 2}]}
        handler = bare_handler()
        with mock.patch.object(
            handler, "_get_paged_data", return_value=payload
        ) as paged:
            self.assertEqual(handler.get_league_members(9), [{"r": 1}])
            self.assertEqual(handler.get_league_members(9, pending=True), [{"p": 2}])
        self.assertIn("league_id=9", paged.call_args.args[0])


class HandlerFromEnvTests(unittest.TestCase):
    ENV_KEYS = (
        "IRACING_EMAIL",
        "IRACING_PASSWORD",
        "IRACING_CLIENT_ID",
        "IRACING_CLIENT_SECRET",
    )

    def env(self, values):
        return mock.patch.dict("os.environ", values, clear=True)

    def test_each_missing_env_var_returns_none_with_warning(self):
        full = {key: "set" for key in self.ENV_KEYS}
        for missing in self.ENV_KEYS:
            values = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing), self.env(values):
                with self.assertLogs(level="WARNING") as captured:
                    self.assertIsNone(apiModule.iracing_handler_from_env())
            self.assertIn("not fully configured", "\n".join(captured.output))

    def test_complete_credentials_build_authenticated_handler(self):
        values = {
            "IRACING_EMAIL": "a@b.c",
            "IRACING_PASSWORD": "pw",
            "IRACING_CLIENT_ID": "cid",
            "IRACING_CLIENT_SECRET": "cs",
        }
        handler_cls = mock.MagicMock(return_value="handler")
        with self.env(values):
            with mock.patch.object(apiModule, "iRacingAPIHandler", handler_cls):
                handler = apiModule.iracing_handler_from_env()
        self.assertEqual(handler, "handler")
        handler_cls.assert_called_once_with(
            email="a@b.c",
            password="pw",
            client_id="cid",
            client_secret="cs",
            use_oauth=True,
        )

    def test_constructor_failure_becomes_none_with_error_log(self):
        values = {key: "set" for key in self.ENV_KEYS}
        handler_cls = mock.MagicMock(side_effect=Exception("login exploded"))
        with self.env(values):
            with mock.patch.object(apiModule, "iRacingAPIHandler", handler_cls):
                with self.assertLogs(level="ERROR") as captured:
                    self.assertIsNone(apiModule.iracing_handler_from_env())
        self.assertIn("login exploded", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
