#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>
#include <epd3c/GxEPD2_750c_Z08.h>
#include "secrets.h"

// Driver for GDEW075Z08, 800x480 B/W/red. Verify panel marking before uploading.
GxEPD2_750c_Z08 panel(21, 22, 25, 26); // CS, DC, RST, BUSY
constexpr size_t PLANE = 800 * 480 / 8;
constexpr size_t BODY = PLANE * 2;
RTC_DATA_ATTR uint32_t savedMagic = 0;
RTC_DATA_ATTR uint32_t savedCRC = 0;
uint32_t be32(const uint8_t* p) {
  return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3];
}
uint32_t crc32(const uint8_t* p, size_t n) {
  uint32_t crc = 0xffffffff;
  while (n--) {
    crc ^= *p++;
    for (int i = 0; i < 8; ++i) crc = (crc >> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc;
}
bool readExact(WiFiClient& stream, uint8_t* dst, size_t count) {
  uint32_t start = millis();
  size_t done = 0;
  while (done < count && millis() - start < 20000) {
    int available = stream.available();
    if (available > 0) {
      int got = stream.read(dst + done, min(size_t(available), count - done));
      if (got > 0) done += got;
    } else {
      if (!stream.connected()) break;
      delay(2);
    }
  }
  return done == count;
}
void sleepFor(uint32_t seconds) {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.printf("Sleep: %lu s\n", (unsigned long)seconds);
  Serial.flush();
  esp_sleep_enable_timer_wakeup(uint64_t(seconds) * 1000000ULL);
  esp_deep_sleep_start();
}
void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(100);
  if (WiFi.status() != WL_CONNECTED) sleepFor(300);
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(60000); // server may wait for upstream HA calls
  if (!http.begin(client, FRAME_URL)) sleepFor(300);
  http.addHeader("Authorization", String("Bearer ") + FRAME_TOKEN);
  int status = http.GET();
  uint8_t header[16];
  uint8_t* body = nullptr;
  uint32_t seconds = 300;
  uint32_t receivedAt = millis();
  bool valid = status == 200 && http.getSize() == int(BODY + 16);
  if (valid) valid = readExact(*http.getStreamPtr(), header, sizeof(header));
  if (valid) {
    seconds = be32(header + 8);
    valid = memcmp(header, "EIF1", 4) == 0 && header[4] == 3 && header[5] == 32
      && header[6] == 1 && header[7] == 224 && seconds >= 60 && seconds <= 86400;
  }
  if (valid) {
    body = static_cast<uint8_t*>(heap_caps_malloc(BODY, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!body) body = static_cast<uint8_t*>(malloc(BODY));
    valid = body && readExact(*http.getStreamPtr(), body, BODY);
  }
  if (valid) valid = crc32(body, BODY) == be32(header + 12);
  http.end();
  if (!valid) {
    Serial.println("Download invalid; keep previous screen.");
    free(body);
    sleepFor(300);
  }
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  uint32_t crc = be32(header + 12);
  if (savedMagic != 0xe1f18004 || savedCRC != crc) {
    SPI.begin(18, -1, 23, 21);
    panel.init(115200, true, 2, false);
    panel.writeImage(body, body + PLANE, 0, 0, 800, 480);
    panel.refresh(false);
    panel.hibernate();
    savedCRC = crc;
    savedMagic = 0xe1f18004;
  }
  free(body);
  uint32_t elapsed = (millis() - receivedAt) / 1000;
  sleepFor(seconds > elapsed ? seconds - elapsed : 1);
}
void loop() {}
