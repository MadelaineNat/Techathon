# Task: debug and harden the Gemini pipeline of the Harald phone (429 on TTS, 400 on transcription)

Work in the existing codebase. Read the code first, then investigate and fix the issues below. Do not rewrite the threading architecture. It was recently restructured and the overlapping-audio problem is solved, so keep it. Where this description differs from the code, trust the code and tell me.

## 1. Context

"Harald Bluetooth phone" is a Raspberry Pi 5 interactive installation. BLE beacons (`HB-<id>`) make the phone ring. A visitor presses `#` to answer, and Harald plays a pre-recorded talk through a Bluetooth speaker. The visitor holds `*` to ask a question using Bluetooth earbuds as the microphone.

The question pipeline is: record (`parecord` → `harald_question.wav`) → upload to the Gemini Files API → transcribe (Interactions API) → chat (generateContent via a chat session) → text-to-speech (Interactions API, model `gemini-3.8-flash-lite-tts`, voice "Charon") → play on the speaker. The model names are intentional, so do not change them without verifying.

Current architecture (keep it):
- The main thread owns the state machine, keypad and OLED.
- A single audio thread owns the Bluetooth speaker and mic.
- The scanner runs in its own thread.
- Short-lived Gemini worker threads do network calls only.
- A cancel token drops stale work.

Relevant code:
- `Harald.reply` (upload + transcribe + chat)
- `Harald.synthesize` (TTS)
- `Phone._answer_worker`
- `Phone._on_failed`
- `AudioWorker.handle` (the `record_start` and `record_stop` branches)
- `RECORDING_PATH` and `write_temp_wav`
- `BluetoothSpeaker._with_lead_in` in `player.py`

## 2. Symptoms from the field (Danish quotes translated)

1. "He did not say the answer out loud, although the answer appeared in the terminal."
2. "On the second attempt at a question, an error appeared before the answer came."

## 3. Evidence from the log (condensed)

**First question:**
- 19:13:21 file upload (2 HTTP requests, about 1.7 s).
- 19:13:26 `POST /v1beta/interactions` → 200 (transcription OK).
- 19:13:28 `POST .../gemini-3.5-flash-lite:generateContent` → 200.
- 19:13:28.109 log line: "Visitor: Hello Harold | Harald: Greetings, traveler! ..." (so the answer text exists).
- **19:13:28.385 `POST /v1beta/interactions` → 429 Too Many Requests.** This is the TTS call, made 276 ms after the chat response.
- No "Could not answer question" traceback follows, which the `except` in `_answer_worker` should log. The next log lines are 53 s later and unrelated. Either the SDK is retrying 429 internally and silently, blocking that worker for a long time, or the traceback is missing from the paste. Find out which.

**Second question:**
- 19:15:03 file upload OK (2 requests).
- 19:15:06.649 `POST /v1beta/interactions` → **400 Bad Request, "Request contains an invalid argument." (`invalid_request`)**. This is the TRANSCRIPTION call (`Harald.reply`), not TTS.
- The identical call structure succeeded for the first question, so the difference must be in the content: the uploaded audio file, its state, or its mime type.

**Other log lines:** "WARNING Not a plain PCM WAV file, skipping lead-in silence." appears for several plays. That is expected for the ringtone `.mp3`, but at least one of them is probably an exhibition `.wav`, whose first second may then be clipped on the Sonos because the lead-in silence is skipped.

## 4. Diagnosis (hypotheses to verify, not conclusions)

### 4A. No spoken answer: 429 on the TTS call
- A 429 within 300 ms of the previous call is a quota or rate limit. The TTS model likely has a very low free-tier limit (requests per minute or per day), or the API key is shared with other teams or devices.
- Each question currently makes about 5 HTTP requests (2 upload requests + transcription + chat + TTS), and every one of them counts toward limits.
- When TTS fails, Harald says nothing, and the OLED shows `"Error: <raw exception text>"[:21]`, which is useless on a 21-character screen.

### 4B. 400 on transcription: ranked hypotheses
1. **Bad or corrupt WAV.** An improperly finalized WAV (for example `parecord` killed instead of stopped cleanly, so the header sizes are wrong), a header-only or empty file, or a file being overwritten while it is uploaded. `RECORDING_PATH` is the same path every time, and `*` is allowed during PROCESSING, so a new recording can truncate the file while an old worker is still reading it.
2. **The uploaded file was not yet in state ACTIVE** when it was referenced.
3. **Odd mime type or missing fields** on the uploaded file object (for example the server sniffed `application/octet-stream` because of a bad header).
4. The earbuds' HFP microphone delivering almost no audio in the first moments after the profile switch, giving a technically valid but degenerate recording.

Determine which one it is from evidence (section 5), not by guessing.

## 5. Work items

### 5.1 Instrument first (do this before fixing anything)
- Log, with durations, every Gemini step: upload, transcribe, chat, TTS. Include the thread name and a per-question id.
- On any API error, log the exception type, HTTP status, and the full response body. A 429 body usually names the quota metric and a `retryDelay`.
- Log the uploaded file object's `name`, `state`, `mime_type` and `size_bytes`.
- Before upload, log the recording's actual properties: size, WAV header validity, duration, sample rate, channels, and RMS level (to detect silence).
- Keep recordings for post-mortem. Use a unique filename per recording (timestamp), and keep only the last N (for example 10) so the disk does not fill up. This also removes the shared-path race.
- Tell me exactly how to re-run a saved failing recording through the transcription call in isolation, to prove or rule out hypothesis 1.

