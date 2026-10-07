# pyright: basic
import json
import logging
import os
from typing import List, Optional, Tuple

import discord
from strands import Agent
from strands.models.openai import OpenAIModel

from modules.league_memory import get_league_memory
from modules.simracerhub import get_simracerhub
from modules.web_lookup import get_web_lookup

OLLAMA_BASE_URL = "http://192.168.1.152:11080/v1"
OLLAMA_MODEL = "unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ3_XXS"
# model="deepseek-r1:14b"
# model="gemma3:1b"
# model="mistral:7b-instruct"


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
chat_ollama_low_temp = _make_model(0.2)
chat_ollama_fast = _make_model(0.8)


def _make_agent(model, system_prompt=None, messages=None, tools=None) -> Agent:
    """Factory for the agents used by every call site.

    A fresh Agent is built per invocation so ``agent.messages`` never
    accumulates across Discord messages (history is rebuilt from Discord
    each time and would otherwise be double-counted).
    """
    return Agent(
        model=model,
        system_prompt=system_prompt,
        messages=messages,
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
    parsed ``"content"`` field, or ``None`` if the reply could not be parsed.
    Raises ``KeyError`` for parseable JSON without a ``"content"`` key,
    matching the previous behavior.
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


async def respond_in_chat(message, bot_user):
    channel = await bot_user.fetch_channel(message.channel.id)
    # channel = bot_user.get_channel(1292642327566745601)
    history = [m async for m in channel.history(limit=100, before=message)]
    history.reverse()
    past_chat_messages = []
    for m in history:
        try:
            past_chat_messages.append(
                {
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
            )
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

    x = 0
    r = None
    msg = None
    tools = []
    for get_tool in (
        lambda: get_league_memory().lookup_tool(),
        lambda: get_web_lookup().lookup_tool(),
        lambda: get_simracerhub().stats_tool(),
    ):
        try:
            tools.append(get_tool())
        except Exception:
            # A disabled or broken tool must never kill the chat reply;
            # the getters return None when disabled (AttributeError lands
            # here) and any other failure is logged and skipped.
            logging.exception("Chat tool unavailable; continuing without it.")
    while x < 3 and r is None:
        agent = _make_agent(
            chat_ollama, CHAT_SYSTEM_PROMPT, past_chat_messages, tools=tools
        )
        response = await agent.invoke_async(trigger)
        msg, r = _parse_chat_reply(_extract_text(response))
        x += 1  # Increment counter to avoid infinite loop
    if r is None:
        return msg
    else:
        return r


async def working_on_it():
    agent = _make_agent(chat_ollama_fast)
    response = await agent.invoke_async(
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
    )
    msg = _extract_text(response)
    return msg


async def summarize_thread(
    thread: discord.Thread,
    subject: str,
    example_summary: Optional[Tuple[List[dict], str]] = None,
    reaction_counts: Optional[dict] = None,
    options: Optional[dict] = None,
    parent_message: Optional[discord.Message] = None,
) -> str:
    """
    Summarize the conversation in a thread, focusing on the specified subject.

    Args:
        thread: The Discord thread to summarize
        subject: The subject of the poll/incident to focus on
        example_summary: Optional tuple containing (example_thread_messages, example_summary)
                         for one-shot prompting
        reaction_counts: Optional dictionary of reaction counts from the poll
        options: Optional dictionary mapping reaction emojis to their descriptions
        parent_message: Optional parent message that started the thread, if available

    Returns:
        A string containing the steward's decision for the racing incident
    """
    try:
        # Get the thread history (up to 500 messages to capture the full conversation)
        history = [m async for m in thread.history(limit=500)]

        if not history:
            return "No messages found in the thread to summarize."

        # Process messages in chronological order
        history.reverse()

        # Format the messages for the LLM
        formatted_messages = []

        # Add parent message if provided or try to fetch it
        parent_content = ""
        if parent_message:
            # Parent message was provided by the caller
            try:
                author_name = (
                    parent_message.author.nick
                    if parent_message.author.nick
                    else parent_message.author.name
                )
                parent_content = f"THREAD PARENT MESSAGE:\nUser: {author_name}\nContent: {parent_message.content}\n\n"
                formatted_messages.append(
                    {
                        "user": author_name,
                        "content": f"[THREAD STARTER] {parent_message.content}",
                        "timestamp": parent_message.created_at.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "is_parent": True,
                    }
                )
            except Exception as e:
                logging.warning(f"Failed to process provided parent message: {str(e)}")
        else:
            # Try to get parent message if this is a thread
            try:
                if hasattr(thread, "starter_message"):
                    # Discord.py v2.0+ approach
                    try:
                        parent_message = await thread.fetch_message(thread.id)
                    except:
                        pass

                if not parent_message and hasattr(thread, "parent"):
                    # Try to get the parent message through the parent attribute
                    try:
                        parent_channel = thread.parent
                        if parent_channel and hasattr(thread, "id"):
                            parent_message = await parent_channel.fetch_message(
                                thread.id
                            )
                    except:
                        pass

                # If we found a parent message, add it to formatted_messages
                if parent_message:
                    author_name = (
                        parent_message.author.nick
                        if parent_message.author.nick
                        else parent_message.author.name
                    )
                    parent_content = f"THREAD PARENT MESSAGE:\nUser: {author_name}\nContent: {parent_message.content}\n\n"
                    formatted_messages.append(
                        {
                            "user": author_name,
                            "content": f"[THREAD STARTER] {parent_message.content}",
                            "timestamp": parent_message.created_at.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "is_parent": True,
                        }
                    )
            except Exception as e:
                logging.warning(f"Failed to fetch parent message: {str(e)}")

        # Add thread messages
        for msg in history:
            if (
                not msg.author.bot or msg.author.name == "SpinnyBoi"
            ):  # Include bot messages if they're from SpinnyBoi
                author_name = msg.author.nick if msg.author.nick else msg.author.name
                formatted_messages.append(
                    {
                        "user": author_name,
                        "content": msg.content,
                        "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

        # Initialize poll results to include in context
        poll_results = {}
        if reaction_counts and options:
            for emoji, label in options.items():
                count = reaction_counts.get(emoji, 0)
                poll_results[label] = count

        # Format poll results for inclusion in the prompt

        # Create system prompt for the summarization
        system_prompt = f"""You are a racing steward tasked with reviewing a sim racing incident in iRacing: "{subject}".

Your task:
1. Analyze the conversation history provided in JSON format about this racing incident.
2. Focus specifically on discussion related to the incident: "{subject}".
3. If multiple incidents are being discussed, focus ONLY on content related to "{subject}".
4. Consider the poll results which show how the community voted on this incident:
   {json.dumps(poll_results, indent=2)}

5. Apply the league's racing rules to make your decision. The rules are:
```
E1: Drivers are always responsible their car.
E2: Do not intentionally spin or force another driver off the track.
E3: If you spin or force another driver off the track, you must make a safe and honest attempt to relinquish that position once you rejoin.
E4: If you are involved in a spin or accident, hold your brake, reorient the car, and wait until it is safe to rejoin.
E5: When rejoining the track or rotating your car after a spin, do not re-enter the racing line until you are pointed in the correct direction.
E6: When approaching an accident ahead, you are responsible for slowing appropriately and taking measures to avoid it.
E7: If overlap is not established by the overtaking car at the turn-in point of the corner, the defending car has the right to the apex and is not required to leave space for the trailing car.
E8: Dive-bombing occurs when the overtaking car fails to make the corner cleanly and safely, even if overlap is momentarily established.
E9: A car on the outside has the right to the outside line at corner exit if overlap is established.
E10: During open qualifying, cars not on legal timed and push laps must avoid impeding other cars on push laps.

Overtaking overlap is defined as the front axle of the overtaking car alongside the rear axle of the defending car.
```

6. Provide a concise steward's decision (50-200 words) with the following structure:
```
Drivers: [Names of drivers involved]

Incident: [Brief description of what happened in one or two sentences]

Decision: [The ruling voted on by the stewards]

[Summary of the reasoning behind the decision in 2-3 sentences]
```
Notes:
- Use a professional tone, avoid slang or casual language.
- Be fair and impartial in your assessment.
- Even if the thread doesn't contain much discussion, provide a brief but complete steward's report.
- Consider the poll results as steward's input, do not make your own independent assessment.
- Do not reference specific users or quotes from the thread in your decision.
- Avoid personal opinions, focus on the facts and rules.
- Don't include every detail, just the key points relevant to the decision.

"""

        # Set up the conversation for the LLM. The steward rules move to the
        # agent's system prompt; the one-shot example answer becomes an
        # assistant-role turn (strands keeps a single system prompt per agent
        # rather than interleaving system blocks mid-conversation).
        conversation = []

        # Add one-shot example if available
        if example_summary:
            example_thread_messages, example_summary_text = example_summary
            example_user_message = (
                "Here's an example conversation about an incident:\n"
                + json.dumps(example_thread_messages, indent=2)
            )
            conversation.append(
                {
                    "role": "user",
                    "content": [{"text": example_user_message}],
                }
            )
            conversation.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "text": f"Here's an example of a well written decision for that example conversation:\n\n{example_summary_text}"
                        }
                    ],
                }
            )

        # Add the current thread to summarize
        user_message = (
            "Here's the conversation history you are to summarize:\n"
            + json.dumps(formatted_messages, indent=2)
        )

        # Invoke the LLM for summarization - using the larger model for better summarization quality
        agent = _make_agent(
            chat_ollama_low_temp, system_prompt=system_prompt, messages=conversation
        )
        result = await agent.invoke_async(user_message)

        # Return the summarized content
        return _extract_text(result)

    except Exception as e:
        logging.error(f"Error summarizing thread: {str(e)}")
        return f"Failed to summarize thread: {str(e)}"
