"""Integration tests for ChatHandler's unified tool-calling pipeline (t_34736139).

All three real tools (league vector store, web lookup, SimRacerHub stats) are
built from their classes with injected fakes (no network, no Discord, no LLM),
wired through respond_in_chat, and exercised end-to-end: a scripted fake agent
"selects" a tool by name, calls it, and summarizes the result into the
in-character JSON reply. Graceful-fallback paths (disabled, broken, or
network-failing tools) are asserted too.

Run: .venv\\Scripts\\python -m unittest tests.test_chat_tools_integration
"""
import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules import ChatHandler
from modules.league_memory import LeagueMemory, VectorStoreConfig
from modules.simracerhub import SimRacerHubClient, SimRacerHubConfig
from modules.web_lookup import WebLookup, WebLookupConfig


# ---------------------------------------------------------------------------
# Fixtures (mirrors the sibling per-tool test modules)
# ---------------------------------------------------------------------------

class StubEmbedder:
    VOCAB = ["session", "tonight", "rules", "registration", "announcement", "race", "times"]

    def embed(self, texts):
        return [
            [1.0 if word in t.lower() else 0.0 for word in self.VOCAB] for t in texts
        ]


class ExplodingEmbedder:
    def embed(self, texts):
        raise RuntimeError("embedding server down")


WEB_PAYLOAD = {
    "AbstractText": "Kyle Larson won the 2024 NASCAR Cup Series championship.",
    "RelatedTopics": [],
}

SRH_PAYLOAD = {
    "drivers": {"16262": {"driver_id": "16262", "name": "Danny Dummy", "is_ai": False}},
    "rps": {
        "16262": {
            "drid": "16262", "name": "Dummy, Danny", "starts": "8", "counted": "7",
            "tpts": "500", "wins": "3", "poles": "0", "t3": "7", "t5": "7", "t10": "8",
            "laps": "260", "inc": "40", "rat": "990.5",
        }
    },
    "tracks": {},
    "schedules": [],
}


def make_memory(exploding=False):
    cfg = VectorStoreConfig(channel_id=123, relevance_threshold=0.1)
    memory = LeagueMemory(cfg, embedder=ExplodingEmbedder() if exploding else StubEmbedder())
    if exploding:
        return memory  # left empty on purpose; only the query path is exercised
    docs = [
        "Session tonight starts at 8pm ET on the usual server.",
        "Updated registration rules for the league season.",
    ]
    vectors = memory._embedder.embed(docs)
    memory.store.add([(d, v, {}) for d, v in zip(docs, vectors)])
    return memory


def make_web(fetch=None):
    return WebLookup(
        WebLookupConfig(min_interval_seconds=0.0),
        fetch_json=fetch or MagicMock(return_value=WEB_PAYLOAD),
    )


