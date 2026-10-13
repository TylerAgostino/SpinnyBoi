# pyright: basic
"""League memory: an in-memory vector store over one Discord channel.

At startup the bot reads all messages from a configured channel, chunks and
embeds them, and exposes a strands tool the ChatHandler agent can call for
administrative questions (session times, rules, announcements).

Configuration is environment-driven (see ``VectorStoreConfig.from_env``):

    LEAGUE_VECTOR_CHANNEL_ID            Discord channel id to index (feature is
                                        disabled when unset/invalid)
    LEAGUE_VECTOR_EMBEDDING_PROVIDER    "ollama" (default) or "openai"
    LEAGUE_VECTOR_EMBEDDING_MODEL       embedding model id (provider default if unset)
    LEAGUE_VECTOR_EMBEDDING_BASE_URL    OpenAI-compatible base URL override
    LEAGUE_VECTOR_TOP_K                 max snippets per query (default 5)
    LEAGUE_VECTOR_RELEVANCE_THRESHOLD   min cosine similarity (default 0.35)
    LEAGUE_VECTOR_MAX_CHUNK_CHARS       soft chunk size cap (default 700)

Indexing is launched as a background task (``LeagueMemory.start_indexing``) so
it never blocks message handling.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

import numpy as np
from strands import tool as strands_tool

from .env_config import env_float, env_int

log = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE_URL = "http://192.168.1.152:11081/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
PROVIDER_MODELS = {
    "ollama": "nomic-ai/nomic-embed-text-v1.5-GGUF:Q4_K_M",
    "openai": "text-embedding-3-small",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class VectorStoreConfig:
    channel_id: Optional[int] = None
    embedding_provider: str = "ollama"
    embedding_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    top_k: int = 5
    relevance_threshold: float = 0.35
    max_chunk_chars: int = 700

    @property
    def enabled(self) -> bool:
        return self.channel_id is not None

    @property
    def resolved_model(self) -> str:
        return self.embedding_model or PROVIDER_MODELS.get(
            self.embedding_provider, PROVIDER_MODELS["ollama"]
        )

    @property
    def resolved_base_url(self) -> str:
        return self.embedding_base_url or (
            DEFAULT_OPENAI_BASE_URL
            if self.embedding_provider == "openai"
            else DEFAULT_OLLAMA_BASE_URL
        )

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "VectorStoreConfig":
        env = os.environ if env is None else env

        channel_id: Optional[int] = None
        raw_channel = (env.get("LEAGUE_VECTOR_CHANNEL_ID") or "").strip()
        if raw_channel:
            try:
                channel_id = int(raw_channel)
                if channel_id <= 0:
                    log.warning(
                        "Ignoring non-positive LEAGUE_VECTOR_CHANNEL_ID=%r", raw_channel
                    )
                    channel_id = None
            except ValueError:
                log.warning("Invalid LEAGUE_VECTOR_CHANNEL_ID=%r", raw_channel)

        provider = (env.get("LEAGUE_VECTOR_EMBEDDING_PROVIDER") or "ollama").strip().lower()
        if provider not in PROVIDER_MODELS:
            log.warning(
                "Unknown LEAGUE_VECTOR_EMBEDDING_PROVIDER=%r, falling back to 'ollama'",
                provider,
            )
            provider = "ollama"

        top_k = env_int(env, "LEAGUE_VECTOR_TOP_K", 5, minimum=1)
        threshold = env_float(env, "LEAGUE_VECTOR_RELEVANCE_THRESHOLD", 0.35, 0.0, 1.0)
        max_chunk = env_int(env, "LEAGUE_VECTOR_MAX_CHUNK_CHARS", 700, minimum=50)

        return cls(
            channel_id=channel_id,
            embedding_provider=provider,
            embedding_model=(env.get("LEAGUE_VECTOR_EMBEDDING_MODEL") or None),
            embedding_base_url=(env.get("LEAGUE_VECTOR_EMBEDDING_BASE_URL") or None),
            top_k=top_k,
            relevance_threshold=threshold,
            max_chunk_chars=max_chunk,
        )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int) -> List[str]:
    """Split one message body into chunks of at most ``max_chars``.

    Paragraphs are packed greedily; an oversized paragraph is hard-split on
    whitespace. Blank messages yield no chunks.
    """
    text = text.strip()
    if not text:
        return []

    pieces: List[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        words = para.split()
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > max_chars:
                pieces.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            pieces.append(current)

    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def format_chunk(author: str, timestamp: str, text: str) -> str:
    """Render a chunk with enough context for the embedder and the LLM."""
    return f"[{timestamp} {author}] {text}"


def chunk_messages(
    messages: Sequence[Tuple[str, str, str]], max_chars: int
) -> List[Tuple[str, Dict[str, Any]]]:
    """Chunk ``(author, timestamp, content)`` triples.

    Returns ``(chunk_text, metadata)`` pairs; empty-content messages (e.g.
    attachment-only posts) are skipped.
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for index, (author, timestamp, content) in enumerate(messages):
        for chunk in chunk_text(content, max_chars):
            out.append(
                (
                    format_chunk(author, timestamp, chunk),
                    {"author": author, "timestamp": timestamp, "source_index": index},
                )
            )
    return out


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> List[List[float]]: ...


