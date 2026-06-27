"""
Smart Weight Scale - Main Display Application
Raspberry Pi 4B | 4x HX711 | USB Webcam | 1024x600 display
Display: pygame (no GTK required)
Camera capture & photo save: OpenCV (headless is fine)
"""

import cv2
import pygame
import time
import json
import os
import threading
import logging
import numpy as np
from datetime import datetime
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/melody/smartscale/scale.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = "/home/melody/smartscale/config.json"
PHOTOS_DIR  = "/home/melody/smartscale/photos"
Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "display_width":        1024,
    "display_height":       600,
    "weight_strip_height":  50,
    "trigger_weight_g":     500.0,
    "stabilise_seconds":    5.0,
    "unit":                 "g",
    "cell_labels":          ["C1", "C2", "C3", "C4"],
    "clk_pin":              6,
    "dout_pins":            [5, 13, 19, 26],
    "offsets":              [0, 0, 0, 0],
    "cal_factors":          [1.0, 1.0, 1.0, 1.0],
    "camera_index":         0,
    "cam_width":            1280,
    "cam_height":           720,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            log.error(f"Config load error: {e} — using defaults")
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── HX711 driver ──────────────────────────────────────────────────────────────
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

        for _ in range(self.GAIN):
            GPIO.output(self.clk, True)
            GPIO.output(self.clk, False)

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
        if len(readings) >= 4:
            readings = sorted(readings)[1:-1]
        return sum(readings) / len(readings)


class ScaleManager:
    def __init__(self, cfg):
        self.cfg        = cfg
        self.cells      = []
        self.lock       = threading.Lock()
        self._weights_g = [0.0, 0.0, 0.0, 0.0]
        self._raw       = [0,   0,   0,   0  ]

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
        log.info("Taring all cells…")
        for i, cell in enumerate(self.cells):
            raw = cell.read_average(10)
            if raw is not None:
                self.cfg["offsets"][i] = raw
        save_config(self.cfg)
        log.info(f"Tare done — offsets: {self.cfg['offsets']}")

    def calibrate(self, known_weight_g):
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
        weights = []
        for i, cell in enumerate(self.cells):
            raw = cell.read_average(3)
            if raw is None:
                raw = self._raw[i]
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


