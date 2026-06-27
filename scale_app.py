"""
Smart Weight Scale - Main Display Application
Raspberry Pi 4B | 4x HX711 | USB Webcam | 1024x600 display
"""

import cv2
import time
import json
import os
import threading
import logging
from datetime import datetime
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/pi/smartscale/scale.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = "/home/pi/smartscale/config.json"
PHOTOS_DIR  = "/home/pi/smartscale/photos"
Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "display_width":        1024,
    "display_height":       600,
    "weight_strip_height":  50,
    "trigger_weight_g":     500.0,
    "stabilise_seconds":    5.0,
    "unit":                 "g",          # "g" or "kg"
    "cell_labels":          ["C1", "C2", "C3", "C4"],
    # GPIO (BCM)
    "clk_pin":              6,
    "dout_pins":            [5, 13, 19, 26],
    # Calibration — one factor per cell, applied as: weight = (raw - offset) / factor
    "offsets":              [0, 0, 0, 0],
    "cal_factors":          [1.0, 1.0, 1.0, 1.0],
    # Webcam
    "camera_index":         0,
    "cam_width":            1280,
    "cam_height":           720,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            # Fill in any missing keys from defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            log.error(f"Config load error: {e} — using defaults")
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── HX711 wrapper ─────────────────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    HW_AVAILABLE = True
except ImportError:
    HW_AVAILABLE = False
    log.warning("RPi.GPIO not available — running in simulation mode")

class HX711:
    """Minimal, reliable HX711 driver. Gain=128 (channel A)."""
    GAIN = 1  # 25 pulses for gain 128

    def __init__(self, dout, clk):
        self.dout = dout
        self.clk  = clk
        if HW_AVAILABLE:
            GPIO.setup(self.clk,  GPIO.OUT)
            GPIO.setup(self.dout, GPIO.IN)
            self._reset()

    def _reset(self):
        GPIO.output(self.clk, True)
        time.sleep(0.0001)
        GPIO.output(self.clk, False)
        time.sleep(0.0004)

    def _read_raw(self):
        if not HW_AVAILABLE:
            import random
            return random.randint(8_000_000, 8_100_000)

        # Wait for DOUT to go LOW (data ready), timeout 500 ms
        deadline = time.time() + 0.5
        while GPIO.input(self.dout):
            if time.time() > deadline:
                log.warning(f"HX711 dout={self.dout} timeout")
                return None

        count = 0
        for _ in range(24):
            GPIO.output(self.clk, True)
            count = (count << 1) | GPIO.input(self.dout)
            GPIO.output(self.clk, False)

        # Extra pulses to set gain for next reading
        for _ in range(self.GAIN):
            GPIO.output(self.clk, True)
            GPIO.output(self.clk, False)

        # Convert twos-complement 24-bit
        if count & 0x800000:
            count -= 0x1000000
        return count

    def read_average(self, times=5):
        readings = []
        for _ in range(times):
            v = self._read_raw()
            if v is not None:
                readings.append(v)
            time.sleep(0.005)
        if not readings:
            return None
        # Drop highest and lowest if we have enough samples
        if len(readings) >= 4:
            readings = sorted(readings)[1:-1]
        return sum(readings) / len(readings)


