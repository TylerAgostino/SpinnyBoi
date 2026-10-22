# pyright: basic
"""Issue #192 regression pins: docker compose injects empty strings for unset
host vars, and ``os.getenv(K, default)`` treats "" as set — silently dropping
the documented defaults. Each pin sets the var to "" and asserts the code
still uses the documented default; set/absent behavior is unchanged and
covered by the existing suites."""
import importlib
import os
import unittest
from unittest import mock

import modules.ChatHandler as ChatHandler
import modules.league_memory as lm
import modules.sheets_client as sheets_client
from modules.scheduler.scheduler import _db_path_from_env

DEFAULT_OLLAMA_BASE_URL = "http://192.168.1.152:11080/v1"
DEFAULT_OLLAMA_MODEL = "unsloth/Qwen3.8-Flash-Next-GGUF:IQ3_XXS"


class ChatHandlerBlankEnvTests(unittest.TestCase):
    """One dedicated file owns the ChatHandler reload: it re-runs the heavy
    import (two OpenAIModel constructions), so it happens exactly once, and
    cleanup reloads under the restored environment so the other suites that
    imported this module see the same values they were built with."""

    def test_blank_url_and_model_yield_documented_defaults(self):
        self.addCleanup(importlib.reload, ChatHandler)
        with mock.patch.dict(
            os.environ,
            {"OLLAMA_BASE_URL": "", "OLLAMA_MODEL": "", "OPENAI_API_KEY": ""},
            clear=False,
        ):
            reloaded = importlib.reload(ChatHandler)
        self.assertEqual(reloaded.OLLAMA_BASE_URL, DEFAULT_OLLAMA_BASE_URL)
        self.assertEqual(reloaded.OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL)

    def test_blank_api_key_yields_ollama_dummy(self):
        # The site lives inside _make_model; capture the construction instead
        # of reloading again.
        model_cls = mock.MagicMock()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            with mock.patch.object(ChatHandler, "OpenAIModel", model_cls):
                ChatHandler._make_model(0.3)
        client_args = model_cls.call_args.kwargs["client_args"]
        self.assertEqual(client_args["api_key"], "ollama")


class LeagueMemoryBlankApiKeyTests(unittest.TestCase):
    def test_blank_openai_api_key_yields_ollama_dummy(self):
        # Patch the embedder class itself so the blank env must be fixed at
        # the make_embedder site, not by the constructor's ``api_key or``.
        embedder_cls = mock.MagicMock()
        config = lm.VectorStoreConfig(embedding_provider="ollama")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            with mock.patch.object(lm, "OpenAICompatibleEmbedder", embedder_cls):
                lm.make_embedder(config)
        self.assertEqual(embedder_cls.call_args.kwargs["api_key"], "ollama")


class SchedulerBlankDatabaseUrlTests(unittest.TestCase):
    def test_blank_database_url_yields_documented_default(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
            self.assertEqual(_db_path_from_env(), "data/spinny.db")

    def test_unset_database_url_unchanged(self):
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(_db_path_from_env(), "data/spinny.db")


class SheetsClientBlankCredentialsFileTests(unittest.TestCase):
    def setUp(self):
        if not sheets_client.GOOGLE_APIS_AVAILABLE:
            self.skipTest("google API libraries not installed")

    def test_blank_credentials_file_yields_default_filename(self):
        seen_paths = []

        def fake_exists(path):
            seen_paths.append(path)
            return path == "credentials.json"

        creds_cls = mock.MagicMock()
        with mock.patch.dict(os.environ, {"GOOGLE_CREDENTIALS_FILE": ""}, clear=False):
            with mock.patch.object(sheets_client.os.path, "exists", fake_exists):
                with mock.patch.object(
                    sheets_client, "Credentials", creds_cls
                ):
                    creds = sheets_client.credentials_from_env()
        self.assertEqual(seen_paths, ["credentials.json"])
        self.assertIs(creds, creds_cls.from_service_account_file.return_value)
        creds_cls.from_service_account_file.assert_called_once()
        self.assertEqual(
            creds_cls.from_service_account_file.call_args.args[0],
            "credentials.json",
        )


if __name__ == "__main__":
    unittest.main()
