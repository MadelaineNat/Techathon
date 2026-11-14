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
import array
import logging
import os
import re
import threading
import time
import wave
from datetime import datetime
from tempfile import NamedTemporaryFile
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
EXHIBITION_RECORDINGS_DIR = ROOT / "Harald_phone" / "data" / "recordings"
QUESTION_RECORDINGS_DIR = ROOT / "Harald_phone" / "recordings" / "questions"
HANG_UP_AUDIO = EXHIBITION_RECORDINGS_DIR / "hang_up.wav"
DIAL_AUDIO = EXHIBITION_RECORDINGS_DIR / "dial_edited.mp3"
GREETING_AUDIO = EXHIBITION_RECORDINGS_DIR / "greetings.wav"
WHAT_CAN_I_DO_AUDIO = EXHIBITION_RECORDINGS_DIR / "what_can_i_do.wav"
FALLBACK_AUDIO = EXHIBITION_RECORDINGS_DIR / "fangede_den_sgu_ikke.wav"
PROMPT = ROOT / "Harald_phone" / "data" / "harald_prompt.txt"
VOICE_STYLE = ROOT / "Harald_phone" / "data" / "harald_voice.txt"

KEYPAD_ROWS = [14, 15, 18, 23]
KEYPAD_COLUMNS = [24, 25, 8, 7]
MATRIX_KEYS = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

HARALD_NUMBER = "965"          # dial this + # to call Harald yourself
SCAN_ONLY_WHEN_IDLE = True     # no BLE scanning during calls (less radio contention)
RING_TIMEOUT_S = 25            # unanswered call becomes a missed call
RECALL_COOLDOWN_S = 60         # don't ring again for the same beacon right after a call
MIN_QUESTION_S = 0.7           # shorter recordings are treated as accidental presses
POST_RELEASE_RECORD_S = 0.45   # keep listening briefly after * is released
HOUSEKEEPING_S = 5             # how often the connection LEDs are refreshed (idle only)
KEY_RELEASE_SCANS = 3          # key must read "nothing" this many scans to count as released
RETAINED_QUESTION_FILES = 10


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
    recording_dir = ROOT / "Harald_phone" / "recordings"
    recording_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(suffix=".wav", dir=recording_dir, delete=False) as tmp:
        tmp.write(data)
    return Path(tmp.name)


class GeminiPipelineError(RuntimeError):
    def __init__(self, status: str, detail: str, step: str) -> None:
        super().__init__(detail)
        self.status = status
        self.step = step


def describe_wav(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"path": str(path), "size": path.stat().st_size}
    with wave.open(str(path), "rb") as source:
        frames = source.getnframes()
        rate = source.getframerate()
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        raw = source.readframes(frames)
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    rms = (
        (sum(sample * sample for sample in samples) / len(samples)) ** 0.5
        if samples
        else 0.0
    )
    result.update(
        duration=frames / rate if rate else 0.0,
        rate=rate,
        channels=channels,
        sample_width=sample_width,
        rms=round(rms, 1),
    )
    return result


# ======================================================================
# Gemini
# ======================================================================

