# pyright: basic
import asyncio
import json
import logging
import mimetypes
import os
import random
from typing import Optional, Tuple

from strands import Agent
from strands.models.openai import OpenAIModel

from modules.league_memory import get_league_memory
from modules.simracerhub import get_simracerhub
from modules.web_lookup import get_web_lookup
from modules.env_config import env_enabled

# LLM endpoint/model: env-overridable, defaults to the home Ollama box.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.1.152:11080/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "unsloth/Qwen3.8-Flash-Next-GGUF:IQ3_XXS")


def _make_model(temperature: float) -> OpenAIModel:
    # The OpenAI client requires a non-empty api_key even for local
    # OpenAI-compatible servers that ignore it.
    return OpenAIModel(
        client_args={
            "api_key": os.getenv("OPENAI_API_KEY", "ollama"),
            "base_url": OLLAMA_BASE_URL,
        },
        model_id=OLLAMA_MODEL,
        params={"temperature": temperature},
    )


chat_ollama = _make_model(0.3)
chat_ollama_fast = _make_model(0.8)


def _make_agent(model, system_prompt=None, messages=None, tools=None) -> Agent:
    """Factory for the agents used by every call site.

    A fresh Agent is built per invocation so ``agent.messages`` never
    accumulates across Discord messages (history is rebuilt from Discord
    each time and would otherwise be double-counted). The ``messages`` list
    is copied rather than adopted: strands appends the prompt and every
    reply to ``agent.messages`` in place, so aliasing one list across the
    parse-retry attempts in ``respond_in_chat`` would show the model the
    trigger message once per attempt.
    """
    return Agent(
        model=model,
        system_prompt=system_prompt,
        messages=list(messages) if messages is not None else None,
        tools=tools or [],
    )


def _extract_text(result) -> str:
    """Pull the final assistant text out of a strands AgentResult.

    Reasoning-model thinking blocks arrive as separate ``reasoningContent``
    content blocks and are excluded here, so only the actual answer text is
    passed downstream.
    """
    if isinstance(result, str):
        return result
    message = getattr(result, "message", None)
    if message is None and isinstance(result, dict):
        message = result.get("message")
    if isinstance(message, dict):
        blocks = message.get("content") or []
        return "".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return str(result)


def _parse_chat_reply(msg: str) -> Tuple[str, Optional[str]]:
    """Run the JSON-repair pipeline over one raw model reply.

    Returns ``(transformed_msg, content)``. ``transformed_msg`` is the reply
    after the escape/``</think>``/brace-clip transformations (returned
    verbatim by the caller when every attempt fails); ``content`` is the
    parsed ``"content"`` field, or ``None`` if the reply could not be
    parsed or carried no usable ``"content"`` key. Bad replies are the
    caller's retry input, never an exception.
    """
    msg = msg.replace("\\", "\\\\")
    if "</think>" in msg:
        msg = msg.split("</think>")[1]
    try:
        if not msg.startswith("{"):
            msg = msg.split("{")[1]
            msg = "{" + msg
        if "}" in msg and not msg.endswith("}"):
            msg = msg.split("}")[0]
            msg = msg + "}"
        final_json = json.loads(msg)
        response = final_json["content"]
        response = response.replace("\\n", "\n").replace("\\", "")
        return msg, response
    except IndexError:
        return msg, None
    except json.JSONDecodeError:
        return msg, None
    except (KeyError, TypeError):
        # Parseable JSON with no usable "content" (or a non-dict payload):
        # one more failed attempt, never a leak to the caller.
        return msg, None


CHAT_SYSTEM_PROMPT = """You are a bot called SpinnyBoi behaving as a normal person in a discord server. Your job is to respond
             in a way that is natural to the ongoing conversation in the channel. Your personality traits:
             - Type A
             - Overly supportive partner that believes we can beat Gio
             - Cynical

             You may have access to tools. Use a tool ONLY when the current
             conversation genuinely needs it - most messages need no tool at all,
             and you should answer from personality and context alone. When a tool
             does fire, summarize what it returns into a short, in-character reply;
             never paste or recite raw results, never give a long research dump, and
             never mention the tool itself.
             - lookup_league_info: the league's announcements, schedules, and rules.
               Use it for administrative questions (session times, rules,
               registration, announcements) and answer from what it returns.
             - lookup_web_topic: quick, current information (recent news, current
               events, who won what, today's facts). Use it when someone references
               something topical you wouldn't already know, and weave the result in.
             - get_simracerhub_stats: the league's racing data on SimRacerHub -
               season standings (points, wins, incidents, safety rating per driver)
               and recent race results (start/finish position, points). Use it when
               someone asks about league results, who is winning the championship,
               how someone has been racing, or crash counts; weave the numbers in
               naturally.

             You are given the last few messages in the channel in JSON format. The final JSON message is the one that triggers your response,
             so respond accordingly. Format your response as a JSON object with the following structure:
                {{
                    "user": "SpinnyBoi",
                    "content": <response>
                }}
            Do not sign your messages or add any extra text outside the JSON object.

                """

