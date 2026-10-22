"""Unit tests for modules/league_memory (config, chunking, indexing, query).

No Discord connection or embedding server is required: embeddings come from a
deterministic fake embedder and channels are MagicMocks with async history
iterators.

Run: .venv\\Scripts\\python -m unittest tests.test_league_memory
"""
import asyncio
import datetime
import os
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
# History cutoff parsing (LEAGUE_VECTOR_HISTORY_SINCE + per-channel overrides)
# ---------------------------------------------------------------------------


class HistorySinceTests(unittest.TestCase):
    def test_unset_or_blank_indexes_all_time(self):
        self.assertIsNone(lm.parse_history_since(None))
        self.assertIsNone(lm.parse_history_since("   "))

    def test_bare_date_becomes_midnight_utc(self):
        cutoff = lm.parse_history_since("2026-01-01")
        self.assertEqual(
            cutoff.since,
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        )
        self.assertIsNone(cutoff.message_id)
        self.assertIsNone(cutoff.age)

    def test_datetime_offset_preserved_and_z_suffix_parsed(self):
        offset = lm.parse_history_since("2026-01-01T12:00:00+02:00")
        self.assertEqual(offset.since.utcoffset(), datetime.timedelta(hours=2))
        zulu = lm.parse_history_since("2026-01-01T12:00:00Z")
        self.assertEqual(zulu.since.utcoffset(), datetime.timedelta(0))

    def test_relative_age_units(self):
        cases = {
            "30d": datetime.timedelta(days=30),
            "24h": datetime.timedelta(hours=24),
            "2w": datetime.timedelta(weeks=2),
            "90m": datetime.timedelta(minutes=90),
            "45s": datetime.timedelta(seconds=45),
            "1.5 days": datetime.timedelta(days=1.5),
        }
        for raw, expected in cases.items():
            cutoff = lm.parse_history_since(raw)
            self.assertEqual(cutoff.age, expected, raw)
            self.assertIsNone(cutoff.since, raw)
            self.assertIsNone(cutoff.message_id, raw)

    def test_all_digit_value_is_message_id(self):
        cutoff = lm.parse_history_since("1234567890123456789")
        self.assertEqual(cutoff.message_id, 1234567890123456789)

    def test_garbage_yields_none(self):
        for raw in ["whenever", "0", "-5", "30 months", "2026-13-40"]:
            self.assertIsNone(lm.parse_history_since(raw), raw)

    def test_nonpositive_relative_age_yields_none(self):
        for raw in ["0d", "0.0h", "0 minutes"]:
            self.assertIsNone(lm.parse_history_since(raw), raw)

    def test_from_env_loads_cutoff(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={"LEAGUE_VECTOR_HISTORY_SINCE": "30d"}
        )
        self.assertEqual(cfg.history_cutoff.age, datetime.timedelta(days=30))

    def test_channel_overrides_parsed_from_env(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={
                "LEAGUE_VECTOR_HISTORY_SINCE": "30d",
                "LEAGUE_VECTOR_HISTORY_SINCE_111": "365d",
                "LEAGUE_VECTOR_HISTORY_SINCE_222": "2026-01-01",
            }
        )
        self.assertEqual(cfg.history_cutoff.age, datetime.timedelta(days=30))
        self.assertEqual(
            cfg.channel_history_cutoffs[111].age, datetime.timedelta(days=365)
        )
        self.assertEqual(
            cfg.channel_history_cutoffs[222].since,
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        )

    def test_channel_override_garbage_skipped(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={
                "LEAGUE_VECTOR_HISTORY_SINCE": "30d",
                "LEAGUE_VECTOR_HISTORY_SINCE_111": "",
                "LEAGUE_VECTOR_HISTORY_SINCE_222": "whenever",
                "LEAGUE_VECTOR_HISTORY_SINCE_zero": "30d",
            }
        )
        self.assertEqual(cfg.channel_history_cutoffs, {})
        self.assertEqual(cfg.history_cutoff.age, datetime.timedelta(days=30))

    def test_suffixed_key_for_nonpositive_channel_skipped(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={
                "LEAGUE_VECTOR_HISTORY_SINCE_0": "30d",
                "LEAGUE_VECTOR_HISTORY_SINCE_111": "365d",
            }
        )
        self.assertEqual(list(cfg.channel_history_cutoffs), [111])

    def test_cutoff_for_prefers_channel_override(self):
        default = lm.HistoryCutoff(age=datetime.timedelta(days=30))
        deeper = lm.HistoryCutoff(message_id=102)
        cfg = lm.VectorStoreConfig(
            history_cutoff=default, channel_history_cutoffs={111: deeper}
        )
        self.assertIs(cfg.cutoff_for(111), deeper)
        self.assertIs(cfg.cutoff_for(999), default)

    def test_channel_map_parsed_from_env(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={"LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL": "111=365d, 222=2026-01-01"}
        )
        self.assertEqual(
            cfg.channel_history_cutoffs[111].age, datetime.timedelta(days=365)
        )
        self.assertEqual(
            cfg.channel_history_cutoffs[222].since,
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        )

    def test_channel_map_invalid_pairs_skipped(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={"LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL": "111=,noeq,=30d,222=whenever"}
        )
        self.assertEqual(cfg.channel_history_cutoffs, {})

    def test_suffixed_key_overrides_channel_map(self):
        cfg = lm.VectorStoreConfig.from_env(
            env={
                "LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL": "111=30d",
                "LEAGUE_VECTOR_HISTORY_SINCE_111": "365d",
            }
        )
        self.assertEqual(
            cfg.channel_history_cutoffs[111].age, datetime.timedelta(days=365)
        )

    def test_includes_is_inclusive_on_both_bounds(self):
        old, new = MagicMock(), MagicMock()
        old.id, new.id = 99, 100
        old.created_at = datetime.datetime(2025, 12, 31, tzinfo=datetime.timezone.utc)
        new.created_at = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
        self.assertFalse(lm.HistoryCutoff(message_id=100).includes(old))
        self.assertTrue(lm.HistoryCutoff(message_id=100).includes(new))
        since = lm.HistoryCutoff(
            since=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        )
        self.assertFalse(since.includes(old))
        self.assertTrue(since.includes(new))
        age = lm.HistoryCutoff(age=datetime.timedelta(days=1))
        recent = MagicMock()
        recent.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=12)
        self.assertFalse(age.includes(old))
        self.assertTrue(age.includes(recent))

    def test_no_bounds_cutoff_includes_everything(self):
        ancient = MagicMock()
        ancient.id = 0
        ancient.created_at = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        self.assertTrue(lm.HistoryCutoff().includes(ancient))

    def test_naive_created_at_normalized_to_utc(self):
        since = lm.HistoryCutoff(
            since=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        )
        before, after = MagicMock(), MagicMock()
        before.created_at = datetime.datetime(2025, 12, 31, 23, 59)  # naive
        after.created_at = datetime.datetime(2026, 1, 1, 0, 1)  # naive
        self.assertFalse(since.includes(before))
        self.assertTrue(since.includes(after))
        age = lm.HistoryCutoff(age=datetime.timedelta(days=1))
        fresh = MagicMock()
        fresh.created_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self.assertTrue(age.includes(fresh))


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class ChunkingTests(unittest.TestCase):
    def test_short_message_single_chunk(self):
        self.assertEqual(lm.chunk_text("hello there", 100), ["hello there"])

    def test_blank_message_no_chunks(self):
        self.assertEqual(lm.chunk_text("   \n  ", 100), [])

    def test_blank_interior_paragraphs_are_dropped(self):
        self.assertEqual(lm.chunk_text("aaa\n\n\n\nbbb", 100), ["aaa\n\nbbb"])
        self.assertEqual(lm.chunk_text("aaa\n\n \n\nbbb\n\n", 100), ["aaa\n\nbbb"])

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

    async def test_blank_messages_are_never_collected(self):
        embedder = StubEmbedder()
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(channel_ids=(123,)), embedder=embedder
        )
        bot = make_bot(
            {
                123: [
                    ("announcement race times", "mod"),
                    ("", "bot"),  # attachment-only post
                    ("   \n ", "bot"),
                    ("rules updated", "mod"),
                ]
            }
        )
        self.assertEqual(await mem.index_channels(bot), 2)
        results = mem.query("race times")
        self.assertTrue(results)
        # blanks never reach chunk_messages, so they consume no source slot
        self.assertEqual(results[0][2]["source_index"], 0)
        # without the guard, chunk_messages would enumerate the blanks and
        # this slot would be 3, not 1
        rules = mem.query("rules updated")
        self.assertTrue(rules)
        self.assertEqual(rules[0][2]["source_index"], 1)

    async def test_embedder_is_built_from_config_on_first_use(self):
        stub = StubEmbedder()
        built = []
        def factory(config):
            built.append(config)
            return stub
        with patch.object(lm, "make_embedder", side_effect=factory):
            mem = lm.LeagueMemory(lm.VectorStoreConfig(channel_ids=(123,)))
            self.assertEqual(built, [])  # construction alone never embeds
            self.assertEqual(await mem.index_channels(make_bot({123: [("session tonight", "tyler")]})), 1)
            self.assertTrue(mem.query("session"))  # query reuses the same embedder
        self.assertEqual(len(built), 1)
        self.assertIs(built[0], mem.config)

    async def test_unexpected_indexing_crash_returns_partial_size(self):
        cfg = lm.VectorStoreConfig(channel_ids=(111, 222))
        mem = lm.LeagueMemory(cfg, embedder=StubEmbedder())
        bot = make_bot(
            {111: [("announcement race times", "mod")], 222: [("doomsday post", "mod")]}
        )
        real_chunk = lm.chunk_messages
        def explode(messages, max_chars):
            if messages and messages[0][2] == "doomsday post":
                raise RuntimeError("embedding service exploded")
            return real_chunk(messages, max_chars)
        with patch.object(lm, "chunk_messages", side_effect=explode):
            self.assertEqual(await mem.index_channels(bot), 1)  # ch111's work kept
        self.assertFalse(mem.indexed)
        self.assertFalse(mem.indexing)  # in-progress flag cleared despite the crash
        self.assertTrue(mem.query("race times"))


class HistoryCutoffIndexingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message(content, created_at, message_id):
        m = MagicMock()
        m.content = content
        m.author.nick = m.author.name = "mod"
        m.created_at = created_at
        m.id = message_id
        return m

    def _channel_newest_first(self, specs):
        """``specs``: [(content, created_at, id)] oldest-first; history walks newest-first."""

        class HistIter:
            def __init__(self, items):
                self.items = items

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.items:
                    return self.items.pop(0)
                raise StopAsyncIteration

        iterator = HistIter([self._message(*spec) for spec in reversed(specs)])
        channel = MagicMock()
        channel.history = MagicMock(return_value=iterator)
        return channel, iterator

    def _bot_newest_first(self, specs):
        channel, iterator = self._channel_newest_first(specs)
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=channel)
        return bot, iterator

    def _bot_channels(self, specs_by_id):
        """``specs_by_id``: {channel id: oldest-first specs}; one walk per channel."""
        channels = {}
        walks = {}
        for channel_id, specs in specs_by_id.items():
            channel, iterator = self._channel_newest_first(specs)
            channels[channel_id] = channel
            walks[channel_id] = iterator
        bot = MagicMock()
        bot.get_channel = MagicMock(side_effect=lambda channel_id: channels.get(channel_id))
        return bot, walks

    def _jan_messages(self, days):
        base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        return [
            (
                f"announcement session day {i}",
                base + datetime.timedelta(days=day),
                100 + i,
            )
            for i, day in enumerate(days)
        ]

    async def test_message_id_cutoff_indexes_from_that_message(self):
        bot, walk = self._bot_newest_first(self._jan_messages([1, 2, 3, 4, 5]))
        embedder = StubEmbedder()
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(
                channel_ids=(123,), history_cutoff=lm.HistoryCutoff(message_id=102)
            ),
            embedder=embedder,
        )
        self.assertEqual(await mem.index_channels(bot), 3)  # ids 102, 103, 104
        indexed = "\n".join(embedder.calls)
        self.assertIn("day 2", indexed)  # boundary message itself is indexed
        self.assertNotIn("day 0", indexed)
        self.assertNotIn("day 1", indexed)
        self.assertEqual(len(walk.items), 1)  # id 100 never fetched; id 101 halted the walk

    async def test_since_date_cutoff_is_inclusive_and_stops_the_walk(self):
        bot, walk = self._bot_newest_first(self._jan_messages([1, 2, 3, 4, 5]))
        embedder = StubEmbedder()
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(
                channel_ids=(123,),
                history_cutoff=lm.HistoryCutoff(
                    since=datetime.datetime(2026, 1, 4, tzinfo=datetime.timezone.utc)
                ),
            ),
            embedder=embedder,
        )
        self.assertEqual(await mem.index_channels(bot), 3)  # Jan 4, 5, 6
        indexed = "\n".join(embedder.calls)
        self.assertIn("day 2", indexed)  # day index 2 = Jan 4 (boundary included)
        self.assertNotIn("day 1", indexed)
        self.assertEqual(len(walk.items), 1)  # Jan 2 never fetched; Jan 3 halted the walk

    async def test_age_cutoff_is_relative_to_index_time(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        specs = [
            (f"announcement race times {days_ago}d ago", now - datetime.timedelta(days=days_ago), 100 + i)
            for i, days_ago in enumerate([100, 10, 1])  # oldest-first specs
        ]
        bot, _walk = self._bot_newest_first(specs)
        embedder = StubEmbedder()
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(
                channel_ids=(123,),
                history_cutoff=lm.HistoryCutoff(age=datetime.timedelta(days=30)),
            ),
            embedder=embedder,
        )
        self.assertEqual(await mem.index_channels(bot), 2)
        indexed = "\n".join(embedder.calls)
        self.assertIn("1d ago", indexed)
        self.assertIn("10d ago", indexed)
        self.assertNotIn("100d ago", indexed)

    async def test_channel_override_indexes_deeper_than_default(self):
        specs = self._jan_messages([1, 2, 3, 4, 5])  # Jan 2 .. Jan 6
        bot, walks = self._bot_channels({111: specs, 222: specs})
        embedder = StubEmbedder()
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(
                channel_ids=(111, 222),
                history_cutoff=lm.HistoryCutoff(
                    since=datetime.datetime(2026, 1, 5, tzinfo=datetime.timezone.utc)
                ),
                channel_history_cutoffs={
                    111: lm.HistoryCutoff(
                        since=datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
                    )
                },
            ),
            embedder=embedder,
        )
        self.assertEqual(await mem.index_channels(bot), 7)  # 5 via ch111's override + 2 via default
        indexed = "\n".join(embedder.calls)
        self.assertIn("day 0", indexed)  # Jan 2 is only reachable through ch111
        self.assertEqual(len(walks[111].items), 0)  # ch111 walked down to the oldest message
        self.assertEqual(len(walks[222].items), 2)  # default cutoff halted ch222 at Jan 4

    async def test_each_channel_gets_its_own_cutoff(self):
        specs = self._jan_messages([1, 2, 3, 4, 5])
        bot, walks = self._bot_channels({111: specs, 222: specs})
        embedder = StubEmbedder()
        mem = lm.LeagueMemory(
            lm.VectorStoreConfig(
                channel_ids=(111, 222),
                channel_history_cutoffs={
                    111: lm.HistoryCutoff(message_id=103),
                    222: lm.HistoryCutoff(
                        since=datetime.datetime(2026, 1, 5, tzinfo=datetime.timezone.utc)
                    ),
                },
            ),
            embedder=embedder,
        )
        self.assertEqual(await mem.index_channels(bot), 4)  # 2 by message id + 2 by date
        indexed = "\n".join(embedder.calls)
        self.assertIn("day 3", indexed)  # id 103 (Jan 5) is inclusive on ch111
        self.assertNotIn("day 0", indexed)  # neither channel reaches Jan 2
        self.assertEqual(len(walks[111].items), 2)  # id 102 halted ch111's walk
        self.assertEqual(len(walks[222].items), 2)  # Jan 4 halted ch222's walk


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


# ---------------------------------------------------------------------------
# Module-level singleton (get_league_memory)
# ---------------------------------------------------------------------------


class SingletonTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, lm, "_league_memory", lm._league_memory)
        lm._league_memory = None

    def test_disabled_without_channel_ids_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(lm.get_league_memory())
        self.assertIsNone(lm._league_memory)  # disabled: nothing is cached

    def test_enabled_singleton_built_once_and_reused(self):
        with patch.dict(os.environ, {"LEAGUE_VECTOR_CHANNEL_ID": "123"}, clear=True):
            first = lm.get_league_memory()
        self.assertIsNotNone(first)
        self.assertEqual(first.config.channel_ids, (123,))
        self.assertIs(lm.get_league_memory(), first)
        # env changes after construction never rebuild the shared instance
        with patch.dict(os.environ, {"LEAGUE_VECTOR_CHANNEL_ID": "456"}):
            self.assertIs(lm.get_league_memory(), first)
        self.assertIs(lm._league_memory, first)


if __name__ == "__main__":
    unittest.main()
