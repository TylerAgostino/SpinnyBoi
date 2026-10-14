"""Unit tests for modules/league_memory (config, chunking, indexing, query).

No Discord connection or embedding server is required: embeddings come from a
deterministic fake embedder and channels are MagicMocks with async history
iterators.

Run: .venv\\Scripts\\python -m unittest tests.test_league_memory
"""
import asyncio
import datetime
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock

from modules import league_memory as lm


class StubEmbedder:
    """Deterministic bag-of-words embedder over a tiny fixed vocabulary."""

    VOCAB = ["session", "tonight", "rules", "registration", "announcement", "race", "times"]

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        vectors = []
        for t in texts:
            self.calls.append(t)
            lower = t.lower()
            vectors.append([1.0 if word in lower else 0.0 for word in self.VOCAB])
        return vectors


def make_channel_messages(specs):
    """Build discord.py Message doubles from (content, name) tuples."""
    messages = []
    for i, (content, name) in enumerate(specs):
        m = MagicMock()
        m.content = content
        m.author.nick = name
        m.author.name = name
        m.created_at = datetime.datetime(2026, 1, 1, 12, 0) + datetime.timedelta(hours=i)
        messages.append(m)
    return messages


def make_channel(channel_messages):
    class HistIter:
        def __init__(self, items):
            self.items = items

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.items:
                return self.items.pop(0)
            raise StopAsyncIteration

    channel = MagicMock()
    channel.history = MagicMock(return_value=HistIter(make_channel_messages(channel_messages)))
    return channel


