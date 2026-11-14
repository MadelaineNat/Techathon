"""
bluetooth_mic.py - record from a Bluetooth headset microphone on a Raspberry Pi.

Requires PipeWire (or PulseAudio) with Bluetooth support, plus the command line
tools `bluetoothctl`, `pactl` and `parecord` (pulseaudio-utils).

Example:
    mic = BluetoothMic("E8:C9:13:CD:4B:55")
    mic.connect()
    mic.start_recording()
    time.sleep(5)
    path = mic.stop_recording()
    print(BluetoothMic.analyze(path))
"""

import array
import logging
import signal
import subprocess
import time
import wave
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Union

log = logging.getLogger("bluetooth_mic")


class BluetoothMicError(Exception):
    """Raised when connecting, switching profile or recording fails."""


class BluetoothMic:
    # Tried in this order when switching the earbuds to headset (mic) mode.
    HEADSET_PROFILES = (
        "headset-head-unit",
        "headset-head-unit-msbc",
        "headset-head-unit-cvsd",
    )

    def __init__(
        self,
        mac: str,
        output_dir: Union[str, Path] = "recordings",
        sample_rate: int = 16000,
    ):
        self.mac = mac.upper()
        self.mac_underscored = self.mac.replace(":", "_")
        self.card_name = f"bluez_card.{self.mac_underscored}"
        self.output_dir = Path(output_dir)
        self.sample_rate = sample_rate

        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _run(cmd: list, timeout: float = 15) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            raise BluetoothMicError(
                f"'{cmd[0]}' not found. Install it (pactl/parecord: "
                f"sudo apt install pulseaudio-utils)."
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timed out")

    def _wait_for(self, condition, timeout: float, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(interval)
        return condition()

    # --------------------------------------------------------------- connection
    def is_connected(self) -> bool:
        """True if BlueZ reports the earbuds as connected."""
        info = self._run(["bluetoothctl", "info", self.mac])
        return "Connected: yes" in info.stdout

    def card_exists(self) -> bool:
        """True if PipeWire has created an audio card for the earbuds."""
        cards = self._run(["pactl", "list", "cards", "short"])
        return self.card_name in cards.stdout

    def find_source(self) -> Optional[str]:
        """Name of the earbuds' microphone source, or None if not available."""
        out = self._run(["pactl", "list", "short", "sources"]).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[1]
            if (
                name.startswith("bluez_input.")
                and self.mac_underscored in name.replace(":", "_")
                and not name.endswith(".monitor")
            ):
                return name
        return None

    def is_ready(self) -> bool:
        """Connected AND the microphone source exists (i.e. headset profile active)."""
        return self.is_connected() and self.find_source() is not None

    def set_headset_profile(self) -> str:
        """Switch the earbuds to headset mode so the microphone is exposed."""
        for profile in self.HEADSET_PROFILES:
            result = self._run(
                ["pactl", "set-card-profile", self.card_name, profile]
            )
            if result.returncode == 0:
                log.info("Headset profile set: %s", profile)
                return profile
        raise BluetoothMicError(
            f"Could not switch {self.card_name} to a headset profile. "
            f"See `pactl list cards` for the profiles it offers."
        )

    def connect(self, retries: int = 3, timeout: float = 20) -> None:
        """
        Connect to the earbuds and make the microphone available.
        Safe to call when already connected. Raises BluetoothMicError on failure.
        """
        self._run(["bluetoothctl", "power", "on"])

        if not self.is_connected():
            for attempt in range(1, retries + 1):
                log.info("Connecting to %s (attempt %d/%d)", self.mac, attempt, retries)
                result = self._run(["bluetoothctl", "connect", self.mac], timeout=timeout)
                if "Connection successful" in result.stdout or self.is_connected():
                    break
                log.warning("Connect failed: %s", (result.stdout + result.stderr).strip())
                time.sleep(2)
            else:
                raise BluetoothMicError(
                    f"Could not connect to {self.mac}. Are the earbuds out of the "
                    f"case, in range, and not connected to another device?"
                )

        if not self._wait_for(self.card_exists, timeout=10):
            raise BluetoothMicError(
                f"Connected, but no audio card {self.card_name} appeared. "
                f"Is WirePlumber running?"
            )

        if self.find_source() is None:
            self.set_headset_profile()
            if not self._wait_for(lambda: self.find_source() is not None, timeout=10):
                raise BluetoothMicError(
                    "Switched to headset profile, but no microphone source appeared."
                )

    def ensure_connected(self) -> None:
        """Reconnect if the link dropped. Cheap to call before each recording."""
        if not self.is_ready():
            self.connect()

    def disconnect(self) -> None:
        if self.is_recording:
            self.stop_recording()
        self._run(["bluetoothctl", "disconnect", self.mac])

    # ---------------------------------------------------------------- recording
    @property
    def is_recording(self) -> bool:
        """True only while the recorder process is actually running."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def recording_seconds(self) -> float:
        if self._started_at is None or not self.is_recording:
            return 0.0
        return time.monotonic() - self._started_at

    def start_recording(self, path: Union[str, Path, None] = None) -> Path:
        """
        Start recording to a WAV file (mono, 16-bit) and return its path.
        If no path is given, a timestamped file is created in output_dir.
        """
        if self.is_recording:
            raise BluetoothMicError("Already recording.")

        source = self.find_source()
        if source is None:
            raise BluetoothMicError(
                "Microphone source not found. Call connect() first."
            )

        if path is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.output_dir / f"rec_{datetime.now():%Y%m%d_%H%M%S}.wav"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._proc = subprocess.Popen(
            [
                "parecord",
                "--device", source,
                "--channels=1",
                f"--rate={self.sample_rate}",
                "--format=s16le",
                "--file-format=wav",
                str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._path = path
        self._started_at = time.monotonic()

        time.sleep(0.3)  # catch immediate failures
        if self._proc.poll() is not None:
            err = self._proc.stderr.read().strip() if self._proc.stderr else ""
            self._proc = None
            raise BluetoothMicError(f"Recorder exited immediately: {err}")
        return path

    def stop_recording(self) -> Optional[Path]:
        """
        Stop recording and return the path of the finished WAV file.
        Returns None if nothing was recorded. If the earbuds disconnected
        mid-recording, the audio captured so far is still returned.
        """
        proc, path = self._proc, self._path
        if proc is None:
            return None

        if proc.poll() is None:
            # SIGINT lets parecord finalise the WAV header properly.
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            log.warning("Recorder had already exited (earbuds disconnected?).")

        if proc.stderr:
            proc.stderr.close()
        self._proc = None
        self._started_at = None
        return path if path and path.exists() else None

    def record(self, seconds: float, path: Union[str, Path, None] = None) -> Path:
        """Convenience: record for a fixed duration and return the file path."""
        self.ensure_connected()
        out = self.start_recording(path)
        try:
            time.sleep(seconds)
        finally:
            result = self.stop_recording()
        return result or out

    @contextmanager
    def recording(self, path: Union[str, Path, None] = None) -> Iterator[Path]:
        """with mic.recording() as path: ...   (stops automatically on exit)"""
        out = self.start_recording(path)
        try:
            yield out
        finally:
            self.stop_recording()

    # ---------------------------------------------------------------- streaming
    def stream(self, chunk_ms: int = 100) -> Iterator[bytes]:
        """
        Yield raw audio chunks (16-bit signed little-endian, mono) as they arrive,
        for live processing. Stops when the earbuds disconnect or the generator
        is closed. Use numpy.frombuffer(chunk, dtype=numpy.int16) to get samples.
        """
        source = self.find_source()
        if source is None:
            raise BluetoothMicError("Microphone source not found. Call connect() first.")

        chunk_bytes = int(self.sample_rate * chunk_ms / 1000) * 2
        proc = subprocess.Popen(
            [
                "parec",
                "--device", source,
                "--channels=1",
                f"--rate={self.sample_rate}",
                "--format=s16le",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            while True:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    log.warning("Audio stream ended (earbuds disconnected?).")
                    return
                yield data
        finally:
            proc.terminate()
            proc.wait()

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "connected": self.is_connected(),
            "mic_source": self.find_source(),
            "recording": self.is_recording,
            "recording_seconds": round(self.recording_seconds, 1),
        }

    # ----------------------------------------------------------------- context
    def __enter__(self) -> "BluetoothMic":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        if self.is_recording:
            self.stop_recording()


if __name__ == "__main__":
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mic = BluetoothMic("E8:C9:13:CD:4B:55")
    with mic:
        print("Recording 5 seconds - speak now...")
        file = mic.record(5)
        print("Saved:", file)

    """
    mic = BluetoothMic("E8:C9:13:CD:4B:55")
    if not mic.is_connected():
        print("Not connected. Attempting to connect...")
        mic.connect()
    if mic.is_connected():
        print("Connected:", mic.is_connected())
    print("Starting recording for 5 seconds...")
    file = mic.record(5)
    print("Saved:", file)
    