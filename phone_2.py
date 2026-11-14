#!/usr/bin/env python3
"""Raspberry Pi Harald phone: keypad, OLED, Bluetooth audio and beacons.

Threading model (why it is built this way):
  main thread     keypad, OLED and the call state machine. The ONLY thread that touches
                  state or the display.
  audio thread    the ONLY thread that touches the Bluetooth speaker and mic. Everything
                  else just sends it commands.
  scanner thread  BLE beacon scanning (asyncio). Only puts events on the queue.
  gemini threads  short-lived: network calls only. They write a unique wav and queue it.

Threads talk through queues. Stale work (after hang-up, or when * is pressed) is dropped
using a token that is bumped on every cancel.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
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
RECORDING_PATH = Path(tempfile.gettempdir()) / "harald_question.wav"

KEYPAD_ROWS = [14, 15, 18, 23]
KEYPAD_COLUMNS = [24, 25, 8, 7]
MATRIX_KEYS = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

HARALD_NUMBER = "656"          # dial this + # to call Harald yourself
SCAN_ONLY_WHEN_IDLE = True     # no BLE scanning during calls (less radio contention)
RING_TIMEOUT_S = 25            # unanswered call becomes a missed call
RECALL_COOLDOWN_S = 60         # don't ring again for the same beacon right after a call
MIN_QUESTION_S = 0.7           # shorter recordings are treated as accidental presses
HOUSEKEEPING_S = 5             # how often the connection LEDs are refreshed (idle only)
KEY_RELEASE_SCANS = 3          # key must read "nothing" this many scans to count as released


class CallState(Enum):
    IDLE = auto()
    RINGING = auto()
    CONNECTED = auto()         # in a call, waiting for the visitor
    PROCESSING = auto()        # thinking / answering a question


def _unlink(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def write_temp_wav(data: bytes) -> Path:
    """Every playback gets its own file, so overlapping requests can't corrupt each other."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(data)
    return Path(tmp.name)


# ======================================================================
# Gemini
# ======================================================================

class Harald:
    """Gemini conversation, transcription and speech synthesis. Network only, no hardware."""

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
        self._chat_lock = threading.Lock()

    def reply(self, audio_path: Path) -> tuple[str, str]:
        """Recording -> (what the visitor asked, Harald's answer as text)."""
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

        with self._chat_lock:
            response = self.chat.send_message(question).text.strip()
        log.info("Visitor: %s | Harald: %s", question, response)
        return question, response

    def synthesize(self, text: str) -> bytes:
        """Text -> wav bytes in Harald's voice."""
        interaction = self.tts_client.interactions.create(
            model=os.getenv("HARALD_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": text,
                    "annotations": [{
                        "type": "speech_metadata",
                        "style": self.voice_style,
                    }],
                }],
            }],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": "Charon"}]},
        )
        return base64.b64decode(interaction.output_audio.data)


# ======================================================================
# Audio thread: the only code that touches the speaker and the mic
# ======================================================================

