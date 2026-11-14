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
    for path in sorted(exhibition_directory.glob("exhibition_*.txt"))
]

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
        model="gemini-3.8-flash-tts-lite",
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

text_to_speech(EXHIBITION_TALKS[0])