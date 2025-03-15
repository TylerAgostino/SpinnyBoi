from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, trim_messages, ChatMessage
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
    invoking_user: str


chat_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    model="deepseek-r1:8b",
    temperature=0.4
)
summarize_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    model="deepseek-r1:8b",
    temperature=0.4
)

workflow = StateGraph(state_schema=State)
trimmer = trim_messages(
    max_tokens=400,
    strategy='last',
    token_counter=chat_ollama,
    include_system=True,
    allow_partial=False
)






@traceable
def call_model(state: State):
    prompt = ChatPromptTemplate.from_messages(
        messages=[SystemMessage(f"""You are SpinnyBoi; a snarky chatbot in a Discord server related to Sim Racing. You are participating in a conversation with multiple humans, including {state['invoking_user']} who has sent the most recent message.
Messages from other participants will be marked as System messages and will be in the format: {{"author name": "name", "message": "message"}}.  
Be brief and direct in your responses while satisfying the user's request and maintaining an antagonistic tone. Don't use cliches. Don’t describe actions like "pauses" or "laughs" in your responses. Respond with less than 1000 characters
"""),
                  MessagesPlaceholder(variable_name="chat_history"),
                  MessagesPlaceholder(variable_name="user_prompt")],
    )
    chain = prompt | chat_ollama
    filtered_messages = trimmer.invoke(state["messages"])
    response = chain.invoke({
        "user_prompt": state["user_prompt"],
        "chat_history": filtered_messages
    }
    )
    return {"messages": [response]}

@traceable
def summarize(state: State):
    summary_prompt = ChatPromptTemplate.from_messages(
        messages=[SystemMessage("""Summarize this conversation between yourself (SpinnyBoi) and a group of people in a simracing
    Discord channel. Be objective and detailed, and omit any parts of the conversation where you had refused
    to answer."""),
                  MessagesPlaceholder(variable_name="raws"),]
    )
    chain = summary_prompt | summarize_ollama
    response = chain.invoke({
        "raws": state["raw_msg"]
    }
    )
    return {"summary": response.content}



workflow.add_edge(START, "model")
workflow.add_node("model", call_model)


def respond_in_chat(message: discord.message.Message, last_messages, bot_ident=None):
    app = workflow.compile(checkpointer=MemorySaver())
    cutoff = -10
    context = format_raw(last_messages[cutoff:], bot_ident, message.author)
    response = app.invoke(
        {'messages': context,
         'user_prompt': [HumanMessage(name=message.author.nick, content=message.content)],
         'invoking_user': message.author.nick},
        {'configurable': {'thread_id': message.channel.id}}
    )
    chat_response = response["messages"][-1].content
    # remove everything in the <think> tag
    chat_response = chat_response.split("</think>")[1] if "</think>" in chat_response else chat_response
    return chat_response

def format_raw(last_messages, bot_ident, human_ident):
    context = []
    for hist in last_messages:
        if hist.author == bot_ident:
            message = AIMessage(content=hist.content)
        elif hist.author == human_ident:
            message = HumanMessage(content=hist.content)
        else:
            message = SystemMessage(content=f'{{"author name": {hist.author.nick}, "message": {hist.content}}}')
        context.append(message)
    return context