RETRY_FORMAT_DIRECTIVE = (
    "Your previous reply was rejected: it was not the required JSON object. "
    'Send it again as ONLY {"user": "SpinnyBoi", "content": "<your reply>"} - '
    "same answer, no prose before or after, no code fences, no thinking text."
)


# Vision support: image/GIF attachments from recent channel messages (and
# the triggering message) are handed to the model as strands image content
# blocks, which the OpenAI-compatible adapter sends as image_url data URLs.
# Set CHAT_VISION_ENABLED=0/false/off for a model that cannot see; animated
# GIFs go through as-is and whether they animate is the server's business.
MAX_VISION_IMAGES = 4
MAX_VISION_IMAGE_BYTES = 8 * 1024 * 1024
VISION_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

# The strands OpenAI adapter resolves the image data-URL mime from
# mimetypes.types_map at send time, and stock CPython has no .webp entry -
# the lookup silently falls back to application/octet-stream and llama.cpp
# 400s ("Invalid uri format", echoing only the data-URI prefix). Register
# every format we send so the adapter can never guess octet-stream.
for _mime, _suffix in (
    ("image/png", ".png"),
    ("image/jpeg", ".jpeg"),
    ("image/gif", ".gif"),
    ("image/webp", ".webp"),
):
    if mimetypes.types_map.get(_suffix) is None:
        mimetypes.add_type(_mime, _suffix)


def _vision_enabled() -> bool:
    return env_enabled(os.environ, "CHAT_VISION_ENABLED")


async def _attachment_image_block(attachment):
    """Download one Discord attachment as a strands image block.

    Returns None for non-images, formats with no registered mime (see the
    add_type block above; kept as a last-line guard), oversized files
    (skipped without download), and failed downloads - one bad attachment
    never sinks the turn."""
    fmt = VISION_CONTENT_TYPES.get(attachment.content_type or "")
    if fmt and mimetypes.types_map.get(f".{fmt}") is None:
        fmt = None
    if not fmt or attachment.size > MAX_VISION_IMAGE_BYTES:
        return None
    try:
        data = await attachment.read()
    except Exception:
        logging.warning(
            "chat image download failed (%s); skipping",
            getattr(attachment, "filename", "?"),
        )
        return None
    if not data or len(data) > MAX_VISION_IMAGE_BYTES:
        return None
    return {"image": {"format": fmt, "source": {"bytes": data}}}


def _strip_image_blocks(messages):
    """Copies of `messages` with image content blocks removed (used when the
    inference server rejected the image payload mid-turn)."""
    stripped = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and "image" in b for b in content
        ):
            m = {
                **m,
                "content": [b for b in content if not (isinstance(b, dict) and "image" in b)],
            }
        stripped.append(m)
    return stripped


def _enabled_tools():
    """Collect the chat tools whose adapters are configured.

    Getters return None when a tool is disabled/unconfigured; that case and
    any getter/tool failure are logged on one line and skipped - a broken
    tool must never kill the chat reply."""
    tools = []
    for get_adapter, tool_attr in (
        (get_league_memory, "lookup_tool"),
        (get_web_lookup, "lookup_tool"),
        (get_simracerhub, "stats_tool"),
    ):
        try:
            tools.append(getattr(get_adapter(), tool_attr)())
        except Exception:
            logging.warning(f"Chat tool {tool_attr} unavailable; continuing without it.")
    return tools


