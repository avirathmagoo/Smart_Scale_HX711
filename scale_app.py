"""
Smart Scale — Main Application
Raspberry Pi 4B | 4x HX711 (shared CLK) | USB Webcam | 1024x600

ARCHITECTURE
  hx711-acq  thread  — GPIO reads into ring buffers (highest priority)
  hx711-proc thread  — filtering + calibration, publishes kg values
  camera     thread  — frame grab (lowest priority)
  main       thread  — pygame display + state machine
"""

import cv2
import pygame
import time
import json
import os
import threading
import logging
import collections
import statistics
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
    "trigger_weight_kg":    0.5,
    "stabilise_seconds":    5.0,
    "unit":                 "kg",
    "cell_labels":          ["C1", "C2", "C3", "C4"],
    "clk_pin":              6,
    "dout_pins":            [5, 13, 19, 26],
    "offsets":              [0, 0, 0, 0],
    "cal_factors":          [1.0, 1.0, 1.0, 1.0],
    "camera_index":         0,
    "stream_width":         640,
    "stream_height":        480,
    "photo_width":          1280,
    "photo_height":         720,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            if "trigger_weight_g" in cfg and "trigger_weight_kg" not in cfg:
                cfg["trigger_weight_kg"] = cfg.pop("trigger_weight_g") / 1000.0
            return cfg
        except Exception as e:
            log.error(f"Config load error: {e} — using defaults")
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# HX711 DRIVER  — proven simple approach that actually works
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import RPi.GPIO as GPIO
    HW_AVAILABLE = True
except ImportError:
    HW_AVAILABLE = False
    log.warning("RPi.GPIO not available — simulation mode")

# 24-bit ADC hard limits
HX711_MIN =  -8388608   # -2^23
HX711_MAX =   8388607   #  2^23 - 1
# Maximum believable jump between consecutive reads (static load)
HX711_MAX_JUMP = 500000

