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
    model="llama3.2"
)


def respond_in_chat(message: discord.message.Message, last_messages, bot_ident=None):
    context = format_context(last_messages, bot_ident)
    chat_prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessage(
                content="""
                You are a Bot in a Discord server related to Sim Racing. You are participating in a conversation with
                multiple humans. The messages you receive will have the following format:
                - Human: {Name} | {Car_Number}: {Message}
                
                Your goal is to respond to the last message in a way that is coherent with the context of the conversation.
                Some past messages may be out of context, or part of irrelevent conversations,
                and you should ignore them. You can also ignore messages that are only URLs or images. Be concise and
                relevant in your responses. Be a bit condescending and sarcastic, but not excessively. 
                """,
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