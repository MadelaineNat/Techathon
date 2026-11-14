import base64
import asyncio
import importlib.util
import os
import subprocess
import sys
import threading
from time import sleep
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from player import BluetoothSpeaker


workspace_directory = Path(__file__).parents[1]
sys.path.insert(0, str(workspace_directory))
from Kristoffer.ble_scanner2 import Scanner


keypad_module_path = workspace_directory / "keypad-oled" / "main.py"
keypad_spec = importlib.util.spec_from_file_location("keypad_oled", keypad_module_path)
keypad_oled = importlib.util.module_from_spec(keypad_spec)
sys.modules[keypad_spec.name] = keypad_oled
keypad_spec.loader.exec_module(keypad_oled)

col_buttons = keypad_oled.col_buttons
display = keypad_oled.display
green1 = keypad_oled.green1
green2 = keypad_oled.green2
red = keypad_oled.red
row_leds = keypad_oled.row_leds
scankeys = keypad_oled.scankeys
update_display = keypad_oled.update_display
wait_for_release = keypad_oled.wait_for_release


load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("GEMINI_API_KEY is not set.")

speaker_mac = os.getenv("SPEAKER_MAC")
if not speaker_mac:
    raise RuntimeError("SPEAKER_MAC is not set.")

speaker = BluetoothSpeaker(speaker_mac)

client = genai.Client(api_key=api_key)
tts_client = genai.Client(http_options={"api_version": "v1beta"}, api_key=api_key)

prompt_file_path = Path(__file__).parent / "data/harald_prompt.txt"
HARALD_PROMPT = prompt_file_path.read_text()

voice_style_file_path = Path(__file__).parent / "data/harald_voice.txt"
VOICE_STYLE = voice_style_file_path.read_text()

exhibition_directory = Path(__file__).parent / "data/exhibition_data"
EXHIBITION_TALKS = [
    path.read_text().strip()
    for path in sorted(exhibition_directory.glob("exhibition_*.txt"))
]

chat = client.chats.create(
    model="gemini-3.5-flash-lite",
    config=types.GenerateContentConfig(
        system_instruction=HARALD_PROMPT,
        temperature=0.7,
    ),
)

def play_audio(audio_data: bytes) -> None:
    output_path = Path(__file__).with_name("harald_response.wav")
    output_path.write_bytes(audio_data)

    try:
        speaker.play(output_path)
    except Exception as error:
        print(f"Audio saved to {output_path}, but playback failed: {error}")

def text_to_speech(text: str) -> None:
    interaction = tts_client.interactions.create(
        model="gemini-3.8-flash-lite-tts",
        input=[
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {
                                "type": "speech_metadata",
                                "style": VOICE_STYLE,
                            }
                        ],
                    }
                ],
            }
        ],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": "Charon"}]},
    )

    play_audio(base64.b64decode(interaction.output_audio.data))

def speech_to_text(audio_file_path: str) -> str:
    audio_file = tts_client.files.upload(file=audio_file_path)

    interaction = tts_client.interactions.create(
        model="gemini-3.5-transcribe",
        input=[
            {
                "type": "audio",
                "uri": audio_file.uri,
                "mime_type": audio_file.mime_type,
            }
        ],
        generation_config={
            "transcription_config": {
            "mode": "smart",
            "language_codes": ["da-DK", "en-US"],
            }
        },
    )

    return interaction.output_text.strip()

def set_status(message: str) -> None:
    keypad_oled.status_message = message
    update_display()


def record_question_while_held() -> None:
    recording_path = Path(__file__).with_name("question.wav")
    try:
        recorder = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", str(recording_path)]
        )
    except OSError as error:
        set_status(f"Record error: {error}")
        return

    set_status("Listening... release *")
    while scankeys() == "*":
        sleep(0.02)
    recorder.terminate()
    recorder.wait()
    set_status("Thinking...")

    def answer_recording() -> None:
        try:
            question = speech_to_text(str(recording_path))
            if not question:
                raise ValueError("No speech was transcribed.")

            response = chat.send_message(question).text.strip()
            print(f"Visitor: {question}\nHarald: {response}")
            set_status("Harald is speaking...")
            text_to_speech(response)
            set_status("Ready")
        except Exception as error:
            print(f"Could not answer question: {error}")
            set_status("Ready - try again")

    threading.Thread(target=answer_recording, daemon=True).start()


def play_exhibition_talk(talk: str) -> None:
    set_status("Harald is speaking...")

    def play() -> None:
        try:
            text_to_speech(talk)
        finally:
            set_status("Ready")

    threading.Thread(target=play, daemon=True).start()


def start_beacon_scanner() -> None:
    def on_enter(installation_id: int) -> None:
        if not 1 <= installation_id <= len(EXHIBITION_TALKS):
            print(f"No exhibition talk configured for beacon HB-{installation_id}")
            return

        talk = EXHIBITION_TALKS[installation_id - 1]
        print(f"Entered exhibition {installation_id}")
        play_exhibition_talk(talk)

    def on_leave(installation_id: int) -> None:
        print(f"Left exhibition {installation_id}")

    async def scan() -> None:
        scanner = Scanner(on_enter=on_enter, on_leave=on_leave, verbose=True)
        await scanner.run()

    def run_scanner() -> None:
        try:
            asyncio.run(scan())
        except Exception as error:
            print(f"Bluetooth scanner stopped: {error}")
            set_status("Bluetooth unavailable")

    threading.Thread(target=run_scanner, name="bluetooth-scanner", daemon=True).start()


def main() -> None:
    print("Keypad ready: 1-3 for exhibitions, A for introduction, hold * to ask.")
    update_display()
    start_beacon_scanner()

    def introduce_harald() -> None:
        introduction = "Greetings, traveler! I am Harald Bluetooth, King of Denmark. Welcome to Jelling."
        set_status("Harald is speaking...")
        text_to_speech(introduction)
        set_status("Ready")

    threading.Thread(target=introduce_harald, daemon=True).start()

    try:
        while True:
            key = scankeys()
            if key is None:
                sleep(0.01)
                continue

            print(f"Pressed: {key}")
            if key == "*":
                red.on()
                record_question_while_held()
                red.off()
            elif key == "A":
                threading.Thread(target=introduce_harald, daemon=True).start()
            elif key in "123":
                talk_index = int(key) - 1
                if talk_index < len(EXHIBITION_TALKS):
                    play_exhibition_talk(EXHIBITION_TALKS[talk_index])
                else:
                    set_status("No talk configured")
            else:
                set_status("Use 1 2 3 A or *")
            wait_for_release()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            display.clear()
        except OSError:
            print("Could not clear OLED.")
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



if __name__ == "__main__":
    main()