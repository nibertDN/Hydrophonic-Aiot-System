/*
  Smart AI Irrigation - ESP32 monitoring firmware (MONITORING ONLY)

  There is no pump or relay in this build: nothing here switches water on or off.

  Libraries (Arduino Library Manager): "PubSubClient" by Nick O'Leary, "ArduinoJson" v7 by Benoit Blanchon.
  Board: "ESP32 Dev Module" (esp32 core 2.x or 3.x).

  Pins (as wired):
    Soil sensor  GPIO34 (analog)      Float switch GPIO18 (LOW = enough water, HIGH = low water)
    Water LEDs   red GPIO23, green GPIO21     Soil LEDs  red GPIO16, green GPIO17     Buzzer GPIO22

  Behaviour:
    soil >= SOIL_THRESHOLD (3300) -> soil is DRY  (higher ADC value = drier)
    float switch HIGH             -> water is LOW
    Buzzer beeps while either alert is active (assumes an ACTIVE buzzer; HIGH = on).
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "secrets.h"   // copy secrets.h.example -> secrets.h
#include "root_ca.h"

#define PIN_SOIL        34
#define PIN_FLOAT       18
#define PIN_WATER_RED   23
#define PIN_WATER_GREEN 21
#define PIN_SOIL_RED    16
#define PIN_SOIL_GREEN  17
#define PIN_BUZZER      22

const int  SOIL_THRESHOLD   = 3300;
const char DEVICE_ID[]      = "device01";
const char TOPIC_SENSORS[]  = "irrigation/device01/sensors";
const char TOPIC_STATUS[]   = "irrigation/device01/status";
const unsigned long PUBLISH_EVERY_MS = 5000;

WiFiClient       plainClient;
WiFiClientSecure tlsClient;
PubSubClient     mqtt;

unsigned long lastPublish = 0, lastMqttTry = 0, lastWifiTry = 0, mqttBackoff = 2000;
bool waterLow = false, soilDry = false, buzzerOn = false;
int  soilValue = 0;

// Median of 9 samples removes ADC noise spikes.
int readSoil() {
  int s[9];
  for (int i = 0; i < 9; i++) { s[i] = analogRead(PIN_SOIL); delay(3); }
  for (int i = 0; i < 8; i++) for (int j = i + 1; j < 9; j++) if (s[j] < s[i]) { int t = s[i]; s[i] = s[j]; s[j] = t; }
  return s[4];
}

// Float switch must read the same level 5 times in a row (debounce against water ripple).
bool readWaterLow() {
  int high = 0;
  for (int i = 0; i < 5; i++) { high += digitalRead(PIN_FLOAT); delay(4); }
  return high >= 3;   // HIGH = low water
}

void updateIndicators() {
  digitalWrite(PIN_WATER_RED,   waterLow ? HIGH : LOW);
  digitalWrite(PIN_WATER_GREEN, waterLow ? LOW : HIGH);
  digitalWrite(PIN_SOIL_RED,    soilDry ? HIGH : LOW);
  digitalWrite(PIN_SOIL_GREEN,  soilDry ? LOW : HIGH);
}

// Non-blocking beep: 200 ms on / 800 ms off while an alert is active.
void updateBuzzer() {
  bool alert = waterLow || soilDry;
  buzzerOn = alert && (millis() % 1000UL < 200UL);
  digitalWrite(PIN_BUZZER, buzzerOn ? HIGH : LOW);
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiTry < 10000 && lastWifiTry != 0) return;
  lastWifiTry = millis();
  Serial.println("WiFi connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void ensureMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  if (millis() - lastMqttTry < mqttBackoff && lastMqttTry != 0) return;
  lastMqttTry = millis();
  String cid = String("esp32-") + DEVICE_ID + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  // Last-will: the broker publishes "offline" for us if we vanish.
  if (mqtt.connect(cid.c_str(), MQTT_USERNAME, MQTT_PASSWORD, TOPIC_STATUS, 1, true, "offline")) {
    Serial.println("MQTT connected");
    mqtt.publish(TOPIC_STATUS, "online", true);
    mqttBackoff = 2000;
  } else {
    Serial.printf("MQTT failed rc=%d, retry in %lu ms\n", mqtt.state(), mqttBackoff);
    mqttBackoff = min(mqttBackoff * 2, 60000UL);
  }
}

void publishReading() {
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["soil"] = soilValue;
  doc["soil_threshold"] = SOIL_THRESHOLD;
  doc["soil_dry"] = soilDry;
  doc["water_low"] = waterLow;
  JsonObject leds = doc["leds"].to<JsonObject>();
  leds["water_red"] = waterLow;  leds["water_green"] = !waterLow;
  leds["soil_red"] = soilDry;    leds["soil_green"] = !soilDry;
  doc["buzzer"] = waterLow || soilDry;       // "alert active" (the beep itself pulses)
  doc["rssi"] = WiFi.RSSI();
  doc["uptime_s"] = (uint32_t)(millis() / 1000UL);
  doc["simulated"] = false;
  char buf[384];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  if (!mqtt.publish(TOPIC_SENSORS, (const uint8_t*)buf, n, false)) Serial.println("publish failed");
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_FLOAT, INPUT_PULLUP); // matches tested wiring: LOW = enough water, HIGH = low water
  for (int p : {PIN_WATER_RED, PIN_WATER_GREEN, PIN_SOIL_RED, PIN_SOIL_GREEN, PIN_BUZZER}) pinMode(p, OUTPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_SOIL, ADC_11db);

#if MQTT_USE_TLS
  tlsClient.setCACert(ROOT_CA);   // verifies the broker certificate (no setInsecure)
  mqtt.setClient(tlsClient);
#else
  mqtt.setClient(plainClient);
#endif
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  configTime(0, 0, "pool.ntp.org", "time.google.com");  // correct clock for TLS certificate checks
  ensureWifi();
}

void loop() {
  ensureWifi();
  ensureMqtt();
  mqtt.loop();

  static unsigned long lastRead = 0;
  if (millis() - lastRead >= 250) {          // local logic runs even when offline
    lastRead = millis();
    soilValue = readSoil();
    soilDry   = soilValue >= SOIL_THRESHOLD;
    waterLow  = readWaterLow();
    updateIndicators();
  }
  updateBuzzer();

  if (mqtt.connected() && millis() - lastPublish >= PUBLISH_EVERY_MS) {
    lastPublish = millis();
    publishReading();
  }
}
