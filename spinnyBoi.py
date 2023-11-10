import os
import logging
import sys
import discord
from selenium import webdriver
from PIL import Image
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException
import time
import datetime
import typing  # For type hinting
import functools
import asyncio
import shutil
import yaml
import urllib.parse
import random
import itertools
import csv
import io
import requests
import pandas as pd
from modules import CommandHandler

status_message = "/spin"

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
grey_list = [515926385731305502, 335864725600600064]


def message_handler(file='messages.txt'):
    roll = random.random()
    fp = open(file)
    messages = [message for message in fp.readlines()]
    lines = len(messages)
    return messages[int(int(roll * 100) % lines)]


def get_message():
    return message_handler(file='messages.txt')


def get_greylist():
    return message_handler(file='greymessages.txt')


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


def get_info():
    with open("profiles.yaml") as profile_file:
        y = yaml.safe_load(profile_file)
    x = "\n\n"
    for top_level in y:
        x = x + top_level + '\n'
        for second_level in y[top_level]:
            x = x + "   " + second_level + '\n'

    r = "Type `/spin ` followed by a category. You can spin the top category (`/spin free`) or pick a sub-category (`/spin free cars`). Your options are: " + str(
        x)
    r = r + "\n\n" + "You can also give custom options, by typing `/spin custom` followed by a comma-separated list. Like this: \n`/spin custom A,B,C`."
    return r


class MyClient(discord.Client):
    async def on_ready(self):
        logger.info('Logged on as ' + str(self.user))
        activity = discord.Activity(name=status_message, type=discord.ActivityType.playing)
        await self.change_presence(status=discord.Status.online, activity=activity)

    async def on_message(self, message):
        # don't respond to ourselves
        if message.author == self.user:
            return

        if str(message.content).lower().startswith('/spin'):
            command = str(message.content).lower().removeprefix('/spin').strip(' ')
            response = CommandHandler.CommandHandler(command)
            await message.channel.send(response.response_text, file=response.response_attachment)
            return


        # grey list
        if str(message.content).lower().startswith('/spin') and message.author.id in grey_list and random.randrange(1,
                                                                                                                    100) <= 20:
            if random.randrange(1, 100) <= 10:
                original = await message.channel.send("Got it, one sec...")
                url = 'https://media4.giphy.com/media/LrmU6jXIjwziE/giphy.gif?cid=ecf05e47t84wtpzmxqziq5zugsn7ms53jg279h4lp6y9u1w1&ep=v1_gifs_related&rid=giphy.gif&ct=g'
                response = requests.get(url)
                file = io.BytesIO(response.content)
                file.name = 'upload.gif'
                # time.sleep(1)
                fp = discord.File(file)
                await original.edit(content=message.author.mention + " " + get_message(), attachments=[fp])
                return
            else:
                response_body = get_greylist()
                response = await message.channel.send(message.author.mention + " " + response_body)
                return

        if str(message.content).lower() == '/spinfo':
            response_body = get_info()
            response = await message.channel.send(response_body)

        if str(message.content).lower().startswith('/spin custom '):
            try:
                provided_values = str(message.content)[12:].strip(' ')
                fh = io.StringIO(provided_values)
                csv_reader = csv.reader(fh)
                for row in csv_reader:
                    break
                option_sets = [[{a: 1} for a in row]]
                url = generate_url_from_option_sets(option_sets)
            except:
                await message.channel.send('Something went wrong reading your input.')
            else:
                original = await message.channel.send("Got it, one sec...")
                file = await spin_dat_wheel(url)
                if file is None:
                    await original.edit(content='Something went wrong.')
                else:
                    fp = discord.File(file)
                    await original.edit(content=message.author.mention + " " + get_message(), attachments=[fp])
            finally:
                return

        if str(message.content).lower().startswith('/spin preset '):
            original = await message.channel.send("Got it, one sec...")
            preset_name = str(message.content.lower()).removeprefix('/spin preset ').strip(' ')
            urls = generate_preset_urls(preset_name)
            files = []
            for url in urls:
                wheel = await spin_dat_wheel(url)
                if wheel is not None:
                    dicordfiles = discord.File(wheel)
                    files.append(dicordfiles)
            await original.edit(content=message.author.mention + " " + get_message(), attachments=files)
            return

        if str(message.content).lower().startswith('/spin '):
            original = await message.channel.send("Got it, one sec...")
            full_filter_string = str(message.content.lower()).removeprefix('/spin ').strip(' ')
            tab = full_filter_string.split(' ')[0]
            filter_string = ' '.join(full_filter_string.split(' ')[1:])
            url = generate_spreadsheet_url(tab, filter_string)
            if url is not None and not url.__contains__('?choices=&weights'):
                file = await spin_dat_wheel(url)
                if file is None:
                    await original.edit(content='Something went wrong.')
                else:
                    fp = discord.File(file)
                    await original.edit(content=message.author.mention + " " + get_message(), attachments=[fp])
            else:
                await original.edit(content='Something went wrong.')
            return

        if str(message.content).lower() == '/spin':
            original = await message.channel.send("Got it, one sec...")
            urls = generate_preset_urls('DEFAULT')
            files = []
            for url in urls:
                wheel = await spin_dat_wheel(url)
                if wheel is not None:
                    dicordfiles = discord.File(wheel)
                    files.append(dicordfiles)
            await original.edit(content=message.author.mention + " " + get_message(), attachments=files)


