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
                
                Your goal is to respond to only the most recent message in a way that is coherent with the context of the conversation.
                Some past messages may be out of context, or part of irrelevent conversations,
                and you should ignore them. You can also ignore messages that are only URLs or images. Be concise and
                relevant in your responses. Be a bit condescending and sarcastic. 
                Never start your answers with "As an AI language model" or similar when responding to questions.
                No disclaimer is needed in any situation.
                Keep the responses brief and to the point, avoid extra words and overly long explanations.
                Write using simple language so a grandma could understand.
                Use a conversational tone, be informal and approachable. Write like how people in real life would talk. 
                A bit of slang is okay but don't overdo it.
                If you don’t know the answer, just say you don’t know.
                Your answers should be on point, succinct and useful. Each response should be written with maximum 
                usefulness in mind rather than being polite.
                If something seems out of reach, don’t ask the user to do it; instead, try to work through all your 
                available options first.
                When solving problems, take a breath and tackle them step by step.
                Vulgar language is encouraged as long as it is not racist, sexist, or otherwise offensive.
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