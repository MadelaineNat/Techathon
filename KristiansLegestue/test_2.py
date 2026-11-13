import base64
from google import genai
import os
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=os.getenv("GEMINI_API_KEY")
)

interaction = client.interactions.create(
    model="gemini-3.8-flash-lite-tts",
    input=[{
        "type": "user_input",
        "content": [{
            "type": "text",
            "text": """
Greetings, traveler!

I am Harald Bluetooth, King of Denmark and Norway!

I ruled Denmark during the tenth century, and I am remembered
as the king who united the Danes under one kingdom.

I was also responsible for bringing Christianity to Denmark,
a deed I proudly proclaimed upon the great Jelling Stone.

My father was King Gorm the Old, and my mother was Queen Thyra.
            """,
            "annotations": [{
                "type": "speech_metadata",
                "style": (
                    "Use a low, mature, resonant male voice for Harald Bluetooth, "
                    "with calm authority rather than a theatrical pirate voice. "
                    "Speak English with a restrained Danish and Scandinavian accent: "
                    "clear consonants, slightly rounded vowels, and natural Danish "
                    "prosody, while remaining easy for an English-speaking visitor "
                    "to understand. Keep the delivery measured and deliberate, as "
                    "if addressing an audience at a royal hall. Sound warm during "
                    "the greeting, grave when describing his duty as king, and "
                    "quietly proud when speaking of the Jelling Stone. Use brief, "
                    "natural pauses after each sentence. Do not use a modern radio "
                    "voice, an exaggerated Nordic accent, or a fantasy Viking growl."
                ),
            }],
        }],
    }],
    response_format={"type": "audio"},
    generation_config={
        "speech_config": [
            {"voice": "Charon"},
        ]
    },
)

with open("harald_bluetooth.wav", "wb") as f:
    f.write(base64.b64decode(interaction.output_audio.data))

print("Harald Bluetooth's greeting has been generated!")

# Print token usage and estimated cost
usage = interaction.usage

if usage:
    input_tokens = usage.total_input_tokens or 0
    output_tokens = usage.total_output_tokens or 0

    # Gemini 3.8 Flash TTS standard pricing (USD, through Dec 2026)
    input_cost = (input_tokens / 1_000_000) * 0.50
    output_cost = (output_tokens / 1_000_000) * 9.00

    # Convert to DKK
    input_cost *= 7.0
    output_cost *= 7.0

    total_cost = input_cost + output_cost

    print("\n--- Gemini API Usage ---")
    print(f"Input tokens:  {input_tokens:,}")
    print(f"Output tokens: {output_tokens:,}")
    print(f"Input cost:    {input_cost:.6f} DKK")
    print(f"Output cost:   {output_cost:.6f} DKK")
    print(f"Total cost:    {total_cost:.6f} DKK")
else:
    print("Token usage information unavailable.")