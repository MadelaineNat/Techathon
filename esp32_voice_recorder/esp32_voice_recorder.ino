/*
  ESP32 voice recorder with SY-M213 microphone sensor

  Hold the BOOT button (GPIO 0) to record, release to save.
  The recording is saved as a WAV file in flash (LittleFS) and can be
  played/downloaded from a web page served by the ESP32.

  Wiring:
    SY-M213 VCC  -> 3V3
    SY-M213 GND  -> GND
    SY-M213 AO   -> GPIO 34 (ADC1)

  Usage:
    1. Upload, open Serial Monitor at 115200 baud.
    2. Connect your phone/PC to Wi-Fi "ESP32-Recorder" (password: recorder123).
    3. Open http://192.168.4.1 in a browser.
    4. Hold the BOOT button, speak, release. Reload the page to play it.
*/

#include <WiFi.h>
#include <WebServer.h>
#include <LittleFS.h>

// ---------- Settings ----------
const int      BUTTON_PIN   = 9;      // built-in BOOT button (active LOW)
const int      MIC_PIN      = 6;     // SY-M213 analog output (ADC1 pin)
const uint32_t SAMPLE_RATE  = 8000;   // Hz
const uint32_t MAX_SECONDS  = 5;      // max recording length (limited by RAM)
const float    GAIN         = 2.0;    // increase if too quiet, decrease if distorted
const char*    AP_SSID      = "ESP32-Recorder";
const char*    AP_PASS      = "recorder123";   // min 8 characters
const char*    FILE_PATH    = "/recording.wav";
// ------------------------------

const uint32_t MAX_SAMPLES = SAMPLE_RATE * MAX_SECONDS;

int16_t*  samples = nullptr;
WebServer server(80);
uint32_t  recordingCount = 0;

void writeWavHeader(File &f, uint32_t numSamples) {
  uint32_t dataSize = numSamples * 2;
  uint32_t chunkSize = 36 + dataSize;
  uint32_t fmtSize = 16;
  uint16_t audioFormat = 1;      // PCM
  uint16_t channels = 1;
  uint32_t sampleRate = SAMPLE_RATE;
  uint32_t byteRate = SAMPLE_RATE * 2;
  uint16_t blockAlign = 2;
  uint16_t bitsPerSample = 16;

  f.write((const uint8_t*)"RIFF", 4);
  f.write((const uint8_t*)&chunkSize, 4);
  f.write((const uint8_t*)"WAVE", 4);
  f.write((const uint8_t*)"fmt ", 4);
  f.write((const uint8_t*)&fmtSize, 4);
  f.write((const uint8_t*)&audioFormat, 2);
  f.write((const uint8_t*)&channels, 2);
  f.write((const uint8_t*)&sampleRate, 4);
  f.write((const uint8_t*)&byteRate, 4);
  f.write((const uint8_t*)&blockAlign, 2);
  f.write((const uint8_t*)&bitsPerSample, 2);
  f.write((const uint8_t*)"data", 4);
  f.write((const uint8_t*)&dataSize, 4);
}

void saveRecording(uint32_t numSamples) {
  File f = LittleFS.open(FILE_PATH, FILE_WRITE);
  if (!f) {
    Serial.println("Could not open file for writing");
    return;
  }
  writeWavHeader(f, numSamples);
  f.write((const uint8_t*)samples, numSamples * 2);
  f.close();
  recordingCount++;
  Serial.printf("Saved %u samples (%.1f s)\n", numSamples, (float)numSamples / SAMPLE_RATE);
}

void recordWhileHeld() {
  Serial.println("Recording...");
#ifdef LED_BUILTIN
  digitalWrite(LED_BUILTIN, HIGH);
#endif

  // Estimate the DC offset of the mic (sits around mid-scale when quiet)
  float dc = 0;
  for (int i = 0; i < 200; i++) {
    dc += analogRead(MIC_PIN);
    delayMicroseconds(100);
  }
  dc /= 200.0f;

  const uint32_t period = 1000000UL / SAMPLE_RATE;
  uint32_t next = micros();
  uint32_t n = 0;

  while (digitalRead(BUTTON_PIN) == LOW && n < MAX_SAMPLES) {
    while ((int32_t)(micros() - next) < 0) { }   // wait for next sample time
    next += period;

    int raw = analogRead(MIC_PIN);               // 0..4095
    dc += (raw - dc) * 0.001f;                   // slowly track DC drift
    float v = (raw - dc) * 16.0f * GAIN;         // scale 12-bit -> 16-bit
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    samples[n++] = (int16_t)v;
  }

#ifdef LED_BUILTIN
  digitalWrite(LED_BUILTIN, LOW);
#endif

  if (n < SAMPLE_RATE / 10) {
    Serial.println("Too short, discarded");
    return;
  }
  saveRecording(n);
}

void handleRoot() {
  String html =
    "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>ESP32 Recorder</title></head>"
    "<body style='font-family:sans-serif;max-width:480px;margin:2em auto;padding:0 1em'>"
    "<h2>ESP32 Recorder</h2>"
    "<p>Hold the BOOT button on the ESP32 to record, release to save. Then reload this page.</p>";
  if (LittleFS.exists(FILE_PATH)) {
    html += "<audio controls src='/recording.wav?v=" + String(recordingCount) + "' style='width:100%'></audio>"
            "<p><a href='/recording.wav' download='recording.wav'>Download WAV</a></p>";
  } else {
    html += "<p><i>No recording yet.</i></p>";
  }
  html += "</body></html>";
  server.send(200, "text/html", html);
}

void handleWav() {
  File f = LittleFS.open(FILE_PATH, FILE_READ);
  if (!f) {
    server.send(404, "text/plain", "No recording yet");
    return;
  }
  server.streamFile(f, "audio/wav");
  f.close();
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
#ifdef LED_BUILTIN
  pinMode(LED_BUILTIN, OUTPUT);
#endif

  analogReadResolution(12);
  analogSetPinAttenuation(MIC_PIN, ADC_11db);

  samples = (int16_t*)malloc(MAX_SAMPLES * sizeof(int16_t));
  if (!samples) {
    Serial.println("Not enough RAM - lower MAX_SECONDS");
    while (true) delay(1000);
  }

  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS mount failed");
    while (true) delay(1000);
  }

  WiFi.softAP(AP_SSID, AP_PASS);
  server.on("/", handleRoot);
  server.on("/recording.wav", handleWav);
  server.begin();

  Serial.print("Wi-Fi: ");
  Serial.print(AP_SSID);
  Serial.print("  -> open http://");
  Serial.println(WiFi.softAPIP());
  Serial.println("Hold BOOT to record.");
}

void loop() {
  server.handleClient();
  if (digitalRead(BUTTON_PIN) == LOW) {
    delay(20);                                   // simple debounce
    if (digitalRead(BUTTON_PIN) == LOW) {
      recordWhileHeld();
      while (digitalRead(BUTTON_PIN) == LOW) delay(10);   // wait for release
    }
  }
}
