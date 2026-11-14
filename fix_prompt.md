# Task: fix the Harald phone (Raspberry Pi 5): choppy audio, thread contention, keypad and call-flow bugs

You are working in the codebase of a hackathon project. Read the relevant files first, then make the changes below. Where the codebase differs from my description, trust the codebase and tell me what differed. If a file named `phone_fixed.py` exists in the repo, it is a reference implementation of the design in section 7. Use it as a starting point, but check it against the real code, because it has never been run on the hardware.

## 1. Project overview

"Harald Bluetooth phone" is an interactive museum-style installation. A visitor walks a corridor holding a phone-shaped "horn". Each installation has an ESP32 that only advertises a BLE name `HB-<id>` (for example `HB-3`). The horn (a Raspberry Pi 5) scans for these beacons and uses smoothed RSSI to decide which installation the visitor is near.

When the visitor enters an installation's zone, the phone rings. The visitor presses `#` to answer, and King Harald Bluetooth then talks about that installation through a Bluetooth speaker. The visitor can hold `*` to ask Harald a question using Bluetooth earbuds as the microphone. Gemini handles transcription, chat and text-to-speech, and Harald answers through the speaker. Visitors can also dial `656` and `#` to call Harald directly.

## 2. Hardware and environment

- Raspberry Pi 5, Raspberry Pi OS (Bookworm), PipeWire + WirePlumber + BlueZ, Python 3.12. It runs directly on the host, not in Docker.
- 4x4 keypad matrix on GPIO via `gpiozero`: rows `[14, 15, 18, 23]` are driven as `LED` outputs, columns `[24, 25, 8, 7]` are `Button` inputs (default pull-up).
- SSD1306 OLED, 128x64, I2C bus 1, address 0x3C, via `luma.oled` and PIL.
- LEDs: GPIO 17 = earbuds connected, GPIO 22 = speaker connected, GPIO 27 = recording.
- ESP32 beacons advertise `HB-<id>` with BLE, reduced TX power.
- Bluetooth speaker (Sonos Roam), played with `paplay` through PipeWire, A2DP.
- Bluetooth earbuds used only as a microphone, recorded with `parecord` in headset (HFP) profile.
- The Pi's single onboard Bluetooth radio is shared by A2DP streaming, the HFP link, BLE scanning and 2.4 GHz WiFi coexistence.

## 3. Code map (adjust to the actual repo)

- **Main phone script** (contains `CallState`, `Harald`, `Phone`): keypad, OLED, call state machine, threads, Gemini calls.
- **`player.py` → `BluetoothSpeaker`.** API: `connect()`, `ensure_connected()`, `is_connected()`, `is_ready()`, `find_sink()`, `play(path, blocking=True, volume=None)`, `wait()`, `stop()`, `is_playing`, `set_volume()`, `kill_stale_players()`, `close()`.
  - It holds single-process state (`_proc`, `_temp_file`, `_stopped_by_us`), so it is NOT thread-safe.
  - `connect()` calls `kill_stale_players()`, which runs `pkill` on paplay processes.
  - `stop()` closes `proc.stderr`, while a concurrent `wait()` in another thread reads it (`ValueError`).
  - `play()` calls `ensure_connected()` every time, which spawns `bluetoothctl` and `pactl`.
- **`voice_recorder.py` → `BluetoothMic`.** API: `connect()`, `ensure_connected()`, `is_connected()`, `is_ready()`, `find_source()`, `set_headset_profile()`, `start_recording(path)`, `stop_recording()`, `recording_seconds`, `close()`.
  - `connect()` forces the earbuds into a headset profile if the mic source is missing.
- **`ble_scanner2.py` → `Scanner(on_enter=None, on_leave=None, verbose=False)`.** `await scanner.run()` scans with bleak, smooths RSSI per beacon, and fires `on_enter(id)` / `on_leave(id)` once per zone entry or exit (NEAR -60 dBm, LEAVE -75 dBm hysteresis, 5 dB margin over the runner-up). It keeps internal state, so a new instance forgets which zone it was in.
- **`data/`**: `harald_prompt.txt`, `harald_voice.txt`, `exhibition_texts/exhibition_*.txt`, `recordings/school.mp3` (ringtone).
- **`.env`** variables: `GEMINI_API_KEY`, `AIRPODS_MAC`, `SPEAKER_MAC`, `SPEAKER_VOLUME`, `SPEAKER_LEAD_IN`, `HARALD_CHAT_MODEL`, `HARALD_TRANSCRIBE_MODEL`, `HARALD_TTS_MODEL`, `LOG_LEVEL`.

