# pyright: basic
"""OpenAICompatibleEmbedder + make_embedder: index re-sorting, provider
key rules, base_url/model wiring — all with the openai client mocked."""

import unittest
from types import SimpleNamespace
from unittest import mock

import modules.league_memory as lm
from modules.league_memory import (
    OpenAICompatibleEmbedder,
    VectorStoreConfig,
    make_embedder,
)


def embedder_with(responses):
    emb = OpenAICompatibleEmbedder("http://base", "model-x", api_key="k")
    emb._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=mock.Mock(return_value=responses))
    )
    return emb


class EmbedOrderingTests(unittest.TestCase):
    def test_out_of_order_response_data_is_resorted_by_index(self):
        response = SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[10.0, 11.0]),
                SimpleNamespace(index=0, embedding=[0.0, 1.0]),
            ]
        )
        emb = embedder_with(response)
        self.assertEqual(
            emb.embed(["a", "b"]), [[0.0, 1.0], [10.0, 11.0]]
        )

    def test_called_with_configured_model_and_text_list(self):
        response = SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[1.0])]
        )
        emb = embedder_with(response)
        emb.embed(("x", "y"))
        emb._client.embeddings.create.assert_called_once_with(
            model="model-x", input=["x", "y"]
        )


class MakeEmbedderTests(unittest.TestCase):
    def test_openai_provider_without_key_raises(self):
        config = VectorStoreConfig(embedding_provider="openai")
        with mock.patch.dict(lm.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                make_embedder(config)

    def test_openai_provider_uses_openai_defaults_and_key(self):
        config = VectorStoreConfig(embedding_provider="openai")
        client_cls = mock.MagicMock()
        with mock.patch.dict(
            lm.os.environ, {"OPENAI_API_KEY": "sk-secret"}, clear=True
        ):
            with mock.patch("openai.OpenAI", client_cls):
                emb = make_embedder(config)
        self.assertEqual(emb.model, lm.PROVIDER_MODELS["openai"])
        self.assertEqual(emb.base_url, lm.DEFAULT_OPENAI_BASE_URL)
        client_cls.assert_called_once_with(
            base_url=lm.DEFAULT_OPENAI_BASE_URL, api_key="sk-secret"
        )

    def test_ollama_provider_falls_back_to_dummy_key(self):
        config = VectorStoreConfig()
        client_cls = mock.MagicMock()
        with mock.patch.dict(lm.os.environ, {}, clear=True):
            with mock.patch("openai.OpenAI", client_cls):
                emb = make_embedder(config)
        self.assertEqual(emb.base_url, lm.DEFAULT_OLLAMA_BASE_URL)
        client_cls.assert_called_once_with(
            base_url=lm.DEFAULT_OLLAMA_BASE_URL, api_key="ollama"
        )

    def test_ollama_provider_uses_openai_key_when_present(self):
        config = VectorStoreConfig()
        client_cls = mock.MagicMock()
        with mock.patch.dict(
            lm.os.environ, {"OPENAI_API_KEY": "sk-side"}, clear=True
        ):
            with mock.patch("openai.OpenAI", client_cls):
                make_embedder(config)
        client_cls.assert_called_once_with(
            base_url=lm.DEFAULT_OLLAMA_BASE_URL, api_key="sk-side"
        )

    def test_explicit_model_and_base_url_win(self):
        config = VectorStoreConfig(
            embedding_provider="ollama",
            embedding_model="mine",
            embedding_base_url="http://custom:1",
        )
        client_cls = mock.MagicMock()
        with mock.patch.dict(lm.os.environ, {}, clear=True):
            with mock.patch("openai.OpenAI", client_cls):
                emb = make_embedder(config)
        self.assertEqual(emb.model, "mine")
        self.assertEqual(emb.base_url, "http://custom:1")
        client_cls.assert_called_once_with(
            base_url="http://custom:1", api_key="ollama"
        )

    def test_ollama_default_model_is_used(self):
        config = VectorStoreConfig(embedding_provider="ollama")
        with mock.patch.dict(lm.os.environ, {}, clear=True):
            with mock.patch("openai.OpenAI", mock.MagicMock()):
                emb = make_embedder(config)
        self.assertEqual(emb.model, lm.PROVIDER_MODELS["ollama"])


class EmbedderDefaultKeyTests(unittest.TestCase):
    def test_missing_api_key_defaults_to_ollama_dummy(self):
        client_cls = mock.MagicMock()
        with mock.patch("openai.OpenAI", client_cls):
            emb = OpenAICompatibleEmbedder("http://base", "model-x")
        self.assertIs(emb._client, client_cls.return_value)
        client_cls.assert_called_once_with(base_url="http://base", api_key="ollama")

    def test_explicit_api_key_survives_constructor(self):
        client_cls = mock.MagicMock()
        with mock.patch("openai.OpenAI", client_cls):
            OpenAICompatibleEmbedder("http://base", "m", api_key="real")
        client_cls.assert_called_once_with(base_url="http://base", api_key="real")


if __name__ == "__main__":
    unittest.main()