def generate_preset_urls(preset_name):
    spreadsheet = os.getenv('GSHEET_ID')
    url = f'https://docs.google.com/spreadsheets/d/{spreadsheet}/gviz/tq?tqx=out:csv&sheet=presets'
    filters = pd.read_csv(url).query(f"Fullname.str.lower()=='{preset_name.lower()}'").to_dict('records')[0]
    urls = []
    for tabname in filters.keys():
        if isinstance(filters[tabname], str) and tabname != 'Fullname':
            urls.append(generate_spreadsheet_url(tabname, str(filters[tabname]).lower()))
    return urls


def get_value_from_nested_dict(d, s):
    keys = s.split('.')
    for key in keys[:-1]:
        if key in d:
            d = d[key]
        else:
            return None
    return {keys[-1]: d[keys[-1]]}


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    client = MyClient(intents=intents)
    bot_token = os.getenv('BOT_TOKEN')

    client.run(bot_token)


def generate_url(profile):
    with open("profiles.yaml") as profile_file:
        y = yaml.safe_load(profile_file)
    initial_keywords = [x for x in y]
    selection = None
    for keyword in initial_keywords:
        if profile.upper().startswith(keyword.upper()):
            selection = (keyword, profile[len(keyword):].strip(' '))
            break
    if selection is None:
        return None

    option_sets = [y[selection[0]][option] for option in y[selection[0]]
                   if option.upper() == selection[1].upper() or selection[1] == '']
    url = generate_url_from_option_sets(option_sets)
    return url