class HX711:
    """
    Simple, reliable HX711 driver — matches the logic that worked before
    but adds range validation and jump detection.
    Gain = 128, Channel A (25 clock pulses total).
    CLK is shared — only one HX711 is read at a time.
    """
    def __init__(self, dout: int, clk: int):
        self.dout      = dout
        self.clk       = clk
        self._last_raw = None
        if HW_AVAILABLE:
            # CLK already set up as OUTPUT by ScaleManager
            GPIO.setup(self.dout, GPIO.IN)

    def is_ready(self) -> bool:
        if not HW_AVAILABLE:
            return True
        return GPIO.input(self.dout) == 0

    def read_raw(self) -> int | None:
        """
        Read one sample. Returns signed 24-bit int or None on error.
        DOUT goes LOW when HX711 conversion is complete — we poll for this.
        No power-cycle reset: that was causing all 4 chips to lose sync.
        """
        if not HW_AVAILABLE:
            base = self._last_raw if self._last_raw is not None else 0
            import random
            val = base + random.randint(-300, 300)
            self._last_raw = val
            return val

        # Wait for DOUT LOW (conversion ready), 500 ms timeout
        deadline = time.monotonic() + 0.5
        while GPIO.input(self.dout):
            if time.monotonic() > deadline:
                log.warning(f"HX711 DOUT={self.dout} not ready (timeout)")
                return None

        # Clock in 24 bits
        raw = 0
        for _ in range(24):
            GPIO.output(self.clk, True)
            raw = (raw << 1) | GPIO.input(self.dout)
            GPIO.output(self.clk, False)

        # 25th pulse — sets gain=128 Channel A for next conversion
        GPIO.output(self.clk, True)
        GPIO.output(self.clk, False)

        # Two's complement
        if raw & 0x800000:
            raw -= 0x1000000

        # Hard range check
        if not (HX711_MIN <= raw <= HX711_MAX):
            log.warning(f"DOUT={self.dout} raw={raw} out of range — discarded")
            return None

        # Jump check — catches single-bit corruptions
        if self._last_raw is not None:
            jump = abs(raw - self._last_raw)
            if jump > HX711_MAX_JUMP:
                log.warning(f"DOUT={self.dout} spike Δ={jump} — discarded")
                return None

        self._last_raw = raw
        return raw

    def read_average(self, n: int = 5) -> float | None:
        """
        Read n samples, discard outliers, return trimmed mean.
        Used for tare and calibration only.
        """
        samples = []
        for _ in range(n):
            v = self.read_raw()
            if v is not None:
                samples.append(v)
            time.sleep(0.012)   # ~80 Hz HX711 sample rate

        if len(samples) < max(3, n // 2):
            log.warning(f"DOUT={self.dout} only got {len(samples)}/{n} samples")
            return None

        # Trimmed mean: drop top and bottom 15%
        k = max(1, int(len(samples) * 0.15))
        trimmed = sorted(samples)[k: len(samples) - k]
        return sum(trimmed) / len(trimmed) if trimmed else sum(samples) / len(samples)


# ═══════════════════════════════════════════════════════════════════════════════
# ACQUISITION + PROCESSING PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

RING_DEPTH    = 24    # raw samples kept per channel
FILTER_WINDOW = 16    # samples used per filtered output
EMA_ALPHA     = 0.12  # display smoothing (lower = smoother)
STABLE_THRESH = 0.008 # kg — reading must stay within this to be "stable"
STABLE_COUNT  = 10    # consecutive stable readings needed


class ScaleManager:
    """
    Two threads:
      acq-thread  — reads all 4 cells sequentially, pushes to ring buffers
      proc-thread — filters ring buffers, applies calibration, publishes kg

    Main loop calls get_values() — returns instantly, never blocks.
    Tare/calibrate pause the acq-thread and read directly.
    """

    def __init__(self, cfg: dict):
        self.cfg    = cfg
        self._cells = []
        self._n     = len(cfg["dout_pins"])

        # Ring buffers — one deque per cell
        self._ring     = [collections.deque(maxlen=RING_DEPTH) for _ in range(self._n)]
        self._ring_lock = threading.Lock()

        # Published output (written by proc-thread, read by main + Flask)
        self._out_lock = threading.Lock()
        self._out = {
            "kg":       [0.0] * self._n,
            "ema_kg":   [0.0] * self._n,
            "mean_kg":  0.0,
            "ema_mean": 0.0,
            "stable":   False,
            "diag":     ["no_data"] * self._n,
        }

        # EMA state (proc-thread only)
        self._ema_ch   = [0.0] * self._n
        self._ema_mean = 0.0
        self._prev_ema = 0.0
        self._stable_ctr = 0

        # Control events
        self._stop  = threading.Event()
        self._pause = threading.Event()   # set → acq-thread pauses
        self._paused = threading.Event()  # set → acq-thread confirmed paused

        if HW_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(cfg["clk_pin"], GPIO.OUT, initial=GPIO.LOW)

        for dout in cfg["dout_pins"]:
            self._cells.append(HX711(dout, cfg["clk_pin"]))

        log.info(f"ScaleManager init — {self._n} cells, "
                 f"CLK=GPIO{cfg['clk_pin']}, DOUT={cfg['dout_pins']}, "
                 f"HW={'yes' if HW_AVAILABLE else 'SIM'}")

    # ── Acquisition thread ─────────────────────────────────────────────────
    def _acq_loop(self):
        while not self._stop.is_set():
            if self._pause.is_set():
                self._paused.set()
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.01)
                self._paused.clear()
                continue

            for i, cell in enumerate(self._cells):
                if self._stop.is_set() or self._pause.is_set():
                    break
                raw = cell.read_raw()
                if raw is not None:
                    with self._ring_lock:
                        self._ring[i].append(raw)

    # ── Processing thread ──────────────────────────────────────────────────
    def _proc_loop(self):
        while not self._stop.is_set():
            time.sleep(0.08)   # process at ~12 Hz

            with self._ring_lock:
                snap = [list(buf) for buf in self._ring]

            kg_vals = []
            diags   = []

            for i in range(self._n):
                buf = snap[i]

                if len(buf) < 4:
                    kg_vals.append(self._ema_ch[i])
                    diags.append("no_data")
                    continue

                # Use most recent FILTER_WINDOW samples
                window = buf[-FILTER_WINDOW:]

                # Saturation check
                if abs(window[-1]) > 8_000_000:
                    kg_vals.append(self._ema_ch[i])
                    diags.append("saturated")
                    continue

                # Noise check
                try:
                    std = statistics.stdev(window)
                except Exception:
                    std = 0
                if std > 80_000:
                    kg_vals.append(self._ema_ch[i])
                    diags.append("noisy")
                    continue

                # Trimmed mean
                k       = max(1, int(len(window) * 0.15))
                trimmed = sorted(window)[k: len(window) - k]
                raw_avg = sum(trimmed) / len(trimmed) if trimmed else sum(window) / len(window)

                # Calibration
                offset = self.cfg["offsets"][i]
                factor = self.cfg["cal_factors"][i]
                factor = factor if abs(factor) >= 1 else 1.0
                kg     = (raw_avg - offset) / factor

                kg_vals.append(kg)
                diags.append("ok")

            mean_kg = sum(kg_vals) / len(kg_vals)

            # EMA per channel
            a = EMA_ALPHA
            ema_ch = [a * kg_vals[i] + (1 - a) * self._ema_ch[i]
                      for i in range(self._n)]
            ema_mean = a * mean_kg + (1 - a) * self._ema_mean

            # Stability
            if abs(ema_mean - self._prev_ema) < STABLE_THRESH:
                self._stable_ctr = min(self._stable_ctr + 1, STABLE_COUNT + 1)
            else:
                self._stable_ctr = 0
            stable = self._stable_ctr >= STABLE_COUNT

            self._ema_ch   = ema_ch
            self._ema_mean = ema_mean
            self._prev_ema = ema_mean

            with self._out_lock:
                self._out = {
                    "kg":       kg_vals,
                    "ema_kg":   ema_ch,
                    "mean_kg":  mean_kg,
                    "ema_mean": ema_mean,
                    "stable":   stable,
                    "diag":     diags,
                }

    # ── Public API ─────────────────────────────────────────────────────────
    def start(self):
        t1 = threading.Thread(target=self._acq_loop,  daemon=True, name="hx711-acq")
        t2 = threading.Thread(target=self._proc_loop, daemon=True, name="hx711-proc")
        t1.start()
        t2.start()
        log.info("HX711 acquisition + processing threads started")

    def get_values(self) -> dict:
        with self._out_lock:
            return dict(self._out)

    def _pause_acq(self, timeout=3.0) -> bool:
        self._paused.clear()
        self._pause.set()
        ok = self._paused.wait(timeout)
        if not ok:
            log.error("Acquisition thread did not pause in time")
            self._pause.clear()
        return ok

    def _resume_acq(self):
        self._pause.clear()

    def tare(self) -> bool:
        log.info("Tare: pausing acquisition…")
        if not self._pause_acq():
            return False

        log.info("Tare: reading 20 samples per cell…")
        new_offsets = []
        ok = True

        for i, cell in enumerate(self._cells):
            avg = cell.read_average(20)
            if avg is None:
                log.error(f"Tare: cell {i} failed")
                ok = False
                new_offsets.append(self.cfg["offsets"][i])  # keep old
            else:
                log.info(f"  Cell {i}: offset={avg:.1f}")
                new_offsets.append(avg)

        self._resume_acq()

        if ok:
            self.cfg["offsets"] = new_offsets
            save_config(self.cfg)
            # Reset EMA to zero
            with self._out_lock:
                self._ema_ch   = [0.0] * self._n
                self._ema_mean = 0.0
                self._prev_ema = 0.0
                self._stable_ctr = 0
            log.info(f"Tare done — offsets: {[round(o) for o in new_offsets]}")

        return ok

    def calibrate(self, known_kg: float) -> bool:
        if known_kg <= 0:
            log.error(f"Calibrate: invalid known_kg={known_kg}")
            return False

        log.info(f"Calibrate: {known_kg:.4f} kg — pausing acquisition…")
        if not self._pause_acq():
            return False

        log.info("Calibrate: reading 20 samples per cell…")
        new_factors = []
        valid       = []

        for i, cell in enumerate(self._cells):
            avg = cell.read_average(20)
            if avg is None:
                log.warning(f"  Cell {i}: no reads — skipping")
                new_factors.append(None)
                continue

            net = avg - self.cfg["offsets"][i]
            log.info(f"  Cell {i}: avg={avg:.1f} offset={self.cfg['offsets'][i]:.1f} net={net:.1f}")

            if abs(net) < 500:
                log.warning(f"  Cell {i}: net_raw too small ({net:.1f}) — skipping")
                new_factors.append(None)
                continue

            factor = net / known_kg
            if not (100 <= abs(factor) <= 5_000_000):
                log.warning(f"  Cell {i}: factor={factor:.1f} out of range — skipping")
                new_factors.append(None)
                continue

            log.info(f"  Cell {i}: factor={factor:.2f} ✓")
            new_factors.append(factor)
            valid.append(factor)

        self._resume_acq()

        if not valid:
            log.error("Calibrate: no valid factors — check wiring and weight")
            return False

        mean_f = sum(valid) / len(valid)
        final  = [f if f is not None else mean_f for f in new_factors]
        self.cfg["cal_factors"] = final
        save_config(self.cfg)
        log.info(f"Calibrate done — factors: {[round(f) for f in final]}")
        return True

    def cleanup(self):
        self._stop.set()
        if HW_AVAILABLE:
            GPIO.cleanup()


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED STATE for Flask
# ═══════════════════════════════════════════════════════════════════════════════