def make_bot(channels):
    """``channels`` maps channel id -> [(content, name), ...]; unknown ids resolve to None."""
    by_id = {channel_id: make_channel(msgs) for channel_id, msgs in channels.items()}
    bot = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda channel_id: by_id.get(channel_id))
    bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: by_id.get(channel_id))
    return bot


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_defaults_when_unset(self):
        cfg = lm.VectorStoreConfig.from_env(env={})
        self.assertEqual(cfg.channel_ids, ())
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.embedding_provider, "ollama")
        self.assertEqual(cfg.top_k, 5)
        self.assertEqual(cfg.relevance_threshold, 0.35)

    def test_all_fields_loaded(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={
                "LEAGUE_VECTOR_CHANNEL_ID": "987654321",
                "LEAGUE_VECTOR_EMBEDDING_PROVIDER": "openai",
                "LEAGUE_VECTOR_EMBEDDING_MODEL": "text-embedding-3-large",
                "LEAGUE_VECTOR_EMBEDDING_BASE_URL": "http://example/v1",
                "LEAGUE_VECTOR_TOP_K": "3",
                "LEAGUE_VECTOR_RELEVANCE_THRESHOLD": "0.5",
            }
        )
        self.assertEqual(cfg.channel_ids, (987654321,))
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.embedding_provider, "openai")
        self.assertEqual(cfg.resolved_model, "text-embedding-3-large")
        self.assertEqual(cfg.resolved_base_url, "http://example/v1")
        self.assertEqual(cfg.top_k, 3)
        self.assertEqual(cfg.relevance_threshold, 0.5)

    def test_channel_ids_accept_comma_separated_list(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={"LEAGUE_VECTOR_CHANNEL_ID": "111, 222,111,not-an-id,-3,"}
        )
        self.assertEqual(cfg.channel_ids, (111, 222))
        self.assertTrue(cfg.enabled)

    def test_provider_defaults_per_provider(self):
        self.assertEqual(
            lm.VectorStoreConfig(embedding_provider="ollama").resolved_model,
            "nomic-ai/nomic-embed-text-v1.5-GGUF:Q4_K_M",
        )
        self.assertEqual(
            lm.VectorStoreConfig(embedding_provider="openai").resolved_model,
            "text-embedding-3-small",
        )

    def test_invalid_values_fall_back(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={
                "LEAGUE_VECTOR_CHANNEL_ID": "not-an-id",
                "LEAGUE_VECTOR_EMBEDDING_PROVIDER": "watson",
                "LEAGUE_VECTOR_TOP_K": "-4",
                "LEAGUE_VECTOR_RELEVANCE_THRESHOLD": "abc",
            }
        )
        self.assertEqual(cfg.channel_ids, ())
        self.assertEqual(cfg.embedding_provider, "ollama")
        self.assertEqual(cfg.top_k, 1)  # -4 below minimum; clamped up to 1
        self.assertEqual(cfg.relevance_threshold, 0.35)

    def test_threshold_clamped_to_range(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={"LEAGUE_VECTOR_RELEVANCE_THRESHOLD": "1.7"}
        )
        self.assertEqual(cfg.relevance_threshold, 1.0)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class ChunkingTests(unittest.TestCase):
    def test_short_message_single_chunk(self):
        self.assertEqual(lm.chunk_text("hello there", 100), ["hello there"])

    def test_blank_message_no_chunks(self):
        self.assertEqual(lm.chunk_text("   \n  ", 100), [])

    def test_paragraphs_packed_up_to_limit(self):
        text = "aaaa\n\nbbbb\n\ncccc"
        chunks = lm.chunk_text(text, 12)
        self.assertEqual(chunks, ["aaaa\n\nbbbb", "cccc"])
        self.assertTrue(all(len(c) <= 12 for c in chunks))

    def test_oversized_paragraph_hard_split_on_words(self):
        text = " ".join(["word"] * 40)
        chunks = lm.chunk_text(text, 30)
        self.assertTrue(all(len(c) <= 30 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_chunk_messages_formats_and_skips_empty(self):
        out = lm.chunk_messages(
            [("tyler", "2026-01-01 12:00", "session at 8"), ("bot", "2026-01-02 12:00", "  ")],
            100,
        )
        self.assertEqual(len(out), 1)
        text, meta = out[0]
        self.assertEqual(text, "[2026-01-01 12:00 tyler] session at 8")
        self.assertEqual(meta["author"], "tyler")
        self.assertEqual(meta["source_index"], 0)


# ---------------------------------------------------------------------------
# Vector store + query filtering
# ---------------------------------------------------------------------------


class VectorStoreTests(unittest.TestCase):
    def _store(self):
        store = lm.InMemoryVectorStore()
        items = [
            ("the session is tonight", [1.0, 0.0], {"id": 1}),
            ("rules for registration", [0.0, 1.0], {"id": 2}),
            ("unrelated chatter", [0.0, 0.0], {"id": 3}),
        ]
        store.add(items)
        return store

    def test_size(self):
        self.assertEqual(self._store().size, 3)

    def test_top_k_respected(self):
        store = lm.InMemoryVectorStore()
        store.add([("a", [1.0, 0.0], {}), ("b", [0.9, 0.1], {}), ("c", [0.8, 0.2], {})])
        results = store.query([1.0, 0.0], top_k=2, threshold=0.0)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][1], "a")

    def test_threshold_filters_results(self):
        results = self._store().query([1.0, 0.0], top_k=5, threshold=0.5)
        self.assertEqual([t for _, t, _ in results], ["the session is tonight"])

    def test_no_results_below_threshold(self):
        results = self._store().query([0.3, 0.3], top_k=5, threshold=0.9)
        self.assertEqual(results, [])

    def test_empty_store_returns_empty(self):
        self.assertEqual(lm.InMemoryVectorStore().query([1.0], top_k=5, threshold=0.0), [])

    def test_dimension_mismatch_returns_empty(self):
        self.assertEqual(self._store().query([1.0, 0.0, 0.0], top_k=5, threshold=0.0), [])


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


