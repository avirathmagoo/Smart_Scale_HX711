/* ═══════════════════════════════════════════════════════════════════════════
   Smart Scale — ESP32 HX711 UART Bridge  (v2 — library-based, read-only)
   ═══════════════════════════════════════════════════════════════════════════

   ROLE OF THIS FIRMWARE
     - Read 4x HX711 load-cell ADCs using the standard, well-tested HX711
       Arduino library (NOT a hand-rolled bit-bang — v1 of this firmware
       used a custom implementation and produced occasional wild/garbage
       readings; a single flipped bit in a 24-bit reading causes a
       multi-million-count jump, which is exactly what that looked like)
     - Lightly average each channel on-device (last 4 raw samples) before
       sending, so the Pi isn't filtering pure single-sample ADC noise
     - Send RAW readings to the Pi over UART — no scale factor, no tare.
       This board ONLY reads sensors and outputs data. All tare/calibration
       math lives entirely on the Pi.

   WHY NO TARE COMMAND (removed from v1)
     v1 had a 'T' command that made the ESP32 pause and average fresh
     samples per channel — but that pause (several seconds, worst case) is
     several seconds of NOT sending data packets, which looks exactly like
     a dead UART link from the Pi's side, and the Pi's own timeout for the
     ack was shorter than the pause could take. Simplest fix: this board
     never tares anything, ever. It just reads and reports, continuously,
     with nothing that can pause the stream. The Pi tares itself by simply
     remembering "whatever the raw stream reads right now" as zero — no
     coordination with the ESP32 required.

   PACKET FORMAT — unchanged from v1, still the whole protocol:
     ESP32 -> Pi, every SEND_INTERVAL_MS:
         D,<raw1>,<raw2>,<raw3>,<raw4>,<checksum>\n
       checksum = 2-digit uppercase hex, XOR of every byte in the
       "<raw1>,<raw2>,<raw3>,<raw4>" substring.
     ESP32 -> Pi, once at boot:
         A,READY\n
     (No Pi -> ESP32 commands at all in this version.)

   HARDWARE
     Board: ESP32-CAM (AI-Thinker). Camera/WiFi are NOT used.
     UART0 (GPIO1 TX / GPIO3 RX) wired straight to the Pi's GPIO14/15
     hardware UART pins — same pins used to flash via USB-TTL adapter;
     disconnect the Pi link while flashing.

     HX711 pins (unchanged):
         Shared SCK : GPIO 15
         DOUT 1     : GPIO 2   (load cell 1 — Top-Left)
         DOUT 2     : GPIO 14  (load cell 2 — Top-Right)
         DOUT 3     : GPIO 13  (load cell 3 — Bottom-Left)
         DOUT 4     : GPIO 4   (load cell 4 — Bottom-Right)

   REQUIRED LIBRARY (install via Arduino IDE Library Manager):
     "HX711" by Bogdan Necula (a.k.a. bogde/HX711) — search "HX711",
     install the one with author "Bogdan Necula / Andreas Motl".
   ═══════════════════════════════════════════════════════════════════════════ */

#include <Arduino.h>
#include <HX711.h>

// ── Pins ─────────────────────────────────────────────────────────────────────
#define SHARED_SCK_PIN   15
#define DOUT_PIN_1        2
#define DOUT_PIN_2       14
#define DOUT_PIN_3       13
#define DOUT_PIN_4        4

const uint8_t DOUT_PINS[4] = { DOUT_PIN_1, DOUT_PIN_2, DOUT_PIN_3, DOUT_PIN_4 };

HX711 scales[4];

// ── Timing ───────────────────────────────────────────────────────────────────
#define BAUD_RATE         115200
#define SEND_INTERVAL_MS   150     // ~6.7Hz — HX711 itself only converts at
                                    // ~10Hz, so sending faster just repeats
                                    // stale data. Slower send = calmer Pi
                                    // display without losing real information.
#define SMOOTH_N            4      // on-device averaging window per channel

// ── State ────────────────────────────────────────────────────────────────────
long rawHistory[4][SMOOTH_N];
uint8_t histIdx[4]   = { 0, 0, 0, 0 };
uint8_t histCount[4] = { 0, 0, 0, 0 };

// ═══════════════════════════════════════════════════════════════════════════
//  NON-BLOCKING ROUND-ROBIN POLL
//  One channel checked per call via the library's is_ready() (just a
//  digitalRead, instant) — read() is only called once we already know
//  that channel is ready, so it returns immediately (no risk of the
//  library's internal wait-loop ever blocking noticeably).
// ═══════════════════════════════════════════════════════════════════════════
void pollScales() {
  static uint8_t cellIndex = 0;

  if (scales[cellIndex].is_ready()) {
    long r = scales[cellIndex].read();   // raw signed value, no tare/scale applied

    rawHistory[cellIndex][histIdx[cellIndex]] = r;
    histIdx[cellIndex] = (histIdx[cellIndex] + 1) % SMOOTH_N;
    if (histCount[cellIndex] < SMOOTH_N) histCount[cellIndex]++;
  }

  cellIndex = (cellIndex + 1) % 4;
}

long averagedRaw(uint8_t ch) {
  if (histCount[ch] == 0) return 0;
  long sum = 0;
  for (uint8_t i = 0; i < histCount[ch]; i++) sum += rawHistory[ch][i];
  return sum / histCount[ch];
}

// ═══════════════════════════════════════════════════════════════════════════
//  UART OUTPUT
// ═══════════════════════════════════════════════════════════════════════════
void sendDataPacket() {
  char body[64];
  snprintf(body, sizeof(body), "%ld,%ld,%ld,%ld",
           averagedRaw(0), averagedRaw(1), averagedRaw(2), averagedRaw(3));

  uint8_t checksum = 0;
  for (size_t i = 0; body[i] != '\0'; i++) checksum ^= (uint8_t)body[i];

  Serial.printf("D,%s,%02X\n", body, checksum);
}

// ═══════════════════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(BAUD_RATE);
  delay(100);

  for (uint8_t i = 0; i < 4; i++) {
    scales[i].begin(DOUT_PINS[i], SHARED_SCK_PIN);
  }

  Serial.println("A,READY");
}

// ═══════════════════════════════════════════════════════════════════════════
//  LOOP  —  read continuously, send on a timer. Nothing in here ever blocks
//  for more than a fraction of a millisecond, so the UART stream never goes
//  quiet — there is no tare, no calibration, nothing that pauses reading.
// ═══════════════════════════════════════════════════════════════════════════
void loop() {
  static uint32_t lastSend = 0;

  pollScales();

  uint32_t now = millis();
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    sendDataPacket();
  }
}