class AudioWorker(threading.Thread):

    def __init__(self, speaker: Optional[BluetoothSpeaker], mic: Optional[BluetoothMic],
                 events: SimpleQueue) -> None:
        super().__init__(name="audio", daemon=True)
        self.speaker = speaker
        self.mic = mic
        self.events = events                   # (name, value) tuples back to the main thread
        self.commands: SimpleQueue = SimpleQueue()
        self.stop_event = threading.Event()

        self._token = 0
        self._token_lock = threading.Lock()
        self._current: Optional[tuple[str, Optional[Path]]] = None   # (tag, temp file to delete)
        self._recording = False
        self._last_housekeeping = 0.0
        self._last_bt: Optional[tuple[bool, bool]] = None

    # ----- called from other threads -----------------------------------

    @property
    def token(self) -> int:
        return self._token

    def cancel(self) -> int:
        """Stop playback/recording and invalidate all pending work. Returns the new token."""
        with self._token_lock:
            self._token += 1
            token = self._token
        self.commands.put(("cancel",))
        return token

    def play(self, path, token: int, tag: str, volume: Optional[float] = None,
             delete: bool = False) -> None:
        self.commands.put(("play", Path(path), token, tag, volume, delete))

    def start_recording(self, token: int) -> None:
        self.commands.put(("record_start", token))

    def stop_recording(self, token: int) -> None:
        self.commands.put(("record_stop", token))

    def shutdown(self) -> None:
        self.stop_event.set()

    # ----- audio thread -------------------------------------------------

    def run(self) -> None:
        self._connect_at_startup()
        while not self.stop_event.is_set():
            try:
                cmd = self.commands.get(timeout=0.05)
            except Empty:
                cmd = None
            try:
                if cmd is not None:
                    self._handle(cmd)
                self._check_playback()
                self._housekeeping()
            except Exception:
                log.exception("Audio thread error")
        self._stop_playback()
        self._discard_recording()

    def _connect_at_startup(self) -> None:
        for name, device in (("speaker", self.speaker), ("earbuds", self.mic)):
            if device is None:
                continue
            try:
                device.connect()
                log.info("%s connected", name)
            except (BluetoothSpeakerError, BluetoothMicError, OSError) as error:
                log.warning("%s connection failed: %s", name, error)
        self._report_bt()

    def _handle(self, cmd: tuple) -> None:
        kind = cmd[0]

        if kind == "cancel":
            self._stop_playback()
            self._discard_recording()

        elif kind == "play":
            _, path, token, tag, volume, delete = cmd
            if token != self._token or self._recording or self.speaker is None:
                if delete:
                    _unlink(path)
                if self.speaker is None and token == self._token:
                    self.events.put(("audio_error", (tag, "Set SPEAKER_MAC")))
                return
            self._stop_playback()
            try:
                self.speaker.play(path, blocking=False, volume=volume)
                self._current = (tag, path if delete else None)
            except (BluetoothSpeakerError, OSError) as error:
                log.error("Playback failed: %s", error)
                if delete:
                    _unlink(path)
                self.events.put(("audio_error", (tag, str(error))))

        elif kind == "record_start":
            _, token = cmd
            if token != self._token:
                return
            if self.mic is None:
                self.events.put(("record_error", "Set AIRPODS_MAC"))
                return
            self._stop_playback()
            try:
                self.mic.ensure_connected()    # may switch the earbuds to headset profile
                self.mic.start_recording(RECORDING_PATH)
                self._recording = True
                self.events.put(("recording", token))
            except (BluetoothMicError, OSError) as error:
                log.error("Could not start recording: %s", error)
                self.events.put(("record_error", f"Mic: {error}"))

        elif kind == "record_stop":
            _, token = cmd
            if not self._recording or self.mic is None:
                return
            self._recording = False
            seconds = self.mic.recording_seconds
            path = self.mic.stop_recording()
            self.events.put(("recorded", (token, path, seconds)))

    def _check_playback(self) -> None:
        if self._current is None or self.speaker is None or self.speaker.is_playing:
            return
        tag, temp = self._current
        self._current = None
        finished = False
        try:
            finished = self.speaker.wait()       # True = played to the end
        except BluetoothSpeakerError as error:
            log.error("Playback failed: %s", error)
            self.events.put(("audio_error", (tag, str(error))))
        if temp is not None:
            _unlink(temp)
        if finished:
            self.events.put(("audio_done", tag))

    def _stop_playback(self) -> None:
        if self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception as error:
                log.warning("Could not stop speaker: %s", error)
        if self._current is not None and self._current[1] is not None:
            _unlink(self._current[1])
        self._current = None

    def _discard_recording(self) -> None:
        if self._recording and self.mic is not None:
            self._recording = False
            try:
                self.mic.stop_recording()
            except Exception as error:
                log.warning("Could not stop recording: %s", error)

    def _housekeeping(self) -> None:
        """Refresh the connection LEDs, but only while nothing is playing or recording."""
        now = time.monotonic()
        if now - self._last_housekeeping < HOUSEKEEPING_S:
            return
        if self._current is not None or self._recording:
            return
        self._last_housekeeping = now
        self._report_bt()

    def _report_bt(self) -> None:
        status = (self._is_connected(self.mic), self._is_connected(self.speaker))
        if status != self._last_bt:
            self._last_bt = status
            self.events.put(("bt", status))

    @staticmethod
    def _is_connected(device) -> bool:
        if device is None:
            return False
        try:
            return bool(device.is_connected())
        except (BluetoothMicError, BluetoothSpeakerError, OSError) as error:
            log.warning("Bluetooth status check failed: %s", error)
            return False


