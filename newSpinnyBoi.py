import discord
from discord.ext import commands, tasks
from typing import Dict, List, Optional, Tuple
import logging
import os
from modules import CommandHandler, ChatHandler
import random
from modules.scheduler import (
    init_db,
    schedule_event,
    get_pending_events,
    mark_event_completed,
)
from modules.incidentCog import IncidentCog


intents = discord.Intents.default()
intents.message_content = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)

# Reactions

reactions_file = open("reactions.txt", "r", encoding="utf-8")
reactions = reactions_file.readlines()
reaction_dict = {}
for reaction in reactions:
    try:
        tup = reaction.split(",")
        reaction_key = tup[0].lower()
        reaction_dict[reaction_key] = []
        for emote_id in tup[1:]:
            reaction_dict[reaction_key].append(emote_id.strip())
    except IndexError:
        logging.error("Invalid reaction line: " + reaction)
        logging.error("Its probably Brian's fault")
    except Exception as e:
        logging.error("Something went wrong: " + str(e))
reactions_file.close()


bot = commands.Bot(
    command_prefix="/",
    debug_guilds=[420037391454044171, 981935710514839572],
    intents=intents,
)
spin = bot.create_group("spin", "Spin commands")

@spin.command(name="_")
async def spin_default(ctx):
    await ctx.defer()
    await spin_handler(ctx, "preset", 'default')

@spin.command(name="custom")
@discord.option(
    "custom_options",
    str,
    required=True,
    description="Comma-separated custom options",
)
async def spin_custom(ctx, custom_options: str):
    await ctx.defer()
    await spin_handler(ctx, "custom", custom_options)

async def spin_handler(ctx, method, *args, **kwargs):
    bot_response = await ctx.respond(ChatHandler.working_on_it())
    handler = CommandHandler.CommandHandler()
    try:
        method = getattr(handler, method)
        output = method(*args, **kwargs)
        response_text = output[0] if isinstance(output, tuple) else output
        response_attachment = (
            output[1] if isinstance(output, tuple) and len(output) > 1 else None
        )
        response_filename = (
            output[2] if isinstance(output, tuple) and len(output) > 2 else "wheel.gif"
        )
        if isinstance(response_attachment, list):
            response_attachment = [
                discord.File(fp=f, filename=response_filename)
                for f in response_attachment
            ]
        else:
            response_attachment = (
                [discord.File(fp=response_attachment, filename=response_filename)]
                if response_attachment
                else None
            )
        await bot_response.edit(
            content=f"{ctx.author.mention} {response_text}",
            files=response_attachment,
        )
    except Exception as e:
        logging.error(f"Error in spin_handler: {str(e)}")
        await bot_response.edit(content=f"An error occurred: {str(e)}")
    finally:
        handler.driver.quit()


static_handler = CommandHandler.CommandHandler()


async def get_presets(ctx):
    # await ctx.defer()
    handler = static_handler
    return [x["Fullname"] for x in handler.presets_df.to_dict("records")]


async def get_preset_tabs(ctx):
    # await ctx.defer()
    preset_name = ctx.options.get("preset_name")
    handler = static_handler
    presets = handler.presets_df.to_dict("records")
    p = next((x for x in presets if x["Fullname"] == preset_name), None)
    if p:
        return [k for k, v in p.items() if isinstance(v, str) and k != "Fullname"]
    return []


# @bot.slash_command(name="spin")
# @discord.option(
#     "preset_name",
#     str,
#     required=False,
#     default="default",
#     description="Name of the preset to use",
#     autocomplete=discord.utils.basic_autocomplete(get_presets),
# )
# @discord.option(
#     "custom_options",
#     str,
#     required=False,
#     default=None,
#     description="Comma-separated custom options",
# )
# async def spin_default(
#     ctx,
#     preset_name: Optional[str] = None,
#     custom_options: Optional[str] = None,
#     tab_name: Optional[str] = None,
#     tab_filter: Optional[str] = None,
# ):
#     await ctx.defer()
#     if (
#         preset_name
#         and (custom_options or tab_name or tab_filter)
#         or custom_options
#         and (preset_name or tab_name or tab_filter)
#         or tab_name
#         and (preset_name or custom_options)
#     ):
#         await ctx.respond(
#             "You can only use one of preset_name, custom_options, or tab_name at a time."
#         )
#         return
#     if tab_filter and not tab_name:
#         await ctx.respond("You must provide a tab_name if you provide a tab_filter.")
#         return
#
#     if not (preset_name or custom_options or tab_name):
#         preset_name = "default"
#
#     if custom_options:
#         await spin_handler(ctx, "custom", custom_options)
#     if preset_name:
#         await spin_handler(ctx, "preset", preset_name)
#     if tab_name:
#         await spin_handler(
#             ctx, "spin_single_sheet", preset_name, tab_filter if tab_filter else ""
#         )


@bot.slash_command(name="spinfo")
@discord.option(
    "preset_name",
    str,
    required=False,
    default=None,
    description="Filter for the preset names",
    autocomplete=discord.utils.basic_autocomplete(get_presets),
)
@discord.option(
    "tab_name",
    str,
    required=False,
    default=None,
    description="Filter for the tab names",
    autocomplete=discord.utils.basic_autocomplete(get_preset_tabs),
)
async def spinfo(
    ctx, preset_name: Optional[str] = None, tab_name: Optional[str] = None
):
    await ctx.defer()
    if preset_name:
        if not tab_name:
            await ctx.respond(
                "You must provide a tab_name if you provide a preset_name."
            )
        await spin_handler(ctx, "fo", f"{preset_name} {tab_name}")
    else:
        await spin_handler(ctx, "fo")


@bot.slash_command(name="spinspect")
async def spinspect(ctx):
    await ctx.defer()
    await spin_handler(ctx, "spect")


@bot.slash_command(name="spintermix")
@discord.option(
    "custom_options",
    str,
    required=False,
    default=None,
    description="Comma-separated custom options",
)
async def spintermix(ctx, custom_options: Optional[str] = None):
    await ctx.defer()
    await spin_handler(ctx, "termix", custom_options)


@bot.listen()
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id == 1362287075142930442:  # Complaining
        await message.delete()
        return

    for key in reaction_dict.keys():
        if str(message.content).lower().find(key) != -1:
            try:
                e_id = random.choice(reaction_dict[key])
                await message.add_reaction(e_id)
            except Exception as ex:
                logging.error(
                    f"Error adding reaction {reaction_dict[key]} to message {message.id}: {str(ex)}"
                )

    if bot.user.mentioned_in(message):
        async with message.channel.typing():
            msg_channel = message.channel
            history = [m async for m in msg_channel.history(limit=80, before=message)]
            history.reverse()
            response = await ChatHandler.respond_in_chat(message, bot)
            await message.channel.send(response)

    if message.author.id == 292447304395522048 and random.randint(0, 100) < 20:
        await message.add_reaction("<a:wheel:1096138684786544883>")

bot.add_cog(IncidentCog(bot))
bot.run(os.getenv("BOT_TOKEN"))
