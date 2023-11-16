import io
import os
import uuid
from selenium import webdriver
import drawsvg as draw
import random
import math
from PIL import Image
import shutil

class WheelSpinner:
    def __init__(self, options: list = None):
        self.colors_set = -1
        if options is None:
            options = ['Never', 'Gonna', 'Give', 'You', 'Up', 'Never', 'Gonna', 'Let', 'You', 'Down']

        self.weighted_options = []
        self.repeated_options = []
        for option in options:
            # if it's a tuple, it's a weighted option
            if isinstance(option, tuple):
                self.weighted_options.append(option)
                for i in range(option[1]):
                    self.repeated_options.append(option)
            else:
                self.weighted_options.append((option, 1))
                self.repeated_options.append((option, 1))
        self.shuffle()
        self.animation = self.generate_animation()

    def save_svg(self, filename):
        animation = self.generate_animation()
        animation.save_svg(filename)
        return self.weighted_options[0]

    def return_svg(self):
        animation = self.generate_animation()
        return animation.as_svg()

    def get_color(self):
        colors = ['#F7B71D', '#263F1A', '#F3E59E', '#AFA939']
        self.colors_set = (self.colors_set + 1) % len(colors)
        return colors[self.colors_set]

    def return_gif(self):
        # create a directory with a unique name
        # generate a uuid for the directory name
        run_id = f'{os.getcwd()}/{str(uuid.uuid4())[0:8]}'
        os.makedirs(run_id)
        try:
            options = webdriver.FirefoxOptions()
            options.add_argument('--headless')
            options.add_argument("--height=1100")
            options.add_argument("--width=1000")
            driver = webdriver.Firefox(options=options)
            self.animation.save_html(f'{run_id}/wheel.html')
            driver.get(f'file://{run_id}/wheel.html')
            frames = []
            for i in range(150):
                driver.get_screenshot_as_file(f"{run_id}/{i}.png")
                f = Image.open(f"{run_id}/{i}.png")
                f.info['duration'] = 0.1
                frames.append(f)

            # Close the browser
            driver.close()
            driver.quit()

            fh = io.BytesIO()
            frames[0].save(fh, format='GIF', append_images=frames[1:], save_all=True, duration=50, optimize=False, loop=1)
            fh.seek(0)
        finally:
            shutil.rmtree(run_id)
        return fh

    def generate_animation(self):
        d = draw.Drawing(200, 200, origin='center', animation_config=draw.types.SyncedAnimationConfig(
            # Animation configuration
            duration=3,  # Seconds
            show_playback_progress=False,
            show_playback_controls=False,
            pause_on_load=False,
            repeat_count=1))
        wheel = self.get_wheel()
        start_pos = random.randint(3000, 5000)
        # end_pos = 180/len(self.weighted_options)
        # end_pos = 0
        end_pos = self.weighted_options[0][1]/sum([weight for option, weight in self.weighted_options])*180
        diff = start_pos - end_pos



        wheel.append_anim(draw.AnimateTransform('rotate',
                                                2,
                                                repeat_count='1',
                                                fill='freeze',
                                                calc_mode='linear',
                                                from_or_values=f'{start_pos}; {start_pos}; {0.6*diff}; {0.3*diff}; {0.1*diff} ; {0.05*diff}; {end_pos}; {end_pos}',
                                                key_times='0; 0.1; 0.2; 0.3; 0.4; 0.5 ; 0.6 ; 1',

                                                )
                          )
        d.append(wheel)

        d.append(self.get_winner_box())
        # Draw arrow to highlight selection
        arrow = draw.Marker(-0.1, -0.51, 0.9, 0.5, scale=4, orient='auto')
        arrow.append(draw.Lines(-0.1, 0.5, -0.1, -0.5, 0.9, 0, fill='red', close=True))

        d.append(draw.Line(95, 0, 90, 0,
                           stroke='red', stroke_width=3, fill='none',
                           marker_end=arrow))  # Add an arrow to the end of a line

        d.set_pixel_scale(5)  # Set number of pixels per geometry unit
        return d

    def get_wheel(self):
        wheel = draw.Group()
        total_weight = sum([weight for option, weight in self.weighted_options])
        current_position = 0
        for option, weight in self.weighted_options:
            next_position = current_position + (weight/total_weight)*360
            slice = self.get_slice(current_position, next_position, option, color=self.get_color())
            current_position = next_position
            wheel.append(slice)
        return wheel

    def get_slice(self, start_degree, end_degree, option, color=None):
        if color is None:
            color = f'hsl({random.randint(0, 360)}, {random.randint(30, 100)}%, {random.randint(30, 100)}%)'

        font_size = (85)/len(option)
        slice_angle = end_degree-start_degree
        max_height = 2*30*(1-math.cos(math.radians(slice_angle/2)))

        limitation = min(max_height*10, font_size)

        font_size = limitation
        slice = draw.Group(fill=option)
        p = draw.Path(fill=color, stroke='white', stroke_width=0)
        p.arc(0, 0, 85, -1*start_degree, -1*end_degree, cw=False)
        p.arc(0, 0, 0, 0, 0, cw=True, include_l=True)
        p.Z()
        slice.append(p)
        slice.append(draw.Text(option, font_size, 20, 0, transform=f'rotate({(-1*(end_degree-start_degree)/2)-start_degree})', text_anchor='start', center=True, fill='white'))
        return slice

    def get_winner_box(self):
        def add_line_breaks(text, soft_wrap=30):
            # a new string of words from the text until we reach the soft_wrap limit, only adding whole words
            new_text = ''
            current_line = ''
            longest_line = 0
            for word in text.split(' '):
                if len(current_line) + len(word) > soft_wrap:
                    if len(current_line) > longest_line:
                        longest_line = len(current_line)
                    new_text += current_line + '\n'
                    current_line = ''
                current_line += word + ' '
            if len(current_line) > longest_line:
                longest_line = len(current_line)
            new_text += current_line
            return new_text, longest_line

        def get_font_size(text, longest_line, max_width, max_height):
            font_size = 1
            while True:
                width = longest_line*0.4*font_size
                height = (1+str(text).count('\n'))*font_size*1.2
                if width <= max_width and height <= max_height:
                    font_size += 1
                else:
                    font_size -= 1
                    return font_size

        box = draw.Group(opacity=0)
        box.append(draw.Rectangle(-50, -20, 100, 40, fill='white', stroke='black', stroke_width=1))

        text = self.weighted_options[0][0]
        text, max_length = add_line_breaks(text)
        font_size = get_font_size(text, max_length, 100, 40)


        box.append(draw.Text(text, font_size, 0, 0, text_anchor='middle', center=True, fill='black'))
        box.append(draw.Animate('opacity', 3, from_or_values='0; 0; 0; 0; 0; 0; 0; 0; 100', key_times='0; 0.1; 0.2; 0.3; 0.4; 0.5 ; 0.8 ; 1', repeat_count='1', fill='freeze'))
        return box

    def shuffle(self):
        # Lava Lamp for Extra Randomness
        #               .88888888.
        #              .8888888888.
        #             .88        88.
        #            .88          88.
        #           .88            88.
        #          .88              88.
        #         .88     ::.        88.
        #        .88      ':'         88.
        #       .88           .::.     88.
        #      .88           .::::      88.
        #     .88            ':::'       88.
        #    .88 .:::.  .:.         .:    88.
        #   .88  :::::  ':'        ':'     88.
        #   88   ':::'       .::.           88
        #   88        .::.  .:::::     .:.  88
        #   88        '::'  :::::'     ':'  88
        #   '88            .:::::  .::.    88'
        #    '88         .:::::'  :::::   88'
        #     '88        ::::::.  ':::'  88'
        #      '88       ::::::::.      88'
        #       '88   .:. ::::::::.    88'
        #        '88  '::.::::::::::..88'
        #         '88 .::::::::::::::88'
        #          '88::::::::::::::88'
        #           '88::::::::::::88'
        #            88::::::::::::88
        #          .888888888888888888.
        #         .888%%%%%%%%%%%%%%888.
        #        .888%%%%%%%%%%%%%%%%888.
        #       .888%%%%%%%%%%%%%%%%%%888.
        #      .888%%%%%%%%%%%%%%%%%%%%888.
        #     888%%JGS%%%%%%%%%%%%%%%%%%%888

        random.shuffle(self.repeated_options)
        for i in range(len(self.weighted_options)):
            if self.weighted_options[i] == self.repeated_options[0]:
                self.weighted_options.insert(0, self.weighted_options.pop(i))
                return
