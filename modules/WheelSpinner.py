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
        if options is None:
            options = ['red', 'black', 'green', ('blue', 2)]

        self.weighted_options = []
        for option in options:
            # if it's a tuple, it's a weighted option
            if isinstance(option, tuple):
                self.weighted_options.append(option)
            else:
                self.weighted_options.append((option, 1))
        self.shuffle()
        self.animation = self.generate_animation()

    def save_svg(self, filename):
        animation = self.generate_animation()
        animation.save_svg(filename)
        return self.weighted_options[0]

    def return_svg(self):
        animation = self.generate_animation()
        return animation.as_svg()

    def return_gif(self):
        # create a directory with a unique name
        # generate a uuid for the directory name
        run_id = f'{os.getcwd()}/{str(uuid.uuid4())[0:8]}'
        os.makedirs(run_id)
        options = webdriver.FirefoxOptions()
        options.add_argument('--headless')
        options.add_argument('--window-size=400x400')

        driver = webdriver.Firefox(options=options)
        self.animation.save_html(f'{run_id}/wheel.html')
        driver.get(f'file://{run_id}/wheel.html')
        frames = []
        for i in range(100):
            driver.get_screenshot_as_file(f"{run_id}/{i}.png")
            f = Image.open(f"{run_id}/{i}.png")
            f.info['duration'] = 0.1
            frames.append(f)

        # Close the browser
        driver.close()
        driver.quit()


        # Return the gif
        fh = io.BytesIO()

        frames[0].save(fh, format='GIF', append_images=frames[1:], save_all=True, duration=5, optimize=False, loop=0)
        fh.seek(0)
        shutil.rmtree(run_id)
        return fh

    def generate_animation(self):
        d = draw.Drawing(200, 200, origin='center', animation_config=draw.types.SyncedAnimationConfig(
            # Animation configuration
            duration=5,  # Seconds
            show_playback_progress=False,
            show_playback_controls=False,
            pause_on_load=False))
        wheel = self.get_wheel()
        start_pos = random.randint(0, 360)
        end_pos = 180/len(self.weighted_options)



        wheel.append_anim(draw.AnimateTransform('rotate',
                                                5,
                                                repeat_count='1',
                                                fill='freeze',
                                                calc_mode='linear',
                                                from_or_values=f'{start_pos}; {start_pos}; -3600; -1800; -900; -450; -180; {end_pos}; {end_pos}',
                                                key_times='0; 0.1; 0.2; 0.3; 0.4; 0.5 ; 0.8 ; 1',

                                                )
                          )
        d.append(wheel)

        d.append(self.get_winner_box())
        # Draw arrow to highlight selection
        arrow = draw.Marker(-0.1, -0.51, 0.9, 0.5, scale=4, orient='auto')
        arrow.append(draw.Lines(-0.1, 0.5, -0.1, -0.5, 0.9, 0, fill='red', close=True))

        d.append(draw.Line(90, 0, 80, 0,
                           stroke='red', stroke_width=3, fill='none',
                           marker_end=arrow))  # Add an arrow to the end of a line

        d.set_pixel_scale(4)  # Set number of pixels per geometry unit
        return d

    def get_wheel(self):
        wheel = draw.Group()
        total_weight = sum([weight for option, weight in self.weighted_options])
        current_position = 0
        for option, weight in self.weighted_options:
            next_position = current_position + (weight/total_weight)*360
            slice = self.get_slice(current_position, next_position, option)
            current_position = next_position
            wheel.append(slice)
        return wheel

    def get_slice(self, start_degree, end_degree, option, color=None):
        if color is None:
            color = f'hsl({random.randint(0, 360)}, {random.randint(30, 100)}%, {random.randint(30, 100)}%)'

        font_size = (85*1.2)/len(option)
        slice_angle = end_degree-start_degree
        max_height = 2*30*(1-math.cos(math.radians(slice_angle/2)))

        limitation = min(max_height*20, font_size)

        font_size = 1.25*limitation
        slice = draw.Group(fill=option)
        p = draw.Path(fill=color, stroke='black', stroke_width=1)
        p.arc(0, 0, 85, -1*start_degree, -1*end_degree, cw=False)
        p.arc(0, 0, 0, 0, 0, cw=True, include_l=True)
        p.Z()
        slice.append(p)
        slice.append(draw.Text(option, font_size, 30, 0, transform=f'rotate({(-1*(end_degree-start_degree)/2)-start_degree})', text_anchor='start', center=True, fill='white'))
        return slice

    def get_winner_box(self):
        box = draw.Group(opacity=0)
        box.append(draw.Rectangle(-50, -20, 100, 40, fill='white', stroke='black', stroke_width=1))
        box.append(draw.Text(self.weighted_options[0][0], 10, 0, 0, text_anchor='middle', center=True, fill='black'))
        box.append(draw.Animate('opacity', 12, from_or_values='0; 0; 0; 0; 0; 0; 0; 0; 100', key_times='0; 0.1; 0.2; 0.3; 0.4; 0.5 ; 0.8 ; 1', repeat_count='1', fill='freeze'))
        return box

    def shuffle(self):
        random.shuffle(self.weighted_options)


