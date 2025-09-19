import io
import os
import uuid
import drawsvg as draw
import random
import math
from PIL import Image
import shutil
import logging


class WheelSpinner:
    def __init__(self, options):
        self.colors = ['#F7B71D', '#263F1A', '#F3E59E', '#AFA939']
        self.colors_set = random.randint(0, len(self.colors)-1)

        self.weighted_options = options
        self.repeated_options = []
        for option in options:
            for i in range(option.weight):
                self.repeated_options.append(option)
        self.shuffle()
        next_spin = self.repeated_options[0].on_select
        if next_spin is None or not isinstance(next_spin, str):
            self.next_spin = None
        else:
            try:
                tab = next_spin.split(' ')[0]
                filter_string = ' '.join(next_spin.split(' ')[1:])
                self.next_spin = tab, filter_string
            except IndexError:
                self.next_spin = None
                logging.error(f'Invalid next spin: {next_spin}. Ignoring.')
        self.animation = self.generate_animation()
        self.response = self.weighted_options[0].include_text

    def get_color(self):
        self.colors_set = (self.colors_set + 1) % len(self.colors)
        return self.colors[self.colors_set]

    def return_gif(self, driver=None):
        logging.info('Generating gif')
        # create a directory with a unique name
        # generate a uuid for the directory name
        run_id = f'{os.getcwd()}/{str(uuid.uuid4())[0:8]}'
        os.makedirs(run_id)
        try:
            if driver is None:
                from selenium import webdriver
                logging.info('No browser provided, starting a new one')
                options = webdriver.FirefoxOptions()
                options.add_argument('--headless')
                options.add_argument("--height=1100")
                options.add_argument("--width=1000")
                driver = webdriver.Firefox(options=options)

            logging.info('Saving html')
            self.animation.save_html(f'{run_id}/wheel.html')

            logging.info('Loading html')
            driver.get(f'file://{run_id}/wheel.html')

            logging.info('Taking screenshots')
            frames = []
            for i in range(90):
                driver.get_screenshot_as_file(f"{run_id}/{i}.png")
                f = Image.open(f"{run_id}/{i}.png")
                f.info['duration'] = 0.1
                frames.append(f)

            # logging.info('Cleaning up')
            # # Close the browser
            # driver.close()
            # driver.quit()

            logging.info('Saving gif')
            fh = io.BytesIO()
            frames[0].save(fh, format='GIF', append_images=frames[1:], save_all=True, duration=50, optimize=False, loop=1)
            fh.seek(0)
        finally:
            shutil.rmtree(run_id)
        logging.info('Done generating gif')
        return fh

    def generate_animation(self):
        # 1 in 30 chance of comic sans
        if random.randint(0, 30) == 0:
            font = 'Comic Sans MS'
        else:
            font = 'Shantell Sans'
        d = draw.Drawing(200, 200, origin='center', animation_config=draw.types.SyncedAnimationConfig(
            # Animation configuration
            duration=2,  # Seconds
            show_playback_progress=False,
            show_playback_controls=False,
            pause_on_load=False,
            repeat_count=1),
                         font_family=font)

        wheel = self.get_wheel()
        start_pos = random.randint(360, 400)
        end_pos = self.weighted_options[0].weight/sum([option.weight for option in self.weighted_options])*180
        diff = start_pos - end_pos

        # define keyframes for a natural deceleration
        positions = [start_pos, start_pos, end_pos+180, end_pos+90, end_pos+65, end_pos+40, end_pos+30, end_pos+20, end_pos+15, end_pos+10, end_pos+5, end_pos+2.5, end_pos]
        key_times = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1]



        wheel.append_anim(draw.AnimateTransform('rotate',
                                                1,
                                                repeat_count='1',
                                                fill='freeze',
                                                calc_mode='linear',
                                                from_or_values=';'.join([str(x) for x in positions]),
                                                key_times=';'.join([str(x) for x in key_times]),

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
        total_weight = sum([option.weight for option in self.weighted_options])
        current_position = 0
        for option in self.weighted_options:
            next_position = current_position + (option.weight/total_weight)*360
            slice = self.get_slice(current_position, next_position, option.option, color=self.get_color())
            current_position = next_position
            wheel.append(slice)
        return wheel

    @staticmethod
    def get_slice(start_degree, end_degree, option, color=None):
        if color is None:
            color = f'hsl({random.randint(0, 360)}, {random.randint(30, 100)}%, {random.randint(30, 100)}%)'

        font_size = (150)/len(option)
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
        def add_line_breaks(text, soft_wrap=20):
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
                width = longest_line*0.5*font_size
                height = (1+str(text).count('\n'))*font_size*1.2
                if width <= max_width and height <= max_height:
                    font_size += 1
                else:
                    font_size -= 1
                    return font_size

        box = draw.Group(opacity=0)
        box.append(draw.Rectangle(-60, -25, 120, 50, fill='white', stroke='black', stroke_width=1, opacity=0.8))

        text = self.weighted_options[0].option
        text, max_length = add_line_breaks(text)
        font_size = get_font_size(text, max_length, 100, 40)
        box.append(draw.Text(text, font_size, 0, 0, text_anchor='middle', center=True, fill='black'))
        box.append(draw.Animate('opacity', 2, from_or_values='0; 0; 0; 0; 0; 0; 0; 0; 100', key_times='0; 0.1; 0.2; 0.3; 0.4; 0.5 ; 0.8 ; 1', repeat_count='1', fill='freeze'))
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

    @staticmethod
    def create_spindex(options):
        """Creates a WheelSpinner instance configured for spindex animation instead of wheel spin.

        Args:
            options: List of strings representing items to be displayed in random order

        Returns:
            WheelSpinner instance configured for spindex animation
        """

        # Convert string options to WheelOption-like objects
        class SimpleOption:
            def __init__(self, text):
                self.option = text
                self.weight = 1

        option_objects = [SimpleOption(opt) for opt in options]

        # Create and configure the spinner
        spinner = WheelSpinner(option_objects)
        # Replace the wheel animation with the spindex animation
        spinner.animation = spinner.generate_spindex_animation()
        return spinner

    def generate_spindex_animation(self):
        """Generates an animation of items flying in one at a time from right to left.
        Items appear in random order and end up left-aligned in the image.
        Items are organized in columns with a maximum of 15 items per column.

        Returns:
            drawsvg Drawing object with the animation
        """
        # 1 in 30 chance of comic sans
        if random.randint(0, 30) == 0:
            font = "Comic Sans MS"
        else:
            font = "Shantell Sans"

        # Randomize the order of items for the animation
        random.shuffle(self.weighted_options)

        # Calculate timing for each item
        total_items = len(self.weighted_options)
        # Each item gets equal time slice with a small gap at the beginning and end
        start_delay = 0.05  # Initial delay before first item
        end_padding = 0.15  # Time left at the end after all items have appeared
        # Spread items out evenly across the animation time
        total_animation_time = 1.0 - start_delay - end_padding

        # Column layout configuration
        max_items_per_column = 25
        num_columns = math.ceil(total_items / max_items_per_column)

        # Calculate column widths based on the longest item in each column
        column_widths = [0] * num_columns
        for i, option in enumerate(self.weighted_options):
            col_idx = i // max_items_per_column
            text_len = len(option.option) + 3  # +3 for numbering and period
            column_widths[col_idx] = max(column_widths[col_idx], text_len)

        base_font_size = 10

        # Create drawing canvas
        width = sum(column_widths) * (base_font_size)
        d = draw.Drawing(
            height=200,
            width=width,
            origin="center",
            animation_config=draw.types.SyncedAnimationConfig(
                duration=10,  # Seconds - adjusted for better timing
                show_playback_progress=False,
                show_playback_controls=False,
                pause_on_load=False,
                repeat_count=0,
                fill="freeze",
            ),
            font_family=font,
        )
        # Add each item with its animation
        for i, option in enumerate(self.weighted_options):
            # Calculate timing for this item - make each item appear sequentially
            # Distribute items evenly across total time, with a slight overlap
            item_start_time = start_delay + (i * (total_animation_time / total_items))
            # Convert to 0-1 scale for key_times
            appear_at = item_start_time

            # Create text element for this item
            text = option.option  # No line breaks, keep as one line

            # Determine which column this item belongs to
            column_index = i // max_items_per_column
            row_index = i % max_items_per_column

            # Calculate responsive font size based on text length
            font_size = base_font_size # min(base_font_size, 250 / max(len(text), 1))

            # Calculate positions
            # Column positions are calculated from left to right
            # Each column starts at x = -90 and columns are spaced based on the width of the items
            column_spacing = 5  # Space between columns

            # Calculate position based on previous columns' widths
            x_position = (width/2) + 90  # Start at margin
            for col in range(column_index):
                # Add width of previous columns plus spacing
                x_position += column_widths[col] * (font_size * 0.55) + column_spacing

            # Row positions are calculated from top to bottom within each column
            row_height = min(
                200 / max(max_items_per_column, 1), 20
            )  # Distribute rows evenly with max height
            y_position = -92 + (row_index * row_height)

            # Create text with fly-in animation, left-aligned
            item_text = draw.Text(
                f'{i+1}. {str(text).title()}',
                font_size,
                x_position,  # X position - based on column
                y_position,  # Y position - based on row
                text_anchor="start",  # Left-aligned text
                fill="black",  # Keep text black for better readability
                opacity=0,
            )

            # Animate the text flying in
            item_text.append(
                draw.AnimateTransform(
                    "translate",
                    0.2,  # Faster animation for each individual item
                    begin=appear_at,
                    repeatCount="0",
                    fill="freeze",
                    from_or_values=f"0,0; -{width + 90},0",  # From right to final position (start further right)
                    # key_times=f"0; {appear_at}; {appear_at + 0.2}",
                )
            )

            # Fade in with a quick transition
            item_text.append(
                draw.Animate(
                    "opacity",
                    0.2,  # Quick fade-in (0.2 seconds)
                    from_or_values="0; 1",
                    key_times=f"0; {appear_at}",
                    repeatCount="0",
                    fill="freeze",
                )
            )

            d.append(item_text)

        # No title needed
        x_scale = max(1, int(2000 / width))
        y_scale = int(max_items_per_column/min(len(self.weighted_options), max_items_per_column)) * 4
        scale = min(x_scale, y_scale)
        d.set_pixel_scale(scale)  # Set number of pixels per geometry unit
        return d

    @staticmethod
    def add_line_breaks(text, soft_wrap=20):
        # a new string of words from the text until we reach the soft_wrap limit, only adding whole words
        new_text = ""
        current_line = ""
        longest_line = 0
        for word in text.split(" "):
            if len(current_line) + len(word) > soft_wrap:
                if len(current_line) > longest_line:
                    longest_line = len(current_line)
                new_text += current_line + "\n"
                current_line = ""
            current_line += word + " "
        if len(current_line) > longest_line:
            longest_line = len(current_line)
        new_text += current_line
        return new_text, longest_line

    @staticmethod
    def get_font_size(text, longest_line, max_width, max_height):
        font_size = 1
        while True:
            width = longest_line * 0.5 * font_size
            height = (1 + str(text).count("\n")) * font_size * 1.2
            if width <= max_width and height <= max_height:
                font_size += 1
            else:
                font_size -= 1
                return font_size
