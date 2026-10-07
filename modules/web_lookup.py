# pyright: basic
"""Lightweight topical web lookup for the ChatHandler agent.

Backed by the DuckDuckGo Instant Answer API (no API key required). The tool
returns a handful of short factual snippets meant to augment the bot's
in-character reply — deliberately not a research dump: snippets are truncated,
only a few results are returned, and the whole payload is size-capped.

Configuration is environment-driven (see ``WebLookupConfig.from_env``):

    WEB_LOOKUP_ENABLED              "0"/"false"/"off" disables the feature
    WEB_LOOKUP_TIMEOUT_SECONDS      per-request HTTP timeout (default 5)
    WEB_LOOKUP_MIN_INTERVAL_SECONDS min seconds between API calls (default 3)
    WEB_LOOKUP_MAX_RESULTS          max snippets per lookup (default 4)
    WEB_LOOKUP_MAX_SNIPPET_CHARS    per-snippet truncation cap (default 220)
    WEB_LOOKUP_MAX_QUERY_CHARS      incoming query length cap (default 120)

Every failure path (network error, timeout, bad JSON, rate limiting) yields a
``{"status": ..., "results": []}`` payload instead of raising, so a flaky
lookup can never take down a chat reply.
"""
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from strands import tool as strands_tool

log = logging.getLogger(__name__)

DDG_API_URL = "https://api.duckduckgo.com/"
USER_AGENT = "SpinnyBoi-Discord-bot/1.0 (topical lookup)"

_FALSE_VALUES = {"0", "false", "off", "no"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class WebLookupConfig:
    enabled: bool = True
    timeout_seconds: float = 5.0
    min_interval_seconds: float = 3.0
    max_results: int = 4
    max_snippet_chars: int = 220
    max_query_chars: int = 120

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "WebLookupConfig":
        env = os.environ if env is None else env

        raw_enabled = (env.get("WEB_LOOKUP_ENABLED") or "").strip().lower()
        enabled = raw_enabled not in _FALSE_VALUES if raw_enabled else True

        return cls(
            enabled=enabled,
            timeout_seconds=_env_float(env, "WEB_LOOKUP_TIMEOUT_SECONDS", 5.0, 1.0, 30.0),
            min_interval_seconds=_env_float(
                env, "WEB_LOOKUP_MIN_INTERVAL_SECONDS", 3.0, 0.0, 60.0
            ),
            max_results=_env_int(env, "WEB_LOOKUP_MAX_RESULTS", 4, minimum=1, maximum=10),
            max_snippet_chars=_env_int(
                env, "WEB_LOOKUP_MAX_SNIPPET_CHARS", 220, minimum=40, maximum=2000
            ),
            max_query_chars=_env_int(
                env, "WEB_LOOKUP_MAX_QUERY_CHARS", 120, minimum=10, maximum=500
            ),
        )


def _env_int(
    env: Dict[str, str], key: str, default: int, minimum: int, maximum: int
) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %s", key, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _env_float(
    env: Dict[str, str], key: str, default: float, minimum: float, maximum: float
) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %s", key, raw, default)
        return default
    return max(minimum, min(maximum, value))


# ---------------------------------------------------------------------------
# Query formatting / snippet extraction / truncation (pure functions)
# ---------------------------------------------------------------------------


def format_query(raw: str, max_chars: int) -> str:
    """Normalise a model-supplied query: collapse whitespace, hard-cap length."""
    query = re.sub(r"\s+", " ", raw or "").strip()
    if len(query) > max_chars:
        query = query[:max_chars].rsplit(" ", 1)[0].strip()
    return query


def truncate_snippet(text: str, max_chars: int) -> str:
    """Trim one snippet to ``max_chars`` on a word boundary with an ellipsis."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    cut = clipped.rsplit(" ", 1)[0].strip()
    if not cut:  # single over-long "word"
        cut = clipped.strip()
    return cut + "…"


def extract_snippets(payload: Dict[str, Any], max_results: int) -> List[str]:
    """Pull short factual snippets out of a DDG Instant Answer payload.

    Preference order: the abstract, a definition, an computed-answer, then the
    most-related-topic texts. Duplicates and blank entries are dropped.
    """
    snippets: List[str] = []

    def add(text: Any) -> None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if text and text not in snippets:
            snippets.append(text)

    add(payload.get("AbstractText"))
    add(payload.get("DefineText"))
    add(payload.get("Answer"))
    if isinstance(payload.get("RelatedTopics"), list):
        for topic in payload["RelatedTopics"]:
            if isinstance(topic, dict):
                add(topic.get("Text"))
    return snippets[:max_results]


# ---------------------------------------------------------------------------
# HTTP fetch (injectable for tests)
# ---------------------------------------------------------------------------


def default_fetch_json(url: str, timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body)


# ---------------------------------------------------------------------------
# Web lookup (rate limiting + tool)
# ---------------------------------------------------------------------------


class WebLookup:
    def __init__(
        self,
        config: Optional[WebLookupConfig] = None,
        fetch_json: Optional[Callable[[str, float], Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config or WebLookupConfig()
        self._fetch_json = fetch_json or default_fetch_json
        self._clock = clock
        self._last_call: Optional[float] = None

    def build_url(self, query: str) -> str:
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
            "no_redirect": "1",
        }
        return DDG_API_URL + "?" + urllib.parse.urlencode(params)

    def lookup(self, query: str) -> Dict[str, Any]:
        """Run one lightweight lookup. Never raises; always returns a payload."""
        if not self.config.enabled:
            return {"status": "disabled", "results": []}

        formatted = format_query(query, self.config.max_query_chars)
        if not formatted:
            return {"status": "empty_query", "results": []}

        now = self._clock()
        if (
            self._last_call is not None
            and now - self._last_call < self.config.min_interval_seconds
        ):
            log.info("web lookup rate-limited (query=%r)", formatted)
            return {"status": "rate_limited", "results": []}
        self._last_call = now

        try:
            payload = self._fetch_json(self.build_url(formatted), self.config.timeout_seconds)
        except Exception:
            log.exception("web lookup failed for query %r", formatted)
            return {"status": "error", "results": []}

        if not isinstance(payload, dict):
            return {"status": "error", "results": []}

        snippets = extract_snippets(payload, self.config.max_results)
        results = [truncate_snippet(s, self.config.max_snippet_chars) for s in snippets]
        return {"status": "ok", "query": formatted, "results": results}

    def lookup_tool(self):
        """Build the strands tool the ChatHandler agent can call."""
        lookup = self.lookup

        @strands_tool
        def lookup_web_topic(query: str) -> Dict[str, Any]:
            """Look up a current event, news topic, or quick fact on the web.

            Use this when someone references something topical that you would
            not know about: recent news, current events, who won something,
            what is happening right now. ``query`` is a short keyword-style
            search query (a few words, not a full sentence). Returns up to a
            few short factual snippets, or an empty list if nothing was found.
            """
            return lookup(query)

        return lookup_web_topic


# ---------------------------------------------------------------------------
# Module-level singleton used by the ChatHandler wiring
# ---------------------------------------------------------------------------

_web_lookup: Optional[WebLookup] = None


def get_web_lookup() -> Optional[WebLookup]:
    """Return the shared WebLookup, or None if the feature is disabled."""
    global _web_lookup
    if _web_lookup is None:
        config = WebLookupConfig.from_env()
        if not config.enabled:
            return None
        _web_lookup = WebLookup(config)
    return _web_lookup
