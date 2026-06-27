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
    "trigger_weight_kg":    0.5,        # kg — trigger threshold
    "stabilise_seconds":    5.0,
    "unit":                 "kg",
    "cell_labels":          ["C1", "C2", "C3", "C4"],
    "clk_pin":              6,
    "dout_pins":            [5, 13, 19, 26],
    "offsets":              [0, 0, 0, 0],
    "cal_factors":          [1.0, 1.0, 1.0, 1.0],
    "camera_index":         0,
    # Stream res (display) — lower = faster framerate
    "stream_width":         640,
    "stream_height":        480,
    # Photo res — always 720p
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
            # Migrate old grams trigger key if present
            if "trigger_weight_g" in cfg and "trigger_weight_kg" not in cfg:
                cfg["trigger_weight_kg"] = cfg["trigger_weight_g"] / 1000.0
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
    log.warning("RPi.GPIO not available — simulation mode")

class HX711:
    """Reliable HX711 driver. Gain=128, Channel A."""
    GAIN = 1

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

    def read_average(self, times=3):
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
    """
    Runs HX711 reads in a background thread.
    Main loop calls get_weights() which returns last known values instantly.
    Calibration factors convert raw → kg directly.
    """
    def __init__(self, cfg):
        self.cfg     = cfg
        self.cells   = []
        self._lock   = threading.Lock()
        self._raw    = [0, 0, 0, 0]
        self._kg     = [0.0, 0.0, 0.0, 0.0]
        self._mean   = 0.0
        self._stop   = threading.Event()

        if HW_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

        for dout in cfg["dout_pins"]:
            self.cells.append(HX711(dout, cfg["clk_pin"]))

        log.info(f"ScaleManager init — HW={'yes' if HW_AVAILABLE else 'SIM'}")

    def _raw_to_kg(self, raw, idx):
        offset = self.cfg["offsets"][idx]
        factor = self.cfg["cal_factors"][idx]   # factor = raw_per_kg
        if factor == 0:
            return 0.0
        return (raw - offset) / factor

    def _bg_loop(self):
        """Background thread: continuously reads all 4 cells."""
        while not self._stop.is_set():
            kg_vals = []
            for i, cell in enumerate(self.cells):
                raw = cell.read_average(3)
                if raw is None:
                    raw = self._raw[i]
                self._raw[i] = raw
                kg_vals.append(self._raw_to_kg(raw, i))

            mean = sum(kg_vals) / len(kg_vals)
            with self._lock:
                self._kg   = kg_vals
                self._mean = mean

    def start(self):
        self._thread = threading.Thread(target=self._bg_loop, daemon=True, name="hx711")
        self._thread.start()
        log.info("HX711 background thread started")

    def stop(self):
        self._stop.set()

    def get_weights(self):
        """Returns (list_of_4_kg, mean_kg) — instant, never blocks."""
        with self._lock:
            return list(self._kg), self._mean

    def tare(self):
        log.info("Taring — reading zero offsets…")
        for i, cell in enumerate(self.cells):
            raw = cell.read_average(15)   # more samples for tare accuracy
            if raw is not None:
                self.cfg["offsets"][i] = raw
        save_config(self.cfg)
        # Reset displayed weights to zero
        with self._lock:
            self._kg   = [0.0, 0.0, 0.0, 0.0]
            self._mean = 0.0
        log.info(f"Tare done — offsets: {self.cfg['offsets']}")

    def calibrate(self, known_kg):
        """
        Place known_kg on platform, call this.
        Computes cal_factor = net_raw / known_kg per cell.
        """
        log.info(f"Calibrating with {known_kg:.3f} kg reference…")
        raws = []
        for cell in self.cells:
            raw = cell.read_average(15)
            raws.append(raw if raw is not None else self.cfg["offsets"][0])

        net_raws = [r - self.cfg["offsets"][i] for i, r in enumerate(raws)]
        mean_net = sum(net_raws) / len(net_raws)

        for i in range(len(self.cells)):
            nr = net_raws[i]
            if abs(nr) > 100:   # only use cells with meaningful signal
                self.cfg["cal_factors"][i] = nr / known_kg
            else:
                # Cell didn't respond — use mean
                self.cfg["cal_factors"][i] = mean_net / known_kg if mean_net != 0 else 1.0

        save_config(self.cfg)
        log.info(f"Calibration done — factors: {self.cfg['cal_factors']}")

    def cleanup(self):
        self.stop()
        if HW_AVAILABLE:
            GPIO.cleanup()


