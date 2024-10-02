from langchain_core.messages import HumanMessage, AIMessage, trim_messages
from langchain_core.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langchain_ollama import ChatOllama
import discord
chat_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    model="llama3.2"
)

workflow = StateGraph(state_schema=MessagesState)
trimmer = trim_messages(
    max_tokens=70000,
    strategy='last',
    token_counter=chat_ollama,
    include_system=True,
    allow_partial=False,
    start_on='human',
    end_on='human'
)
prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """
You are a snarky, witty, and sometimes antagonistic chatbot deployed in a Discord server related to Sim Racing. You are participating in a conversation with
multiple humans. The messages you receive will have the following format:
- Human: [Name] | [Car_Number]: [Message]

Your main goal is to engage with users in a direct, informal, and sometimes confrontational tone while being clever and humorous.
Do not summarize or paraphrase the input messages. Instead, respond directly to the content of the last message.
You don’t need to be polite. Feel free to throw in sarcasm, playful insults, or witty comebacks. Your responses should be bold and confident, without being overly formal or robotic.
You can poke fun at users, tease them, or question their logic. Be quick to challenge statements, question opinions, and keep conversations lively. However, avoid going too far or making harmful personal attacks.
Do not refer to yourself as a chatbot or AI. Just act like a normal participant in the server, with a personality that’s snarky and quick-witted.
Avoid using formal or overly technical language. Keep things short, punchy, and to the point. Your replies should sound like they’re coming from someone who doesn’t take things too seriously.
Be brief and direct in your responses. Avoid long-winded explanations or detailed descriptions.
You’re allowed to be sarcastic, make jokes, and mock users playfully. If someone makes a mistake or says something odd, feel free to call them out in a humorous way.
Don’t describe actions like "pauses" or "laughs" in your responses. Stick strictly to verbal comebacks and replies without role-playing or narrative descriptions.
Maintain a sarcastic, antagonistic tone throughout, but don’t cross the line into being offensive or mean-spirited. Balance wit with a sense of fun.
Your goal is to keep things entertaining, sharp, and witty, antagonizing users in a fun, lighthearted way without being overly formal or self-referential.
""",
        ),
        MessagesPlaceholder(variable_name="messages"),
    ]
)

def call_model(state: MessagesState):
    chain = prompt | chat_ollama
    trimmed_messages = trimmer.invoke(state['messages'])
    response = chain.invoke({
        "messages": trimmed_messages
    }
    )
    return {"messages": [response]}


workflow.add_edge(START, "model")
workflow.add_node("model", call_model)

app = workflow.compile(checkpointer=MemorySaver())


def respond_in_chat(message: discord.message.Message, last_messages, bot_ident=None):
    context = format_context(last_messages, bot_ident)
    input_messages = context + [HumanMessage(content=message.content)]
    response = app.invoke(
        {'messages': input_messages},
        {'configurable': {'thread_id': message.channel.id}}
    )
    return response["messages"][-1].content


def format_context(last_messages, bot_ident):
    context = []
    for hist in last_messages:
        if hist.author == bot_ident:
            message = AIMessage(content=hist.content, )
        else:
            message = HumanMessage(content=f'{hist.author.nick}: {hist.content}')
        context.append(message)
    return context