## 4. Intended behavior

| State | Entered when | Keys |
|---|---|---|
| IDLE | startup / hang-up | digits dial, `C`/`D` clear, `#` dials. `656` connects to Harald |
| RINGING | beacon entered while IDLE | `#` answer, `D` decline. Looping ringtone, 25 s timeout = missed call |
| CONNECTED | answered, or dialed 656 | hold `*` = ask, `D` = hang up |
| PROCESSING | question recorded | `*` asks again, `D` hang up |

- Answering plays Harald's talk for that installation. Holding `*` stops Harald, records the question, then plays the answer.
- Hanging up (`D`) returns to IDLE, stops all audio and cancels pending work.
- After a call, the same beacon must not ring again for about 60 s, but it must ring again after that or when the visitor re-enters from a different zone.

## 5. Symptoms

"The threads compete too much. The voice goes in and out. Basically it does not work."

## 6. Diagnosis

### 6A. Bugs confirmed by reading the code

1. **`star_is_pressed()` has the wrong polarity.**
   - The keypad scan works inversely. It drives ONE row high at a time and the rest low, with the columns pulled up. A column reads `is_pressed` (low) for every row except the row of the pressed key, because that row's driver is low and shorts the column to it.
   - `scan_key` collects 3 such rows and infers the missing 4th row as the pressed one.
   - `star_is_pressed()` drives only row 3 high and reads column 0. That reads False when `*` is held and True when 1/4/7 are held, so recording stops almost immediately.
   - If you need a direct check, drive rows 0-2 high and row 3 low and read column 0. Better still, use the full scan with debounce (see 7.4).
2. **There is no hang-up.** `D` in CONNECTED only clears the dial buffer, and state never returns to IDLE. Beacon events are ignored unless IDLE, so after the first answered call the phone never rings again.
3. **`*` does not stop Harald**, so the mic records his voice. Talk and answer playback can queue behind each other.
4. **The OLED is drawn from multiple threads** (`_set_status` / `update_display` are called from workers). `luma` I2C is not thread-safe, which causes garbled frames and `OSError` spam. `self.state` is also mutated from several threads.
5. **`_ask_question` runs in the main thread** and blocks the keypad/OLED loop for as long as `*` is held.
6. **`BluetoothSpeaker` is used from 5 threads**: the ringtone thread, talk thread, answer thread, the main thread's `stop()`, and the monitor thread's `is_ready()` / `connect()`.
   - `stop()` closes `proc.stderr` while another thread's `wait()` reads it, giving an uncaught `ValueError`.
   - `_stopped_by_us` is shared, so a stop followed by a new play can yield spurious "Playback failed (exit -15)".
   - `connect()` → `kill_stale_players()` can kill a running paplay.
7. **`harald_response.wav` is shared** by the talk path and the answer path, so concurrent writers can corrupt playback.
8. **Ringtone problems.** The ringtone thread holds `audio_lock` for the whole song length. It plays once (no loop), has no timeout, and there is no missed-call handling.
9. **The monitor thread spawns many subprocesses.** `_monitor_audio` calls `is_ready()` on both devices (called twice per device per loop), so it runs up to 8 `bluetoothctl` / `pactl` processes every 2 s. It also retries `mic.connect()` every 10 s when the mic source is missing, and that forces the headset profile.
10. **Talks are synthesized on demand during the call**, which adds latency and network traffic while audio is playing. The talk TTS path omits the voice style annotation, so the talk and answer voices differ.
11. **Minor:** debug `print(mac)` calls in `_make_mic` / `_make_speaker`, and `Scanner(verbose=True)` floods the log.

### 6B. Likely cause of the voice dropping in and out (hypotheses, verify)