# ── Shared state for Flask web server ─────────────────────────────────────────
shared = {
    "weights":    [0.0, 0.0, 0.0, 0.0],   # kg
    "mean":       0.0,                      # kg
    "status":     "idle",
    "countdown":  0,
    "last_photo": "",
}
_shared_lock = threading.Lock()

def update_shared(weights, mean, status, countdown=0, last_photo=""):
    with _shared_lock:
        shared["weights"]    = weights
        shared["mean"]       = mean
        shared["status"]     = status
        shared["countdown"]  = countdown
        shared["last_photo"] = last_photo

def get_shared():
    with _shared_lock:
        return dict(shared)


# ── Camera — dual mode (stream vs photo) ──────────────────────────────────────
class Camera:
    """
    Grabs frames in background thread.
    Stream frames: 640x480 MJPG — fast, for display.
    Photo frames:  switches to 1280x720 momentarily, grabs, switches back.
    """
    def __init__(self, index, stream_w, stream_h, photo_w, photo_h):
        self.index    = index
        self.sw, self.sh = stream_w, stream_h
        self.pw, self.ph = photo_w,  photo_h
        self._lock    = threading.Lock()
        self._frame   = None
        self._stop    = threading.Event()
        self._cap     = None
        self._photo_request = threading.Event()
        self._photo_frame   = None
        self._photo_ready   = threading.Event()

    def _open(self, w, h):
        if self._cap:
            self._cap.release()
        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # minimal buffer — always fresh frame
        self._cap = cap
        return cap.isOpened()

    def start(self):
        ok = self._open(self.sw, self.sh)
        if not ok:
            log.error("Camera failed to open")
            return False
        self._thread = threading.Thread(target=self._grab_loop, daemon=True, name="camera")
        self._thread.start()
        log.info(f"Camera started — stream {self.sw}x{self.sh} MJPG")
        return True

    def _grab_loop(self):
        while not self._stop.is_set():
            # Photo request — switch res, grab, switch back
            if self._photo_request.is_set():
                self._photo_request.clear()
                log.info("Switching to photo resolution…")
                self._open(self.pw, self.ph)
                # Flush buffer — grab a few frames
                for _ in range(3):
                    self._cap.grab()
                ret, frame = self._cap.read()
                if ret:
                    self._photo_frame = frame.copy()
                self._photo_ready.set()
                # Switch back to stream res
                self._open(self.sw, self.sh)
                log.info("Back to stream resolution")
                continue

            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.01)

    def get_frame(self):
        """Returns latest stream frame or None."""
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def capture_photo(self, timeout=5.0):
        """
        Request a full-res photo. Blocks until ready.
        Returns frame or None on timeout.
        """
        self._photo_frame = None
        self._photo_ready.clear()
        self._photo_request.set()
        if self._photo_ready.wait(timeout):
            return self._photo_frame
        log.error("Photo capture timed out")
        return None

    def stop(self):
        self._stop.set()
        if self._cap:
            self._cap.release()


# ── Pygame helpers ────────────────────────────────────────────────────────────
COL_WHITE  = (255, 255, 255)
COL_BLACK  = (0,   0,   0  )
COL_GREEN  = (0,   210, 0  )
COL_YELLOW = (230, 210, 0  )
COL_RED    = (220, 50,  50 )
COL_STRIP  = (25,  25,  25 )
COL_LINE   = (70,  70,  70 )

def make_fonts():
    return {
        "small":  pygame.font.SysFont("monospace", 18),
        "medium": pygame.font.SysFont("monospace", 22),
        "large":  pygame.font.SysFont("monospace", 72, bold=True),
    }

def frame_to_surface(frame, w, h):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_LINEAR)
    return pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")

