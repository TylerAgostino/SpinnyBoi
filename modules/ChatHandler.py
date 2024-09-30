from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate
)
from langchain_ollama import ChatOllama
import discord
from langchain.chains import LLMChain
chat_ollama = ChatOllama(
    base_url="http://192.168.1.125:11434",
    model="llama3"
)


def respond_in_chat(message: discord.message.Message, last_messages, bot_ident=None):
    context = format_context(last_messages, bot_ident)
    chat_prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessage(
                content="You are a chatbot participating in a conversation with multiple people. You should respond to the last message in the conversation.",
            ),
            *context,
            HumanMessagePromptTemplate.from_template(
                "{human_ident}: {human_input}"
            )
        ]
    )
    chain = LLMChain(
        llm=chat_ollama,
        prompt=chat_prompt,
        verbose=True
    )
    response = chain.predict(human_input=message.content, human_ident=message.author.nick)
    return response


def format_context(last_messages, bot_ident):
    context = []
    for hist in last_messages:
        if hist.author == bot_ident:
            message = AIMessage(content=hist.content, )
        else:
            message = HumanMessage(content=f'{hist.author.nick}: {hist.content}')
        context.append(message)
    return context