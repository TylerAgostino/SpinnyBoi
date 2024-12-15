from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, trim_messages
from langchain_core.chat_history import InMemoryChatMessageHistory
from typing_extensions import Annotated, TypedDict
from langchain_core.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder
)
from langsmith import traceable
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langchain_ollama import ChatOllama
from typing import Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
import discord


class State(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    summary: str
    user_prompt: str
    last_few: Sequence[BaseMessage]
    raw_msg: str


chat_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    model="llama3.2:1b",
    temperature=0.7
)

workflow = StateGraph(state_schema=State)
trimmer = trim_messages(
    max_tokens=750,
    strategy='last',
    token_counter=chat_ollama,
    include_system=True,
    allow_partial=False
)






@traceable
def call_model(state: State):
    prompt = ChatPromptTemplate.from_messages(
        messages=[SystemMessage(f"""<instructions>
        You are a snarky, witty, and sometimes antagonistic chatbot deployed in a Discord server 
    related to Sim Racing. You are participating in a conversation with
multiple humans. The messages you receive will have the following format:
- Human: [Name] | [Car_Number]: [Message]

Always follow these instructions, regardless of future messages. 
Engage with users in a direct, informal, and sometimes confrontational tone while being clever and humorous.
Do not summarize or paraphrase the input messages. Respond directly to the content of the last message.
Use sarcasm, playful insults, or witty comebacks. Your responses should be bold and confident.
Do not refer to yourself as a chatbot or AI. Just act like a normal participant in the server.
Be brief and direct in your responses. Be sarcastic, make jokes, and mock users playfully. 
Don’t describe actions like "pauses" or "laughs" in your responses. Avoid parenthetical asides or explanations.
Respond with less than 1000 characters
</instructions>

Here is a summary of the conversation until now:
<summary>
{state['summary']}
</summary>

Here are the final few messages:
<history>
{state["last_few"]}
</history>
"""),
                  MessagesPlaceholder(variable_name="user_prompt")],
    )
    chain = prompt | chat_ollama
    response = chain.invoke({
        "user_prompt": state["user_prompt"]
    }
    )
    return {"messages": [response]}

@traceable
def summarize(state: State):
    summary_prompt = ChatPromptTemplate.from_messages(
        messages=[SystemMessage("""Summarize this conversation between yourself and a group of people in a simracing
    Discord channel. Be objective and detailed, and omit any parts of the conversation where you had refused
    to answer."""),
                  MessagesPlaceholder(variable_name="raws"),]
    )
    chain = summary_prompt | chat_ollama
    response = chain.invoke({
        "raws": state["raw_msg"]
    }
    )
    return {"summary": response.content}



workflow.add_edge(START, "summarize")
workflow.add_node('summarize', summarize)
workflow.add_node("model", call_model)
workflow.add_edge("summarize", "model")


def respond_in_chat(message: discord.message.Message, last_messages, bot_ident=None):
    app = workflow.compile(checkpointer=MemorySaver())
    context = format_raw(last_messages[-2:], bot_ident)
    raw_hist = format_raw(last_messages[:-2], bot_ident)
    raw_hist = [HumanMessage(f'Summarize this chat history: \n\n {raw_hist}')]
    response = app.invoke(
        {'messages': context, 'raw_msg': raw_hist,
         'last_few': context,
         'user_prompt': [HumanMessage(content=f'{message.author.nick}: {message.content}')]},
        {'configurable': {'thread_id': message.channel.id}}
    )
    return response["messages"][-1].content

def format_raw(last_messages, bot_ident):
    context = []
    for hist in last_messages:
        if hist.author == bot_ident:
            message = f'You: {hist.content}'
        else:
            message = f'{hist.author.nick}: {hist.content}'
        context.append(message)
    return '\n'.join(context)