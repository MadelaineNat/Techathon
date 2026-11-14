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