def draw_strip(screen, fonts, weights_kg, mean_kg, labels, strip_y, sw, sh):
    pygame.draw.rect(screen, COL_STRIP, (0, strip_y, sw, sh))
    pygame.draw.line(screen, COL_LINE,  (0, strip_y), (sw, strip_y), 1)

    parts = [f"{labels[i]}: {weights_kg[i]:.3f}kg" for i in range(4)]
    parts.append(f"MEAN: {mean_kg:.3f}kg")
    text = "   |   ".join(parts)

    surf = fonts["small"].render(text, True, COL_WHITE)
    tx   = max(8, (sw - surf.get_width()) // 2)
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


# ── Photo save — full res with weight strip burned in ─────────────────────────
def save_photo(frame, weights_kg, mean_kg, labels, photos_dir, strip_h):
    photo   = frame.copy()
    h, w    = photo.shape[:2]
    strip_y = h - strip_h

    cv2.rectangle(photo, (0, strip_y), (w, h), (25, 25, 25), -1)
    cv2.line(photo, (0, strip_y), (w, strip_y), (70, 70, 70), 1)

    parts = [f"{labels[i]}: {weights_kg[i]:.3f}kg" for i in range(4)]
    parts.append(f"MEAN: {mean_kg:.3f}kg")
    text  = "   |   ".join(parts)

    cv2.putText(photo, text, (10, strip_y + strip_h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(photos_dir, f"capture_{ts}.jpg")
    cv2.imwrite(path, photo, [cv2.IMWRITE_JPEG_QUALITY, 95])
    log.info(f"Photo saved: {path}  ({w}x{h})")
    return path


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    cfg = load_config()

    # ── Scale ──────────────────────────────────────────────────────────────
    scale = ScaleManager(cfg)
    log.info("Startup tare — platform must be EMPTY")
    time.sleep(2)
    scale.tare()
    scale.start()   # begin background reading

    # ── Camera ─────────────────────────────────────────────────────────────
    cam = Camera(
        cfg["camera_index"],
        cfg["stream_width"],  cfg["stream_height"],
        cfg["photo_width"],   cfg["photo_height"],
    )
    if not cam.start():
        log.error("Cannot open camera — exiting")
        scale.cleanup()
        return

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
    cfg_mtime     = 0.0   # track config file modification time

    log.info("Main loop started")

    while True:
        # ── Events ─────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cam.stop(); pygame.quit(); scale.cleanup(); return
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    cam.stop(); pygame.quit(); scale.cleanup()
                    log.info("Stopped (q key)"); return
                elif event.key == pygame.K_t:
                    log.info("Manual tare")
                    scale.tare()

        # ── Hot-reload config (only when file changes) ──────────────────
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

        # ── Get latest camera frame ─────────────────────────────────────
        frame = cam.get_frame()

        # ── Get latest weight reading ───────────────────────────────────
        weights_kg, mean_kg = scale.get_weights()

        now = time.time()

        # ── State machine ───────────────────────────────────────────────
        if state == "idle":
            if mean_kg >= trigger_kg:
                state         = "countdown"
                countdown_end = now + stabilise_s
                log.info(f"Trigger: {mean_kg:.3f}kg >= {trigger_kg:.3f}kg")
            update_shared(weights_kg, mean_kg, "idle", 0, last_photo)

        elif state == "countdown":
            remaining = countdown_end - now
            if mean_kg < trigger_kg:
                state = "idle"
                log.info("Weight removed — countdown cancelled")
            elif remaining <= 0:
                # Capture at full resolution
                photo_frame = cam.capture_photo()
                if photo_frame is not None:
                    last_photo = save_photo(
                        photo_frame, weights_kg, mean_kg,
                        labels, PHOTOS_DIR, strip_h
                    )
                state        = "cooldown"
                cooldown_end = now + 3.0
            update_shared(weights_kg, mean_kg, "countdown", max(0, remaining), last_photo)

        elif state == "cooldown":
            if now >= cooldown_end and mean_kg < trigger_kg:
                state = "idle"
            update_shared(weights_kg, mean_kg, "cooldown", 0, last_photo)

        # ── Draw ────────────────────────────────────────────────────────
        if frame is not None:
            screen.blit(frame_to_surface(frame, DW, DH), (0, 0))
        else:
            screen.fill(COL_BLACK)
            draw_msg(screen, fonts, "Waiting for camera…", COL_RED)

        if state == "countdown":
            draw_countdown(screen, fonts, remaining, DW, DH)
        elif state == "cooldown":
            draw_msg(screen, fonts, "Photo saved!", COL_GREEN)

        draw_strip(screen, fonts, weights_kg, mean_kg, labels, strip_y, DW, strip_h)
        pygame.display.flip()
        clock.tick(20)   # 20fps display cap — leaves CPU headroom for HX711 thread

    cam.stop()
    pygame.quit()
    scale.cleanup()
    log.info("Scale app stopped")


if __name__ == "__main__":
    main()