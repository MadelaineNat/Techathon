
#!/usr/bin/env python3

from time import sleep

from gpiozero import LED, Button

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306

from PIL import Image, ImageDraw, ImageFont


# ==========================================
# OLED CONFIGURATION
# ==========================================

OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_ADDRESS = 0x3C

serial = i2c(port=1, address=OLED_ADDRESS)

display = ssd1306(
    serial,
    width=OLED_WIDTH,
    height=OLED_HEIGHT
)

font = ImageFont.load_default()


# ==========================================
# KEYPAD CONFIGURATION
# ==========================================

keypad_rows = [14, 15, 18, 23]
keypad_columns = [24, 25, 8, 7]

row_leds = [LED(pin) for pin in keypad_rows]
col_buttons = [Button(pin) for pin in keypad_columns]

matrix_keys = [
    ['1', '2', '3', 'A'],
    ['4', '5', '6', 'B'],
    ['7', '8', '9', 'C'],
    ['*', '0', '#', 'D']
]

not_pressed_keys = set()
row1 = set([0, 1, 2, 3])


# ==========================================
# DISPLAY HISTORY
# ==========================================

history = []
last_key = None


def update_display():

    image = Image.new(
        "1",
        (OLED_WIDTH, OLED_HEIGHT)
    )

    draw = ImageDraw.Draw(image)

    # Header
    draw.text(
        (0, 0),
        "Grove SSD1315",
        font=font,
        fill=255
    )

    draw.line(
        (0, 12, OLED_WIDTH, 12),
        fill=255
    )

    # Last pressed key
    draw.text(
        (0, 18),
        "Last key:",
        font=font,
        fill=255
    )

    draw.text(
        (75, 16),
        str(last_key) if last_key else "-",
        font=font,
        fill=255
    )

    # Key history
    draw.text(
        (0, 35),
        "History:",
        font=font,
        fill=255
    )

    recent = history[-5:]

    for index, key in enumerate(recent):
        draw.text(
            (index * 24, 50),
            str(key),
            font=font,
            fill=255
        )

    display.display(image)


# ==========================================
# KEYPAD SCANNING
# ==========================================

def scankeys():

    for row, row_led in enumerate(row_leds):

        row_led.on()

        for col, col_button in enumerate(col_buttons):

            if col_button.is_pressed:

                not_pressed_keys.add(row)

                if len(not_pressed_keys) == 3:

                    pressed_row = row1 - not_pressed_keys

                    key = matrix_keys[sum(pressed_row)][col]

                    pressed_row.clear()
                    not_pressed_keys.clear()

                    # Turn off all rows
                    for led in row_leds:
                        led.off()

                    return key

        row_led.off()

    return None


# ==========================================
# MAIN PROGRAM
# ==========================================

def main():

    global last_key

    print("Raspberry Pi Keypad + OLED")
    print("Starting...")

    update_display()

    print("Please press a button")

    try:

        while True:

            key = scankeys()

            if key is not None:

                print("Pressed:", key)

                last_key = key
                history.append(key)

                update_display()

                # Debounce / prevent repeated presses
                sleep(0.5)

            sleep(0.01)

    except KeyboardInterrupt:

        print("\nStopping...")

    finally:

        display.clear()

        for led in row_leds:
            led.off()
            led.close()

        for button in col_buttons:
            button.close()

        print("GPIO cleaned up.")


if __name__ == "__main__":
    main()
