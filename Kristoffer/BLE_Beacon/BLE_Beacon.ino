/*
 * This advertises a BLE name "HB-<id>" (e.g. "HB-1");
 * for use with the horn. the horn does the rest of the processing
 */

#include <NimBLEDevice.h>

// This is the ID - for now the ID defines the number of times the Horn (ESP-led) blinks
#define INSTALLATION_ID 1

// TX power in dBm. Lower = smaller "zone" around the installation.
// ESP32 supports roughly -12 ... +9.
#define TX_POWER_DBM -12


#define ADV_INTERVAL 160 // (160 = 100 ms)

void setup() {
  Serial.begin(115200);

  char name[16];
  snprintf(name, sizeof(name), "HB-%d", INSTALLATION_ID);

  NimBLEDevice::init(name);
  NimBLEDevice::setPower(TX_POWER_DBM);

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();

  NimBLEAdvertisementData advData;
  advData.setFlags(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);
  advData.setName(name);
  adv->setAdvertisementData(advData);

  adv->setMinInterval(ADV_INTERVAL);
  adv->setMaxInterval(ADV_INTERVAL);
  adv->start();

  Serial.printf("Beacon started: %s  (TX %d dBm)\n", name, TX_POWER_DBM);
}

void loop() {
  delay(1000);
}
