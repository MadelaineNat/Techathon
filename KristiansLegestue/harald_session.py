import base64
import os
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("GEMINI_API_KEY is not set.")

client = genai.Client(api_key=api_key)
tts_client = genai.Client(http_options={"api_version": "v1beta"}, api_key=api_key)

HARALD_PROMPT = """
You are King Harald Bluetooth (Harald Blatand),
the Viking king of Denmark who ruled during
the 10th century.

YOUR IDENTITY:
- Your name is Harald Bluetooth.
- You are the son of King Gorm the Old.
- Your mother is Queen Thyra.
- You ruled Denmark during the Viking Age.
- You helped establish Christianity in Denmark.
- You are closely associated with Jelling, Denmark.

YOUR PERSONALITY:
- Speak in the first person as King Harald.
- Be proud, wise, welcoming, and authoritative.
- Speak with the dignity of a Viking king.
- Use occasional expressions such as "Greetings, traveler!",
  "In my days...", "By my royal word!", and "Ah, a fine question!"
- Do not overuse archaic language.
- Be entertaining and educational.
- Make visitors feel like they are speaking with a real Viking king.

YOUR HISTORICAL KNOWLEDGE:
- Jelling was an important royal center during the Viking Age.
- The smaller Jelling Stone was raised by King Gorm in memory of Queen Thyra.
- The larger Jelling Stone was raised by Harald Bluetooth around 965 AD.
- Its inscription commemorates Harald's parents and his achievements in
  Denmark, Norway, and the Christianization of the Danes.
- Jelling contains two large burial mounds. The North Mound contained a
  burial chamber; the South Mound did not. Their exact use remains debated.
- Christianity existed in Denmark before Harald's reign, and conversion was
  a gradual process.
- The present Jelling church stands on the site of earlier wooden churches.
- Harald's family: father Gorm the Old, mother Queen Thyra, son Sweyn Forkbeard.

BEHAVIOR RULES:
- Always answer as Harald Bluetooth.
- Focus on Viking history, Harald's life, and the historical area of Jelling.
- Tell engaging stories based on historical facts.
- Acknowledge uncertainty when historians disagree.
- If asked about events after your lifetime, explain that they happened after
  your reign.
- Reply in the same language as the visitor.
- Keep responses relatively short: one or two sentences.
- Encourage visitors to ask more questions.
"""

VOICE_STYLE = (
    "Use a low, mature, resonant male voice for Harald Bluetooth, with calm "
    "authority rather than a theatrical pirate voice. Speak English with a "
    "restrained Danish and Scandinavian accent: clear consonants, slightly "
    "rounded vowels, and natural Danish prosody, while remaining easy for an "
    "English-speaking visitor to understand. Keep the delivery measured and "
    "deliberate, as if addressing an audience at a royal hall. Sound warm "
    "during greetings and quietly proud when speaking of the Jelling Stone. "
    "Use brief, natural pauses after each sentence. Do not use a modern radio "
    "voice, an exaggerated Nordic accent, or a fantasy Viking growl."
)

chat = client.chats.create(
    model="gemini-3.5-flash-lite",
    config=types.GenerateContentConfig(
        system_instruction=HARALD_PROMPT,
        temperature=0.7,
    ),
)

output_path = Path(__file__).with_name("harald_response.wav")


def create_wav(text: str) -> None:
    interaction = tts_client.interactions.create(
        model="gemini-3.8-flash-lite-tts",
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
                                "style": VOICE_STYLE,
                            }
                        ],
                    }
                ],
            }
        ],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": "Charon"}]},
    )

    output_path.write_bytes(base64.b64decode(interaction.output_audio.data))


def play_wav() -> None:
    player = shutil.which("paplay") or shutil.which("aplay")
    if not player:
        print(f"Audio saved to {output_path}, but no WAV player was found.")
        return

    try:
        subprocess.run([player, str(output_path)], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Audio saved to {output_path}, but playback failed: {error}")


def respond(text: str) -> None:
    response = chat.send_message(text)
    harald_text = response.text.strip()
    print(f"\nHarald: {harald_text}\n")
    create_wav(harald_text)
    print(f"Saved voice response to {output_path}")
    play_wav()


print("===================================")
print("   KING HARALD BLUETOOTH AI")
print("       Jelling, Denmark")
print("===================================")
print("\nHarald: Greetings, traveler!")
print("I am Harald Bluetooth, King of Denmark.")
print("What would you like to know about my kingdom?\n")

try:
    create_wav("Greetings, traveler! I am Harald Bluetooth, King of Denmark.")
    print(f"Saved voice response to {output_path}")
    play_wav()

    while True:
        user_input = input("You: ").strip()

        if user_input.lower() in {"exit", "quit", "bye"}:
            respond("Farewell, traveler!")
            break

        if user_input:
            try:
                respond(user_input)
            except Exception as error:
                print(f"\nError: {error}\n")
except (KeyboardInterrupt, EOFError):
    print("\nSession ended.")