#!/usr/bin/env python3
"""Raspberry Pi Harald phone: keypad, OLED, Bluetooth audio and beacons."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
import time
from enum import Enum, auto
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Optional

from dotenv import load_dotenv
from gpiozero import Button, LED
from google import genai
from google.genai import types
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from PIL import Image, ImageDraw, ImageFont

from Harald_phone.player import BluetoothSpeaker, BluetoothSpeakerError
from Harald_phone.voice_recorder import BluetoothMic, BluetoothMicError
from Kristoffer.ble_scanner2 import Scanner


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
log = logging.getLogger("harald_phone")

OLED_WIDTH, OLED_HEIGHT = 128, 64
RINGTONE = ROOT / "Harald_phone" / "data" / "recordings" / "school.mp3"
PROMPT = ROOT / "Harald_phone" / "data" / "harald_prompt.txt"
VOICE_STYLE = ROOT / "Harald_phone" / "data" / "harald_voice.txt"
TALKS_DIR = ROOT / "Harald_phone" / "data" / "exhibition_texts"

KEYPAD_ROWS = [14, 15, 18, 23]
KEYPAD_COLUMNS = [24, 25, 8, 7]
MATRIX_KEYS = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)


class CallState(Enum):
    IDLE = auto()
    RINGING = auto()
    CALLING = auto()
    CONNECTED = auto()
    PROCESSING = auto()


class Harald:
    """Gemini conversation, transcription and speech synthesis."""

    def __init__(self) -> None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        self.client = genai.Client(api_key=api_key)
        self.tts_client = genai.Client(
            http_options={"api_version": "v1beta"}, api_key=api_key
        )
        self.chat = self.client.chats.create(
            model=os.getenv("HARALD_CHAT_MODEL", "gemini-3.5-flash-lite"),
            config=types.GenerateContentConfig(
                system_instruction=PROMPT.read_text(),
                temperature=0.7,
            ),
        )
        self.voice_style = VOICE_STYLE.read_text()

    def answer(self, audio_path: Path) -> tuple[str, bytes]:
        uploaded = self.tts_client.files.upload(file=str(audio_path))
        transcription = self.tts_client.interactions.create(
            model=os.getenv("HARALD_TRANSCRIBE_MODEL", "gemini-3.5-flash-lite"),
            input=[
                {
                    "type": "text",
                    "text": (
                        "Transcribe this recording exactly. The speaker may use "
                        "Danish or English. Return only the spoken words."
                    ),
                },
                {
                    "type": "audio",
                    "uri": uploaded.uri,
                    "mime_type": uploaded.mime_type,
                },
            ],
        )
        question = transcription.output_text.strip()
        if not question:
            raise ValueError("No speech was transcribed")

        response = self.chat.send_message(question).text.strip()
        interaction = self.tts_client.interactions.create(
            model=os.getenv("HARALD_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": response,
                    "annotations": [{
                        "type": "speech_metadata",
                        "style": self.voice_style,
                    }],
                }],
            }],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": "Charon"}]},
        )
        log.info("Visitor: %s | Harald: %s", question, response)
        return response, base64.b64decode(interaction.output_audio.data)


class Phone:
    def __init__(self) -> None:
        serial = i2c(port=1, address=0x3C)
        self.display = ssd1306(
            serial,
            width=OLED_WIDTH,
            height=OLED_HEIGHT,
            rotate=2,
        )
        self.font = ImageFont.load_default()

        self.green1 = LED(17)  # microphone
        self.green2 = LED(22)  # speaker
        self.red = LED(27)  # recording / question in progress
        self.row_leds = [LED(pin) for pin in KEYPAD_ROWS]
        self.col_buttons = [Button(pin) for pin in KEYPAD_COLUMNS]
        self.not_pressed_keys: set[int] = set()

        self.state = CallState.IDLE
        self.caller: Optional[str] = None
        self.beacon_id: Optional[int] = None
        self.dial_buffer = ""
        self.status = "Ready"
        self.events: SimpleQueue[tuple[str, object]] = SimpleQueue()
        self.stop_event = threading.Event()
        self.audio_lock = threading.Lock()
        self._mic_connected = False
        self._speaker_connected = False
        self._last_mic_connect_attempt = 0.0
        self._last_speaker_connect_attempt = 0.0

        self.mic = self._make_mic()
        self.speaker = self._make_speaker()
        self.harald = Harald()
        self.talks = self._load_talks()

    @staticmethod
    def _make_mic() -> Optional[BluetoothMic]:
        mac = os.getenv("AIRPODS_MAC")
        print(mac)
        return BluetoothMic(mac, ROOT / "recordings") if mac else None

    @staticmethod
    def _make_speaker() -> Optional[BluetoothSpeaker]:
        mac = os.getenv("SPEAKER_MAC")
        print(mac)
        return (
            BluetoothSpeaker(
                mac,
                volume=float(os.getenv("SPEAKER_VOLUME", "0.6")),
                lead_in=float(os.getenv("SPEAKER_LEAD_IN", "1.5")),
            )
            if mac
            else None
        )

    @staticmethod
    def _load_talks() -> list[str]:
        return [
            path.read_text().strip()
            for path in sorted(TALKS_DIR.glob("exhibition_*.txt"))
        ]

    def _set_status(self, status: str) -> None:
        self.status = status
        self.update_display()

    def update_display(self) -> None:
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        draw.text((0, 0), "HARALD PHONE", font=self.font, fill=255)
        draw.line((0, 12, OLED_WIDTH, 12), fill=255)
        if self.state is CallState.RINGING:
            draw.text((0, 18), "HARALD CALLING", font=self.font, fill=255)
            draw.text((0, 31), f"Beacon {self.beacon_id or ''}", font=self.font, fill=255)
            draw.text((0, 49), "# Answer   D Decline", font=self.font, fill=255)
        else:
            title = {
                CallState.CALLING: "CALLING HARALD",
                CallState.CONNECTED: "HARALD CONNECTED",
                CallState.PROCESSING: "HARALD THINKING",
            }.get(self.state, "Dial number:")
            draw.text((0, 18), title, font=self.font, fill=255)
            draw.text((0, 31), self.dial_buffer or "_", font=self.font, fill=255)
            draw.line((0, 43, OLED_WIDTH, 43), fill=255)
            draw.text((0, 48), self.status[:21], font=self.font, fill=255)
        try:
            self.display.display(image)
        except OSError as error:
            log.warning("OLED unavailable: %s", error)

    def scan_key(self) -> Optional[str]:
        for row, row_led in enumerate(self.row_leds):
            row_led.on()
            for col, col_button in enumerate(self.col_buttons):
                if col_button.is_pressed:
                    self.not_pressed_keys.add(row)
                    if len(self.not_pressed_keys) == 3:
                        pressed_row = set(range(4)) - self.not_pressed_keys
                        key = MATRIX_KEYS[sum(pressed_row)][col]
                        self.not_pressed_keys.clear()
                        for led in self.row_leds:
                            led.off()
                        return key
            row_led.off()
        return None

    def wait_for_key_release(self) -> None:
        while self.scan_key() is not None:
            time.sleep(0.02)

    def star_is_pressed(self) -> bool:
        self.row_leds[3].on()
        pressed = self.col_buttons[0].is_pressed
        self.row_leds[3].off()
        return pressed

    def start(self) -> None:
        self.update_display()
        threading.Thread(target=self._monitor_audio, daemon=True).start()
        threading.Thread(target=self._scan_beacons, daemon=True).start()
        try:
            while not self.stop_event.is_set():
                self._handle_events()
                key = self.scan_key()
                if key:
                    self._handle_key(key)
                    if key != "*":
                        self.wait_for_key_release()
                time.sleep(0.03)
        finally:
            self.close()

    def _monitor_audio(self) -> None:
        while not self.stop_event.is_set():
            now = time.monotonic()
            if self.mic and not self._ready(self.mic) and now - self._last_mic_connect_attempt >= 10:
                self._last_mic_connect_attempt = now
                try:
                    self.mic.connect()
                except (BluetoothMicError, OSError) as error:
                    log.warning("Microphone connection failed: %s", error)
            if (
                self.speaker
                and not self._ready(self.speaker)
                and now - self._last_speaker_connect_attempt >= 10
            ):
                self._last_speaker_connect_attempt = now
                try:
                    self.speaker.connect()
                except (BluetoothSpeakerError, OSError) as error:
                    log.warning("Speaker connection failed: %s", error)

            mic_connected = bool(self.mic and self._ready(self.mic))
            speaker_connected = bool(self.speaker and self._ready(self.speaker))
            self.green1.value = mic_connected
            self.green2.value = speaker_connected

            if mic_connected != self._mic_connected:
                print(
                    "Microphone connected"
                    if mic_connected
                    else "Microphone disconnected",
                    flush=True,
                )
                self._mic_connected = mic_connected
            if speaker_connected != self._speaker_connected:
                print(
                    "Speaker connected"
                    if speaker_connected
                    else "Speaker disconnected",
                    flush=True,
                )
                self._speaker_connected = speaker_connected
            time.sleep(2)

    @staticmethod
    def _ready(device: object) -> bool:
        try:
            return bool(device.is_ready())
        except (BluetoothMicError, BluetoothSpeakerError, OSError) as error:
            log.warning("Bluetooth status check failed: %s", error)
            return False

    def _scan_beacons(self) -> None:
        async def scan() -> None:
            scanner = Scanner(
                on_enter=lambda beacon: self.events.put(("beacon", beacon)),
                verbose=True,
            )
            scan_task = asyncio.create_task(scanner.run())
            try:
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.2)
            finally:
                scan_task.cancel()
                await asyncio.gather(scan_task, return_exceptions=True)

        try:
            asyncio.run(scan())
        except Exception as error:
            log.error("Beacon scanner stopped: %s", error)

    def _handle_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "beacon" and self.state is CallState.IDLE:
                    self.beacon_id = int(value)
                    self.caller = "Harald"
                    self.state = CallState.RINGING
                    self._set_status("Incoming call")
                    threading.Thread(target=self._ring, daemon=True).start()
        except Empty:
            return

    def _ring(self) -> None:
        if self.state is not CallState.RINGING:
            return
        if self.speaker is None or not RINGTONE.exists():
            log.warning("Cannot play ringtone: configure SPEAKER_MAC and check %s", RINGTONE)
            return
        try:
            with self.audio_lock:
                self.speaker.play(RINGTONE, blocking=True, volume=0.5)
        except (BluetoothSpeakerError, OSError) as error:
            log.error("Ringtone failed: %s", error)

    def _handle_key(self, key: str) -> None:
        if self.state is CallState.RINGING:
            if key == "#":
                self._answer()
            elif key == "D":
                self._decline()
            return

        if self.state is CallState.CONNECTED and key == "*":
            self._ask_question()
            return
        if key in "0123456789" and len(self.dial_buffer) < 16:
            self.dial_buffer += key
            self._set_status("")
        elif key in ("C", "D"):
            self.dial_buffer = ""
            self._set_status("Number cleared")
        elif key == "#":
            self._dial()

    def _answer(self) -> None:
        self._stop_audio()
        self.state = CallState.CONNECTED
        self._set_status("Hold * to ask")
        if self.beacon_id and 1 <= self.beacon_id <= len(self.talks):
            threading.Thread(
                target=self._play_bytes, args=(self.talks[self.beacon_id - 1],), daemon=True
            ).start()

    def _decline(self) -> None:
        self._stop_audio()
        self.caller = None
        self.beacon_id = None
        self.state = CallState.IDLE
        self._set_status("Call declined")

    def _dial(self) -> None:
        number = self.dial_buffer
        self.dial_buffer = ""
        if number != "656":
            self._set_status("Unknown number")
            return
        self.state = CallState.CALLING
        self._set_status("Calling Harald")
        threading.Thread(target=self._outgoing_call, daemon=True).start()

    def _outgoing_call(self) -> None:
        self.state = CallState.CONNECTED
        self._set_status("Hold * to ask")

    def _ask_question(self) -> None:
        if self.mic is None:
            self._set_status("Set AIRPODS_MAC")
            return
        self.state = CallState.PROCESSING
        self.red.on()
        self._set_status("Listening...")
        path: Optional[Path] = None
        try:
            self.mic.ensure_connected()
            path = self.mic.start_recording()
            while self.star_is_pressed():
                time.sleep(0.03)
            path = self.mic.stop_recording() or path
            self._set_status("Thinking...")
            threading.Thread(target=self._answer_question, args=(path,), daemon=True).start()
        except (BluetoothMicError, OSError) as error:
            self.red.off()
            self.state = CallState.CONNECTED
            self._set_status(f"Mic error: {error}"[:21])

    def _answer_question(self, path: Path) -> None:
        try:
            _, audio = self.harald.answer(path)
            self._play_bytes(audio)
        except Exception as error:
            log.exception("Could not answer question")
            self._set_status(f"Harald error: {error}"[:21])
        finally:
            self.red.off()
            self.state = CallState.CONNECTED
            self._set_status("Hold * to ask")

    def _play_bytes(self, content: str | bytes) -> None:
        if self.speaker is None:
            self._set_status("Set SPEAKER_MAC")
            return
        path = ROOT / "Harald_phone" / "harald_response.wav"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            self._set_status("Harald speaking")
            # Exhibition talks are synthesized using the same Harald voice.
            interaction = self.harald.tts_client.interactions.create(
                model=os.getenv("HARALD_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
                input=[{"type": "user_input", "content": [{"type": "text", "text": content}]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": "Charon"}]},
            )
            path.write_bytes(base64.b64decode(interaction.output_audio.data))
        try:
            with self.audio_lock:
                self.speaker.play(path)
        except (BluetoothSpeakerError, OSError) as error:
            log.error("Harald audio failed: %s", error)

    def _stop_audio(self) -> None:
        if self.speaker is not None:
            self.speaker.stop()

    def close(self) -> None:
        self.stop_event.set()
        self._stop_audio()
        self.green1.off()
        self.green2.off()
        self.red.off()
        for led in self.row_leds:
            led.off()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        Phone().start()
    except KeyboardInterrupt:
        log.info("Phone stopped")


if __name__ == "__main__":
    main()