_shared = {
    "weights": [0.0]*4, "mean": 0.0,
    "status": "idle", "countdown": 0,
    "last_photo": "", "stable": False,
    "diag": ["no_data"]*4,
}
_shared_lock = threading.Lock()

def update_shared(**kw):
    with _shared_lock:
        _shared.update(kw)

def get_shared() -> dict:
    with _shared_lock:
        return dict(_shared)


# ═══════════════════════════════════════════════════════════════════════════════
# CAMERA
# ═══════════════════════════════════════════════════════════════════════════════

class Camera:
    def __init__(self, index, sw, sh, pw, ph):
        self.index = index
        self.sw, self.sh = sw, sh
        self.pw, self.ph = pw, ph
        self._lock  = threading.Lock()
        self._frame = None
        self._stop  = threading.Event()
        self._cap   = None
        self._photo_req  = threading.Event()
        self._photo_done = threading.Event()
        self._photo_frame = None

    def _open(self, w, h) -> bool:
        if self._cap:
            self._cap.release()
        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS,          30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
        self._cap = cap
        return cap.isOpened()

    def start(self) -> bool:
        if not self._open(self.sw, self.sh):
            log.error("Camera: failed to open")
            return False
        t = threading.Thread(target=self._grab_loop, daemon=True, name="camera")
        t.start()
        log.info(f"Camera: {self.sw}x{self.sh} MJPG started")
        return True

    def _grab_loop(self):
        try:
            os.nice(10)
        except Exception:
            pass
        while not self._stop.is_set():
            if self._photo_req.is_set():
                self._photo_req.clear()
                self._open(self.pw, self.ph)
                for _ in range(4):
                    self._cap.grab()
                ret, frame = self._cap.read()
                self._photo_frame = frame.copy() if ret else None
                self._photo_done.set()
                self._open(self.sw, self.sh)
                continue
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.005)

    def get_frame(self):
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def capture_photo(self, timeout=8.0):
        self._photo_frame = None
        self._photo_done.clear()
        self._photo_req.set()
        return self._photo_frame if self._photo_done.wait(timeout) else None

    def stop(self):
        self._stop.set()
        if self._cap:
            self._cap.release()