class ScaleManager:
    def __init__(self, cfg):
        self.cfg     = cfg
        self.cells   = []
        self.lock    = threading.Lock()
        self._weights_g = [0.0, 0.0, 0.0, 0.0]   # calibrated grams per cell
        self._raw       = [0,   0,   0,   0  ]
        self.running = False

        if HW_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

        for dout in cfg["dout_pins"]:
            self.cells.append(HX711(dout, cfg["clk_pin"]))
        log.info(f"ScaleManager initialised — HW={'yes' if HW_AVAILABLE else 'SIM'}")

    def _to_grams(self, raw, idx):
        offset = self.cfg["offsets"][idx]
        factor = self.cfg["cal_factors"][idx]
        if factor == 0:
            return 0.0
        return (raw - offset) / factor

    def tare(self):
        """Read current raw values and store as zero offsets."""
        log.info("Taring all cells…")
        for i, cell in enumerate(self.cells):
            raw = cell.read_average(10)
            if raw is not None:
                self.cfg["offsets"][i] = raw
        save_config(self.cfg)
        log.info(f"Tare done — offsets: {self.cfg['offsets']}")

    def calibrate(self, known_weight_g):
        """
        Place known_weight_g on scale, then call this.
        Calculates factor so that mean of all cells = known_weight_g.
        Each cell gets its own factor proportional to its raw reading.
        """
        log.info(f"Calibrating with {known_weight_g}g reference…")
        raws = []
        for cell in self.cells:
            raw = cell.read_average(10)
            raws.append(raw if raw is not None else 0)

        net_raws = [r - self.cfg["offsets"][i] for i, r in enumerate(raws)]
        mean_net = sum(net_raws) / len(net_raws)

        for i in range(len(self.cells)):
            if net_raws[i] != 0:
                self.cfg["cal_factors"][i] = net_raws[i] / known_weight_g
            else:
                self.cfg["cal_factors"][i] = mean_net / known_weight_g if mean_net != 0 else 1.0

        save_config(self.cfg)
        log.info(f"Calibration done — factors: {self.cfg['cal_factors']}")

    def read_weights(self):
        """Return (list_of_4_grams, mean_grams). Updates internal state."""
        weights = []
        for i, cell in enumerate(self.cells):
            raw = cell.read_average(3)
            if raw is None:
                raw = self._raw[i]   # reuse last good value
            self._raw[i] = raw
            weights.append(self._to_grams(raw, i))
        with self.lock:
            self._weights_g = weights
        mean = sum(weights) / len(weights)
        return weights, mean

    def get_last_weights(self):
        with self.lock:
            return list(self._weights_g), sum(self._weights_g) / len(self._weights_g)

    def cleanup(self):
        if HW_AVAILABLE:
            GPIO.cleanup()


# ── Shared state (read by Flask, written by main loop) ────────────────────────
shared = {
    "weights": [0.0, 0.0, 0.0, 0.0],
    "mean":    0.0,
    "status":  "idle",     # idle | countdown | capturing
    "countdown": 0,
    "last_photo": "",
}
shared_lock = threading.Lock()

def update_shared(weights, mean, status, countdown=0, last_photo=""):
    with shared_lock:
        shared["weights"]    = weights
        shared["mean"]       = mean
        shared["status"]     = status
        shared["countdown"]  = countdown
        shared["last_photo"] = last_photo

def get_shared():
    with shared_lock:
        return dict(shared)


# ── OpenCV overlay helpers ────────────────────────────────────────────────────
FONT       = cv2.FONT_HERSHEY_SIMPLEX
FONT_SMALL = 0.55
FONT_MED   = 0.75
FONT_BIG   = 2.0
WHITE      = (255, 255, 255)
BLACK      = (0,   0,   0  )
GREEN      = (0,   200, 0  )
RED        = (0,   0,   220)
YELLOW     = (0,   200, 220)
STRIP_BG   = (30,  30,  30 )

