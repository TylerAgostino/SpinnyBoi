# pyright: basic
"""League memory: an in-memory vector store over configured Discord channels.

At startup the bot reads all messages from the configured channels, chunks and
embeds them, and exposes a strands tool the ChatHandler agent can call for
administrative questions (session times, rules, announcements).

Configuration is environment-driven (see ``VectorStoreConfig.from_env``):

    LEAGUE_VECTOR_CHANNEL_ID            comma-separated Discord channel ids to
                                        index (feature disabled when unset/invalid)
    LEAGUE_VECTOR_EMBEDDING_PROVIDER    "ollama" (default) or "openai"
    LEAGUE_VECTOR_EMBEDDING_MODEL       embedding model id (provider default if unset)
    LEAGUE_VECTOR_EMBEDDING_BASE_URL    OpenAI-compatible base URL override
    LEAGUE_VECTOR_TOP_K                 max snippets per query (default 5)
    LEAGUE_VECTOR_RELEVANCE_THRESHOLD   min cosine similarity (default 0.35)
    LEAGUE_VECTOR_MAX_CHUNK_CHARS       soft chunk size cap (default 700)
    LEAGUE_VECTOR_HISTORY_SINCE         default how far back to index: ISO date/
                                        datetime (UTC when naive), a relative age
                                        (30d, 24h, 2w), or the oldest message id
                                        (unset: everything, all time)
    LEAGUE_VECTOR_HISTORY_SINCE_<id>    per-channel override of the cutoff above
                                        for channel <id> (e.g.
                                        LEAGUE_VECTOR_HISTORY_SINCE_1234=90d)
    LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL
                                        the same per-channel overrides packed
                                        into one key: "111=365d,222=2026-01-05"
                                        (for env-allowlisted deploys; suffixed
                                        keys win on collision)

Indexing is launched as a background task (``LeagueMemory.start_indexing``) so
it never blocks message handling.
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
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

def _parse_channel_ids(raw: Optional[str]) -> Tuple[int, ...]:
    """Parse a comma-separated list of positive channel ids, skipping bad entries."""
    ids: List[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            log.warning("Invalid LEAGUE_VECTOR_CHANNEL_ID entry=%r, skipping", part)
            continue
        if value <= 0:
            log.warning("Ignoring non-positive LEAGUE_VECTOR_CHANNEL_ID entry=%r", part)
            continue
        if value not in ids:
            ids.append(value)
    return tuple(ids)


_RELATIVE_AGE_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*"
    r"(s|secs?|seconds|m|mins?|minutes|h|hrs?|hours|d|days?|w|wks?|weeks?)$"
)
_AGE_UNIT_KWARG = {
    "s": "seconds", "sec": "seconds", "secs": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "wk": "weeks", "wks": "weeks", "weeks": "weeks",
}


@dataclass(frozen=True)
class HistoryCutoff:
    """How far back to index; ``parse_history_since`` sets exactly one bound.

    - ``since``: inclusive lower bound on message creation time.
    - ``message_id``: inclusive lower bound on discord snowflake ids (ids are
      time-ordered, so this indexes from that message forward).
    - ``age``: relative window (``now - age``), resolved at every indexing run.
    """

    since: Optional[datetime] = None
    message_id: Optional[int] = None
    age: Optional[timedelta] = None

    def includes(self, message) -> bool:
        if self.message_id is not None:
            return message.id >= self.message_id
        if self.since is None and self.age is None:
            return True
        created = message.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if self.age is not None:
            return created >= datetime.now(timezone.utc) - self.age
        return created >= self.since


def parse_history_since(
    raw: Optional[str], key: str = "LEAGUE_VECTOR_HISTORY_SINCE"
) -> Optional[HistoryCutoff]:
    """Parse how far back to index: ISO date/datetime, relative age, or message id.

    Accepted spellings (case-insensitive units): ``2026-01-01``,
    ``2026-01-01T12:00:00-05:00``, ``30d``/``24h``/``2w``/``90m``, or an
    all-digit discord message id. Unset/empty/garbage yields None (index all
    time).
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    if raw.isdigit():
        message_id = int(raw)
        if message_id <= 0:
            log.warning("Invalid %s=%r (non-positive message id), ignoring", key, raw)
            return None
        return HistoryCutoff(message_id=message_id)

    match = _RELATIVE_AGE_RE.match(raw.lower())
    if match:
        amount = float(match.group(1))
        if amount <= 0:
            log.warning("Invalid %s=%r (non-positive age), ignoring", key, raw)
            return None
        return HistoryCutoff(age=timedelta(**{_AGE_UNIT_KWARG[match.group(2)]: amount}))

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        log.warning(
            "Invalid %s=%r; expected an ISO date/datetime, a relative age like "
            "30d, or a message id. Indexing all time.",
            key,
            raw,
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return HistoryCutoff(since=parsed)


_HISTORY_SINCE_PREFIX = "LEAGUE_VECTOR_HISTORY_SINCE_"
_HISTORY_SINCE_MAP_KEY = "LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL"


def parse_channel_history_cutoffs(env: Dict[str, str]) -> Dict[int, HistoryCutoff]:
    """Collect per-channel overrides from env, in two forms (suffixed keys win):

    - ``LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL=111=365d,222=2026-01-05`` —
      comma-separated ``channel_id=value`` pairs in one key, so deployments
      with an explicit env allowlist (docker compose) can pass it through.
    - ``LEAGUE_VECTOR_HISTORY_SINCE_<channel_id>`` suffixed keys.

    Values parse exactly like ``parse_history_since``; blank or invalid ones
    are skipped so that channel falls back to the shared default cutoff.
    """
    cutoffs: Dict[int, HistoryCutoff] = {}
    for pair in (env.get(_HISTORY_SINCE_MAP_KEY) or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        channel_part, sep, value = pair.partition("=")
        channel_id = channel_part.strip()
        if not sep or not channel_id.isdigit() or int(channel_id) <= 0:
            log.warning(
                "Invalid %s entry=%r (expected channel_id=value), skipping",
                _HISTORY_SINCE_MAP_KEY,
                pair,
            )
            continue
        cutoff = parse_history_since(value.strip(), key=f"{_HISTORY_SINCE_MAP_KEY}:{channel_id}")
        if cutoff is not None:
            cutoffs[int(channel_id)] = cutoff
    for key, raw in env.items():
        if not key.startswith(_HISTORY_SINCE_PREFIX):
            continue
        suffix = key[len(_HISTORY_SINCE_PREFIX):]
        if not suffix.isdigit():
            continue  # the shared default key or unrelated env entries
        channel_id = int(suffix)
        if channel_id <= 0:
            log.warning("Invalid %s (non-positive channel id), skipping", key)
            continue
        cutoff = parse_history_since(raw, key=key)
        if cutoff is not None:
            cutoffs[channel_id] = cutoff
    return cutoffs


@dataclass
class VectorStoreConfig:
    channel_ids: Tuple[int, ...] = ()
    embedding_provider: str = "ollama"
    embedding_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    top_k: int = 5
    relevance_threshold: float = 0.35
    max_chunk_chars: int = 700
    history_cutoff: Optional[HistoryCutoff] = None
    channel_history_cutoffs: Dict[int, HistoryCutoff] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.channel_ids)

    def cutoff_for(self, channel_id: int) -> Optional[HistoryCutoff]:
        """Per-channel override for ``channel_id`` if set, else the shared default."""
        return self.channel_history_cutoffs.get(channel_id, self.history_cutoff)

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
            channel_ids=_parse_channel_ids(env.get("LEAGUE_VECTOR_CHANNEL_ID")),
            embedding_provider=provider,
            embedding_model=(env.get("LEAGUE_VECTOR_EMBEDDING_MODEL") or None),
            embedding_base_url=(env.get("LEAGUE_VECTOR_EMBEDDING_BASE_URL") or None),
            top_k=top_k,
            relevance_threshold=threshold,
            max_chunk_chars=max_chunk,
            history_cutoff=parse_history_since(env.get("LEAGUE_VECTOR_HISTORY_SINCE")),
            channel_history_cutoffs=parse_channel_history_cutoffs(env),
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

    async def index_channels(self, bot_user) -> int:
        """Read messages inside the history window and embed them.

        ``bot_user`` is the discord.py Bot/User. Returns the total number of
        chunks indexed. Errors are logged, not raised, so one bad channel
        never takes the bot down or blocks the others (queries simply return
        what was indexed until re-indexed).
        """
        if not self.config.enabled:
            log.info("League memory disabled (no channel ids configured)")
            return 0

        self._indexing = True
        total_messages = 0
        failures = 0
        try:
            for channel_id in self.config.channel_ids:
                try:
                    channel = bot_user.get_channel(channel_id)
                    if channel is None:
                        channel = await bot_user.fetch_channel(channel_id)
                    if channel is None:
                        raise LookupError("channel not found")
                    messages = await self._collect_messages(
                        channel, self.config.cutoff_for(channel_id)
                    )
                except Exception:
                    failures += 1
                    log.exception("League memory: channel %s failed", channel_id)
                    continue

                total_messages += len(messages)
                chunks = chunk_messages(messages, self.config.max_chunk_chars)
                for _, meta in chunks:
                    meta["channel_id"] = channel_id
                if not chunks:
                    log.info(
                        "League memory: no indexable messages in channel %s", channel_id
                    )
                    continue

                embedder = self._get_embedder()
                vectors = await asyncio.to_thread(
                    _embed_batches, embedder, [c for c, _ in chunks]
                )
                self.store.add(
                    [(text, vec, meta) for (text, meta), vec in zip(chunks, vectors)]
                )

            if failures == 0:
                self._indexed = True
            log.info(
                "League memory: indexed %d chunks from %d messages across %d "
                "channel(s) (%d failed)",
                self.store.size,
                total_messages,
                len(self.config.channel_ids),
                failures,
            )
            return self.store.size
        except Exception:
            log.exception("League memory indexing failed")
            return self.store.size
        finally:
            self._indexing = False

    async def _collect_messages(
        self, channel, cutoff: Optional[HistoryCutoff]
    ) -> List[Tuple[str, str, str]]:
        """Pull ``(author, timestamp, content)`` triples from one channel.

        discord.py walks history newest-first, so when a message falls before
        ``cutoff`` the walk stops there and older pages are never fetched.
        """
        messages: List[Tuple[str, str, str]] = []
        async for m in channel.history(limit=None):
            if cutoff is not None and not cutoff.includes(m):
                break
            if not m.content or not m.content.strip():
                continue
            author = m.author.nick if getattr(m.author, "nick", None) else m.author.name
            messages.append(
                (author, m.created_at.strftime("%Y-%m-%d %H:%M"), m.content)
            )
        return messages

    def start_indexing(self, bot_user) -> Optional[asyncio.Task]:
        """Kick off indexing of all configured channels (never blocks callers)."""
        if not self.config.enabled:
            return None
        self._task = asyncio.create_task(self.index_channels(bot_user))
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
