#include <Arduino.h>

/*
  ╔══════════════════════════════════════════════════════════════════════════╗
  ║         ESP32-CAM + 4× HX711 Weight Scale — Integrated Firmware v3       ║
  ╠══════════════════════════════════════════════════════════════════════════╣
  ║  Board      : AI Thinker ESP32-CAM                                       ║
  ║  Partition  : Huge APP (3MB No OTA)  ← REQUIRED                          ║
  ║  CPU Freq   : 240 MHz                                                    ║
  ║                                                                          ║
  ║  HX711 Pin Assignment:                                                   ║
  ║    Shared SCK : GPIO 15                                                  ║
  ║    DOUT 1     : GPIO 2   (load cell 1 — Top-Left)                        ║
  ║    DOUT 2     : GPIO 14  (load cell 2 — Top-Right)                       ║
  ║    DOUT 3     : GPIO 13  (load cell 3 — Bottom-Left)                     ║
  ║    DOUT 4     : GPIO 4   (load cell 4 — Bottom-Right)                    ║
  ╚══════════════════════════════════════════════════════════════════════════╝
*/

#include "HX711.h"

// ═══════════════════════════════════════════════════════════════════════════════
//  USER CONFIG
// ═══════════════════════════════════════════════════════════════════════════════

#define SCALE_FACTOR   100.0f
#define NOISE_FLOOR_G  50.0f

#define SHARED_SCK_PIN  15
#define DOUT_PIN_1       2
#define DOUT_PIN_2      14
#define DOUT_PIN_3      13
#define DOUT_PIN_4       4

HX711 scale1, scale2, scale3, scale4;
volatile bool tareRequested = false;
float weight1 = 0, weight2 = 0, weight3 = 0, weight4 = 0;
float avgWeight = 0;

// ═══════════════════════════════════════════════════════════════════════════════
//  HX711 INIT
// ═══════════════════════════════════════════════════════════════════════════════
void initScales() {
  scale1.begin(DOUT_PIN_1, SHARED_SCK_PIN);
  scale2.begin(DOUT_PIN_2, SHARED_SCK_PIN);
  scale3.begin(DOUT_PIN_3, SHARED_SCK_PIN);
  scale4.begin(DOUT_PIN_4, SHARED_SCK_PIN);

  scale1.set_scale(SCALE_FACTOR);
  scale2.set_scale(SCALE_FACTOR);
  scale3.set_scale(SCALE_FACTOR);
  scale4.set_scale(SCALE_FACTOR);

  scale1.tare();
  scale2.tare();
  scale3.tare();
  scale4.tare();

  Serial.println("Scales initialized and tared.");
}

void tareAll() {
  scale1.tare();
  scale2.tare();
  scale3.tare();
  scale4.tare();
  Serial.println("All scales tared.");
}

// ═══════════════════════════════════════════════════════════════════════════════
//  NON-BLOCKING ROUND-ROBIN HX711 POLLING
// ═══════════════════════════════════════════════════════════════════════════════
void pollScales() {
  static uint8_t cellIndex = 0;

  HX711* scales[4]  = { &scale1, &scale2, &scale3, &scale4 };
  float* weights[4] = { &weight1, &weight2, &weight3, &weight4 };

  HX711* s = scales[cellIndex];
  if (s->is_ready()) {
    float v = fabsf(s->get_units(1));
    if (v < NOISE_FLOOR_G) v = 0.0f;
    *weights[cellIndex] = v;
  }

  cellIndex = (cellIndex + 1) % 4;
  avgWeight = ((weight1/4.0f) + (weight2/4.0f) + (weight3/4.0f) + (weight4/4.0f));
}

// ═══════════════════════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  Serial.println("\n\nESP32-CAM Weight Scale Monitor starting...");

  initScales();
  Serial.println("Ready.");
}

// ═══════════════════════════════════════════════════════════════════════════════
//  LOOP
// ═══════════════════════════════════════════════════════════════════════════════
void loop() {
  uint32_t now = millis();

  // 1. Check sensors without blocking
  pollScales();

  // 2. Stream data to Pi every 150ms
  static uint32_t lastPush = 0;
  if (now - lastPush >= 150) {
    lastPush = now;
    
    float w1 = weight1 / 4.0f;
    float w2 = weight2 / 4.0f;
    float w3 = weight3 / 4.0f;
    float w4 = weight4 / 4.0f;
    
    char payload[64];
    // Create the payload: W1, W2, W3, W4, Total
    snprintf(payload, sizeof(payload), "%.1f,%.1f,%.1f,%.1f,%.1f", w1, w2, w3, w4, avgWeight);
    
    // Calculate XOR checksum of the payload bytes
    uint8_t csum = 0;
    for (int i = 0; payload[i] != '\0'; i++) {
        csum ^= payload[i];
    }
    
    // Send packet format: D,<W1>,<W2>,<W3>,<W4>,<Total>,<Checksum>
    Serial.printf("D,%s,%02X\n", payload, csum);
  }

  // 3. Process any local tare requests
  if (tareRequested) {
    tareRequested = false;
    tareAll();
  }

  // 4. Robust UART Command Listener (Sliding Window for Hex Sequence)
  const uint8_t TARE_CMD[] = {0xAA, 0x55, 0x01, 'T', 'A', 'R', 'E', '\r', '\n'};
  const int CMD_LEN = sizeof(TARE_CMD);
  static uint8_t rxBuf[sizeof(TARE_CMD)] = {0};

  while (Serial.available()) {
    // Shift buffer left by 1 byte
    memmove(rxBuf, rxBuf + 1, CMD_LEN - 1);
    // Insert new byte at the end
    rxBuf[CMD_LEN - 1] = Serial.read();
    
    // Check if the buffer exactly matches the hex command
    if (memcmp(rxBuf, TARE_CMD, CMD_LEN) == 0) {
      tareRequested = true;
      memset(rxBuf, 0, CMD_LEN); // Clear buffer to prevent double-trigger
    }
  }

  delay(1);
}