def generate_spreadsheet_url(tab, filter_string):
    try:
        spreadsheet = os.getenv('GSHEET_ID')
        url = f'https://docs.google.com/spreadsheets/d/{spreadsheet}/gviz/tq?tqx=out:csv&sheet={tab}'
        df = pd.read_csv(url)
        df.columns = df.columns.str.lower()
        filter_string = filter_string.strip(' ')
        filters = filter_string.split(',')
        filter_queries = []
        weighting = None
        for filter in filters:
            try:
                if filter.find('|') > 0:
                    or_query = []
                    for option in filter.split('|'):
                        if option.find('<>') > 0:
                            a = option.split('<>')
                            or_query.append(f"not `{a[0].strip(' ')}`.str.lower().str.contains('{a[1].strip(' ')}')")
                        elif option.find('>=') > 0:
                            a = option.split('>=')
                            or_query.append(f"`{a[0].strip(' ')}`>={a[1].strip(' ')}")
                        elif option.find('<=') > 0:
                            a = option.split('<=')
                            or_query.append(f"`{a[0].strip(' ')}`<={a[1].strip(' ')}")
                        elif option.find('<') > 0:
                            a = option.split('<')
                            or_query.append(f"`{a[0].strip(' ')}`<{a[1].strip(' ')}")
                        elif option.find('>') > 0:
                            a = option.split('>')
                            or_query.append(f"`{a[0].strip(' ')}`>{a[1].strip(' ')}")
                        elif option.find(':') > 0:
                            a = option.split(':')
                            or_query.append(f"`{a[0].strip(' ')}`.str.lower().str.contains('{a[1].strip(' ')}')")
                        elif option.find('=') > 0:
                            a = option.split('=')
                            or_query.append(f"`{a[0].strip(' ')}`.str.lower()=='{a[1].strip(' ')}'")
                    filter_queries.append(" | ".join(or_query))
                else:
                    if filter.find('!weight=') > 0:
                        weighting = filter.split('=')[1]
                    elif filter.find('<>') > 0:
                        a = filter.split('<>')
                        filter_queries.append(f"not `{a[0].strip(' ')}`.str.lower().str.contains('{a[1].strip(' ')}')")
                    elif filter.find('>=') > 0:
                        a = filter.split('>=')
                        filter_queries.append(f"`{a[0].strip(' ')}`>={a[1].strip(' ')}")
                    elif filter.find('<=') > 0:
                        a = filter.split('<=')
                        filter_queries.append(f"`{a[0].strip(' ')}`<={a[1].strip(' ')}")
                    elif filter.find('<') > 0:
                        a = filter.split('<')
                        filter_queries.append(f"`{a[0].strip(' ')}`<{a[1].strip(' ')}")
                    elif filter.find('>') > 0:
                        a = filter.split('>')
                        filter_queries.append(f"`{a[0].strip(' ')}`>{a[1].strip(' ')}")
                    elif filter.find(':') > 0:
                        a = filter.split(':')
                        filter_queries.append(f"`{a[0].strip(' ')}`.str.lower().str.contains('{a[1].strip(' ')}')")
                    elif filter.find('=') > 0:
                        a = filter.split('=')
                        filter_queries.append(f"`{a[0].strip(' ')}`.str.lower()=='{a[1].strip(' ')}'")
            except Exception as e:
                logger.error(str(e))

        filtereddf = df
        for query_string in filter_queries:
            filtereddf = filtereddf.query(query_string)
        selections = filtereddf.to_dict('records')
        if weighting is None:
            option_set = [[{selection['fullname']: 1} for selection in selections]]
        else:
            option_set = [[{selection['fullname']: int(selection[weighting])} for selection in selections]]
        url = generate_url_from_option_sets(option_set)
        return url
    except Exception as e:
        logging.exception(str(e))
        return None


def generate_url_from_option_sets(option_sets):
    options = []
    weights = []
    base_url = 'https://pickerwheel.com/emb/?'
    for set, weight in get_combinations(option_sets):
        options.append(str('-'.join(set)).replace(',', ''))
        weights.append(str(weight))
    url_choices = urllib.parse.quote(','.join(options))
    url_weights = urllib.parse.quote(','.join(weights))
    url = base_url + 'choices=' + url_choices + '&weights=' + url_weights + '&confetti=true'
    return url


def get_combinations(option_arrays):
    combinations = itertools.product(*option_arrays)
    for combination in combinations:
        keys = []
        product = 1
        for d in combination:
            if isinstance(d, str):
                keys.extend([1])
            keys.extend(d.keys())
            for value in d.values():
                product *= value
        yield keys, product


@to_thread
def spin_dat_wheel(url, f=True):
    directory = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    os.makedirs(directory)

    options = webdriver.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    # Create a new instance of the Chrome driver
    driver = webdriver.Remote("http://192.168.1.125:4444/wd/hub", options=options)
    # driver = webdriver.Chrome(desired_capabilities=DesiredCapabilities.CHROME)
    try:
        # Navigate to the specified URL
        driver.get(url)
        element = None
        attempts = 0
        while element is None and attempts < 5 and f:
            try:
                element = driver.find_element(By.CSS_SELECTOR, "[class^='ReactTurntablestyle__ButtonText']")
                element.click()
            except Exception:
                time.sleep(1)
                attempts += 1
        if element is None and f:
            return None
        frames = []

        for i in range(60):
            driver.get_screenshot_as_file(directory + f"/screenshot_{i}.png")
            frames.append(Image.open(directory + f"/screenshot_{i}.png"))

        # Close the browser
        driver.close()
        driver.quit()

        # Save the gif
        frames[0].save(directory + '.gif', format='GIF', append_images=frames[1:], save_all=True)
        shutil.rmtree(directory)
        return directory + '.gif'
    except WebDriverException as e:
        logger.error(e.msg)
        driver.close()
        driver.quit()
        return None


if __name__ == '__main__':
    main()