Python threads and the GIL cannot make a separate `paplay` process stutter. Dropouts come from (a) playback being killed, restarted or overlapped by our code (items 3, 6, 7 above), or (b) radio or PipeWire contention.

- **Periodic profile flipping.** WirePlumber auto-switches Bluetooth profiles. The earbuds drop back to A2DP when nothing is recording, so `find_source()` often returns None. The monitor then runs `mic.connect()`, which forces HFP again. Each switch sets up an SCO/HFP link on the same radio that is streaming A2DP to the speaker.
- **BLE scanning in parallel.** A continuous BLE scan with duplicate reporting on the same radio as an A2DP stream is a known cause of stutter.
- **WiFi.** Gemini upload and download traffic shares the 2.4 GHz antenna.

Verify with timestamped logging (see 9) before concluding. Add `LOG_LEVEL=DEBUG` output for every speaker play/stop, profile switch, connect, and `pkill`, with the thread name and monotonic time.

## 7. Required design

### 7.1 Threading model

- **Main thread**: keypad polling, the call state machine, and ALL OLED drawing. It is the only thread that reads or writes call state.
- **One audio thread**: the ONLY code that touches `BluetoothSpeaker` and `BluetoothMic`. All other threads send it commands through a queue.
- **Scanner thread**: asyncio loop running `Scanner`. It only puts events on the main queue.
- **Short-lived Gemini worker threads**: network calls only. They never touch Bluetooth, state or the display. They write a unique temp wav and enqueue it for playback.
- Threads communicate only through queues (`SimpleQueue`). No shared mutable state besides a lock-guarded integer token.

### 7.2 Cancellation token

- The audio worker keeps an integer token. `cancel()` increments it (under a lock), enqueues a "cancel" command, and returns the new token.
- Every piece of work (talk synthesis, answer synthesis, play commands, recording) carries the token it was started under. Before each expensive step, and in the audio thread before playing, compare against the current token. Stale work is dropped and its temp file deleted.
- `cancel()` is called on: ringing starts, answer, hang-up, decline, missed call, and `*` pressed (it silences Harald and invalidates any pending answer).

### 7.3 Audio worker commands and events

- **Commands:** `cancel`, `play(path, token, tag, volume, delete)`, `record_start(token)`, `record_stop(token)`.
- **Events back to main:** `bt (mic_ok, speaker_ok)`, `recording(token)`, `record_error(msg)`, `recorded(token, path, seconds)`, `audio_done(tag)` (only on natural completion, never on cancel), `audio_error(tag, msg)`.
- The worker loop uses `queue.get(timeout=0.05)` and polls `speaker.is_playing` itself, instead of using blocking `play`. A new `play` or `cancel` can therefore always interrupt, and every speaker call happens in this one thread.
- `play` is dropped if its token is stale or a recording is active. It stops any current playback first.
- `record_start`: `cancel` playback, `mic.ensure_connected()`, `mic.start_recording(RECORDING_PATH)`, emit `recording`. Do the reconnect-once-and-retry logic here.
- `record_stop`: ignore if not recording. Otherwise read `mic.recording_seconds`, call `mic.stop_recording()`, emit `recorded`.
- **No background reconnect storms.** The mic is connected right before recording (`ensure_connected`). The speaker reconnects inside `play()`. Do the initial connect of both devices once at startup inside the audio thread.
- **Housekeeping:** every ~5 s, and ONLY when nothing is playing or recording, call `is_connected()` (one `bluetoothctl` call) on each device and emit `bt` if it changed. The main thread drives the green LEDs from these events.

### 7.4 Keypad

- Keep the existing matrix-scan logic (including its inverted polarity). Use a local set per scan instead of an instance-level set that persists.
- Add `poll_keys()` returning `("press", key)` / `("release", key)` events with release debounce: the key must read None for 3 consecutive scans before a release is reported. This replaces the blocking `wait_for_key_release()` and `star_is_pressed()`.
- Push-to-talk: `*` press → `_start_question`, `*` release → `audio.stop_recording(question_token)`.

### 7.5 Display

- Draw only from the main thread, via a `_render()` called once per loop iteration.
- Skip redraws when `(state, status, dial_buffer, beacon_id)` is unchanged.
- On `OSError`, back off about 1 s instead of retrying every loop.

