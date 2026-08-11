/* ═══════════════════════════════════════════════════════════════════════════
   Smart Scale — ESP32 HX711 UART Bridge
   ═══════════════════════════════════════════════════════════════════════════

   ROLE OF THIS FIRMWARE
     - Read 4x HX711 load-cell ADCs (round-robin, non-blocking poll)
     - Send RAW 24-bit signed readings to the Raspberry Pi over UART0
       (no scaling, no unit conversion — the Pi owns all calibration math)
     - On receiving 'T' from the Pi, average a fresh batch of samples per
       channel and store as a LOCAL zero-offset, subtracted before every
       future send. This is a hardware-side zero — the Pi does its OWN
       independent tare on top of this stream, so the two are complementary,
       not redundant: this one removes bulk zero-drift at the source: the
       Pi's tare then zeroes out whatever small remainder is left.

   PACKET FORMAT (this is the whole protocol — intentionally simple)
     ESP32 -> Pi, every SEND_INTERVAL_MS:
         D,<raw1>,<raw2>,<raw3>,<raw4>,<checksum>\n
       checksum = 2-digit uppercase hex, XOR of every byte in the
       "<raw1>,<raw2>,<raw3>,<raw4>" substring (comma-included, not
       including the leading "D," or the trailing "*"/newline).

     ESP32 -> Pi, one-off status lines:
         A,READY\n            sent once at boot
         A,TARE_OK\n          sent after a successful tare
         A,TARE_FAIL\n        sent if one or more channels timed out during tare

     Pi -> ESP32:
         T\n   (or just the single byte 'T' / 't')  — run a tare

   HARDWARE
     Board: ESP32-CAM (AI-Thinker). Camera/WiFi are NOT used by this
     firmware — the Pi's own USB webcam is the only camera in this project.
     UART0 (GPIO1 TX / GPIO3 RX) is wired straight to the Pi's GPIO14/15
     hardware UART pins. This is the SAME pair used to flash the ESP32 via
     an external USB-TTL adapter — disconnect the Pi link while flashing.

     HX711 pins (unchanged from before, already chosen to avoid every
     camera-related and boot-strapping pin on this module):
         Shared SCK : GPIO 15
         DOUT 1     : GPIO 2   (load cell 1 — Top-Left)
         DOUT 2     : GPIO 14  (load cell 2 — Top-Right)
         DOUT 3     : GPIO 13  (load cell 3 — Bottom-Left)
         DOUT 4     : GPIO 4   (load cell 4 — Bottom-Right)

   No external libraries required — the HX711 bit-bang is implemented
   directly below (fewer moving parts, one less thing to install).
   ═══════════════════════════════════════════════════════════════════════════ */

#include <Arduino.h>

// ── Pins ─────────────────────────────────────────────────────────────────────
#define SHARED_SCK_PIN   15
#define DOUT_PIN_1        2
#define DOUT_PIN_2       14
#define DOUT_PIN_3       13
#define DOUT_PIN_4        4

const uint8_t DOUT_PINS[4] = { DOUT_PIN_1, DOUT_PIN_2, DOUT_PIN_3, DOUT_PIN_4 };

// ── Timing ───────────────────────────────────────────────────────────────────
#define BAUD_RATE            115200
#define SEND_INTERVAL_MS      50     // ~20 Hz packet rate to the Pi
#define READY_POLL_TIMEOUT_US 2000   // per-channel non-blocking readiness check
#define TARE_SAMPLES          15     // samples averaged per channel on tare

// ── State ────────────────────────────────────────────────────────────────────
long lastRaw[4]     = { 0, 0, 0, 0 };
long tareOffset[4]  = { 0, 0, 0, 0 };
bool everRead[4]    = { false, false, false, false };
bool tareRequested  = false;

// ═══════════════════════════════════════════════════════════════════════════
//  LOW-LEVEL HX711 BIT-BANG
//  Matches the timing-proven approach from the original Pi driver:
//  poll DOUT for LOW (ready) without blocking forever, then clock out
//  24 bits + 1 gain-set pulse. Two's-complement sign extension.
// ═══════════════════════════════════════════════════════════════════════════
inline bool hx711IsReady(uint8_t doutPin) {
  return digitalRead(doutPin) == LOW;
}

