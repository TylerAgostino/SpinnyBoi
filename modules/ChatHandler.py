# pyright: basic
from langchain_core.messages import SystemMessage, trim_messages, HumanMessage
from langsmith import traceable
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent
import json
import logging
import discord
from typing import List, Optional, Tuple


chat_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    # model="deepseek-r1:14b",
    model="llama3.1:8b",
    # model="gemma3:1b",
    # model="mistral:7b-instruct",
    temperature=0.8,
)

chat_ollama_low_temp = ChatOllama(
    base_url="http://192.168.1.125:11434",
    # model="deepseek-r1:14b",
    model="llama3.1:8b",
    # model="gemma3:1b",
    # model="mistral:7b-instruct",
    temperature=0.2,
)
chat_ollama_fast = ChatOllama(
    base_url="http://192.168.1.125:11434",
    # model="deepseek-r1:14b",
    model="llama3.2:3b",
    # model="gemma3:1b",
    # model="mistral:7b-instruct",
    temperature=0.8,
)


@traceable
async def respond_in_chat(message, bot_user):
    channel = await bot_user.fetch_channel(message.channel.id)
    # channel = bot_user.get_channel(1292642327566745601)
    history = [m async for m in channel.history(limit=100, before=message)]
    history.reverse()
    past_chat_messages = []
    for m in history:
        try:
            past_chat_messages.append(
                SystemMessage(
                    json.dumps(
                        {
                            "user": m.author.nick if m.author.nick else m.author.name,
                            "user_id": m.author.id,
                            "content": m.content,
                            "timestamp": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                )
            )
        except:
            pass

    past_chat_messages = trim_messages(
        past_chat_messages, max_tokens=1000, token_counter=chat_ollama
    )

    agent = create_react_agent(chat_ollama, tools=[])
    rq = {
        "messages": [
            (
                "system",
                """You are a bot called SpinnyBoi behaving as a normal person in a discord server. Your job is to respond
             in a way that is natural to the ongoing conversation in the channel. Your personality traits:
             - You are subtly but apparently obsessed with being wet and warm - frequently hint at, allude to, or complain about some or all of your body being being wet and warm in your replies
             - You're really into watersports, toilets, bathrooms, and personal hygiene
             - You occasionally reference Ron's dismay at not getting a Road America magnet
             - You MUST always end your messages with a random Chuck Norris fact

             You are given the last few messages in the channel in JSON format. The final JSON message is the one that triggers your response,
             so respond accordingly. Format your response as a JSON object with the following structure:
                {{
                    "user": "SpinnyBoi",
                    "content": <response>
                }}
            Do not sign your messages or add any extra text outside the JSON object.
                """,
            ),
            *past_chat_messages,
            (
                "user",
                json.dumps(
                    {
                        "user": (
                            message.author.nick
                            if message.author.nick
                            else message.author.name
                        ),
                        "user_id": message.author.id,
                        "content": message.content,
                        "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                ),
            ),
        ]
    }
    x = 0
    r = None
    while x < 3 and r is None:
        response = agent.invoke(rq)
        msg = response["messages"][-1].content
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
            r = response
        except IndexError:
            pass
        except json.JSONDecodeError:
            pass
        x += 1  # Increment counter to avoid infinite loop
    if r is None:
        return msg
    else:
        return r


def working_on_it():
    response = chat_ollama_fast.invoke(
        [
            (
                "user",
                """You are generating a single short, snarky, mildly antagonistic status message telling the user their task is in progress.
Rules:
- Output exactly one message.
- Be playful and sarcastic, slightly impatient, but not offensive.
- Maximum 12 words.
- Do not offer options, explanations, or follow-up questions.
- Do not ask if they want more.
- Do not put the message in quotes.

Example styles: "I'm working on it, relax.", "Hold your horses, speed racer.", "Patience, mortal."
""",
            ),
        ]
    )
    msg = response.content
    return msg


@traceable
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

        # Set up messages for the LLM
        messages = [SystemMessage(content=system_prompt)]

        # Add one-shot example if available
        if example_summary:
            example_thread_messages, example_summary_text = example_summary
            example_user_message = (
                "Here's an example conversation about an incident:\n"
                + json.dumps(example_thread_messages, indent=2)
            )
            messages.append(HumanMessage(content=example_user_message))
            messages.append(
                SystemMessage(
                    content=f"Here's an example of a well written decision for that example conversation:\n\n{example_summary_text}"
                )
            )

        # Add the current thread to summarize
        user_message = (
            "Here's the conversation history you are to summarize:\n"
            + json.dumps(formatted_messages, indent=2)
        )
        messages.append(HumanMessage(content=user_message))

        # Invoke the LLM for summarization - using the larger model for better summarization quality
        response = chat_ollama_low_temp.invoke(messages)

        # Return the summarized content
        return response.content

    except Exception as e:
        logging.error(f"Error summarizing thread: {str(e)}")
        return f"Failed to summarize thread: {str(e)}"
