import os
import logging
import sys

import discord
from selenium import webdriver
from PIL import Image
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import time
import datetime
import typing # For typehinting
import functools
import asyncio
import shutil
import yaml
import urllib.parse
import random
import itertools
import tempfile

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def get_message():
    roll = random.random()
    fp = open('messages.txt')
    messages = [message for message in fp.readlines()]
    lines = len(messages)
    return messages[int(int(roll*100) % lines)]

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

    r = "Type '/spin ' followed by a category. You can spin the top category ('/spin free') or pick a sub-category ('/spin free cars'). Your options are: " + str(x)
    return r

class MyClient(discord.Client):
    async def on_ready(self):
        logger.info('Logged on as ' + str(self.user))

    async def on_message(self, message):
        # don't respond to ourselves
        if message.author == self.user:
            return

        if str(message.content).lower() == '/spinfo':
            response_body = get_info()
            response = await message.channel.send(response_body)

        if str(message.content).lower().startswith('/spin '):
            selected_profile = str(message.content)[5:].strip(' ')
            url = generate_url(selected_profile)
            original = await message.channel.send("Got it, one sec...")
            if url is not None:
                file = await spin_dat_wheel(url)
                if file is None:
                    await original.edit('Something went wrong.')
                else:
                    fp = discord.File(file)
                    await original.edit(content=message.author.mention + " " + get_message(), attachments=[fp])
            else:
                await message.channel.send('Sorry, I don\'t recognize that command.')

        if str(message.content).lower() == '/spin':
            url = generate_url('default')
            original = await message.channel.send("Got it, one sec...")
            file = await spin_dat_wheel(url)
            if file is None:
                await original.edit('Something went wrong.')
            else:
                fp = discord.File(file)
                await original.edit(content=message.author.mention + " " + get_message(), attachments=[fp])


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

    option_sets = [y[selection[0]][option] for option in y[selection[0]] if option.upper() == selection[1].upper() or selection[1] == '']

    options = []
    weights = []
    base_url = 'https://pickerwheel.com/emb/?'
    for set, weight in get_combinations(option_sets):
        options.append(str('-'.join(set)).replace(',', ''))
        weights.append(str(weight))
    url_choices = urllib.parse.quote(','.join(options))
    url_weights = urllib.parse.quote(','.join(weights))
    url = base_url+'choices='+url_choices+'&weights='+url_weights+'&confetti=true'
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
def spin_dat_wheel(url):
    directory = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    os.makedirs(directory)

    # Create a new instance of the Chrome driver
    # driver = webdriver.Remote("http://selenium:4444/wd/hub", DesiredCapabilities.CHROME)
    driver = webdriver.Chrome(desired_capabilities=DesiredCapabilities.CHROME)

    # Navigate to the specified URL
    driver.get(url)
    element = None
    attempts = 0
    while element is None and attempts < 5:
        try:
            element = driver.find_element(By.CSS_SELECTOR,"[class^='ReactTurntablestyle__ButtonText']")
            element.click()
        except Exception:
            time.sleep(1)
            attempts += 1
    if element is None:
        return None
    frames = []



    for i in range(90):
        driver.get_screenshot_as_file(directory+f"/screenshot_{i}.png")
        frames.append(Image.open(directory+f"/screenshot_{i}.png"))

    # Close the browser
    driver.close()

    # Save the gif
    frames[0].save(directory+'.gif', format='GIF', append_images=frames[1:], save_all=True)
    shutil.rmtree(directory)
    return directory+'.gif'


if __name__ == '__main__':
    main()