long hx711ReadRaw(uint8_t doutPin) {
  long value = 0;
  noInterrupts();
  for (int i = 0; i < 24; i++) {
    digitalWrite(SHARED_SCK_PIN, HIGH);
    delayMicroseconds(1);
    value = (value << 1) | digitalRead(doutPin);
    digitalWrite(SHARED_SCK_PIN, LOW);
    delayMicroseconds(1);
  }
  // 25th pulse — sets gain = 128, channel A for the next conversion
  digitalWrite(SHARED_SCK_PIN, HIGH);
  delayMicroseconds(1);
  digitalWrite(SHARED_SCK_PIN, LOW);
  interrupts();

  if (value & 0x800000L) value -= 0x1000000L;
  return value;
}

// ═══════════════════════════════════════════════════════════════════════════
//  NON-BLOCKING ROUND-ROBIN POLL
//  One channel checked per call. If that channel's DOUT is LOW (ready),
//  take one reading (a few microseconds) and store it. Cycling through
//  all 4 channels this way never stalls the main loop.
// ═══════════════════════════════════════════════════════════════════════════
void pollScales() {
  static uint8_t cellIndex = 0;

  uint8_t pin = DOUT_PINS[cellIndex];
  if (hx711IsReady(pin)) {
    lastRaw[cellIndex] = hx711ReadRaw(pin);
    everRead[cellIndex] = true;
  }

  cellIndex = (cellIndex + 1) % 4;
}

// ═══════════════════════════════════════════════════════════════════════════
//  TARE — average TARE_SAMPLES fresh readings per channel, store as offset.
//  This briefly blocks (a few tens of ms per channel, worst case), which is
//  fine: it only runs on an explicit 'T' command, never during normal flow.
// ═══════════════════════════════════════════════════════════════════════════
bool tareAll() {
  bool allOk = true;

  for (uint8_t ch = 0; ch < 4; ch++) {
    uint8_t pin = DOUT_PINS[ch];
    long sum = 0;
    int got = 0;

    for (int s = 0; s < TARE_SAMPLES; s++) {
      unsigned long deadline = millis() + 200; // 200ms timeout per sample
      while (!hx711IsReady(pin)) {
        if (millis() > deadline) break;
      }
      if (hx711IsReady(pin)) {
        sum += hx711ReadRaw(pin);
        got++;
      }
    }

    if (got < TARE_SAMPLES / 2) {
      Serial.printf("Tare: channel %d only got %d/%d samples\n", ch, got, TARE_SAMPLES);
      allOk = false;
      continue; // keep previous offset for this channel
    }

    tareOffset[ch] = sum / got;
  }

  return allOk;
}

// ═══════════════════════════════════════════════════════════════════════════
//  UART OUTPUT
// ═══════════════════════════════════════════════════════════════════════════
void sendDataPacket() {
  char body[64];
  snprintf(body, sizeof(body), "%ld,%ld,%ld,%ld",
           lastRaw[0] - tareOffset[0],
           lastRaw[1] - tareOffset[1],
           lastRaw[2] - tareOffset[2],
           lastRaw[3] - tareOffset[3]);

  uint8_t checksum = 0;
  for (size_t i = 0; body[i] != '\0'; i++) checksum ^= (uint8_t)body[i];

  Serial.printf("D,%s,%02X\n", body, checksum);
}

// ═══════════════════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════════════════
void setup() {
  pinMode(SHARED_SCK_PIN, OUTPUT);
  digitalWrite(SHARED_SCK_PIN, LOW);
  for (uint8_t i = 0; i < 4; i++) {
    pinMode(DOUT_PINS[i], INPUT);
  }

  Serial.begin(BAUD_RATE);
  delay(100);
  Serial.println("A,READY");
}

// ═══════════════════════════════════════════════════════════════════════════
//  LOOP
// ═══════════════════════════════════════════════════════════════════════════
void loop() {
  static uint32_t lastSend = 0;

  pollScales();

  uint32_t now = millis();
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    sendDataPacket();
  }

  if (Serial.available()) {
    char c = Serial.read();
    if (c == 't' || c == 'T') tareRequested = true;
  }

  if (tareRequested) {
    tareRequested = false;
    bool ok = tareAll();
    Serial.println(ok ? "A,TARE_OK" : "A,TARE_FAIL");
  }
}
