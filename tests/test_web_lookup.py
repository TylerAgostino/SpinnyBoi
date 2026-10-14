"""Unit tests for modules/web_lookup.py (topical web lookup tool).

No network access required: HTTP is injected via ``fetch_json`` doubles and
rate limiting is tested against a fake clock.

Run: python -m unittest tests.test_web_lookup
"""
import asyncio
import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules import ChatHandler
from modules.web_lookup import (
    WebLookup,
    WebLookupConfig,
    extract_snippets,
    format_query,
    get_web_lookup,
    truncate_snippet,
)


SAMPLE_PAYLOAD = {
    "AbstractText": "Kyle Larson is an American stock car racing driver.",
    "RelatedTopics": [
        {"Text": "Kyle Larson won the 2021 NASCAR Cup Series championship."},
        {"Text": "He drives the number 57 Chevrolet for Trackhouse Racing."},
        {"Text": "Kyle Larson is an American stock car racing driver."},  # dup
        {"FirstURL": "no text key"},
        "not a dict",
    ],
}


def make_lookup(fetch, config=None, clock_values=(100.0, 101.0, 102.0, 103.0)):
    """WebLookup with injected fetch and a fake monotonic clock."""
    return WebLookup(
        config or WebLookupConfig(),
        fetch_json=fetch,
        clock=MagicMock(side_effect=list(clock_values)),
    )


def make_discord_message(content="hello"):
    m = MagicMock()
    m.channel.id = 1
    m.author.nick = "tyler"
    m.author.name = "tyler_l"
    m.author.id = 123
    m.content = content
    m.created_at = datetime.datetime(2026, 1, 2, 3, 4, 5)
    m.attachments = []
    return m


def make_bot_user():
    async def empty_history(*args, **kwargs):
        return
        yield

    channel = MagicMock()
    channel.history = MagicMock(return_value=empty_history())
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


class QueryFormattingTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(
            format_query("  nascar   champion  \n 2026  ", 120),
            "nascar champion 2026",
        )

    def test_truncates_on_word_boundary(self):
        query = format_query("word " * 200, 20)
        self.assertLessEqual(len(query), 20)
        self.assertFalse(query.endswith(" "))
        self.assertTrue(query.startswith("word word"))

    def test_hard_cap_exact_length(self):
        out = format_query("word " * 40, 25)
        self.assertLessEqual(len(out), 25)
        self.assertTrue(out.startswith("word word"))

    def test_empty_and_none(self):
        self.assertEqual(format_query("", 100), "")
        self.assertEqual(format_query("   ", 100), "")
        self.assertEqual(format_query(None, 100), "")


class TruncationTests(unittest.TestCase):
    def test_short_snippet_untouched(self):
        self.assertEqual(truncate_snippet("short one", 220), "short one")

    def test_long_snippet_word_boundary_ellipsis(self):
        text = " ".join(["word"] * 100)
        out = truncate_snippet(text, 50)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 51)  # ellipsis included
        self.assertTrue(out[:-1].endswith("word"))  # no mid-word cut

    def test_snippet_whitespace_collapsed(self):
        self.assertEqual(truncate_snippet("a\n\n b   c ", 220), "a b c")

    def test_single_overlong_word(self):
        out = truncate_snippet("x" * 300, 50)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 51)


class ExtractSnippetsTests(unittest.TestCase):
    def test_order_and_max_results(self):
        snippets = extract_snippets(SAMPLE_PAYLOAD, max_results=2)
        self.assertEqual(len(snippets), 2)
        self.assertEqual(
            snippets[0], "Kyle Larson is an American stock car racing driver."
        )

    def test_dedup_and_skip_malformed_topics(self):
        snippets = extract_snippets(SAMPLE_PAYLOAD, max_results=10)
        self.assertEqual(len(snippets), 3)
        self.assertEqual(
            snippets[2], "He drives the number 57 Chevrolet for Trackhouse Racing."
        )

    def test_empty_payload(self):
        self.assertEqual(extract_snippets({}, 4), [])
        self.assertEqual(extract_snippets({"RelatedTopics": []}, 4), [])


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = WebLookupConfig.from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.timeout_seconds, 5.0)
        self.assertEqual(cfg.max_results, 4)

    def test_disabled_flag(self):
        for value in ("0", "false", "OFF"):
            self.assertFalse(
                WebLookupConfig.from_env({"WEB_LOOKUP_ENABLED": value}).enabled
            )

    def test_clamping_and_bad_values(self):
        cfg = WebLookupConfig.from_env(
            {"WEB_LOOKUP_MAX_RESULTS": "999", "WEB_LOOKUP_TIMEOUT_SECONDS": "junk"}
        )
        self.assertEqual(cfg.max_results, 10)
        self.assertEqual(cfg.timeout_seconds, 5.0)


