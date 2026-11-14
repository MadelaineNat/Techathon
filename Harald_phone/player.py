"""
sonos_player.py - play audio files on a paired Bluetooth speaker (e.g. Sonos Roam)
from a Raspberry Pi running PipeWire.

Requires the command line tools `bluetoothctl`, `pactl` and `paplay`
(pulseaudio-utils). The speaker must already be paired and trusted once
(see the bluetoothctl steps: pair / trust / connect).

Example:
    speaker = BluetoothSpeaker("C4:38:75:C2:F2:00")
    speaker.connect()
    speaker.play("recording.wav")              # waits until finished
    speaker.play("recording.wav", blocking=False)
    ...
    speaker.stop()
"""

import atexit
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger("bluetooth_speaker")


def _trace(operation: str, *args) -> None:
    log.debug(
        "mono=%.3f thread=%s speaker %s %s",
        time.monotonic(),
        threading.current_thread().name,
        operation,
        " ".join(map(str, args)),
    )


class BluetoothSpeakerError(Exception):
    """Raised when connecting or playing fails."""


class BluetoothSpeaker:
    def __init__(
        self,
        mac: str,
        volume: float = 0.6,
        lead_in: float = 0.5,
    ):
        """
        mac      Bluetooth address of the speaker.
        volume   Default playback volume, 0.0 - 1.0. Applied before every
                 play(), because Bluetooth speakers can reset it.
        lead_in  Seconds of silence added to the start of each playback so a
                 sleeping speaker has time to wake up without cutting off
                 the start of the sound. Set 0 to disable.
        """
        self.mac = mac.upper()
        self.mac_underscored = self.mac.replace(":", "_")
        self.volume = volume
        self.lead_in = lead_in

        self._proc: Optional[subprocess.Popen] = None
        self._temp_file: Optional[Path] = None
        self._stopped_by_us = False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _run(cmd: list, timeout: float = 15) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise BluetoothSpeakerError(
                f"'{cmd[0]}' not found. Install it with: sudo apt install pulseaudio-utils"
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timed out")

    @staticmethod
    def _wait_for(condition, timeout: float, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(interval)
        return condition()

    # --------------------------------------------------------------- connection
    def is_paired(self) -> bool:
        """True if the Pi knows this speaker and has it paired."""
        info = self._run(["bluetoothctl", "info", self.mac])
        return "Paired: yes" in info.stdout

    def is_connected(self) -> bool:
        """True if BlueZ reports the speaker as connected."""
        _trace("is_connected")
        info = self._run(["bluetoothctl", "info", self.mac])
        return "Connected: yes" in info.stdout

    def find_sink(self) -> Optional[str]:
        """Name of the speaker's audio output, or None if it isn't available."""
        _trace("find_sink")
        out = self._run(["pactl", "list", "short", "sinks"]).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[1]
            # PipeWire uses colons or underscores in the name, so normalise.
            if (
                name.startswith("bluez_output.")
                and self.mac_underscored in name.replace(":", "_")
                and not name.endswith(".monitor")
            ):
                return name
        return None

    def is_ready(self) -> bool:
        """Connected AND the audio output exists, so play() will work."""
        return self.is_connected() and self.find_sink() is not None

    def connect(self, retries: int = 3, timeout: float = 20) -> None:
        """
        Connect to the speaker and wait for its audio output to appear.
        Safe to call when already connected. Raises BluetoothSpeakerError.
        """
        _trace("connect")
        self.kill_stale_players()
        self._run(["bluetoothctl", "power", "on"])

        if not self.is_connected():
            for attempt in range(1, retries + 1):
                log.info("Connecting to %s (attempt %d/%d)", self.mac, attempt, retries)
                result = self._run(["bluetoothctl", "connect", self.mac], timeout=timeout)
                output = result.stdout + result.stderr
                if "Connection successful" in output or self.is_connected():
                    break
                if "not available" in output:
                    raise BluetoothSpeakerError(
                        f"{self.mac} is not known to the Pi. Put the speaker in pairing "
                        f"mode (hold power ~2 s until the light flashes blue) and "
                        f"pair/trust it in bluetoothctl first."
                    )
                log.warning("Connect failed: %s", output.strip())
                time.sleep(2)
            else:
                raise BluetoothSpeakerError(
                    f"Could not connect to {self.mac}. Is it switched on, in range, "
                    f"and not connected to another device (e.g. your phone)?"
                )

        if not self._wait_for(lambda: self.find_sink() is not None, timeout=10):
            raise BluetoothSpeakerError(
                "Connected, but no audio output appeared for the speaker. "
                "Is WirePlumber running?"
            )

    def ensure_connected(self) -> None:
        """Reconnect if the link dropped. Cheap when everything is fine."""
        _trace("ensure_connected")
        if not self.is_ready():
            self.connect()

    def disconnect(self) -> None:
        self.close(disconnect=True)

    # ------------------------------------------------------------------- volume
    def set_volume(self, level: float) -> None:
        """Set the speaker's output volume (0.0 - 1.0) and unmute it."""
        sink = self.find_sink()
        if sink is None:
            raise BluetoothSpeakerError("Speaker audio output not found. Call connect() first.")
        level = max(0.0, min(1.0, level))
        self._run(["pactl", "set-sink-mute", sink, "0"])
        self._run(["pactl", "set-sink-volume", sink, f"{round(level * 100)}%"])

    # ------------------------------------------------------------------ playing
    @property
    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _with_lead_in(self, path: Path, seconds: float) -> Path:
        """Return a copy of a WAV file with silence added at the start."""
        if seconds <= 0:
            return path
        try:
            with wave.open(str(path)) as src:
                params = src.getparams()
                frames = src.readframes(src.getnframes())
        except (wave.Error, EOFError):
            log.warning("Not a plain PCM WAV file, skipping lead-in silence.")
            return path

        silence = b"\x00" * (
            int(params.framerate * seconds) * params.nchannels * params.sampwidth
        )
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="btspeaker_")
        os.close(fd)
        with wave.open(tmp, "wb") as dst:
            dst.setparams(params)
            dst.writeframes(silence + frames)
        self._temp_file = Path(tmp)
        return self._temp_file

    def _cleanup_temp(self) -> None:
        if self._temp_file is not None:
            try:
                self._temp_file.unlink()
            except FileNotFoundError:
                pass
            self._temp_file = None

    def play(
        self,
        path: Union[str, Path],
        blocking: bool = True,
        volume: Optional[float] = None,
    ) -> None:
        """
        Play an audio file on the speaker.
        blocking=True   returns when playback has finished.
        blocking=False  returns immediately; use is_playing, wait() and stop().
        """
        path = Path(path)
        _trace("play", path, "blocking=", blocking)
        if not path.exists():
            raise BluetoothSpeakerError(f"File not found: {path}")
        if self.is_playing:
            raise BluetoothSpeakerError("Already playing. Call stop() first.")

        self.ensure_connected()
        sink = self.find_sink()
        self.set_volume(self.volume if volume is None else volume)

        play_path = self._with_lead_in(path, self.lead_in)
        self._stopped_by_us = False
        self._proc = subprocess.Popen(
            ["paplay", "--device", sink, str(play_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if blocking:
            self.wait()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for playback to finish. Returns True if it played to the end,
        False if it was stopped or timed out. Raises if playback failed.
        """
        _trace("wait")
        proc = self._proc
        if proc is None:
            return False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False

        try:
            err = proc.stderr.read().strip() if proc.stderr else ""
        except ValueError:
            err = ""
        if proc.stderr:
            try:
                proc.stderr.close()
            except ValueError:
                pass
        self._proc = None
        self._cleanup_temp()

        if self._stopped_by_us:
            return False
        if proc.returncode != 0:
            raise BluetoothSpeakerError(f"Playback failed (exit {proc.returncode}): {err}")
        return True

    def stop(self) -> None:
        """Stop playback immediately. Safe to call when nothing is playing."""
        _trace("stop")
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            self._stopped_by_us = True
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stderr:
            proc.stderr.close()
        self._proc = None
        self._cleanup_temp()

    # ----------------------------------------------------------------- shutdown
    def kill_stale_players(self) -> None:
        """Stop players left over from a previous run that was killed."""
        _trace("kill_stale_players")
        if self.is_playing:
            return
        mac_pattern = self.mac.replace(":", "[_:]")  # either name format
        self._run(["pkill", "-TERM", "-f", f"paplay --device bluez_output\\.{mac_pattern}"])

    def close(self, disconnect: bool = False) -> None:
        """Stop playback and optionally drop the connection. Safe to call twice."""
        try:
            self.stop()
        finally:
            if disconnect:
                self._run(["bluetoothctl", "disconnect", self.mac])

    def install_shutdown_handlers(self, disconnect: bool = False) -> None:
        """
        Call once at startup. Ctrl+C and `kill` then exit cleanly, with
        playback stopped. Must be called from the main thread.
        """
        atexit.register(self.close, disconnect)

        def handler(signum, frame):
            log.info("Signal %s received, shutting down", signum)
            sys.exit(128 + signum)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "paired": self.is_paired(),
            "connected": self.is_connected(),
            "sink": self.find_sink(),
            "playing": self.is_playing,
        }

    def __enter__(self) -> "BluetoothSpeaker":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


if __name__ == "__main__":
    speaker = BluetoothSpeaker("C4:38:75:C2:F2:00")
    speaker.install_shutdown_handlers()
    if speaker.connect():
        print("Connected.")
    print("Status:", speaker.status())
    print("Playing")
    speaker.play("/home/svend/Techathon/Harald_phone/school.mp3", volume=0.3)
    #speaker.play("/home/svend/Techathon/Harald_phone/recordings/rec_20261114_160658.wav", volume=0.5)
    print("Done.")
