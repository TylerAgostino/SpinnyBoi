import os
import discord
import logging
import random
import datetime
import asyncio
import json
import pytz
from typing import Dict, List, Optional, Tuple
from modules import CommandHandler, ChatHandler
from modules.scheduler import (
    init_db,
    schedule_event,
    get_pending_events,
    mark_event_completed,
)

status_message = "/spin"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)


# Global dictionary to store example summaries
SUMMARY_EXAMPLES: Dict[str, Tuple[int, int]] = {}


# Initialize the database
def ensure_db_initialized():
    try:
        # Make sure the data directory exists
        os.makedirs(
            os.path.dirname(
                os.getenv("DATABASE_URL", "sqlite:///data/spinny.db").replace(
                    "sqlite://", ""
                )
            ),
            exist_ok=True,
        )
        init_db()
        logging.info("Scheduler database initialized successfully")
    except Exception as ex:
        logging.error(f"Failed to initialize scheduler database: {str(ex)}")


# Load summary examples
def load_summary_examples():
    examples_file_path = "summary_examples.txt"
    try:
        with open(examples_file_path, "r") as file:
            for line in file:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) == 2:
                    thread_id, message_id = parts
                    SUMMARY_EXAMPLES[thread_id] = (int(thread_id), int(message_id))

        logging.info(f"Loaded {len(SUMMARY_EXAMPLES)} summary examples")
    except FileNotFoundError:
        logging.warning(f"Summary examples file not found: {examples_file_path}")
    except Exception as ex:
        logging.error(f"Error loading summary examples: {str(ex)}")


# Ensure the DB is initialized at startup
ensure_db_initialized()

# Load summary examples
load_summary_examples()

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