class WebLookupTests(unittest.TestCase):
    def test_success_returns_truncated_snippets(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        result = make_lookup(fetch).lookup("kyle larson")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query"], "kyle larson")
        self.assertEqual(len(result["results"]), 3)  # dedup drops the repeat
        for snippet in result["results"]:
            self.assertLessEqual(len(snippet), 220)

    def test_build_url_params(self):
        lookup = make_lookup(MagicMock())
        url = lookup.build_url("who won the race")
        self.assertTrue(url.startswith("https://api.duckduckgo.com/?"))
        self.assertIn("q=who+won+the+race", url)
        self.assertIn("format=json", url)

    def test_fetch_receives_timeout(self):
        fetch = MagicMock(return_value={})
        lookup = make_lookup(fetch, WebLookupConfig(timeout_seconds=2.5))
        lookup.lookup("x")
        _, timeout = fetch.call_args[0]
        self.assertEqual(timeout, 2.5)

    def test_network_error_handled_gracefully(self):
        fetch = MagicMock(side_effect=ConnectionError("boom"))
        result = make_lookup(fetch).lookup("anything")
        self.assertEqual(result, {"status": "error", "results": []})

    def test_timeout_error_handled_gracefully(self):
        import socket

        fetch = MagicMock(side_effect=socket.timeout("timed out"))
        result = make_lookup(fetch).lookup("anything")
        self.assertEqual(result, {"status": "error", "results": []})

    def test_non_dict_payload_handled(self):
        result = make_lookup(MagicMock(return_value=[])).lookup("x")
        self.assertEqual(result["status"], "error")

    def test_empty_query_short_circuits(self):
        fetch = MagicMock()
        result = make_lookup(fetch).lookup("   ")
        self.assertEqual(result["status"], "empty_query")
        fetch.assert_not_called()

    def test_rate_limiting(self):
        fetch = MagicMock(return_value={})
        # second call only 1s after the first, min interval is 3s
        lookup = make_lookup(fetch, clock_values=[100.0, 101.0])
        first = lookup.lookup("one")
        second = lookup.lookup("two")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "rate_limited")
        self.assertEqual(second["results"], [])
        self.assertEqual(fetch.call_count, 1)

    def test_rate_window_elapses(self):
        fetch = MagicMock(return_value={})
        lookup = make_lookup(fetch, clock_values=[100.0, 200.0])
        self.assertEqual(lookup.lookup("one")["status"], "ok")
        self.assertEqual(lookup.lookup("two")["status"], "ok")
        self.assertEqual(fetch.call_count, 2)

    def test_disabled_lookup(self):
        fetch = MagicMock()
        lookup = WebLookup(WebLookupConfig(enabled=False), fetch_json=fetch)
        self.assertEqual(lookup.lookup("x"), {"status": "disabled", "results": []})
        fetch.assert_not_called()

    def test_singleton_disabled_returns_none(self):
        import modules.web_lookup as wl

        with patch.dict(
            "modules.web_lookup.os.environ", {"WEB_LOOKUP_ENABLED": "0"}, clear=False
        ):
            original = wl._web_lookup
            wl._web_lookup = None
            try:
                self.assertIsNone(get_web_lookup())
            finally:
                wl._web_lookup = original


class ToolRegistrationTests(unittest.TestCase):
    def test_lookup_tool_is_callable_with_schema(self):
        lookup = make_lookup(MagicMock(return_value=SAMPLE_PAYLOAD))
        tool = lookup.lookup_tool()
        self.assertTrue(callable(tool))
        # strands exposes the generated tool spec (name/description/input schema):
        spec = tool.tool_spec
        self.assertEqual(spec["name"], "lookup_web_topic")
        self.assertIn("query", str(spec["inputSchema"]))

    def test_tool_call_delegates_to_lookup(self):
        fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        lookup = make_lookup(fetch)
        tool = lookup.lookup_tool()
        out = tool(query="  nascar  champion ")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["query"], "nascar champion")

    def test_respond_in_chat_registers_tool(self):
        """ChatHandler wires the lookup tool into the chat agent."""
        instance = make_lookup(MagicMock(return_value={}))
        trigger = make_discord_message("what happened today")
        bot = make_bot_user()

        captured = {}

        def fake_make_agent(model, system_prompt=None, messages=None, tools=None):
            captured["tools"] = tools
            agent = MagicMock()

            async def invoke_async(prompt):
                return '{"user": "SpinnyBoi", "content": "done"}'

            agent.invoke_async = AsyncMock(side_effect=invoke_async)
            return agent

        with patch.object(ChatHandler, "_make_agent", side_effect=fake_make_agent), \
             patch.object(ChatHandler, "get_web_lookup", return_value=instance), \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None):
            out = asyncio.run(ChatHandler.respond_in_chat(trigger, bot))

        self.assertEqual(out, "done")
        self.assertEqual(len(captured["tools"]), 1)
        self.assertTrue(callable(captured["tools"][0]))

    def test_respond_in_chat_without_tool(self):
        """When the feature is disabled, respond_in_chat passes no tools."""
        trigger = make_discord_message("hi")
        bot = make_bot_user()

        agent = MagicMock()

        async def invoke_async(prompt):
            return '{"content": "yo"}'

        agent.invoke_async = AsyncMock(side_effect=invoke_async)

        with patch.object(ChatHandler, "get_web_lookup", return_value=None), \
             patch.object(ChatHandler, "get_league_memory", return_value=None), \
             patch.object(ChatHandler, "get_simracerhub", return_value=None), \
             patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent:
            out = asyncio.run(ChatHandler.respond_in_chat(trigger, bot))

        self.assertEqual(out, "yo")
        _, kwargs = make_agent.call_args
        self.assertEqual(kwargs["tools"], [])


if __name__ == "__main__":
    unittest.main()