### 7.6 Gemini and TTS

- Split `Harald.answer()` into `reply(audio_path) -> (question, text)` (STT + chat, chat guarded by a lock) and `synthesize(text) -> wav bytes`. Use the voice style annotation for both talks and answers.
- Cache synthesized talks in memory, and prefetch all talks in a background thread at startup. Calls then need no network for the talk.
- Each playback uses its own `NamedTemporaryFile(delete=False)` wav, deleted after playback or on cancel. Never share `harald_response.wav`.
- Keep the existing model env vars and model names as they are. They are intentional.

### 7.7 Beacon scanning

- Run the scanner only while IDLE (constant `SCAN_ONLY_WHEN_IDLE = True`, to reduce radio contention). When not idle, cancel the scanner task. When idle again, create a NEW `Scanner`.
- Because a new `Scanner` has no memory of the current zone, add a cooldown: remember the last call's beacon id and ignore `on_enter` for that id for `RECALL_COOLDOWN_S = 60`.
- Set `Scanner(verbose=False)`. If the scanner task dies, log the exception and retry after 3 s instead of spinning.

### 7.8 State machine and ringing

- Ring = looping ringtone (re-`play` on `audio_done("ring")` while still RINGING), blinking OLED, and a 25 s timeout to "Missed call".
- Hang-up, decline and missed call all go through one `_go_idle(status)` that: records the cooldown, calls `audio.cancel()`, and clears `beacon_id`, `dial_buffer`, `star_down` and `recording`.
- The recording LED (GPIO 27) follows the real recorder state (the `recording` event), not the button state.

## 8. Constraints (do not break)

- Do not change GPIO pin numbers, the keypad matrix logic, or the OLED address.
- Keep the env variable names, the file layout, and the imports (`Harald_phone.player`, `Harald_phone.voice_recorder`, `Kristoffer.ble_scanner2`).
- No new dependencies. Python 3.12 and the existing libraries only.
- Edit `player.py`, `voice_recorder.py` and `ble_scanner2.py` minimally. A defensive fix to `BluetoothSpeaker.wait()` is acceptable (catch the closed-stderr `ValueError`), but with the single-audio-thread design it should no longer be hit.
- Do not add a Docker setup. It runs on the host.
- Do not invent APIs. If an API is unclear, read the source.

## 9. Verification and acceptance criteria

Add timestamped DEBUG logging with thread names for every speaker/mic operation. Where possible, test with a mocked speaker and mic first (no hardware). Then verify on the Pi:

1. Ring → answer → a talk plays continuously, with no dropouts for 60 s or more.
2. Pressing `*` during a talk stops Harald within ~100-200 ms, then records. Release leads to Thinking, then the answer plays.
3. Hold `*` shorter than 0.7 s → "Too short", no Gemini call.
4. `D` hangs up from any state. The next beacon rings again after the cooldown.
5. Rapid answer / decline / hang-up / `*` sequences leave no orphaned `paplay` or `parecord` processes (check with `pgrep`), no tracebacks, and no stale answers playing after a hang-up.
6. A debug-mode assertion confirms that only the `audio` thread calls speaker and mic methods, and that only the main thread draws the OLED.
7. Compile check: `python -m py_compile` on all changed files.

If it is still choppy, run these experiments in order and report each result:

1. Set `SCAN_ONLY_WHEN_IDLE = False`.
2. Run with the earbuds disconnected.
3. If the speaker is clean without the earbuds, the HFP/A2DP link sharing the radio is the cause. Report that instead of working around it in code.

Mitigations, in order of effort:

- Use Ethernet or 5 GHz WiFi.
- Use a USB Bluetooth dongle so the speaker and earbuds are on separate radios.
- Replace the earbuds mic with a wired USB mic, which removes HFP and profile switching.
- Optionally lower the BlueZ LE scan duty cycle.

## 10. What I expect back

- The changed files, with a short summary per file.
- A list of which diagnosed items you confirmed, which you could not verify, and why.
- Anything in the codebase that contradicted this description.
- The exact commands to run the result and the logging to watch while testing.