class OpenAICompatibleEmbedder:
    """Embeddings via any OpenAI-compatible /embeddings endpoint (Ollama, OpenAI)."""

    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None):
        from openai import OpenAI

        self.base_url = base_url
        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key or "ollama")

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        response = self._client.embeddings.create(model=self.model, input=list(texts))
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]


def make_embedder(config: VectorStoreConfig) -> Embedder:
    if config.embedding_provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when LEAGUE_VECTOR_EMBEDDING_PROVIDER=openai"
            )
    else:
        api_key = os.getenv("OPENAI_API_KEY", "ollama")
    return OpenAICompatibleEmbedder(
        base_url=config.resolved_base_url,
        model=config.resolved_model,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------


class InMemoryVectorStore:
    """Dense vector store with cosine-similarity top-k search."""

    def __init__(self):
        self._texts: List[str] = []
        self._metadata: List[Dict[str, Any]] = []
        self._matrix: Optional[np.ndarray] = None

    def add(self, items: Sequence[Tuple[str, Sequence[float], Dict[str, Any]]]) -> None:
        for text, vector, metadata in items:
            self._texts.append(text)
            self._metadata.append(metadata)
            rows = np.asarray(vector, dtype=np.float64).reshape(1, -1)
            if self._matrix is None:
                self._matrix = rows
            else:
                self._matrix = np.vstack([self._matrix, rows])

    @property
    def size(self) -> int:
        return len(self._texts)

    def query(
        self, vector: Sequence[float], top_k: int, threshold: float
    ) -> List[Tuple[float, str, Dict[str, Any]]]:
        """Return up to ``top_k`` ``(score, text, metadata)`` with score >= threshold."""
        if self._matrix is None or self.size == 0:
            return []
        query = np.asarray(vector, dtype=np.float64)
        if query.shape[0] != self._matrix.shape[1]:
            log.warning(
                "Query vector dim %s != stored dim %s; returning no results",
                query.shape[0],
                self._matrix.shape[1],
            )
            return []

        norms = np.linalg.norm(self._matrix, axis=1) * np.linalg.norm(query)
        # Zero-norm vectors can't be compared meaningfully; score them 0.
        similarities = np.zeros(self.size, dtype=np.float64)
        nonzero = norms > 0
        similarities[nonzero] = (self._matrix[nonzero] @ query) / norms[nonzero]

        order = np.argsort(similarities)[::-1][:top_k]
        results = []
        for idx in order:
            score = float(similarities[idx])
            if score < threshold:
                continue
            results.append((score, self._texts[idx], self._metadata[idx]))
        return results


# ---------------------------------------------------------------------------
# League memory (indexing + query + tool)
# ---------------------------------------------------------------------------


class LeagueMemory:
    def __init__(
        self,
        config: VectorStoreConfig,
        embedder: Optional[Embedder] = None,
        store: Optional[InMemoryVectorStore] = None,
    ):
        self.config = config
        self._embedder = embedder
        self.store = store if store is not None else InMemoryVectorStore()
        self._indexing = False
        self._indexed = False
        self._task: Optional[asyncio.Task] = None

    @property
    def indexed(self) -> bool:
        return self._indexed

    @property
    def indexing(self) -> bool:
        return self._indexing

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = make_embedder(self.config)
        return self._embedder

    async def index_channel(self, bot_user) -> int:
        """Read every message in the configured channel and embed it.

        ``bot_user`` is the discord.py Bot/User. Returns the number of chunks
        indexed. Errors are logged, not raised, so a failed index never takes
        the bot down (queries simply return no results until re-indexed).
        """
        if not self.config.enabled:
            log.info("League memory disabled (no channel id configured)")
            return 0

        self._indexing = True
        try:
            channel_id = int(self.config.channel_id)  # type: ignore[arg-type]
            try:
                channel = bot_user.get_channel(channel_id)
                if channel is None:
                    channel = await bot_user.fetch_channel(channel_id)
            except Exception:
                log.exception("League memory: channel %s not found", channel_id)
                return 0

            messages: List[Tuple[str, str, str]] = []
            async for m in channel.history(limit=None):
                if not m.content or not m.content.strip():
                    continue
                author = m.author.nick if getattr(m.author, "nick", None) else m.author.name
                messages.append(
                    (author, m.created_at.strftime("%Y-%m-%d %H:%M"), m.content)
                )

            chunks = chunk_messages(messages, self.config.max_chunk_chars)
            if not chunks:
                log.info("League memory: no indexable messages in channel %s", channel_id)
                self._indexed = True
                return 0

            embedder = self._get_embedder()
            vectors = await asyncio.to_thread(
                _embed_batches, embedder, [c for c, _ in chunks]
            )
            self.store.add(
                [(text, vec, meta) for (text, meta), vec in zip(chunks, vectors)]
            )
            self._indexed = True
            log.info(
                "League memory: indexed %d chunks from %d messages in channel %s",
                self.store.size,
                len(messages),
                channel_id,
            )
            return self.store.size
        except Exception:
            log.exception("League memory indexing failed")
            return 0
        finally:
            self._indexing = False

    def start_indexing(self, bot_user) -> Optional[asyncio.Task]:
        """Kick off channel indexing as a background task (never blocks callers)."""
        if not self.config.enabled:
            return None
        self._task = asyncio.create_task(self.index_channel(bot_user))
        return self._task

    def query(self, question: str) -> List[Tuple[float, str, Dict[str, Any]]]:
        """Similarity search; empty list when nothing clears the threshold."""
        if self.store.size == 0 or not question.strip():
            return []
        try:
            vector = self._get_embedder().embed([question])[0]
        except Exception:
            log.exception("League memory: query embedding failed")
            return []
        return self.store.query(vector, self.config.top_k, self.config.relevance_threshold)

    def lookup_tool(self):
        """Build the strands tool the ChatHandler agent can call."""
        memory = self

        @strands_tool
        def lookup_league_info(query: str) -> Dict[str, Any]:
            """Look up league and server information from pinned-style announcements.

            Use this for administrative questions about the league: session
            times and schedules, rules, registration, announcements, event
            details, and server info. ``query`` is a short natural-language
            question. Returns the most relevant archived channel messages
            (with timestamps), or an empty list when nothing is relevant.
            """
            results = memory.query(query)
            return {
                "status": "ok",
                "results": [{"snippet": text, "score": round(score, 3)} for score, text, _meta in results],
            }

        return lookup_league_info


def _embed_batches(embedder: Embedder, texts: Sequence[str], batch_size: int = 32):
    vectors: List[List[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(embedder.embed(texts[start : start + batch_size]))
    return vectors


# ---------------------------------------------------------------------------
# Module-level singleton used by the bot wiring
# ---------------------------------------------------------------------------

_league_memory: Optional[LeagueMemory] = None


def get_league_memory() -> Optional[LeagueMemory]:
    """Return the shared LeagueMemory, or None if the feature is disabled."""
    global _league_memory
    if _league_memory is None:
        config = VectorStoreConfig.from_env()
        if not config.enabled:
            return None
        _league_memory = LeagueMemory(config)
    return _league_memory
