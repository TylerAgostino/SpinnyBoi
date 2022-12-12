import os
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


class MyClient(discord.Client):
    async def on_ready(self):
        print('Logged on as', self.user)

    async def on_message(self, message):
        # don't respond to ourselves
        if message.author == self.user:
            return

        if str(message.content).lower().startswith('/spin '):
            selected_profile = str(message.content)[5:]
            url = generate_url(selected_profile)
            file = await spin_dat_wheel(url)
            if file is None:
                await message.channel.send('Something went wrong.')
            else:
                fp = discord.File(file)
                await message.channel.send(get_message(), file=fp)

        if str(message.content).lower() == '/spin':
            url = generate_url('default')
            file = await spin_dat_wheel(url)
            if file is None:
                await message.channel.send('Something went wrong.')
            else:
                fp = discord.File(file)
                await message.channel.send(get_message(), file=fp)


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    client = MyClient(intents=intents)
    bot_token = os.getenv('BOT_TOKEN')

    client.run(bot_token)

def generate_url(profile):
    with open("profiles.yaml") as profile_file:
        y = yaml.safe_load(profile_file)
    try:
        selected_profile = y[profile]
    except KeyError:
        return None
    options = []
    weights = []
    base_url = 'https://pickerwheel.com/emb/?'
    for track in selected_profile['tracks']:
        track = str(track).replace(',', '')
        for car in selected_profile['cars']:
            car = str(car).replace(',', '')
            description = str(car + ' at ' + track)
            options.append(description)
            weight = selected_profile['tracks'][track] * selected_profile['cars'][car]
            weights.append(str(weight))
    url_choices = urllib.parse.quote(','.join(options))
    url_weights = urllib.parse.quote(','.join(weights))
    url = base_url+'choices='+url_choices+'&weights='+url_weights+'&confetti=true'
    return url


@to_thread
def spin_dat_wheel(url):
    directory = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    os.makedirs(directory)

    # Create a new instance of the Chrome driver
    driver = webdriver.Remote("http://selenium:4444/wd/hub", DesiredCapabilities.CHROME)


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

    for i in range(100):
        driver.get_screenshot_as_file(directory+f"/screenshot_{i}.png")
        frames.append(Image.open(directory+f"/screenshot_{i}.png"))

    # Close the browser
    driver.close()

    # Save the gif
    frames[0].save('recording.gif', format='GIF', append_images=frames[1:], save_all=True)

    # Delete the originals
    shutil.rmtree(directory)
    return 'recording.gif'


if __name__ == '__main__':
    main()