class MyClient(discord.Client):
    async def fetch_example_summary(
        self
    ) -> Optional[Tuple[List[dict], str]]:
        """
        Fetch an example thread and its summary for use in one-shot prompting.

        Returns:
            A tuple containing (thread_messages, summary) if found, None otherwise
        """
        try:
            if not SUMMARY_EXAMPLES:
                logging.info("No summary examples available")
                return None

            # For now, just use the first example (future: add logic to select based on subject)
            thread_id, message_id = next(iter(SUMMARY_EXAMPLES.values()))

            # Fetch the thread
            thread = await self.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                logging.error(f"Channel {thread_id} is not a thread")
                return None

            # Fetch thread messages
            history = [m async for m in thread.history(limit=500)]
            history.reverse()  # Chronological order

            # Format the messages
            formatted_messages = []
            for msg in history:
                if not (
                    msg.author.bot
                    or msg.author.name == "SpinnyBoi"
                    or msg.id == 1412494921461399673
                ):
                    author_name = (
                        msg.author.nick if msg.author.nick else msg.author.name
                    )
                    formatted_messages.append(
                        {
                            "user": author_name,
                            "content": msg.content,
                            "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )

            # Fetch the summary message
            try:
                # First try to find it in the thread
                summary_message = await thread.fetch_message(message_id)
            except discord.NotFound:
                # If not in thread, try to fetch from any channel the bot can access
                for guild in self.guilds:
                    for channel in guild.text_channels:
                        try:
                            summary_message = await channel.fetch_message(message_id)
                            break
                        except Exception:
                            continue
                    else:
                        continue
                    break
                else:
                    logging.error(f"Summary message {message_id} not found")
                    return None

            summary_text = summary_message.content

            logging.info(
                f"Successfully fetched example summary (thread: {thread_id}, message: {message_id})"
            )
            return formatted_messages, summary_text

        except Exception as ex:
            logging.error(f"Error fetching example summary: {str(ex)}")
            return None

    async def on_ready(self):
        print("Logged on as " + str(self.user))
        activity = discord.Activity(
            name=status_message, type=discord.ActivityType.playing
        )
        await self.change_presence(status=discord.Status.online, activity=activity) # noqa

        # Ensure database is initialized after bot is ready
        ensure_db_initialized()

        # Start the scheduler loop
        self.bg_task = self.loop.create_task(self.check_scheduled_events())

    async def check_scheduled_events(self):
        """Check for pending scheduled events every minute and execute them."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                # Get pending events
                events = get_pending_events()

                for event in events:
                    try:
                        # Process the event
                        logging.info(
                            f"Processing scheduled event: {event.function_name} for message {event.message_id}"
                        )

                        if event.function_name == "close_poll":
                            await self.close_poll(
                                event.channel_id, event.message_id, event.data
                            )
                        # Add more function handlers here as needed

                        # Mark the event as completed
                        mark_event_completed(event.id)
                    except Exception as ex:
                        logging.error(f"Error processing event {event.id}: {str(ex)}")

            except Exception as ex:
                logging.error(f"Error in scheduled events loop: {str(ex)}")

            # Check every 60 seconds
            await asyncio.sleep(60)

    async def close_poll(self, channel_id, message_id, data=None):
        """Close a poll by counting reactions and posting results."""
        try:
            channel = self.get_channel(channel_id)
            if not channel:
                logging.error(f"Channel {channel_id} not found")
                return

            message = await channel.fetch_message(message_id)
            if not message:
                logging.error(f"Message {message_id} not found")
                return

            # Extract subject from data or message content
            subject = None
            if data:
                try:
                    data_json = json.loads(data)
                    subject = data_json.get("subject")
                except json.JSONDecodeError:
                    logging.error(f"Failed to parse event data: {data}")

            # If subject not in data, extract from message content
            if not subject:
                content = message.content
                subject_line = [
                    line for line in content.split("\n") if "# Incident Poll:" in line
                ]
                subject = (
                    "Poll"
                    if not subject_line
                    else subject_line[0].split(":", 1)[1].strip()
                )

            # Count reactions
            reaction_counts = {}
            options = {
                "🇦": "No Action",
                "🇧": "1 Point",
                "🇨": "2 Points",
                "🇩": "3 Points",
                "🇪": "Other",
            }

            for react in message.reactions:
                if str(react.emoji) in options:
                    # Don't count the bot's own reaction
                    count = react.count - 1 if react.me else react.count
                    reaction_counts[str(react.emoji)] = count

            # Create results message
            results_message = f"# Poll Results: {subject}\n\n"

            # Display vote counts in a clean format
            for emoji, label in options.items():
                count = reaction_counts.get(emoji, 0)
                results_message += f"{emoji} {label}: **{count}** votes\n"

            # Get the winning option
            if reaction_counts:
                max_votes = max(reaction_counts.values())
                winners = [
                    emoji
                    for emoji, count in reaction_counts.items()
                    if count == max_votes
                ]

                # Add a separator line
                results_message += "\n---\n"

                if len(winners) == 1:
                    winner_emoji = winners[0]
                    results_message += (
                        f"**Winner: {options[winner_emoji]} with {max_votes} votes**"
                    )
                else:
                    winner_labels = [options[emoji] for emoji in winners]
                    results_message += f"**Tie between: {', '.join(winner_labels)} with {max_votes} votes each**"
            else:
                # Add a separator line
                results_message += "\n---\n"
                results_message += "**No votes were cast**"

            # Check if message is in a thread
            thread_summary = ""

            # Fetch example summary for one-shot prompting
            example_summary = await self.fetch_example_summary()

            # Try to fetch parent message if in a thread
            parent_message = None
            try:
                if isinstance(channel, discord.Thread):
                    # Try different approaches to get the parent message
                    if hasattr(channel, "starter_message"):
                        parent_message = channel.starter_message
                    elif hasattr(channel, "parent_id") and channel.parent_id:
                        parent_channel = channel.parent
                        async for msg in parent_channel.history(limit=100):
                            if msg.id == channel.id:
                                parent_message = msg
                                break
            except Exception as ex:
                logging.warning(f"Failed to fetch thread parent: {str(ex)}")

            # Pass poll results to the summarization function
            options = {
                "🇦": "No Action",
                "🇧": "1 Point",
                "🇨": "2 Points",
                "🇩": "3 Points",
                "🇪": "Other",
            }

            if isinstance(channel, discord.Thread):
                # This is a thread, so summarize it
                logging.info(f"Summarizing thread {channel.name} for poll: {subject}")
                thread_summary = await ChatHandler.summarize_thread(
                    channel,
                    subject,
                    example_summary,
                    reaction_counts,
                    options,
                    parent_message,
                )
                if thread_summary:
                    results_message += f"\n\n# Steward's Decision\n\n{thread_summary}"
            elif hasattr(message, "thread") and message.thread is not None:
                # The message has a thread attached to it
                logging.info(
                    f"Summarizing thread {message.thread.name} for poll: {subject}"
                )
                thread_summary = await ChatHandler.summarize_thread(
                    message.thread,
                    subject,
                    example_summary,
                    reaction_counts,
                    options,
                    parent_message,
                )
                if thread_summary:
                    results_message += f"\n\n# Steward's Decision\n\n{thread_summary}"

            # Results are sent after checking message length in the code below

            # If there was a thread summary, log it
            if thread_summary:
                logging.info(f"Added thread summary for poll: {subject}")

            # Make sure we don't exceed Discord's message size limit
            if len(results_message) > 2000:
                # Split the message if it's too long
                await channel.send(results_message[:2000])
                remaining = results_message[2000:]
                for i in range(0, len(remaining), 2000):
                    await channel.send(remaining[i : i + 2000])
                logging.info(
                    f"Poll results were split into multiple messages due to size"
                )
            else:
                # Send results as a single message
                await channel.send(results_message)

            logging.info(f"Poll closed: {subject}")

        except Exception as ex:
            logging.error(f"Error closing poll: {str(ex)}")

    async def on_message(self, message):
        # don't respond to ourselves
        if message.author == self.user:
            return

        if message.channel.id == 1362287075142930442:  # Complaining
            await message.delete()
            return

        if str(message.content).lower().startswith("/spincident"):
            try:
                subject = (
                    str(message.content).lower().removeprefix("/spincident").strip(" ")
                )
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                now_et = now_utc.astimezone(tz=pytz.timezone("America/New_York"))
                next_sunday_9p_et = now_et + datetime.timedelta(
                    days=(6 - now_et.weekday())
                )
                next_sunday_9p_et = next_sunday_9p_et.replace(
                    hour=21, minute=0, second=0, microsecond=0
                )
                if now_et >= next_sunday_9p_et:
                    next_sunday_9p_et += datetime.timedelta(days=7)
                next_sunday_date_9pm = next_sunday_9p_et.astimezone(pytz.utc)

                next_sunday_date_str = f"<t:{int(next_sunday_date_9pm.timestamp())}:R>"
                msg = f"""
                >>> # Incident Poll: {subject}
:regional_indicator_a: No Action
:regional_indicator_b: 1 Point
:regional_indicator_c: 2 Points
:regional_indicator_d: 3 Points
:regional_indicator_e: Other (Please explain below)

-# Voting ends {next_sunday_date_str}
                """
                bot_response = await message.channel.send(msg)
                await bot_response.add_reaction("🇦")
                await bot_response.add_reaction("🇧")
                await bot_response.add_reaction("🇨")
                await bot_response.add_reaction("🇩")
                await bot_response.add_reaction("🇪")

                # Schedule the poll to close at the specified time
                try:
                    timestamp = next_sunday_date_9pm.timestamp()
                    data = json.dumps({"subject": subject})

                    # Schedule the event
                    event_id = schedule_event(
                        timestamp=timestamp,
                        function_name="close_poll",
                        message_id=bot_response.id,
                        channel_id=bot_response.channel.id,
                        data=data,
                    )

                    logging.info(
                        f"Scheduled poll closing for {subject} at {next_sunday_date_9pm.isoformat()}, event ID: {event_id}"
                    )
                except Exception as ex:
                    logging.error(f"Failed to schedule poll closing: {str(ex)}")
            except Exception as ex:
                print(str(ex))
                await message.channel.send("Something went wrong.")
            return

        if str(message.content).lower().startswith("/spin"):
            try:
                msg = ChatHandler.working_on_it()
                bot_response = await message.channel.send(msg)
            except Exception as ex:
                logging.error(f"Error sending working on it message: {str(ex)}")
                bot_response = await message.channel.send(
                    "Working on it. Go easy on me, I'm still in beta."
                )
            try:
                command_with_args = (
                    str(message.content).lower().removeprefix("/spin").strip(" ")
                )
                command = command_with_args.split(" ")[0]
                args = " ".join(command_with_args.split(" ")[1:])
                if args == "":
                    response_text, response_attachment = (
                        await CommandHandler.CommandHandler().run_command(command)
                    )
                else:
                    response_text, response_attachment = (
                        await CommandHandler.CommandHandler().run_command(command, args)
                    )
                # await message.channel.send(response_text, files=response_attachment)
                await bot_response.edit(
                    content=f"{message.author.mention} {response_text}",
                    attachments=response_attachment,
                )
            except Exception as ex:
                print(str(ex))
                await bot_response.edit(content="Something went wrong.")
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

        if self.user.mentioned_in(message):
            async with message.channel.typing():
                msg_channel = message.channel
                history = [
                    m async for m in msg_channel.history(limit=80, before=message)
                ]
                history.reverse()
                response = await ChatHandler.respond_in_chat(message, self)
                await message.channel.send(response)

        if message.author.id == 292447304395522048 and random.randint(0, 100) < 20:
            await message.add_reaction("<a:wheel:1096138684786544883>")


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = MyClient(intents=intents)
    bot_token = os.getenv("BOT_TOKEN")
    client.run(bot_token)


if __name__ == "__main__":
    main()
