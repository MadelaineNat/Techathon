
#!/usr/bin/env python3

from time import sleep, monotonic
from queue import SimpleQueue, Empty

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

serial = i2c(
    port=1,
    address=OLED_ADDRESS
)

display = ssd1306(
    serial,
    width=OLED_WIDTH,
    height=OLED_HEIGHT
)

font = ImageFont.load_default()


# ==========================================
# LED CONFIGURATION
# ==========================================

green1 = LED(17)
green2 = LED(22)
red = LED(27)


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
# APPLICATION STATE
# ==========================================

dial_buffer = ""
history = []

last_key = None
status_message = ""

incoming_caller = None

# Thread-safe queue for incoming calls
incoming_queue = SimpleQueue()


# ==========================================
# CALL FUNCTIONS
# ==========================================

def call_100():

    global status_message

    print("Calling 100...")

    green1.on()
    green2.off()

    status_message = "Calling 100"


def call_300():

    global status_message

    print("Calling 300...")

    green1.off()
    green2.on()

    status_message = "Calling 300"


# ==========================================
# INCOMING CALL FUNCTION
# ==========================================

def incoming_call(number):

    """
    Call this function when another device
    or service initiates an incoming call.
    """

    incoming_queue.put(str(number))

    print(f"Incoming call from {number}")


# ==========================================
# OLED DISPLAY
# ==========================================

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

    if incoming_caller is not None:

        # Incoming call screen
        draw.text(
            (0, 17),
            "INCOMING CALL",
            font=font,
            fill=255
        )

        draw.text(
            (0, 30),
            f"From: {incoming_caller}"[:21],
            font=font,
            fill=255
        )

        draw.text(
            (0, 46),
            "# Accept",
            font=font,
            fill=255
        )

        draw.text(
            (65, 46),
            "D Decline",
            font=font,
            fill=255
        )

    else:

        # Dial screen
        draw.text(
            (0, 17),
            "Dial number:",
            font=font,
            fill=255
        )

        draw.text(
            (0, 30),
            dial_buffer if dial_buffer else "_",
            font=font,
            fill=255
        )

        draw.line(
            (0, 43, OLED_WIDTH, 43),
            fill=255
        )

        draw.text(
            (0, 47),
            status_message[:21],
            font=font,
            fill=255
        )

        draw.text(
            (0, 57),
            "C=Clear  #=Call",
            font=font,
            fill=255
        )

    # Retry OLED communication
    for attempt in range(5):

        try:

            display.display(image)

            return True

        except OSError as error:

            print(
                f"OLED error ({attempt + 1}/5): {error}"
            )

            sleep(0.2)

    print("OLED unavailable. Keypad still running.")

    return False


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

                    not_pressed_keys.clear()

                    # Turn off all rows
                    for led in row_leds:
                        led.off()

                    return key

        row_led.off()

    return None


# ==========================================
# WAIT FOR KEY RELEASE
# ==========================================

def wait_for_release(hold_red=False):

    while True:

        key = scankeys()

        if hold_red:

            if key == '*':
                red.on()

        if key is None:
            break

        sleep(0.02)

    if hold_red:
        red.off()

    sleep(0.05)


# ==========================================
# HANDLE KEYPAD INPUT
# ==========================================

def handle_key(key):

    global dial_buffer
    global last_key
    global status_message
    global incoming_caller

    last_key = key
    history.append(key)

    # --------------------------------------
    # INCOMING CALL
    # --------------------------------------

    if incoming_caller is not None:

        if key == '#':

            print(f"Accepted call from {incoming_caller}")

            status_message = "Call accepted"

            incoming_caller = None

        elif key == 'D':

            print(f"Declined call from {incoming_caller}")

            status_message = "Call declined"

            incoming_caller = None

        else:

            print("Press # to accept or D to decline")

        return

    # --------------------------------------
    # DIGITS
    # --------------------------------------

    if key in "0123456789":

        if len(dial_buffer) < 16:

            dial_buffer += key

            status_message = ""

            print(f"Dialing: {dial_buffer}")

    # --------------------------------------
    # CLEAR NUMBER
    # --------------------------------------

    elif key == 'C' or key == 'D':

        dial_buffer = ""

        status_message = "Number cleared"

        print("Number cleared")

    # --------------------------------------
    # CALL
    # --------------------------------------

    elif key == '#':

        if dial_buffer == "100":

            call_100()

        elif dial_buffer == "300":

            call_300()

        else:

            status_message = "Unknown number"

            print(f"Unknown number: {dial_buffer}")

        dial_buffer = ""


# ==========================================
# CHECK INCOMING CALLS
# ==========================================

def check_incoming_calls():

    global incoming_caller
    global status_message

    try:

        caller = incoming_queue.get_nowait()

        incoming_caller = caller

        status_message = ""

        print(f"Incoming call: {caller}")

        update_display()

    except Empty:
        pass


# ==========================================
# MAIN PROGRAM
# ==========================================

def main():

    global last_key

    print("================================")
    print(" Raspberry Pi Phone System")
    print("================================")

    print("Initializing OLED...")

    update_display()

    print("Keypad ready.")
    print("Enter 100 or 300 to call.")
    print("Press C to clear.")
    print("Hold * for red LED.")
    print()

    try:

        while True:
            # Simulate an incoming call every 30 seconds
            if monotonic() >= next_call_time:
                print(f"Incoming call from {caller_number}")
                incoming_call(str(caller_number))

                caller_number += 1
                next_call_time = monotonic() + 30
                
            # Check for incoming calls
            check_incoming_calls()

            # Scan keypad
            key = scankeys()

            if key is not None:

                print(f"Pressed: {key}")

                # Star activates red LED while held
                if key == '*':

                    red.on()

                    wait_for_release(hold_red=True)

                    red.off()

                    continue

                # Handle normal keypad input
                handle_key(key)

                update_display()

                # Prevent repeated key presses
                wait_for_release()

            sleep(0.01)

    except KeyboardInterrupt:

        print("\nStopping...")

    finally:

        print("Cleaning up...")

        # Clear OLED
        try:
            display.clear()
        except OSError:
            print("Could not clear OLED.")

        # Turn off all LEDs
        for led in row_leds + [green1, green2, red]:

            try:
                led.off()
                led.close()
            except Exception:
                pass

        for button in col_buttons:

            try:
                button.close()
            except Exception:
                pass

        print("GPIO cleaned up.")
        print("Goodbye!")


# ==========================================
# START
# ==========================================

if __name__ == "__main__":
    main()