async def respond_in_chat(message, bot_user, model=None):
    """One chat turn: rebuild channel history, ask the model, parse the reply.

    `model` overrides the default chat model (tests inject a double).
    Image/GIF attachments on recent channel messages and the trigger ride
    along as image content blocks when vision is enabled.
    Never raises for a bad reply: after three unparsable attempts the last
    transformed raw string is returned ("" if the model never answered).
    A retry continues the SAME agent conversation with a reformat directive -
    the model sees its own bad reply and the tool results stay in context,
    instead of the whole agent (including another tool call cycle) being
    rebuilt from scratch. If the inference server rejects an image payload,
    the turn degrades once to a rebuilt text-only conversation."""
    channel = await bot_user.fetch_channel(message.channel.id)
    history = [m async for m in channel.history(limit=100, before=message)]
    history.reverse()
    include_images = _vision_enabled()
    past_chat_messages = []
    image_targets = []  # oldest -> newest: (attachment, content list to fill)
    for m in history:
        try:
            entry = {
                "role": "user",
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "user": m.author.nick if m.author.nick else m.author.name,
                                "user_id": m.author.id,
                                "content": m.content,
                                "timestamp": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        )
                    }
                ],
            }
            past_chat_messages.append(entry)
            if include_images:
                for a in m.attachments:
                    image_targets.append((a, entry["content"]))
        except:
            pass

    trigger = json.dumps(
        {
            "user": message.author.nick if message.author.nick else message.author.name,
            "user_id": message.author.id,
            "content": message.content,
            "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    trigger_blocks = [{"text": trigger}]
    if include_images:
        for a in message.attachments:
            image_targets.append((a, trigger_blocks))
    # newest attachments win when there are more than the per-turn cap
    image_blocks_added = 0
    for attachment, content_list in image_targets[-MAX_VISION_IMAGES:]:
        block = await _attachment_image_block(attachment)
        if block is not None:
            content_list.append(block)
            image_blocks_added += 1

    tools = _enabled_tools()
    agent = None
    prompt = trigger_blocks if len(trigger_blocks) > 1 else trigger
    x = 0
    r = None
    msg = None
    images_in_turn = image_blocks_added > 0
    degraded_to_text = False
    while x < 3 and r is None:
        if agent is None:
            agent = _make_agent(
                model or chat_ollama, CHAT_SYSTEM_PROMPT, past_chat_messages, tools=tools
            )
        else:
            logging.warning(
                "chat reply %d was not parseable JSON; asking the model to reformat", x
            )
            prompt = RETRY_FORMAT_DIRECTIVE
        try:
            response = await agent.invoke_async(prompt)
            raw_text = _extract_text(response)
        except Exception:
            logging.exception("chat agent invocation failed (attempt %d)", x + 1)
            if images_in_turn and not degraded_to_text:
                # likely the server choking on the image payload (e.g. llama.cpp
                # 400s on data URLs it cannot sniff): rebuild text-only once,
                # free of charge - a still answer beats a silent bot
                logging.warning("degrading chat turn to text-only after image failure")
                degraded_to_text = True
                past_chat_messages = _strip_image_blocks(past_chat_messages)
                agent = None
                prompt = trigger
                continue
            x += 1  # a model/server failure burns an attempt too
            continue
        msg, r = _parse_chat_reply(raw_text)
        x += 1  # Increment counter to avoid infinite loop
    if r is not None:
        return r
    return msg if isinstance(msg, str) else ""


# Canned status lines matching the LLM's prompt style; used when the LLM
# is down or slow so commands never fail over their decorative message.
_WORKING_FALLBACKS = [
    "I'm working on it, relax.",
    "Hold your horses, speed racer.",
    "Patience, mortal.",
    "Spinning up the thing you asked for.",
    "Processing. Yes, this part takes effort.",
    "One moment. I'm busy, but not that busy.",
]


async def working_on_it(model=None):
    """Return a short status message for in-progress commands.

    Decorative only: if the LLM is unavailable, returns an empty result, or
    takes longer than a small timeout, falls back to a canned message so the
    calling command always proceeds.
    """
    try:
        agent = _make_agent(model or chat_ollama_fast)
        response = await asyncio.wait_for(
            agent.invoke_async(
                """You are generating a single short, snarky, mildly antagonistic status message telling the user their task is in progress.
Rules:
- Output exactly one message.
- Be playful and sarcastic, slightly impatient, but not offensive.
- Maximum 12 words.
- Do not offer options, explanations, or follow-up questions.
- Do not ask if they want more.
- Do not put the message in quotes.

Example styles: "I'm working on it, relax.", "Hold your horses, speed racer.", "Patience, mortal."
"""
            ),
            timeout=5.0,
        )
        msg = _extract_text(response).strip()
        if msg:
            return msg
        logging.warning("LLM returned an empty status message; using fallback.")
    except Exception as ex:
        logging.warning(
            f"LLM unavailable for status message ({ex!r}); using fallback."
        )
    return random.choice(_WORKING_FALLBACKS)