class IndexingTests(unittest.IsolatedAsyncioTestCase):
    async def test_index_channels_populates_store(self):
        cfg = lm.VectorStoreConfig(channel_ids=(123,), max_chunk_chars=700)
        mem = lm.LeagueMemory(cfg, embedder=StubEmbedder())
        bot = make_bot(
            {123: [("session tonight at 8", "tyler"), ("rules updated", "mod")]}
        )
        n = await mem.index_channels(bot)
        self.assertEqual(n, 2)
        self.assertTrue(mem.indexed)
        self.assertFalse(mem.indexing)

    async def test_disabled_without_channel_ids(self):
        mem = lm.LeagueMemory(lm.VectorStoreConfig(), embedder=StubEmbedder())
        self.assertEqual(await mem.index_channels(MagicMock()), 0)
        self.assertIsNone(mem.start_indexing(MagicMock()))

    async def test_index_failure_never_raises(self):
        cfg = lm.VectorStoreConfig(channel_ids=(123,))
        mem = lm.LeagueMemory(cfg, embedder=StubEmbedder())
        bot = MagicMock()
        bot.get_channel = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(await mem.index_channels(bot), 0)
        self.assertFalse(mem.indexed)

    async def test_start_indexing_does_not_block(self):
        cfg = lm.VectorStoreConfig(channel_ids=(123,))
        gate = threading.Event()

        class SlowEmbedder(StubEmbedder):
            def embed(self, texts):
                gate.wait()  # runs in a worker thread; parks here until set
                return super().embed(texts)

        bot = make_bot({123: [("announcement race times", "mod")]})
        mem = lm.LeagueMemory(cfg, embedder=SlowEmbedder())
        task = mem.start_indexing(bot)
        self.assertIsNotNone(task)
        await asyncio.sleep(0)  # let the task start; caller never waits on it
        self.assertTrue(task is not None and not task.done())
        self.assertTrue(mem.indexing)  # callable returned while indexing in flight
        gate.set()
        await task
        self.assertTrue(mem.indexed)

    async def test_empty_channel_indexes_nothing(self):
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(channel_ids=(123,)), embedder=StubEmbedder()
        )
        self.assertEqual(await mem.index_channels(make_bot({123: []})), 0)


    async def test_index_channels_reads_every_configured_channel(self):
        cfg = lm.VectorStoreConfig(channel_ids=(111, 222))
        mem = lm.LeagueMemory(cfg, embedder=StubEmbedder())
        bot = make_bot(
            {
                111: [("the session is tonight", "tyler")],
                222: [("rules for registration", "mod")],
            }
        )
        self.assertEqual(await mem.index_channels(bot), 2)
        self.assertTrue(mem.indexed)
        hits = {mem.query("session")[0][2]["channel_id"], mem.query("rules")[0][2]["channel_id"]}
        self.assertEqual(hits, {111, 222})

    async def test_one_bad_channel_does_not_block_the_others(self):
        cfg = lm.VectorStoreConfig(channel_ids=(111, 404))
        mem = lm.LeagueMemory(cfg, embedder=StubEmbedder())
        bot = make_bot({111: [("announcement race times", "mod")]})  # 404 missing
        self.assertEqual(await mem.index_channels(bot), 1)
        self.assertFalse(mem.indexed)  # 404 never resolved; retry on next startup
        self.assertTrue(mem.query("race times"))

# ---------------------------------------------------------------------------
# LeagueMemory.query + tool surface
# ---------------------------------------------------------------------------


class QueryAndToolTests(unittest.IsolatedAsyncioTestCase):
    async def _memory_with_index(self):
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(channel_ids=(123,), top_k=2, relevance_threshold=0.5),
            embedder=StubEmbedder(),
        )
        await mem.index_channels(
            make_bot({123: [("the session is tonight", "tyler"), ("rules for registration", "mod")]})
        )
        return mem

    async def test_relevant_query_returns_snippets(self):
        mem = await self._memory_with_index()
        results = mem.query("session tonight?")
        self.assertTrue(results)
        self.assertIn("session is tonight", results[0][1])

    async def test_irrelevant_query_returns_empty(self):
        mem = await self._memory_with_index()
        self.assertEqual(mem.query("unrelated chatter nothing"), [])

    async def test_blank_query_returns_empty(self):
        mem = await self._memory_with_index()
        self.assertEqual(mem.query("   "), [])

    async def test_query_embedding_failure_returns_empty(self):
        mem = await self._memory_with_index()

        class Broken:
            def embed(self, texts):
                raise RuntimeError("server down")

        mem._embedder = Broken()
        self.assertEqual(mem.query("session tonight"), [])

    async def test_tool_returns_structured_results(self):
        mem = await self._memory_with_index()
        tool = mem.lookup_tool()
        out = tool("session tonight")
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["results"])
        self.assertIn("snippet", out["results"][0])

    async def test_tool_empty_results_when_irrelevant(self):
        mem = await self._memory_with_index()
        tool = mem.lookup_tool()
        out = tool("nothing matches this at all")
        self.assertEqual(out["results"], [])

    async def test_tool_has_schema_name(self):
        mem = await self._memory_with_index()
        tool = mem.lookup_tool()
        self.assertEqual(tool.tool_name, "lookup_league_info")


if __name__ == "__main__":
    unittest.main()