# ═══════════════════════════════════════════════════════════════════════════════
# PYGAME HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

COL_WHITE  = (255, 255, 255)
COL_BLACK  = (0,   0,   0  )
COL_GREEN  = (0,   210, 0  )
COL_YELLOW = (230, 200, 0  )
COL_RED    = (220, 50,  50 )
COL_ORANGE = (230, 130, 0  )
COL_STRIP  = (22,  22,  22 )
COL_LINE   = (65,  65,  65 )

def make_fonts():
    return {
        "small":  pygame.font.SysFont("monospace", 17),
        "medium": pygame.font.SysFont("monospace", 22),
        "large":  pygame.font.SysFont("monospace", 68, bold=True),
    }

def frame_to_surface(frame, w, h):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    if frame.shape[1] != w or frame.shape[0] != h:
        rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_LINEAR)
    return pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")

def draw_strip(screen, fonts, vals, labels, strip_y, dw, sh):
    pygame.draw.rect(screen, COL_STRIP, (0, strip_y, dw, sh))
    pygame.draw.line(screen, COL_LINE,  (0, strip_y), (dw, strip_y), 1)

    ema  = vals["ema_kg"]
    mean = vals["ema_mean"]
    stbl = vals["stable"]

    parts = [f"{labels[i]}: {ema[i]:.3f}kg" for i in range(4)]
    mean_txt = f"MEAN: {mean:.3f}kg" + (" ✓" if stbl else "")
    text = "   |   ".join(parts) + "   |   " + mean_txt

    col  = (0, 210, 0) if stbl else COL_WHITE
    surf = fonts["small"].render(text, True, col)
    tx   = max(8, (dw - surf.get_width()) // 2)
    ty   = strip_y + (sh - surf.get_height()) // 2
    screen.blit(surf, (tx, ty))

def draw_countdown(screen, fonts, secs, dw, dh):
    txt    = f"Photo in {secs:.1f}s"
    shadow = fonts["large"].render(txt, True, COL_BLACK)
    surf   = fonts["large"].render(txt, True, COL_YELLOW)
    cx = (dw - surf.get_width())  // 2
    cy = (dh - surf.get_height()) // 2
    screen.blit(shadow, (cx+2, cy+2))
    screen.blit(surf,   (cx,   cy  ))

def draw_msg(screen, fonts, text, color):
    surf = fonts["medium"].render(text, True, color)
    screen.blit(surf, (10, 10))


# ═══════════════════════════════════════════════════════════════════════════════
# PHOTO SAVE
# ═══════════════════════════════════════════════════════════════════════════════

def save_photo(frame, vals, labels, strip_h) -> str:
    photo   = frame.copy()
    h, w    = photo.shape[:2]
    strip_y = h - strip_h

    cv2.rectangle(photo, (0, strip_y), (w, h), (22, 22, 22), -1)
    cv2.line(photo, (0, strip_y), (w, strip_y), (65, 65, 65), 1)

    ema  = vals["ema_kg"]
    mean = vals["ema_mean"]
    parts = [f"{labels[i]}: {ema[i]:.3f}kg" for i in range(4)]
    parts.append(f"MEAN: {mean:.3f}kg")
    cv2.putText(photo, "   |   ".join(parts),
                (10, strip_y + strip_h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                (255, 255, 255), 1, cv2.LINE_AA)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTOS_DIR, f"capture_{ts}.jpg")
    cv2.imwrite(path, photo, [cv2.IMWRITE_JPEG_QUALITY, 95])
    log.info(f"Photo saved: {path} ({w}x{h})")
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    cfg = load_config()

    # ── Scale: start acquisition first, then tare ──────────────────────────
    scale = ScaleManager(cfg)
    scale.start()
    log.info("Startup tare — platform must be EMPTY. Waiting 5s for sensors…")
    time.sleep(5)
    scale.tare()

    # ── Camera ─────────────────────────────────────────────────────────────
    cam    = Camera(cfg["camera_index"],
                    cfg["stream_width"], cfg["stream_height"],
                    cfg["photo_width"],  cfg["photo_height"])
    cam_ok = cam.start()

    # ── Pygame ─────────────────────────────────────────────────────────────
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")
    pygame.init()
    DW, DH  = cfg["display_width"], cfg["display_height"]
    screen  = pygame.display.set_mode((DW, DH), pygame.FULLSCREEN)
    pygame.display.set_caption("SmartScale")
    pygame.mouse.set_visible(False)
    fonts   = make_fonts()
    clock   = pygame.time.Clock()
    strip_h = cfg["weight_strip_height"]
    strip_y = DH - strip_h

    trigger_kg  = cfg["trigger_weight_kg"]
    stabilise_s = cfg["stabilise_seconds"]
    labels      = cfg["cell_labels"]

    state         = "idle"
    countdown_end = 0.0
    cooldown_end  = 0.0
    last_photo    = ""
    remaining     = 0.0
    cfg_mtime     = 0.0

    log.info("Main loop started")

    while True:
        # ── Events ─────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cam.stop(); pygame.quit(); scale.cleanup(); return
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    cam.stop(); pygame.quit(); scale.cleanup()
                    log.info("Stopped"); return
                elif event.key == pygame.K_t:
                    threading.Thread(target=scale.tare, daemon=True).start()

        # ── Config hot-reload ───────────────────────────────────────────
        try:
            mt = os.path.getmtime(CONFIG_FILE)
            if mt != cfg_mtime:
                cfg_mtime   = mt
                cfg         = load_config()
                scale.cfg   = cfg
                trigger_kg  = cfg["trigger_weight_kg"]
                stabilise_s = cfg["stabilise_seconds"]
                labels      = cfg["cell_labels"]
                strip_h     = cfg["weight_strip_height"]
                strip_y     = DH - strip_h
                log.info("Config reloaded")
        except Exception:
            pass

        # ── Weights ─────────────────────────────────────────────────────
        vals    = scale.get_values()
        mean_kg = vals["ema_mean"]
        now     = time.time()

        # ── State machine ───────────────────────────────────────────────
        if state == "idle":
            if mean_kg >= trigger_kg:
                state         = "countdown"
                countdown_end = now + stabilise_s
                log.info(f"Trigger: {mean_kg:.3f}kg")
            update_shared(weights=vals["ema_kg"], mean=mean_kg,
                          status="idle", stable=vals["stable"], diag=vals["diag"])

        elif state == "countdown":
            remaining = countdown_end - now
            if mean_kg < trigger_kg:
                state = "idle"
                log.info("Weight removed — cancelled")
            elif remaining <= 0:
                photo_frame = cam.capture_photo() if cam_ok else None
                if photo_frame is not None:
                    last_photo = save_photo(photo_frame, vals, labels, strip_h)
                state        = "cooldown"
                cooldown_end = now + 3.0
            update_shared(weights=vals["ema_kg"], mean=mean_kg,
                          status="countdown", countdown=max(0, remaining),
                          last_photo=last_photo, stable=vals["stable"], diag=vals["diag"])

        elif state == "cooldown":
            if now >= cooldown_end and mean_kg < trigger_kg:
                state = "idle"
            update_shared(weights=vals["ema_kg"], mean=mean_kg,
                          status="cooldown", last_photo=last_photo,
                          stable=vals["stable"], diag=vals["diag"])

        # ── Draw ────────────────────────────────────────────────────────
        frame = cam.get_frame() if cam_ok else None
        if frame is not None:
            screen.blit(frame_to_surface(frame, DW, DH), (0, 0))
        else:
            screen.fill(COL_BLACK)
            if cam_ok:
                draw_msg(screen, fonts, "Waiting for camera…", COL_ORANGE)

        if state == "countdown":
            draw_countdown(screen, fonts, remaining, DW, DH)
        elif state == "cooldown":
            draw_msg(screen, fonts, "Photo saved!", COL_GREEN)

        draw_strip(screen, fonts, vals, labels, strip_y, DW, strip_h)
        pygame.display.flip()
        clock.tick(20)

    cam.stop()
    pygame.quit()
    scale.cleanup()
    log.info("Stopped")


if __name__ == "__main__":
    main()