# pyright: basic
import asyncio
import io
import json
import logging
import mimetypes
import os
import random
from datetime import timedelta
from typing import Optional, Tuple

from PIL import Image
from strands import Agent
from strands.models.openai import OpenAIModel

from modules.env_config import env_enabled, env_int
from modules.league_memory import get_league_memory
from modules.simracerhub import get_simracerhub
from modules.web_lookup import get_web_lookup

# LLM endpoint/model: env-overridable, defaults to the home Ollama box.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.1.152:11080/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "unsloth/Qwen3.8-Flash-Next-GGUF:IQ3_XXS")

# Without max_tokens, llama.cpp/Ollama generate unbounded (num_predict=-1):
# a degraded model repeats nonsense forever and the turn never returns.
# Every completion gets a hard token cap. Reasoning ("thinking") tokens are
# counted against this same budget, so it must comfortably exceed a think +
# short JSON reply; the cap bounds degeneration, not the happy path.
CHAT_MAX_TOKENS = env_int(os.environ, "CHAT_MAX_TOKENS", 2048, 64, 8192)


def _turn_timeout_seconds() -> int:
    """Wall-clock cap for one agent invocation. httpx read timeouts are
    per-chunk, so a stream that keeps trickling tokens (degraded loop) or the
    strands retry backoff can never trip them; working_on_it already bounds
    itself with asyncio.wait_for, respond_in_chat must too."""
    return env_int(os.environ, "CHAT_TURN_TIMEOUT_SECONDS", 300, 1, 900)