# ======================================================================
# The phone: keypad, OLED and the call state machine (main thread only)
# ======================================================================

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
        self._last_frame = None
        self._render_retry_at = 0.0

        self.green1 = LED(17)  # earbuds connected
        self.green2 = LED(22)  # speaker connected
        self.red = LED(27)     # recording
        self.row_leds = [LED(pin) for pin in KEYPAD_ROWS]
        self.col_buttons = [Button(pin) for pin in KEYPAD_COLUMNS]
        self._held_key: Optional[str] = None
        self._none_count = 0

        self.state = CallState.IDLE
        self.beacon_id: Optional[int] = None
        self.dial_buffer = ""
        self.status = "Ready"
        self.ring_started = 0.0
        self.last_call_beacon: Optional[int] = None
        self.cooldown_until = 0.0
        self.star_down = False       # * is held (or was pressed and not yet released)
        self.recording = False       # the recorder is actually running
        self.question_token = 0

        self.events: SimpleQueue = SimpleQueue()
        self.stop_event = threading.Event()

        self.mic = self._make_mic()
        self.speaker = self._make_speaker()
        self.audio = AudioWorker(self.speaker, self.mic, self.events)
        self.harald = Harald()
        self.talks = self._load_talks()
        self._talk_cache: dict[int, bytes] = {}

    @staticmethod
    def _make_mic() -> Optional[BluetoothMic]:
        mac = os.getenv("AIRPODS_MAC")
        return BluetoothMic(mac, ROOT / "recordings") if mac else None

    @staticmethod
    def _make_speaker() -> Optional[BluetoothSpeaker]:
        mac = os.getenv("SPEAKER_MAC")
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

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.audio.start()
        threading.Thread(target=self._scan_beacons, daemon=True).start()
        threading.Thread(target=self._prefetch_talks, daemon=True).start()
        try:
            while not self.stop_event.is_set():
                self._handle_events()
                for kind, key in self._poll_keys():
                    self._handle_key(kind, key)
                self._tick()
                self._render()
                time.sleep(0.02)
        finally:
            self.close()

    def _tick(self) -> None:
        self.red.value = self.recording
        if (
            self.state is CallState.RINGING
            and time.monotonic() - self.ring_started > RING_TIMEOUT_S
        ):
            self._go_idle("Missed call")

    # ------------------------------------------------------------------
    # Keypad
    # ------------------------------------------------------------------

    def _scan_key(self) -> Optional[str]:
        """One matrix scan: the key currently pressed, or None."""
        not_pressed_rows: set[int] = set()
        for row, row_led in enumerate(self.row_leds):
            row_led.on()
            for col, col_button in enumerate(self.col_buttons):
                if col_button.is_pressed:
                    not_pressed_rows.add(row)
                    if len(not_pressed_rows) == 3:
                        pressed_row = set(range(4)) - not_pressed_rows
                        for led in self.row_leds:
                            led.off()
                        return MATRIX_KEYS[sum(pressed_row)][col]
            row_led.off()
        return None

    def _poll_keys(self) -> list[tuple[str, str]]:
        """Returns ("press", key) / ("release", key) events. A held key gives one of each."""
        events: list[tuple[str, str]] = []
        key = self._scan_key()
        if key is not None:
            self._none_count = 0
            if key != self._held_key:
                if self._held_key is not None:
                    events.append(("release", self._held_key))
                self._held_key = key
                events.append(("press", key))
        elif self._held_key is not None:
            self._none_count += 1
            if self._none_count >= KEY_RELEASE_SCANS:
                events.append(("release", self._held_key))
                self._held_key = None
                self._none_count = 0
        return events

    def _handle_key(self, kind: str, key: str) -> None:
        if kind == "release":
            if key == "*" and self.star_down:
                self.star_down = False
                self.audio.stop_recording(self.question_token)
            return

        if self.state is CallState.RINGING:
            if key == "#":
                self._answer()
            elif key == "D":
                self._go_idle("Call declined")

        elif self.state in (CallState.CONNECTED, CallState.PROCESSING):
            if key == "D":
                self._go_idle("Call ended")
            elif key == "*":
                self._start_question()

        else:  # IDLE: dialing
            if key in "0123456789":
                if len(self.dial_buffer) < 16:
                    self.dial_buffer += key
                    self.status = ""
            elif key in ("C", "D"):
                self.dial_buffer = ""
                self.status = "Number cleared"
            elif key == "#":
                self._dial()

    # ------------------------------------------------------------------
    # Call actions (main thread)
    # ------------------------------------------------------------------

    def _go_idle(self, status: str = "") -> None:
        if self.beacon_id is not None:
            self.last_call_beacon = self.beacon_id
            self.cooldown_until = time.monotonic() + RECALL_COOLDOWN_S
        self.audio.cancel()          # silence everything and drop pending answers
        self.state = CallState.IDLE
        self.beacon_id = None
        self.dial_buffer = ""
        self.star_down = False
        self.recording = False
        self.status = status

    def _answer(self) -> None:
        number = self.beacon_id
        token = self.audio.cancel()  # stops the ringtone
        self.state = CallState.CONNECTED
        if not number or not 1 <= number <= len(self.talks):
            self.status = "Hold * to ask"
            return
        self.status = "Harald is speaking..."
        threading.Thread(target=self._talk_worker, args=(number, token), daemon=True).start()

    def _dial(self) -> None:
        number, self.dial_buffer = self.dial_buffer, ""
        if number != HARALD_NUMBER:
            self.status = "Unknown number"
            return
        self.beacon_id = None
        self.state = CallState.CONNECTED
        self.status = "Hold * to ask"

    def _start_question(self) -> None:
        if self.mic is None:
            self.status = "Set AIRPODS_MAC"
            return
        self.star_down = True
        self.question_token = self.audio.cancel()   # silences Harald, drops any pending answer
        self.state = CallState.CONNECTED
        self.status = "Starting mic..."
        self.audio.start_recording(self.question_token)

    # ------------------------------------------------------------------
    # Events from other threads
    # ------------------------------------------------------------------

    def _handle_events(self) -> None:
        while True:
            try:
                name, value = self.events.get_nowait()
            except Empty:
                return
            handler = getattr(self, f"_on_{name}", None)
            if handler is None:
                log.warning("Unknown event: %s", name)
            else:
                handler(value)

    def _on_beacon(self, beacon_id) -> None:
        now = time.monotonic()
        if self.state is not CallState.IDLE:
            return
        if beacon_id == self.last_call_beacon and now < self.cooldown_until:
            return
        self.beacon_id = int(beacon_id)
        self.state = CallState.RINGING
        self.ring_started = now
        self.status = "Incoming call"
        token = self.audio.cancel()
        if self.speaker is not None and RINGTONE.exists():
            self.audio.play(RINGTONE, token, "ring", volume=0.5)
        else:
            log.warning("Cannot play ringtone: check SPEAKER_MAC and %s", RINGTONE)

    def _on_bt(self, status) -> None:
        mic_ok, speaker_ok = status
        self.green1.value = mic_ok
        self.green2.value = speaker_ok

    def _on_recording(self, token) -> None:
        if token == self.audio.token and self.star_down:
            self.recording = True
            self.status = "Listening..."

    def _on_record_error(self, message) -> None:
        self.star_down = False
        self.recording = False
        self.status = str(message)[:21]

    def _on_recorded(self, value) -> None:
        token, path, seconds = value
        self.recording = False
        if token != self.audio.token:
            return           # hung up in the meantime
        if self.state not in (CallState.CONNECTED, CallState.PROCESSING):
            return
        if path is None or seconds < MIN_QUESTION_S:
            self.state = CallState.CONNECTED
            self.status = "Too short, hold *"
            return
        self.state = CallState.PROCESSING
        self.status = "Thinking..."
        threading.Thread(
            target=self._answer_worker, args=(Path(path), token), daemon=True
        ).start()

    def _on_status(self, value) -> None:
        token, text = value
        if token == self.audio.token:
            self.status = text

    def _on_failed(self, value) -> None:
        token, message = value
        if token == self.audio.token and self.state in (CallState.CONNECTED, CallState.PROCESSING):
            self.state = CallState.CONNECTED
            self.status = f"Error: {message}"[:21]

    def _on_audio_done(self, tag) -> None:
        if tag == "ring":
            if self.state is CallState.RINGING:      # keep ringing until answered
                self.audio.play(RINGTONE, self.audio.token, "ring", volume=0.5)
        elif self.state in (CallState.CONNECTED, CallState.PROCESSING) and not self.star_down:
            self.state = CallState.CONNECTED
            self.status = "Hold * to ask"

    def _on_audio_error(self, value) -> None:
        tag, message = value
        if tag != "ring" and self.state in (CallState.CONNECTED, CallState.PROCESSING):
            self.state = CallState.CONNECTED
            self.status = "Audio error"

    # ------------------------------------------------------------------
    # Gemini workers (network only, never touch hardware)
    # ------------------------------------------------------------------

    def _talk_audio(self, number: int) -> bytes:
        data = self._talk_cache.get(number)
        if data is None:
            data = self.harald.synthesize(self.talks[number - 1])
            self._talk_cache[number] = data
        return data

    def _prefetch_talks(self) -> None:
        """Synthesize all exhibition talks at startup, so no network traffic is needed in calls."""
        for number in range(1, len(self.talks) + 1):
            if self.stop_event.is_set():
                return
            try:
                self._talk_audio(number)
            except Exception as error:
                log.warning("Could not prefetch talk %d: %s", number, error)

    def _talk_worker(self, number: int, token: int) -> None:
        try:
            audio = self._talk_audio(number)
            if token != self.audio.token:
                return
            self.audio.play(write_temp_wav(audio), token, "talk", delete=True)
        except Exception as error:
            log.exception("Could not prepare exhibition talk")
            self.events.put(("failed", (token, str(error))))

    def _answer_worker(self, path: Path, token: int) -> None:
        try:
            _, response = self.harald.reply(path)
            if token != self.audio.token:
                return
            audio = self.harald.synthesize(response)
            if token != self.audio.token:
                return
            self.events.put(("status", (token, "Harald is speaking...")))
            self.audio.play(write_temp_wav(audio), token, "answer", delete=True)
        except Exception as error:
            log.exception("Could not answer question")
            self.events.put(("failed", (token, str(error))))

    # ------------------------------------------------------------------
    # Beacons
    # ------------------------------------------------------------------

    def _scan_wanted(self) -> bool:
        return self.state is CallState.IDLE or not SCAN_ONLY_WHEN_IDLE

    def _scan_beacons(self) -> None:
        async def scan() -> None:
            while not self.stop_event.is_set():
                if not self._scan_wanted():
                    await asyncio.sleep(0.2)
                    continue

                scanner = Scanner(
                    on_enter=lambda beacon: self.events.put(("beacon", beacon)),
                    verbose=False,
                )
                task = asyncio.create_task(scanner.run())
                failure = None
                try:
                    while not self.stop_event.is_set() and self._scan_wanted():
                        if task.done():
                            failure = None if task.cancelled() else task.exception()
                            break
                        await asyncio.sleep(0.2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if failure is not None:
                    log.error("Beacon scanner error: %s", failure)
                    await asyncio.sleep(3)

        try:
            asyncio.run(scan())
        except Exception:
            log.exception("Beacon scanner stopped")

    # ------------------------------------------------------------------
    # Display (main thread only)
    # ------------------------------------------------------------------

    def _render(self) -> None:
        frame = (self.state, self.status, self.dial_buffer, self.beacon_id)
        if frame == self._last_frame or time.monotonic() < self._render_retry_at:
            return

        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        draw.text((0, 0), "HARALD PHONE", font=self.font, fill=255)
        draw.line((0, 12, OLED_WIDTH, 12), fill=255)

        if self.state is CallState.RINGING:
            draw.text((0, 18), "HARALD CALLING", font=self.font, fill=255)
            draw.text((0, 31), f"Beacon {self.beacon_id or ''}", font=self.font, fill=255)
            draw.text((0, 49), "# Answer   D Decline", font=self.font, fill=255)
        else:
            in_call = self.state in (CallState.CONNECTED, CallState.PROCESSING)
            if in_call:
                title = "HARALD CONNECTED"
                middle = f"Beacon {self.beacon_id}" if self.beacon_id else ""
                hint = "Hold *=Ask  D=End"
            else:
                title = "Dial number:"
                middle = self.dial_buffer or "_"
                hint = "#=Call  C=Clear"
            draw.text((0, 18), title, font=self.font, fill=255)
            draw.text((0, 31), middle, font=self.font, fill=255)
            draw.line((0, 43, OLED_WIDTH, 43), fill=255)
            draw.text((0, 46), self.status[:21], font=self.font, fill=255)
            draw.text((0, 55), hint, font=self.font, fill=255)

        try:
            self.display.display(image)
            self._last_frame = frame
        except OSError as error:
            log.warning("OLED unavailable: %s", error)
            self._render_retry_at = time.monotonic() + 1

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.stop_event.set()
        self.audio.cancel()
        self.audio.shutdown()
        if self.audio.is_alive():
            self.audio.join(timeout=5)
        for led in (self.green1, self.green2, self.red, *self.row_leds):
            try:
                led.off()
            except Exception:
                pass
        try:
            self.display.clear()
        except OSError:
            pass


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        Phone().start()
    except KeyboardInterrupt:
        log.info("Phone stopped")


if __name__ == "__main__":
    main()