### 5.2 TTS 429 handling
- Determine the real quota for the TTS model (from the 429 body and the project's quota page) and report it.
- The SDK call must not be able to block silently. Set explicit timeouts and a small bounded retry policy on the client. Honor a short `retryDelay` if the server gives one, and give up after a hard overall budget (for example about 10 s).
- When TTS fails after retries, Harald must still say something, so the visitor never sits in silence. Options, in order of preference (verify feasibility on the Pi and pick one with me):
  - (a) a small set of pre-generated fallback clips, such as "I am lost for words, ask again in a moment";
  - (b) a local offline TTS on the Pi (for example Piper, if Danish and English voices are available and fast enough) for the fallback.
- Offer a way to use a separate API key per device through an env var. If the cause is a shared key, say so.
- Show short, human-readable statuses on the OLED (21 characters max), mapped from the exception class and status code. Examples: "Rate limit, wait", "Bad audio, retry", "No network". Never show raw exception text.

### 5.3 Transcription 400 handling
- Validate the recording before uploading. If it is empty or silent, show "Didn't hear you" without calling the API.
- Preferred fix: send the (small) audio inline instead of via the Files API. That removes the upload round trips (about 2 s of latency), the ACTIVE-state race, and the file-lifecycle problems. Check the SDK docs and the installed version to confirm the exact field for inline audio in the Interactions API and in `generateContent`. Do not guess field names.
- If the Files API is kept: poll until the file is ACTIVE before use, and delete the uploaded file afterwards.
- On a 400 or 5xx from transcription, retry once with a fresh upload (or fresh inline bytes). On repeated failure, show a clear status and return to the "Hold * to ask" state.

### 5.4 Fewer API calls and lower latency
- Today the time from release to the answer text is about 6.6 s before TTS even starts. Evaluate merging transcription and chat into one multimodal call (audio in, answer text out). That removes a request and its failure mode, and lowers the 429 risk.
- Keep conversational memory without storing raw audio in the chat history forever. Compare the trade-offs and recommend one option.
- Report measured per-step timings before and after.

### 5.5 Recorder robustness (`voice_recorder.py` / the audio thread)
- Ensure the recorder is stopped cleanly so the WAV header is finalized. If the recorder had already exited, or had to be killed, treat the recording as failed, not as a valid file.
- Do not treat a degenerate recording (very short, or near-silent) as a question. Consider whether the HFP link needs a brief warm-up, and whether the status should tell the visitor when to start speaking ("Listening..." only once audio is actually flowing).
- Simplify the `record_start` retry logic. It currently has a convoluted loop (an `ensure_connected`, then an extra `connect` on failure, then a post-loop `last_error` check). Make the semantics explicit with identical behavior, without redundant reconnects.

### 5.6 Playback quality
- "Not a plain PCM WAV file" means the lead-in silence is skipped. Check every file that triggers it (the exhibition recordings, TTS output). Report which files are non-PCM and what their format is.
- For the exhibition recordings, recommend or perform a one-time conversion to plain 16-bit PCM WAV, with the exact command. Then confirm the first second of each talk is not clipped on the Bluetooth speaker.

### 5.7 Small things to clean up
- `*` is allowed during PROCESSING while a worker may still be reading the recording. Make that safe, using unique files per recording.
- If a question was already sent to the chat session and TTS then fails, the chat history now contains that question. Decide how retry should behave, so the visitor does not get duplicated context.

## 6. Constraints
- Keep the threading model: main thread for state and display, one audio thread for Bluetooth, queues between threads, token-based cancellation. Gemini threads must never touch Bluetooth or the display.
- Keep env variable names, the file layout and the imports. No secrets in logs or in screenshots of logs.
- Do not change the Gemini model names unless you have verified they are wrong, and tell me if you do.
- No new heavy dependencies. A local TTS fallback is allowed only if you confirm it runs fast enough on a Pi 5, and tell me what it adds.

## 7. Acceptance criteria
1. A mocked 429 from TTS leads to a bounded retry, then an audible fallback, and the OLED shows a short human-readable message. The phone returns to "Hold * to ask" and is not stuck.
2. A mocked 400 from transcription leads to one retry, then a clear status, with no stuck state.
3. 20 consecutive real question and answer cycles on the Pi. Report the failures, with the logged evidence for each.
4. Per-step timing is logged, and I can see where each second goes.
5. No stale answer plays after a hang-up or a new `*` press, and no recording file is overwritten while it is being uploaded.
6. `python -m py_compile` passes on the changed files.

## 8. What I expect back
- Root cause for each of the two failures, with the log evidence that proves it. Clearly mark anything you could not verify.
- A summary of the changes per file.
- The exact commands for any one-time setup (audio conversion, quota checks, key setup).
- A list of anything in the codebase that contradicted this description.