def _make_model(temperature: float) -> OpenAIModel:
    # The OpenAI client requires a non-empty api_key even for local
    # OpenAI-compatible servers that ignore it. `timeout` is the httpx-level
    # backstop; the per-turn asyncio.wait_for in respond_in_chat is the
    # guard that actually fires against endless streams.
    return OpenAIModel(
        client_args={
            "api_key": os.getenv("OPENAI_API_KEY", "ollama"),
            "base_url": OLLAMA_BASE_URL,
            "timeout": _turn_timeout_seconds(),
        },
        model_id=OLLAMA_MODEL,
        params={"temperature": temperature, "max_tokens": CHAT_MAX_TOKENS},
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

# Pixel/token budget: the per-image 8 MB cap says nothing about pixel count.
# One 4000x3000 screenshot devours a llama.cpp context in vision tokens,
# silently truncates the history the model needs, and the turn degrades into
# endless nonsense - the exact "many images in channel history" failure.
MAX_VISION_REENCODE_BYTES = 1024 * 1024

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


def _vision_max_dim() -> int:
    """Longest-side pixel cap; images above it get downscaled before send."""
    return env_int(os.environ, "CHAT_IMAGE_MAX_DIM", 1568, 256, 4096)


def _vision_total_budget_bytes() -> int:
    """Cap on the summed (re-encoded) bytes of all image blocks in one turn."""
    return env_int(os.environ, "CHAT_IMAGE_TOTAL_BYTES", 4 * 1024 * 1024, 64 * 1024)


# An 8 MB PNG can hide 100+ megapixels; refuse rather than decode one to
# downscale it. Pillow's own bomb limit is a global monkey-patch this repo
# does not take, so the ceiling below is checked after a lazy load.
MAX_VISION_MAX_PIXELS = 40_000_000


def _normalize_image_bytes(data: bytes, fmt: str) -> Tuple[bytes, str]:
    """Downscale/re-encode an oversized image to fit the vision encoder's
    token budget. Undecodable bytes pass through unchanged - the server
    accepts or rejects them exactly as before this function existed.
    Returns (b"", fmt) when the image must not be sent at all: decompression
    bombs and megapixel outliers (callers treat empty bytes as skip).
    Animated GIFs over the cap collapse to their first still frame (which is
    all the vision encoder ever saw of them anyway).
    Decoding is CPU- and memory-heavy: call via asyncio.to_thread, never
    inline on the Discord event loop."""
    max_dim = _vision_max_dim()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Image.DecompressionBombError:
        logging.warning("chat image exceeds Pillow pixel limit; skipping")
        return b"", fmt
    except Exception:
        return data, fmt
    w, h = image.size
    if w * h > MAX_VISION_MAX_PIXELS:
        logging.warning("chat image %dx%d over pixel budget; skipping", w, h)
        return b"", fmt
    if len(data) <= MAX_VISION_REENCODE_BYTES and max(w, h) <= max_dim:
        return data, fmt
    resized = max(w, h) > max_dim
    if resized:
        scale = max_dim / max(w, h)
        image = image.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    out = io.BytesIO()
    if fmt in ("jpeg", "jpg", "webp") and not has_alpha:
        image.convert("RGB").save(out, "JPEG", quality=85)
        new_fmt = "jpeg"
    else:
        image.save(out, "PNG")
        new_fmt = "png"
    out_bytes = out.getvalue()
    if not out_bytes or (not resized and len(out_bytes) >= len(data)):
        # re-encode bought nothing; keep what the server was getting
        return data, fmt
    return out_bytes, new_fmt


async def _attachment_image_block(attachment):
    """Download one Discord attachment as a strands image block.

    Returns None for non-images, formats with no registered mime (see the
    add_type block above; kept as a last-line guard), oversized files
    (skipped without download), and failed downloads - one bad attachment
    never sinks the turn. Images that pass are normalized
    (_normalize_image_bytes) so their vision-token cost is bounded."""
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
    # Pillow decode touches every pixel; keep it off the Discord event loop
    data, fmt = await asyncio.to_thread(_normalize_image_bytes, data, fmt)
    if not data:
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
    A PARSE retry continues the SAME agent conversation with a reformat
    directive - the model sees its own bad reply and the tool results stay
    in context, instead of the whole agent (including another tool call
    cycle) being rebuilt from scratch. An invocation that never produced a
    reply (exception or timeout) instead rebuilds the agent and re-asks the
    real prompt: a canceled strands call can sit mid-exchange, and there is
    no bad reply for a reformat directive to point at. Every invocation is
    capped on wall clock (_turn_timeout_seconds): a degraded model streaming
    tokens forever must never hold the Discord task hostage. If the inference
    server rejects an image payload or wedges on it (timeout), the turn
    degrades once to a rebuilt text-only conversation."""
    channel = await bot_user.fetch_channel(message.channel.id)
    max_age = message.created_at - timedelta(seconds=60 * 60 * 24 * 3)
    history = [m async for m in channel.history(limit=100, before=message, after=max_age)]
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
        except Exception as exc:
            logging.debug(f"Skipping history entry {m.id}: {exc}")

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
    # newest attachments win when there are more than the per-turn cap, and
    # the newest also claim the per-turn byte budget first (walk newest ->
    # oldest, so dropping an image never drops the one nearest the trigger)
    selected = image_targets[-MAX_VISION_IMAGES:]
    kept_blocks = {}
    budget = _vision_total_budget_bytes()
    for i in range(len(selected) - 1, -1, -1):
        block = await _attachment_image_block(selected[i][0])
        if block is None:
            continue
        size = len(block["image"]["source"]["bytes"])
        if size > budget:
            logging.warning(
                "chat image (%d bytes) over per-turn budget; skipping", size
            )
            continue
        budget -= size
        kept_blocks[i] = block
    image_blocks_added = 0
    for i, (_, content_list) in enumerate(selected):
        if i in kept_blocks:
            content_list.append(kept_blocks[i])
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
        turn_timeout = _turn_timeout_seconds()
        try:
            response = await asyncio.wait_for(
                agent.invoke_async(prompt), timeout=turn_timeout
            )
            raw_text = _extract_text(response)
        except asyncio.TimeoutError:
            # a degraded model keeps trickling nonsense tokens forever and
            # per-chunk httpx timeouts never fire against it: cut on the
            # wall clock so the turn always comes back
            logging.warning(
                "chat agent invocation timed out after %ss (attempt %d)",
                turn_timeout,
                x + 1,
            )
        except Exception:
            logging.exception("chat agent invocation failed (attempt %d)", x + 1)
        else:
            msg, r = _parse_chat_reply(raw_text)
            x += 1  # Increment counter to avoid infinite loop
            continue
        # invocation failed (error or timeout): a fat image payload is the
        # usual suspect (llama.cpp 400s on data URLs it cannot sniff, or
        # wedges encoding a giant one): rebuild text-only once, free of
        # charge - a still answer beats a silent bot
        if images_in_turn and not degraded_to_text:
            logging.warning("degrading chat turn to text-only after image failure")
            degraded_to_text = True
            past_chat_messages = _strip_image_blocks(past_chat_messages)
            agent = None
            prompt = trigger
            continue
        # a failed invocation produced no reply, so there is nothing for the
        # reformat directive to point at, and a canceled strands call may
        # have left agent.messages mid-exchange: rebuild the agent and
        # re-ask the real prompt (minus images once degraded)
        x += 1  # a model/server failure burns an attempt too
        agent = None
        prompt = (
            trigger
            if degraded_to_text or len(trigger_blocks) == 1
            else trigger_blocks
        )
        continue
    if r is not None:
        return r
    return msg if isinstance(msg, str) else ""


async def handle_mention(message, bot):
    """Answer a mention of the bot, swallowing listener-side failures.

    An exception escaping a discord.py listener kills the handler silently,
    so every failure is logged here and answered with a best-effort apology;
    this call never propagates an exception. Success replies exactly as the
    old on_message branch did: typing indicator, channel.send of the reply.
    """
    try:
        async with message.channel.typing():
            response = await respond_in_chat(message, bot)
            if response:
                await message.channel.send(response)
    except Exception:
        logging.exception("mention handling failed in channel %s", message.channel.id)
        try:
            await message.reply("Sorry, I couldn't process that.")
        except Exception:
            logging.debug("mention fallback reply failed in channel %s", message.channel.id)


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