class Harald:
    """Gemini conversation, transcription and speech synthesis. Network only, no hardware."""

    def __init__(self) -> None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        tts_api_key = os.getenv("GEMINI_TTS_API_KEY", api_key)
        self.client = genai.Client(api_key=api_key)
        self.tts_client = genai.Client(
            http_options={
                "api_version": "v1beta",
                "timeout": int(os.getenv("GEMINI_TTS_TIMEOUT_MS", "30000")),
            },
            api_key=tts_api_key,
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

    @staticmethod
    def _error_status(error: Exception) -> int | None:
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            return status
        match = re.search(r"\b(400|429|5\d\d)\b", str(error))
        return int(match.group(1)) if match else None

    @staticmethod
    def _retry_delay(error: Exception) -> float:
        match = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+)s", str(error))
        return min(float(match.group(1)), 5.0) if match else 1.0

    def _call(self, step: str, question_id: int, operation):
        started = time.monotonic()
        log.debug("q=%d step=%s start", question_id, step)
        try:
            result = operation()
        except Exception as error:
            log.error(
                "q=%d step=%s failed duration=%.3fs type=%s status=%s body=%s",
                question_id,
                step,
                time.monotonic() - started,
                type(error).__name__,
                self._error_status(error),
                str(error),
            )
            raise
        log.info(
            "q=%d step=%s done duration=%.3fs",
            question_id,
            step,
            time.monotonic() - started,
        )
        return result

    def _send_chat(self, question: str) -> str:
        with self._chat_lock:
            return self.chat.send_message(question).text.strip()

    def reply(self, audio_path: Path, question_id: int) -> tuple[str, str]:
        """Recording -> (what the visitor asked, Harald's answer as text)."""
        try:
            metadata = describe_wav(audio_path)
        except (OSError, EOFError, wave.Error) as error:
            raise GeminiPipelineError("Bad audio, retry", str(error), "validate") from error
        log.info("q=%d recording=%s", question_id, metadata)
        if metadata["duration"] < MIN_QUESTION_S or float(metadata["rms"]) < 40:
            raise GeminiPipelineError(
                "Didn't hear you", "Recording is too short or silent", "validate"
            )

        transcription = None
        for attempt in range(2):
            uploaded = None
            try:
                uploaded = self._call(
                    f"upload[{attempt + 1}]",
                    question_id,
                    lambda: self.tts_client.files.upload(file=str(audio_path)),
                )
                log.info(
                    "q=%d uploaded name=%s state=%s mime=%s size=%s",
                    question_id,
                    getattr(uploaded, "name", None),
                    getattr(uploaded, "state", None),
                    getattr(uploaded, "mime_type", None),
                    getattr(uploaded, "size_bytes", None),
                )
                transcription = self._call(
                    f"transcribe[{attempt + 1}]",
                    question_id,
                    lambda: self.tts_client.interactions.create(
                        model=os.getenv(
                            "HARALD_TRANSCRIBE_MODEL", "gemini-3.5-flash-lite"
                        ),
                        input=[
                            {
                                "type": "text",
                                "text": (
                                    "Transcribe this recording exactly. The speaker "
                                    "may use Danish or English. Return only the "
                                    "spoken words."
                                ),
                            },
                            {
                                "type": "audio",
                                "uri": uploaded.uri,
                                "mime_type": uploaded.mime_type,
                            },
                        ],
                    ),
                )
                break
            except Exception as error:
                status = self._error_status(error)
                if attempt == 0 and status in {400, 429} | set(range(500, 600)):
                    time.sleep(self._retry_delay(error) if status == 429 else 0.5)
                    continue
                message = "Bad audio, retry" if status == 400 else "No network"
                raise GeminiPipelineError(message, str(error), "transcribe") from error
            finally:
                if uploaded is not None:
                    try:
                        self.tts_client.files.delete(name=uploaded.name)
                    except Exception as error:
                        log.debug("q=%d file cleanup failed: %s", question_id, error)

        if transcription is None:
            raise GeminiPipelineError("Bad audio, retry", "No transcription", "transcribe")
        question = transcription.output_text.strip()
        if not question:
            raise GeminiPipelineError("Didn't hear you", "No speech was transcribed", "transcribe")

        response = self._call(
            "chat",
            question_id,
            lambda: self._send_chat(question),
        )
        log.info("Visitor: %s | Harald: %s", question, response)
        return question, response

    def synthesize(self, text: str, question_id: int) -> bytes:
        """Text -> wav bytes in Harald's voice."""
        deadline = time.monotonic() + 65
        for attempt in range(2):
            try:
                interaction = self._call(
                    f"tts[{attempt + 1}]",
                    question_id,
                    lambda: self.tts_client.interactions.create(
                        model=os.getenv("HARALD_TTS_MODEL", "gemini-3.8-flash-tts"),
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
                    ),
                )
                return base64.b64decode(interaction.output_audio.data)
            except Exception as error:
                status = self._error_status(error)
                if status == 429 and attempt < 1 and time.monotonic() < deadline:
                    time.sleep(min(self._retry_delay(error), max(0, deadline - time.monotonic())))
                    continue
                message = "Rate limit, wait" if status == 429 else "Speech unavailable"
                raise GeminiPipelineError(message, str(error), "tts") from error
        raise GeminiPipelineError("Speech unavailable", "TTS retry budget exhausted", "tts")


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
        self._recording_id: Optional[int] = None
        self._last_housekeeping = 0.0
        self._last_bt: Optional[tuple[bool, bool]] = None

    # ----- called from other threads -----------------------------------

    @property
    def token(self) -> int:
        with self._token_lock:
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

    def start_recording(self, token: int, question_id: int) -> None:
        self.commands.put(("record_start", token, question_id))

    def stop_recording(self, token: int, question_id: int) -> None:
        self.commands.put(("record_stop", token, question_id))

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
                    self.handle(cmd)
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

    def handle(self, cmd: tuple) -> None:
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
            _, token, question_id = cmd
            if token != self._token:
                return
            if self.mic is None:
                self.events.put(("record_error", "Set AIRPODS_MAC"))
                return
            self._stop_playback()
            try:
                self.mic.ensure_connected()
                if token != self._token:
                    return
                QUESTION_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
                path = QUESTION_RECORDINGS_DIR / (
                    f"question_{question_id}_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
                )
                self.mic.start_recording(path)
                self._recording = True
                self._recording_id = question_id
                self.events.put(("recording", (token, question_id)))
            except (BluetoothMicError, OSError) as error:
                log.error("Could not start recording: %s", error)
                self.events.put(("record_error", f"Mic: {error}"))

        elif kind == "record_stop":
            _, token, question_id = cmd
            if not self._recording or self.mic is None:
                return
            time.sleep(POST_RELEASE_RECORD_S)
            self._recording = False
            seconds = self.mic.recording_seconds
            path = self.mic.stop_recording()
            self._recording_id = None
            self._prune_recordings()
            self.events.put(("recorded", (token, path, seconds, question_id)))

    @staticmethod
    def _prune_recordings() -> None:
        files = sorted(
            QUESTION_RECORDINGS_DIR.glob("question_*.wav"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[RETAINED_QUESTION_FILES:]:
            _unlink(path)

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
            self._recording_id = None
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
        self.has_started = False
        self.scanning_enabled = False
        self.ready_for_questions = False
        self.star_down = False       # * is held (or was pressed and not yet released)
        self.recording = False       # the recorder is actually running
        self.question_token = 0
        self.question_id = 0

        self.events: SimpleQueue = SimpleQueue()
        self.stop_event = threading.Event()
        self._scanner_commands: SimpleQueue = SimpleQueue()

        self.mic = self._make_mic()
        self.speaker = self._make_speaker()
        self.audio = AudioWorker(self.speaker, self.mic, self.events)
        self.harald = Harald()

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

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.audio.start()
        self._scanner_commands.put(False)
        threading.Thread(target=self._scan_beacons, daemon=True).start()
        try:
            while not self.stop_event.is_set():
                self.handle_events()
                for kind, key in self._poll_keys():
                    self.handle_key(kind, key)
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

    def handle_key(self, kind: str, key: str) -> None:
        if kind == "release":
            if key == "*" and self.star_down:
                self.star_down = False
                self.audio.stop_recording(self.question_token, self.question_id)
            return

        if self.state is CallState.RINGING:
            if key == "#":
                self._answer()
            elif key == "D":
                self._go_idle("Call declined")

        elif self.state in (CallState.CONNECTED, CallState.PROCESSING):
            if key == "D":
                self._go_idle("Call ended")
            elif key == "*" and self.ready_for_questions:
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
        self.scanning_enabled = False
        self._scanner_commands.put(False)
        self.state = CallState.IDLE
        self.beacon_id = None
        self.dial_buffer = ""
        self.star_down = False
        self.recording = False
        self.ready_for_questions = False
        self.status = status
        if HANG_UP_AUDIO.exists():
            token = self.audio.token
            self.audio.play(HANG_UP_AUDIO, token, "hangup")
        else:
            log.error("Hang-up recording not found: %s", HANG_UP_AUDIO)
            self._resume_scanning()

    def _resume_scanning(self) -> None:
        if self.has_started:
            self.scanning_enabled = True
            self._scanner_commands.put(True)

    def _answer(self) -> None:
        number = self.beacon_id
        token = self.audio.cancel()  # stops the ringtone
        self._scanner_commands.put(not SCAN_ONLY_WHEN_IDLE)
        self.state = CallState.CONNECTED
        recording = (
            EXHIBITION_RECORDINGS_DIR / f"exhibition_{number}.wav"
            if number is not None
            else None
        )
        if recording is None or not recording.exists():
            log.error("Exhibition recording not found: %s", recording)
            self.status = "Hold * to ask"
            return
        self.status = "Harald is speaking..."
        self.ready_for_questions = False
        self.audio.play(recording, token, "talk")

    def _dial(self) -> None:
        number, self.dial_buffer = self.dial_buffer, ""
        if number != HARALD_NUMBER:
            self.status = "Unknown number"
            return
        self.beacon_id = None
        token = self.audio.cancel()
        self.state = CallState.CONNECTED
        self._scanner_commands.put(not SCAN_ONLY_WHEN_IDLE)
        self.ready_for_questions = False
        self.status = "Dialing..."
        if DIAL_AUDIO.exists():
            self.audio.play(DIAL_AUDIO, token, "dial")
        else:
            log.error("Dial recording not found: %s", DIAL_AUDIO)
            self._play_call_introduction(token)

    def _play_call_introduction(self, token: int) -> None:
        if not self.has_started and GREETING_AUDIO.exists():
            self.status = "Harald is speaking..."
            self.audio.play(GREETING_AUDIO, token, "welcome")
        elif self.has_started and WHAT_CAN_I_DO_AUDIO.exists():
            self.status = "Harald is speaking..."
            self.audio.play(WHAT_CAN_I_DO_AUDIO, token, "howto")
        else:
            missing = GREETING_AUDIO if not self.has_started else WHAT_CAN_I_DO_AUDIO
            log.error("Call introduction not found: %s", missing)
            self.status = "Hold * to ask"
            self.ready_for_questions = True

    def _start_question(self) -> None:
        if self.mic is None:
            self.status = "Set AIRPODS_MAC"
            return
        self.star_down = True
        self.question_token = self.audio.cancel()   # silences Harald, drops any pending answer
        self.state = CallState.CONNECTED
        self.status = "Starting mic..."
        self.question_id += 1
        self.audio.start_recording(self.question_token, self.question_id)

    # ------------------------------------------------------------------
    # Events from other threads
    # ------------------------------------------------------------------

    def handle_events(self) -> None:
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
        self._scanner_commands.put(not SCAN_ONLY_WHEN_IDLE)
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
        token, question_id = token
        if token == self.audio.token and self.star_down:
            self.question_id = question_id
            self.recording = True
            self.status = "Listening..."

    def _on_record_error(self, message) -> None:
        self.star_down = False
        self.recording = False
        self.status = str(message)[:21]

    def _on_recorded(self, value) -> None:
        token, path, seconds, question_id = value
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
            target=self._answer_worker,
            args=(Path(path), token, question_id),
            daemon=True,
        ).start()

    def _on_status(self, value) -> None:
        token, text = value
        if token == self.audio.token:
            self.status = text

    def _on_failed(self, value) -> None:
        token, message, step = value
        if token == self.audio.token and self.state in (CallState.CONNECTED, CallState.PROCESSING):
            self.state = CallState.CONNECTED
            self.status = str(message)[:21]

    def _on_audio_done(self, tag) -> None:
        if tag == "ring":
            if self.state is CallState.RINGING:      # keep ringing until answered
                self.audio.play(RINGTONE, self.audio.token, "ring", volume=0.5)
        elif tag == "dial":
            self._play_call_introduction(self.audio.token)
        elif tag == "welcome":
            self.has_started = True
            self._go_idle("Ready")
        elif tag == "hangup":
            self._resume_scanning()
        elif tag == "howto":
            self.ready_for_questions = True
            self.status = "Hold * to ask"
        elif tag in {"talk", "answer", "fallback"}:
            self.ready_for_questions = True
            self.state = CallState.CONNECTED
            self.status = "Hold * to ask"
        elif self.state in (CallState.CONNECTED, CallState.PROCESSING) and not self.star_down:
            self.state = CallState.CONNECTED
            self.status = "Hold * to ask"

    def _on_audio_error(self, value) -> None:
        tag, message = value
        if tag == "hangup":
            self._resume_scanning()
        elif tag == "dial":
            self._play_call_introduction(self.audio.token)
        elif tag != "ring" and self.state in (CallState.CONNECTED, CallState.PROCESSING):
            self.state = CallState.CONNECTED
            self.status = "Audio error"

    # ------------------------------------------------------------------
    # Gemini workers (network only, never touch hardware)
    # ------------------------------------------------------------------

    def _answer_worker(self, path: Path, token: int, question_id: int) -> None:
        try:
            _, response = self.harald.reply(path, question_id)
            if token != self.audio.token:
                return
            audio = self.harald.synthesize(response, question_id)
            if token != self.audio.token:
                return
            self.events.put(("status", (token, "Harald is speaking...")))
            self.audio.play(write_temp_wav(audio), token, "answer", delete=True)
        except Exception as error:
            if isinstance(error, GeminiPipelineError):
                log.warning("Could not answer question (%s): %s", error.step, error)
                self.events.put(("failed", (token, error.status, error.step)))
                if error.step == "tts" and token == self.audio.token and FALLBACK_AUDIO.exists():
                    self.audio.play(FALLBACK_AUDIO, token, "fallback")
            else:
                log.exception("Could not answer question")
                self.events.put(("failed", (token, "No network", "unknown")))

    # ------------------------------------------------------------------
    # Beacons
    # ------------------------------------------------------------------

    def _scan_beacons(self) -> None:
        async def scan() -> None:
            enabled = False
            while not self.stop_event.is_set():
                try:
                    while True:
                        enabled = self._scanner_commands.get_nowait()
                except Empty:
                    pass
                if not enabled:
                    await asyncio.sleep(0.2)
                    continue

                scanner = Scanner(
                    on_enter=lambda beacon: self.events.put(("beacon", beacon)),
                    verbose=True,
                )
                task = asyncio.create_task(scanner.run())
                failure = None
                try:
                    while not self.stop_event.is_set() and enabled:
                        try:
                            while True:
                                enabled = self._scanner_commands.get_nowait()
                        except Empty:
                            pass
                        if task.done():
                            failure = None if task.cancelled() else task.exception()
                            break
                        if not enabled:
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
        ring_phase = int(time.monotonic() * 2) % 2 if self.state is CallState.RINGING else 0
        frame = (self.state, self.status, self.dial_buffer, self.beacon_id, ring_phase)
        if frame == self._last_frame or time.monotonic() < self._render_retry_at:
            return

        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        draw.text((0, 0), "HARALD PHONE", font=self.font, fill=255)
        draw.line((0, 12, OLED_WIDTH, 12), fill=255)

        if self.state is CallState.RINGING:
            draw.text(
                (0, 18),
                "HARALD CALLING" if ring_phase else "INCOMING CALL",
                font=self.font,
                fill=255,
            )
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
        format="%(asctime)s %(threadName)s %(levelname)s %(message)s",
    )
    try:
        Phone().start()
    except KeyboardInterrupt:
        log.info("Phone stopped")


if __name__ == "__main__":
    main()
