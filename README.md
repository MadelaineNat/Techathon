# Techathon
This is Harald's disciple's (aka svends disciple) contribution to Techathon 2026

## Harald phone

Run the combined Raspberry Pi application with:

```bash
export GEMINI_API_KEY="..."
export AIRPODS_MAC="AA:BB:CC:DD:EE:FF"
export SPEAKER_MAC="11:22:33:44:55:66"
python3 phone.py
```

`green1` indicates the AirPods microphone is connected, `green2` indicates the
Bluetooth speaker is connected, and `red` indicates that a question is being
recorded or processed. A nearby `HB-*` beacon rings with
`Harald_phone/data/recordings/school.mp3`; press `#` to answer or `D` to
decline. During a call, hold `*` to ask a question. Dial `656` and press `#`
to start a call with Harald.