def draw_weight_strip(frame, weights, mean, cfg, unit_label):
    h, w = frame.shape[:2]
    sh   = cfg["weight_strip_height"]
    # Dark background strip
    cv2.rectangle(frame, (0, h - sh), (w, h), STRIP_BG, -1)
    cv2.line(frame, (0, h - sh), (w, h - sh), (80, 80, 80), 1)

    labels = cfg["cell_labels"]
    parts  = [f"{labels[i]}: {weights[i]:.0f}{unit_label}" for i in range(4)]
    parts.append(f"MEAN: {mean:.0f}{unit_label}")
    text = "   |   ".join(parts)

    ts, _ = cv2.getTextSize(text, FONT, FONT_SMALL, 1)
    tx = max(10, (w - ts[0]) // 2)
    ty = h - sh + (sh + ts[1]) // 2
    cv2.putText(frame, text, (tx, ty), FONT, FONT_SMALL, WHITE, 1, cv2.LINE_AA)

def draw_countdown(frame, seconds_left):
    h, w = frame.shape[:2]
    txt  = f"Photo in {seconds_left:.1f}s"
    ts, _ = cv2.getTextSize(txt, FONT, FONT_BIG, 3)
    tx = (w - ts[0]) // 2
    ty = (h // 2) + ts[1] // 2
    # Shadow
    cv2.putText(frame, txt, (tx+2, ty+2), FONT, FONT_BIG, BLACK, 3, cv2.LINE_AA)
    cv2.putText(frame, txt, (tx,   ty),   FONT, FONT_BIG, YELLOW, 3, cv2.LINE_AA)

def draw_status(frame, text, color=GREEN):
    cv2.putText(frame, text, (10, 30), FONT, FONT_MED, BLACK, 2, cv2.LINE_AA)
    cv2.putText(frame, text, (10, 30), FONT, FONT_MED, color, 1, cv2.LINE_AA)


# ── Photo save with weight strip burned in ───────────────────────────────────
def save_photo(frame, weights, mean, cfg):
    unit_label = "kg" if cfg["unit"] == "kg" else "g"
    photo = frame.copy()
    draw_weight_strip(photo, weights, mean, cfg, unit_label)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTOS_DIR, f"capture_{ts}.jpg")
    cv2.imwrite(path, photo, [cv2.IMWRITE_JPEG_QUALITY, 92])
    log.info(f"Photo saved: {path}")
    return path


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    cfg   = load_config()
    scale = ScaleManager(cfg)

    # ── Startup tare ──────────────────────────────────────────────────────────
    log.info("Startup tare — ensure platform is EMPTY")
    time.sleep(2)
    scale.tare()

    # ── Open camera ───────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(cfg["camera_index"])
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cfg["cam_width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["cam_height"])
    if not cap.isOpened():
        log.error("Cannot open camera — exiting")
        scale.cleanup()
        return

    # ── Fullscreen window ─────────────────────────────────────────────────────
    DW = cfg["display_width"]
    DH = cfg["display_height"]
    cv2.namedWindow("SmartScale", cv2.WND_PROP_FULLSCREEN)
    cv2.setWindowProperty("SmartScale", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    unit_label    = "kg" if cfg["unit"] == "kg" else "g"
    trigger_g     = cfg["trigger_weight_g"]
    stabilise_s   = cfg["stabilise_seconds"]

    state         = "idle"        # idle | countdown | cooldown
    countdown_end = 0.0
    cooldown_end  = 0.0
    last_photo    = ""

    log.info("Main loop started")

    while True:
        ret, raw_frame = cap.read()
        if not ret:
            log.warning("Camera frame drop")
            time.sleep(0.05)
            continue

        # Resize to display
        frame = cv2.resize(raw_frame, (DW, DH))

        # Read weights (runs fast — 3 samples each)
        weights, mean = scale.read_weights()

        # Reload config if changed by web UI
        new_cfg = load_config()
        if new_cfg != cfg:
            cfg        = new_cfg
            scale.cfg  = cfg
            trigger_g  = cfg["trigger_weight_g"]
            stabilise_s = cfg["stabilise_seconds"]
            unit_label = "kg" if cfg["unit"] == "kg" else "g"
            log.info("Config reloaded")

        now = time.time()

        # ── State machine ──────────────────────────────────────────────────
        if state == "idle":
            if mean >= trigger_g:
                state        = "countdown"
                countdown_end = now + stabilise_s
                log.info(f"Weight {mean:.1f}g ≥ {trigger_g}g — countdown started")
            update_shared(weights, mean, "idle")

        elif state == "countdown":
            remaining = countdown_end - now
            if mean < trigger_g:
                # Weight removed before countdown finished
                state = "idle"
                log.info("Weight removed — countdown cancelled")
            elif remaining <= 0:
                # Take photo
                last_photo = save_photo(frame, weights, mean, cfg)
                state      = "cooldown"
                cooldown_end = now + 3.0   # wait 3 s before going back to idle
                log.info("Photo captured")
            else:
                draw_countdown(frame, remaining)
            update_shared(weights, mean, "countdown", max(0, remaining), last_photo)

        elif state == "cooldown":
            draw_status(frame, "Photo saved!", GREEN)
            if now >= cooldown_end and mean < trigger_g:
                state = "idle"
            update_shared(weights, mean, "cooldown", 0, last_photo)

        # ── Weight strip ───────────────────────────────────────────────────
        draw_weight_strip(frame, weights, mean, cfg, unit_label)

        cv2.imshow("SmartScale", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('t'):
            log.info("Manual tare key pressed")
            scale.tare()

    cap.release()
    cv2.destroyAllWindows()
    scale.cleanup()
    log.info("Scale app stopped")


if __name__ == "__main__":
    main()