def make_srh(fetch=None):
    cfg = SimRacerHubConfig(min_interval_seconds=0.0, default_season_id=29913)
    return SimRacerHubClient(
        cfg,
        fetch_json=fetch or MagicMock(return_value=SRH_PAYLOAD),
        clock=MagicMock(side_effect=lambda n=[0]: (n.__setitem__(0, n[0] + 1), n[0] * 1000.0)[1]),
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
    channel = MagicMock()

    class HistIter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    channel.history = MagicMock(return_value=HistIter())
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def fake_model_call(memory=None, web=None, srh=None):
    """Patch ChatHandler getters to hand respond_in_chat the real tools."""
    return (
        patch.object(ChatHandler, "get_league_memory", return_value=memory),
        patch.object(ChatHandler, "get_web_lookup", return_value=web),
        patch.object(ChatHandler, "get_simracerhub", return_value=srh),
    )


class AgentToolSelectionTests(unittest.IsolatedAsyncioTestCase):
    """The three real tools are registered and callable through the agent's
    tool list; a scripted agent that picks a tool, calls it, and summarizes
    produces an in-character reply."""

    async def _run_with_agent(self, trigger_content, script, memory, web, srh):
        """script: fn(tool_name -> reply_text) invoked with the tool call result."""
        captured = {}

        def fake_make_agent(model, system_prompt=None, messages=None, tools=None):
            captured["tools"] = tools or []
            agent = MagicMock()

            async def invoke_async(prompt):
                return script(captured["tools"])

            agent.invoke_async = AsyncMock(side_effect=invoke_async)
            return agent

        with patch.object(ChatHandler, "_make_agent", side_effect=fake_make_agent), \
             patch.object(ChatHandler, "get_league_memory", return_value=memory), \
             patch.object(ChatHandler, "get_web_lookup", return_value=web), \
             patch.object(ChatHandler, "get_simracerhub", return_value=srh):
            out = await ChatHandler.respond_in_chat(
                make_discord_message(trigger_content), make_bot_user()
            )
        return out, captured["tools"]

    async def test_all_three_real_tools_registered_with_correct_specs(self):
        _, tools = await self._run_with_agent(
            "what time is it", lambda t: '{"content": "sup"}', make_memory(), make_web(), make_srh()
        )
        names = [t.tool_spec["name"] for t in tools]
        self.assertEqual(
            names, ["lookup_league_info", "lookup_web_topic", "get_simracerhub_stats"]
        )

    async def test_admin_question_routes_to_vector_store(self):
        def script(tools):
            league = tools[0]
            result = league(query="when does the session start tonight")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["results"])
            self.assertIn("8pm", result["results"][0]["snippet"])
            return '{"user": "SpinnyBoi", "content": "8pm tonight, be there or be last."}'

        out, _ = await self._run_with_agent("when is the session tonight", script, make_memory(), None, None)
        self.assertEqual(out, "8pm tonight, be there or be last.")

    async def test_topical_question_routes_to_web_lookup(self):
        def script(tools):
            web = tools[0]
            result = web(query="who won the nascar cup")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["results"])
            return '{"user": "SpinnyBoi", "content": "Larson took it, obviously we were favored."}'

        out, _ = await self._run_with_agent("did you see who won nascar", script, None, make_web(), None)
        self.assertIn("Larson", out)

    async def test_stat_question_routes_to_simracerhub(self):
        def script(tools):
            srh = tools[0]
            result = srh(kind="standings", season_id=29913)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["rows"])
            row = result["rows"][0] if len(result["rows"]) == 1 else next(
                r for r in result["rows"] if "Dummy" in r.get("name", "")
            )
            self.assertEqual(row["points"], 500)
            return '{"user": "SpinnyBoi", "content": "Dummy leads with 500, 3 wins. we still beat him tonight."}'

        out, _ = await self._run_with_agent("whos winning the championship", script, None, None, make_srh())
        self.assertIn("500", out)


class GracefulFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Tool failures must never kill or wreck the chat reply."""

    async def test_tool_backend_failure_returns_status_payload_not_exception(self):
        web = make_web(fetch=MagicMock(side_effect=socket_timeout()))
        tool = web.lookup_tool()
        out = tool(query="anything")
        self.assertNotEqual(out["status"], "ok")
        self.assertEqual(out["results"], [])

        srh = make_srh(fetch=MagicMock(side_effect=RuntimeError("conn reset")))
        out = srh.stats_tool()(kind="standings")
        self.assertNotEqual(out["status"], "ok")

        memory = make_memory(exploding=True)  # empty store; query-time embed explodes
        out = memory.lookup_tool()(query="session tonight")
        self.assertEqual(out["results"], [])  # status ok, empty - no raise

    async def test_reply_survives_when_all_getters_raise(self):
        agent = MagicMock()

        async def invoke_async(prompt):
            return '{"user": "SpinnyBoi", "content": "yo"}'

        agent.invoke_async = AsyncMock(side_effect=invoke_async)
        with patch.object(ChatHandler, "_make_agent", return_value=agent) as make_agent, \
             patch.object(ChatHandler, "get_league_memory", side_effect=RuntimeError("boom")), \
             patch.object(ChatHandler, "get_web_lookup", side_effect=RuntimeError("boom")), \
             patch.object(ChatHandler, "get_simracerhub", side_effect=RuntimeError("boom")):
            out = await ChatHandler.respond_in_chat(make_discord_message("hey"), make_bot_user())
        self.assertEqual(out, "yo")
        _, kwargs = make_agent.call_args
        self.assertEqual(kwargs["tools"], [])

    async def test_prompt_guidance_covers_all_tools_and_restraint(self):
        p = ChatHandler.CHAT_SYSTEM_PROMPT
        for name in ("lookup_league_info", "lookup_web_topic", "get_simracerhub_stats"):
            self.assertIn(name, p)
        self.assertIn("ONLY", p)  # tools only when relevant
        self.assertIn("research dump", p)  # no long research dumps


def socket_timeout():
    import socket
    return socket.timeout("timed out")


if __name__ == "__main__":
    unittest.main()
