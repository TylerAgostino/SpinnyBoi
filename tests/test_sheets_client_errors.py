# pyright: basic
"""SheetsClient failure paths: HttpError wrapping, build failure, env credentials."""

import unittest
from unittest import mock

import httplib2
from googleapiclient.errors import HttpError

import modules.sheets_client as sheetsModule
from modules.sheets_client import SheetsClient, SheetsError


def http_error():
    resp = httplib2.Response({"status": 500, "reason": "boom"})
    return HttpError(resp, b"boom")


class BoomValues:
    def __init__(self, exc):
        self.exc = exc

    def get(self, **kwargs):
        raise self.exc

    def append(self, **kwargs):
        raise self.exc

    def update(self, **kwargs):
        raise self.exc

    def batchUpdate(self, **kwargs):  # noqa: N803
        raise self.exc


class BoomService:
    def __init__(self, exc):
        self._boom = BoomValues(exc)

    def spreadsheets(self):
        return self

    def values(self):
        return self._boom


def boom_client():
    client = SheetsClient(credentials=object())
    client._service = BoomService(http_error())
    return client


class HttpErrorWrappingTests(unittest.TestCase):
    def test_read_grid_wraps_http_error(self):
        with self.assertRaisesRegex(SheetsError, "while reading sheet"):
            boom_client().read_grid("sid", "Users")

    def test_append_wraps_http_error(self):
        client = boom_client()
        with mock.patch.object(client, "read_grid", return_value=(["A"], [])):
            with self.assertRaisesRegex(SheetsError, "while appending"):
                client.append_record("sid", "Users", {"A": "1"})

    def test_update_wraps_http_error(self):
        client = boom_client()
        with mock.patch.object(
            client, "read_grid", return_value=(["Id"], [["7"]])
        ):
            with self.assertRaisesRegex(SheetsError, "while updating"):
                client.update_record("sid", "Users", "Id", "7", {"Id": "8"})

    def test_batch_update_wraps_http_error(self):
        client = boom_client()
        with mock.patch.object(client, "read_grid", return_value=(["Pts"], [])):
            with self.assertRaisesRegex(SheetsError, "while writing updates"):
                client.update_column_cells("sid", "Standings", "Pts", {2: 1})

    def test_non_http_error_is_not_swallowed(self):
        client = SheetsClient(credentials=object())
        values_api = mock.MagicMock()
        values_api.get.side_effect = RuntimeError("socket died")
        service = mock.MagicMock()
        service.spreadsheets.return_value.values.return_value = values_api
        client._service = service
        with self.assertRaisesRegex(RuntimeError, "socket died"):
            client.read_grid("sid", "Users")

    def test_empty_cell_values_short_circuits_to_zero(self):
        client = boom_client()
        with mock.patch.object(client, "read_grid") as read:
            self.assertEqual(client.update_column_cells("sid", "S", "Pts", {}), 0)
        read.assert_not_called()

    def test_read_rows_returns_empty_on_empty_sheet(self):
        call = mock.MagicMock()
        call.execute.return_value = {"values": []}
        values_api = mock.MagicMock()
        values_api.get.return_value = call
        client = SheetsClient(credentials=object())
        service = mock.MagicMock()
        service.spreadsheets.return_value.values.return_value = values_api
        client._service = service
        self.assertEqual(client.read_rows("sid", "Users"), [])


class ServiceBuildTests(unittest.TestCase):
    def test_build_failure_raises_sheets_error_and_is_not_cached(self):
        client = SheetsClient(credentials=object())
        with mock.patch.object(
            sheetsModule, "build", side_effect=Exception("api offline")
        ) as build:
            with self.assertRaisesRegex(SheetsError, "Error building"):
                client._get_service()
            # failed build must not poison the cache: retry really rebuilds
            with self.assertRaisesRegex(SheetsError, "Error building"):
                client._get_service()
        self.assertEqual(build.call_count, 2)
        self.assertIsNone(client._service)

    def test_successful_build_is_cached(self):
        client = SheetsClient(credentials=object())
        with mock.patch.object(
            sheetsModule, "build", return_value="svc"
        ) as build:
            self.assertEqual(client._get_service(), "svc")
            self.assertEqual(client._get_service(), "svc")
        build.assert_called_once()


class FromEnvTests(unittest.TestCase):
    def test_from_env_passes_credentials_through(self):
        with mock.patch.object(
            sheetsModule, "credentials_from_env", return_value="creds"
        ):
            client = SheetsClient.from_env()
        self.assertTrue(client.available)
        self.assertIs(client._credentials, "creds")


class CredentialsFromEnvTests(unittest.TestCase):
    def run_env(self, environ, exists=False):
        with mock.patch.dict("os.environ", environ, clear=True):
            with mock.patch.object(
                sheetsModule.os.path, "exists", return_value=exists
            ):
                return sheetsModule.credentials_from_env()

    def test_file_credentials_take_precedence(self):
        creds_cls = mock.MagicMock()
        with mock.patch.object(sheetsModule, "Credentials", creds_cls):
            result = self.run_env(
                {"GOOGLE_CREDENTIALS_FILE": "sa.json", "GOOGLE_CREDENTIALS_JSON": "{}"},
                exists=True,
            )
        self.assertEqual(result, creds_cls.from_service_account_file.return_value)
        creds_cls.from_service_account_file.assert_called_once_with(
            "sa.json", scopes=sheetsModule.SCOPES
        )
        creds_cls.from_service_account_info.assert_not_called()

    def test_json_env_used_when_no_file(self):
        creds_cls = mock.MagicMock()
        with mock.patch.object(sheetsModule, "Credentials", creds_cls):
            result = self.run_env({"GOOGLE_CREDENTIALS_JSON": '{"a": 1}'})
        self.assertEqual(result, creds_cls.from_service_account_info.return_value)
        creds_cls.from_service_account_info.assert_called_once_with(
            {"a": 1}, scopes=sheetsModule.SCOPES
        )

    def test_no_credentials_anywhere_returns_none_with_error(self):
        with self.assertLogs(level="ERROR") as captured:
            self.assertIsNone(self.run_env({}))
        self.assertIn("No Google Sheets credentials found", "\n".join(captured.output))

    def test_file_load_failure_swallowed_to_none(self):
        creds_cls = mock.MagicMock()
        creds_cls.from_service_account_file.side_effect = Exception("bad sa file")
        with mock.patch.object(sheetsModule, "Credentials", creds_cls):
            with self.assertLogs(level="ERROR") as captured:
                self.assertIsNone(
                    self.run_env({"GOOGLE_CREDENTIALS_FILE": "broken.json"}, exists=True)
                )
        self.assertIn("bad sa file", "\n".join(captured.output))

    def test_json_load_failure_swallowed_to_none(self):
        creds_cls = mock.MagicMock()
        creds_cls.from_service_account_info.side_effect = Exception("bad json blob")
        with mock.patch.object(sheetsModule, "Credentials", creds_cls):
            with self.assertLogs(level="ERROR") as captured:
                self.assertIsNone(self.run_env({"GOOGLE_CREDENTIALS_JSON": '{"a":1}'}))
        self.assertIn("bad json blob", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
