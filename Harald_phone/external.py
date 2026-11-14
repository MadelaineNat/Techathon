"""Standalone interface to Harald's AI services.

This module deliberately contains no application, user-interface, recording,
or playback code. The calling application owns those concerns and can use the
returned audio bytes however it needs to.
"""

import base64
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

__all__ = ["HaraldAI"]


class HaraldAI:
    """Provide transcription, chat, and speech synthesis for Harald."""

    CHAT_MODEL = "gemini-3.5-flash-lite"
    TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"
    SPEECH_MODEL = "gemini-3.8-flash-lite-tts"
    SPEECH_VOICE = "Charon"

    def __init__(self, data_directory: str | Path | None = None) -> None:
        load_dotenv()

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")

        data_path = Path(data_directory) if data_directory else Path(__file__).parent / "data"
        prompt = (data_path / "harald_prompt.txt").read_text()
        voice_style = (data_path / "harald_voice.txt").read_text()

        self._client = genai.Client(api_key=api_key)
        self._tts_client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=api_key,
        )
        self._prompt = prompt
        self._voice_style = voice_style
        self.reset_conversation()

    def ask(self, question: str) -> str:
        """Send a visitor's question and return Harald's response."""
        if not question.strip():
            raise ValueError("Question cannot be empty.")

        response = self._chat.send_message(question)
        return (response.text or "").strip()

    def reset_conversation(self) -> None:
        """Start a new conversation while keeping the same model configuration."""
        self._chat = self._client.chats.create(
            model=self.CHAT_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=self._prompt,
                temperature=0.7,
            ),
        )

    def speech_to_text(self, audio_file_path: str | Path) -> str:
        """Transcribe an audio file in Danish or English."""
        audio_file = self._tts_client.files.upload(file=str(audio_file_path))
        interaction = self._tts_client.interactions.create(
            model=self.TRANSCRIPTION_MODEL,
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
        return (interaction.output_text or "").strip()

    def text_to_speech(self, text: str) -> bytes:
        """Synthesize text and return its decoded audio bytes."""
        if not text.strip():
            raise ValueError("Text cannot be empty.")

        interaction = self._tts_client.interactions.create(
            model=self.SPEECH_MODEL,
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
                                    "style": self._voice_style,
                                }
                            ],
                        }
                    ],
                }
            ],
            response_format={"type": "audio"},
            generation_config={
                "speech_config": [{"voice": self.SPEECH_VOICE}],
            },
        )
        return base64.b64decode(interaction.output_audio.data)
