import base64
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


workspace_directory = Path(__file__).parents[1]
sys.path.insert(0, str(workspace_directory))
from Kristoffer.ble_scanner2 import Scanner


load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("GEMINI_API_KEY is not set.")

client = genai.Client(api_key=api_key)
tts_client = genai.Client(http_options={"api_version": "v1beta"}, api_key=api_key)

prompt_file_path = Path(__file__).parent / "data/harald_prompt.txt"
HARALD_PROMPT = prompt_file_path.read_text()

voice_style_file_path = Path(__file__).parent / "data/harald_voice.txt"
VOICE_STYLE = voice_style_file_path.read_text()

exhibition_directory = Path(__file__).parent / "data/exhibition_data"
EXHIBITION_TALKS = [
    path.read_text().strip()
    for path in sorted(exhibition_directory.glob("exhibition_texts/exhibition_*.txt"))
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

    player = shutil.which("paplay") or shutil.which("aplay")
    if not player:
        print(f"Audio saved to {output_path}, but no WAV player was found.")
        return

    try:
        subprocess.run([player, str(output_path)], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
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

def record_question_while_held(window: tk.Tk, status_label: tk.Label) -> None:
    recording_path = Path(__file__).with_name("question.wav")
    recorder: subprocess.Popen[bytes] | None = None
    recording = False

    def start_recording(_event: tk.Event[tk.Misc]) -> None:
        nonlocal recorder, recording
        if recording:
            return

        try:
            recorder = subprocess.Popen(
                [
                    "arecord",
                    "-q",
                    "-f",
                    "S16_LE",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    str(recording_path),
                ]
            )
        except OSError as error:
            status_label.config(text=f"Could not start recording: {error}")
            return

        recording = True
        button.config(text="Release to finish")
        status_label.config(text="Listening... release the button when you are done.")

    def finish_recording(_event: tk.Event[tk.Misc]) -> None:
        nonlocal recorder, recording
        if not recording:
            return

        recording = False
        if recorder is not None:
            recorder.terminate()
            recorder.wait()
            recorder = None

        button.config(text="Hold to ask")
        status_label.config(text="Thinking...")
        threading.Thread(target=answer_recording, daemon=True).start()

    def answer_recording() -> None:
        try:
            question = speech_to_text(str(recording_path))
            if not question:
                raise ValueError("No speech was transcribed.")

            response = chat.send_message(question).text.strip()
            print(f"Visitor: {question}\nHarald: {response}")
            window.after(0, lambda: status_label.config(text="Harald is speaking..."))
            text_to_speech(response)
            window.after(0, lambda: status_label.config(text="Ready"))
        except Exception as error:
            print(f"Could not answer question: {error}")
            window.after(0, lambda: status_label.config(text="Ready - please try again."))

    button = tk.Button(window, text="Hold to ask", width=20, height=3)
    button.bind("<ButtonPress-1>", start_recording)
    button.bind("<ButtonRelease-1>", finish_recording)
    button.pack(padx=30, pady=30)


def play_exhibition_talk(talk: str, status_label: tk.Label) -> None:
    status_label.config(text="Harald is speaking...")

    def play() -> None:
        try:
            text_to_speech(talk)
        finally:
            status_label.after(0, lambda: status_label.config(text="Ready"))

    threading.Thread(target=play, daemon=True).start()


def start_beacon_scanner(window: tk.Tk, status_label: tk.Label) -> None:
    def on_enter(installation_id: int) -> None:
        if not 1 <= installation_id <= len(EXHIBITION_TALKS):
            print(f"No exhibition talk configured for beacon HB-{installation_id}")
            return

        talk = EXHIBITION_TALKS[installation_id - 1]
        print(f"Entered exhibition {installation_id}")
        window.after(0, lambda: play_audio())

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
            window.after(0, lambda: status_label.config(text="Bluetooth scanner unavailable"))

    threading.Thread(target=run_scanner, name="bluetooth-scanner", daemon=True).start()


def main() -> None:
    window = tk.Tk()
    window.title("Harald Bluetooth")
    window.protocol("WM_DELETE_WINDOW", window.destroy)

    tk.Label(window, text="King Harald Bluetooth", font=("Helvetica", 18, "bold")).pack(
        pady=(20, 5)
    )
    status_label = tk.Label(window, text="Starting Harald...")
    status_label.pack()

    talks_frame = tk.Frame(window)
    talks_frame.pack(pady=10)
    for index, talk in enumerate(EXHIBITION_TALKS, start=1):
        tk.Button(
            talks_frame,
            text=f"Exhibition talk {index}",
            command=lambda talk=talk: play_exhibition_talk(talk, status_label),
        ).pack(side=tk.LEFT, padx=4)

    record_question_while_held(window, status_label)
    start_beacon_scanner(window, status_label)

    def introduce_harald() -> None:
        introduction = "Greetings, traveler! I am Harald Bluetooth, King of Denmark. Welcome to Jelling."
        window.after(0, lambda: status_label.config(text="Harald is speaking..."))
        text_to_speech(introduction)
        window.after(0, lambda: status_label.config(text="Ready"))

    threading.Thread(target=introduce_harald, daemon=True).start()
    window.mainloop()



if __name__ == "__main__":
    main()