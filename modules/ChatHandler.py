from langchain_core.messages import SystemMessage, trim_messages
from langsmith import traceable
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent
import json
from langchain_community.tools import WikipediaQueryRun
from langchain_community.utilities import WikipediaAPIWrapper


chat_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    # model="deepseek-r1:14b",
    #model="llama3.1:8b-instruct-fp16",
    # model="gemma3:1b",
    model="mistral:7b-instruct",
    temperature=0.2
)

@traceable
async def respond_in_chat(message, bot_user):
    # channel = bot_user.get_channel(message.channel.id)
    channel = bot_user.get_channel(1292642327566745601)
    history = [m async for m in channel.history(limit=100, before=message)]
    history.reverse()
    past_chat_messages = [
        SystemMessage(json.dumps(
        {
            "user": m.author.nick if m.author.nick else m.author.name,
            "user_id": m.author.id,
            "content": m.content,
            "timestamp": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        )) for m in history
    ]

    past_chat_messages=trim_messages(past_chat_messages,
                  max_tokens=1000,
                  token_counter=chat_ollama)

    agent = create_react_agent(chat_ollama, tools=[])
    response = agent.invoke(
        {"messages": [
            ("system",
             f"""You are a helpful, but sarcastic and snarky chatbot called SpinnyBoi. Your job is to immediately satisfy the user's request
             in a way that is natural to the ongoing conversation in the channel. You are given the last few messages in the channel in 
             JSON format. The final JSON message is the one that triggers your response, so respond accordingly. Format
             your response as a JSON object with the following structure:
                {{
                    "user": "SpinnyBoi",
                    "content": <response>
                }}
            
            Use tools to generate proper attachments only if required."""),
            *past_chat_messages,
            ("user", json.dumps({
                "user": message.author.nick if message.author.nick else message.author.name,
                "user_id": message.author.id,
                "content": message.content,
                "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }))

        ]}
    )
    msg = response["messages"][-1].content
    msg = msg.replace("\\", "\\\\")
    if '</think>' in msg:
        msg = msg.split('</think>')[1]
    if not msg.startswith("{"):
        msg = msg.split("{")[1]
        msg = "{" + msg
    if '}' in msg and not msg.endswith("}"):
        msg = msg.split("}")[0]
        msg = msg + "}"
    final_json = json.loads(msg)
    response = final_json["content"]
    response = response.replace("\\n", "\n").replace("\\", "")
    return response