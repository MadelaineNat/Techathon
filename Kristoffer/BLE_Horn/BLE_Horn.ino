
#include <NimBLEDevice.h>

// ---------- Tuning (calibrate on-site!) ----------
#define MAX_ID            10     // supports installations 1..MAX_ID
#define NEAR_DBM          -60    // smoothed RSSI above this = "near"
#define LEAVE_DBM         -75    // must fall below this to count as "left" (hysteresis)
#define WIN_MARGIN_DB     5      // strongest beacon must beat runner-up by this much
#define EMA_ALPHA         0.3f   // smoothing: higher = faster but noisier
#define STALE_MS          3000   // beacon not heard for this long = gone

struct Beacon {
  volatile float rssi;           // smoothed RSSI
  volatile uint32_t lastSeen;    // millis() of last advertisement
  volatile bool seen;
};

Beacon beacons[MAX_ID + 1];
int activeId = 0;                // installation we're currently "in" (0 = none)

// ---------- BLE scan callback (runs in the BLE task) ----------
class ScanCB : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    if (!dev->haveName()) return;
    std::string name = dev->getName();
    if (name.rfind("HB-", 0) != 0) return;       // must start with "HB-"

    int id = atoi(name.c_str() + 3);
    if (id < 1 || id > MAX_ID) return;

    float r = (float)dev->getRSSI();
    Beacon& b = beacons[id];
    if (!b.seen) {
      b.rssi = r;
      b.seen = true;
    } else {
      b.rssi = EMA_ALPHA * r + (1.0f - EMA_ALPHA) * b.rssi;
    }
    b.lastSeen = millis();
  }
} scanCB;

// ---------- LED ----------
void blinkTimes(int n) {
  for (int i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH);
    delay(200);
    digitalWrite(LED_BUILTIN, LOW);
    delay(250);
  }
}

bool isFresh(int id) {
  return beacons[id].seen && (millis() - beacons[id].lastSeen) < STALE_MS;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  NimBLEDevice::init("HB-horn");

  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setScanCallbacks(&scanCB, true);   // true = report duplicates, we need every RSSI update
  scan->setActiveScan(false);              // passive is enough, we only need advertisements
  scan->setInterval(100);
  scan->setWindow(100);                    // window == interval -> scan continuously
  scan->start(0, false);                   // 0 = scan forever, non-blocking

  Serial.println("Horn scanning...");
}

void loop() {
  delay(100);

  // Find strongest and runner-up among fresh beacons
  int best = 0, second = 0;
  float bestR = -999, secondR = -999;
  for (int id = 1; id <= MAX_ID; id++) {
    if (!isFresh(id)) continue;
    float r = beacons[id].rssi;
    if (r > bestR) {
      second = best; secondR = bestR;
      best = id;     bestR = r;
    } else if (r > secondR) {
      second = id;   secondR = r;
    }
  }

  // Leaving the current zone (hysteresis)
  if (activeId != 0) {
    if (!isFresh(activeId) || beacons[activeId].rssi < LEAVE_DBM) {
      Serial.printf("Left installation %d\n", activeId);
      activeId = 0;
    }
  }

  // Entering a new zone
  if (activeId == 0 && best != 0 && bestR >= NEAR_DBM) {
    bool clearWinner = (second == 0) || (bestR - secondR >= WIN_MARGIN_DB);
    if (clearWinner) {
      activeId = best;
      Serial.printf("Near installation %d (RSSI %.1f) -> blinking %d x\n", best, bestR, best);
      blinkTimes(best);
    }
  }

  // Debug print twice per second
  static uint32_t lastPrint = 0;
  if (millis() - lastPrint > 500) {
    lastPrint = millis();
    for (int id = 1; id <= MAX_ID; id++) {
      if (isFresh(id)) Serial.printf("[HB-%d: %.0f dBm] ", id, beacons[id].rssi);
    }
    Serial.println();
  }
}