# ── Shared state (read by Flask web server) ───────────────────────────────────
shared = {
    "weights":    [0.0, 0.0, 0.0, 0.0],
    "mean":       0.0,
    "status":     "idle",
    "countdown":  0,
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


# ── Pygame drawing helpers ────────────────────────────────────────────────────
COL_WHITE  = (255, 255, 255)
COL_BLACK  = (0,   0,   0  )
COL_GREEN  = (0,   200, 0  )
COL_YELLOW = (220, 200, 0  )
COL_STRIP  = (30,  30,  30 )
COL_LINE   = (80,  80,  80 )

def make_fonts():
    """Return dict of pygame fonts. Called after pygame.init()."""
    return {
        "small":    pygame.font.SysFont("monospace", 18),
        "medium":   pygame.font.SysFont("monospace", 24),
        "large":    pygame.font.SysFont("monospace", 64, bold=True),
    }

def cv2_frame_to_pygame(frame, target_w, target_h):
    """Convert OpenCV BGR frame → pygame surface, resized to target."""
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    frame_rgb = cv2.resize(frame_rgb, (target_w, target_h))
    # Use pygame.image.frombuffer — avoids surfarray axis issues on Pi
    surface = pygame.image.frombuffer(frame_rgb.tobytes(), (target_w, target_h), "RGB")
    return surface

def draw_weight_strip(screen, fonts, weights, mean, cfg, unit_label, strip_y, w):
    sh = cfg["weight_strip_height"]
    pygame.draw.rect(screen, COL_STRIP, (0, strip_y, w, sh))
    pygame.draw.line(screen, COL_LINE,  (0, strip_y), (w, strip_y), 1)

    labels = cfg["cell_labels"]
    parts  = [f"{labels[i]}: {weights[i]:.0f}{unit_label}" for i in range(4)]
    parts.append(f"MEAN: {mean:.0f}{unit_label}")
    text = "   |   ".join(parts)

    surf = fonts["small"].render(text, True, COL_WHITE)
    tx   = max(10, (w - surf.get_width()) // 2)
    ty   = strip_y + (sh - surf.get_height()) // 2
    screen.blit(surf, (tx, ty))

def draw_countdown(screen, fonts, seconds_left, w, h):
    txt  = f"Photo in {seconds_left:.1f}s"
    surf = fonts["large"].render(txt, True, COL_YELLOW)
    # Shadow
    shadow = fonts["large"].render(txt, True, COL_BLACK)
    cx = (w - surf.get_width()) // 2
    cy = (h // 2) - surf.get_height() // 2
    screen.blit(shadow, (cx + 2, cy + 2))
    screen.blit(surf,   (cx,     cy    ))

def draw_status_msg(screen, fonts, text, color, w):
    surf = fonts["medium"].render(text, True, color)
    screen.blit(surf, (10, 10))


# ── Photo save (OpenCV — headless is fine for imwrite) ───────────────────────
def save_photo(frame, weights, mean, cfg):
    """Burn weight strip into a copy of the frame and save as JPG."""
    unit_label = "kg" if cfg["unit"] == "kg" else "g"
    photo      = frame.copy()
    h, w       = photo.shape[:2]
    sh         = cfg["weight_strip_height"]

    # Draw strip using OpenCV (no display needed)
    strip_y = h - sh
    cv2.rectangle(photo, (0, strip_y), (w, h), (30, 30, 30), -1)
    cv2.line(photo, (0, strip_y), (w, strip_y), (80, 80, 80), 1)

    labels = cfg["cell_labels"]
    parts  = [f"{labels[i]}: {weights[i]:.0f}{unit_label}" for i in range(4)]
    parts.append(f"MEAN: {mean:.0f}{unit_label}")
    text   = "   |   ".join(parts)

    cv2.putText(photo, text, (10, strip_y + sh - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTOS_DIR, f"capture_{ts}.jpg")
    cv2.imwrite(path, photo, [cv2.IMWRITE_JPEG_QUALITY, 92])
    log.info(f"Photo saved: {path}")
    return path


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    cfg   = load_config()
    scale = ScaleManager(cfg)

    # Startup tare
    log.info("Startup tare — ensure platform is EMPTY")
    time.sleep(2)
    scale.tare()

    # Open camera (OpenCV headless is fine for capture)
    cap = cv2.VideoCapture(cfg["camera_index"], cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cfg["cam_width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["cam_height"])
    if not cap.isOpened():
        log.error("Cannot open camera — exiting")
        scale.cleanup()
        return

    # Init pygame display
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")
    pygame.init()
    DW    = cfg["display_width"]
    DH    = cfg["display_height"]
    screen = pygame.display.set_mode((DW, DH), pygame.FULLSCREEN)
    pygame.display.set_caption("SmartScale")
    pygame.mouse.set_visible(False)
    fonts  = make_fonts()
    clock  = pygame.time.Clock()

    unit_label  = "kg" if cfg["unit"] == "kg" else "g"
    trigger_g   = cfg["trigger_weight_g"]
    stabilise_s = cfg["stabilise_seconds"]
    strip_y     = DH - cfg["weight_strip_height"]

    state         = "idle"
    countdown_end = 0.0
    cooldown_end  = 0.0
    last_photo    = ""

    log.info("Main loop started")

    while True:
        # ── Pygame events ──────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cap.release()
                pygame.quit()
                scale.cleanup()
                return
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    cap.release()
                    pygame.quit()
                    scale.cleanup()
                    log.info("Scale app stopped (q key)")
                    return
                elif event.key == pygame.K_t:
                    log.info("Manual tare key pressed")
                    scale.tare()

        # ── Camera frame ───────────────────────────────────────────────────
        ret, raw_frame = cap.read()
        if not ret:
            log.warning("Camera frame drop")
            time.sleep(0.05)
            continue

        # ── Read weights ───────────────────────────────────────────────────
        weights, mean = scale.read_weights()

        # ── Reload config if changed from web UI ───────────────────────────
        new_cfg = load_config()
        if new_cfg != cfg:
            cfg         = new_cfg
            scale.cfg   = cfg
            trigger_g   = cfg["trigger_weight_g"]
            stabilise_s = cfg["stabilise_seconds"]
            unit_label  = "kg" if cfg["unit"] == "kg" else "g"
            strip_y     = DH - cfg["weight_strip_height"]
            log.info("Config reloaded")

        now = time.time()

        # ── Draw camera frame ──────────────────────────────────────────────
        cam_surface = cv2_frame_to_pygame(raw_frame, DW, DH)
        screen.blit(cam_surface, (0, 0))

        # ── State machine ──────────────────────────────────────────────────
        if state == "idle":
            if mean >= trigger_g:
                state         = "countdown"
                countdown_end = now + stabilise_s
                log.info(f"Weight {mean:.1f}g >= {trigger_g}g — countdown started")
            update_shared(weights, mean, "idle")

        elif state == "countdown":
            remaining = countdown_end - now
            if mean < trigger_g:
                state = "idle"
                log.info("Weight removed — countdown cancelled")
            elif remaining <= 0:
                last_photo = save_photo(raw_frame, weights, mean, cfg)
                state      = "cooldown"
                cooldown_end = now + 3.0
                log.info("Photo captured")
            else:
                draw_countdown(screen, fonts, remaining, DW, DH)
            update_shared(weights, mean, "countdown", max(0, remaining), last_photo)

        elif state == "cooldown":
            draw_status_msg(screen, fonts, "Photo saved!", COL_GREEN, DW)
            if now >= cooldown_end and mean < trigger_g:
                state = "idle"
            update_shared(weights, mean, "cooldown", 0, last_photo)

        # ── Weight strip ───────────────────────────────────────────────────
        draw_weight_strip(screen, fonts, weights, mean, cfg, unit_label, strip_y, DW)

        pygame.display.flip()
        clock.tick(25)  # 25 fps — light on CPU

    cap.release()
    pygame.quit()
    scale.cleanup()
    log.info("Scale app stopped")


if __name__ == "__main__":
    main()