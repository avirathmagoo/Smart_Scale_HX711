"""
Smart Scale — Main Application  (UART / ESP32 edition, v3)
Raspberry Pi 4B | ESP32 (4x HX711 bridge, over UART) | USB Webcam | 1024x600

ARCHITECTURE
  uart-reader thread — reads ESP32 packets, validates checksum, fills ring buffers
  hx711-proc  thread — filtering + calibration (Pi-side), publishes gram values
  camera      thread — frame grab (lowest priority)
  button      thread — GPIO17 hardware capture button (interrupt-driven)
  main        thread — pygame display + state machine + touch controls

The ESP32 ONLY reads sensors and streams raw data — it has no tare/command
handling at all (v2 firmware). All tare and calibration math lives entirely
on the Pi. This is a deliberate simplification: an earlier version sent a
tare command to the ESP32, but the ESP32's tare routine paused its main
loop long enough that the Pi saw it as a dead link. Removing that
coordination entirely fixed both problems at once — see context.md §12.

DISPLAY UNITS: everything shown to a person — screen, photos, web page — is
in whole GRAMS, no decimals. Calibration math internally still works in kg
(cal_factors are "raw counts per kg", same convention as before) purely
because that's a convenient calibration-time unit; the moment a value is
meant for a human it's converted to grams and rounded.
"""

import cv2
import pygame
import time
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import logging
import zipfile
import collections
import statistics
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
    "control_bar_height":   50,
    "fullscreen":           False,
    "trigger_weight_g":     500,     # grams — everything user-facing is grams
    "stabilise_seconds":    5.0,
    "cell_labels":          ["C1", "C2", "C3", "C4"],
    # UART link to the ESP32
    "uart_port":             "/dev/serial0",
    "uart_baud":             115200,
    # Hardware capture button
    "button_gpio":           17,
    # Calibration — internally raw-counts-per-KG (calibration-time convenience
    # unit only); every consumer of ScaleManager output sees grams.
    "offsets":              [0, 0, 0, 0],
    "cal_factors":          [1.0, 1.0, 1.0, 1.0],
    # Camera
    "camera_index":         0,
    "stream_width":         640,
    "stream_height":        480,
    "photo_width":          1280,
    "photo_height":         720,
    # Behaviour
    "autocapture_enabled":  True,
    # Storage cap — 32GB SD card; default leaves headroom for OS + packages
    "photos_max_mb":        20000,
}

_cfg_lock = threading.Lock()

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
    with _cfg_lock:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# UART READER — talks to the ESP32
try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    log.warning("pyserial not available")

LINK_TIMEOUT = 2.0

class UARTReader:
    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self._latest = [0.0] * 4
        self._lock = threading.Lock()
        self._last_packet_time = 0.0
        self._valid_count = 0
        self._invalid_count = 0
        self._stop = threading.Event()
        self._ser = None

        if SERIAL_AVAILABLE:
            try:
                self._ser = serial.Serial(port, baud, timeout=1.0)
                log.info(f"UART opened: {port} @ {baud}")
            except Exception as e:
                log.error(f"UART open failed ({port}): {e}")

    def start(self):
        threading.Thread(target=self._read_loop, daemon=True, name="uart-reader").start()

    def _read_loop(self):
        while not self._stop.is_set():
            if self._ser is None:
                time.sleep(0.2)
                continue

            try:
                line = self._ser.readline().decode("ascii", errors="ignore").strip()
            except Exception as e:
                log.warning(f"UART read error: {e}")
                time.sleep(0.2)
                continue

            if not line:
                continue

            self._handle_data_line(line)

    def _handle_data_line(self, line: str):
        parts = line.split(",")

        if len(parts) != 4:
            self._invalid_count += 1
            return

        try:
            values = [float(part.strip()) for part in parts]
        except ValueError:
            self._invalid_count += 1
            return

        with self._lock:
            self._latest = values

        self._valid_count += 1
        self._last_packet_time = time.time()

    def get_latest(self):
        with self._lock:
            return list(self._latest)

    @property
    def connected(self) -> bool:
        return (time.time() - self._last_packet_time) < LINK_TIMEOUT

    def stats(self):
        return {
            "connected": self.connected,
            "valid_packets": self._valid_count,
            "invalid_packets": self._invalid_count,
        }

    def cleanup(self):
        self._stop.set()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


# SCALE PROCESSING — ESP32 does all scale calculation.
# The Pi performs only tare: displayed value = ESP32 value - tare offset.
class ScaleManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.uart = UARTReader(cfg["uart_port"], cfg["uart_baud"])
        self._out_lock = threading.Lock()
        self._out = {
            "g": [0.0] * 4,
            "ema_g": [0.0] * 4,
            "disp_g": [0.0] * 4,
            "mean_g": 0.0,
            "ema_mean_g": 0.0,
            "disp_mean_g": 0.0,
            "stable": True,
            "diag": ["no_data"] * 4,
            "uart_ok": False,
        }
        self._stop = threading.Event()
        log.info(f"ScaleManager init — UART={cfg['uart_port']}@{cfg['uart_baud']}")

    def start(self):
        self.uart.start()
        threading.Thread(target=self._proc_loop, daemon=True, name="scale-values").start()

    def _proc_loop(self):
        while not self._stop.is_set():
            time.sleep(0.05)
            raw = self.uart.get_latest()
            link_ok = self.uart.connected
            offsets = self.cfg.get("offsets", [0.0] * 4)

            if link_ok:
                values = [raw[i] - offsets[i] for i in range(4)]
                diags = ["ok"] * 4
            else:
                values = [0.0] * 4
                diags = ["no_data"] * 4

            mean_g = sum(values) / 4.0

            with self._out_lock:
                self._out = {
                    "g": values,
                    "ema_g": values,
                    "disp_g": values,
                    "mean_g": mean_g,
                    "ema_mean_g": mean_g,
                    "disp_mean_g": mean_g,
                    "stable": True,
                    "diag": diags,
                    "uart_ok": link_ok,
                }

    def get_values(self) -> dict:
        with self._out_lock:
            return dict(self._out)

    def tare(self) -> bool:
        if not self.uart.connected:
            log.error("Tare failed — no valid ESP32 data")
            return False

        values = self.uart.get_latest()
        self.cfg["offsets"] = values
        save_config(self.cfg)
        log.info(f"Tare done — offsets: {[round(v, 3) for v in values]}")
        return True

    def calibrate(self, known_kg: float) -> bool:
        log.warning("Calibration is disabled. ESP32 values are used directly.")
        return False

    def diagnostics(self) -> dict:
        return {**self.uart.stats(), "offsets": self.cfg.get("offsets", [0, 0, 0, 0]),
                "cal_factors": self.cfg.get("cal_factors", [1, 1, 1, 1])}

    def cleanup(self):
        self._stop.set()
        self.uart.cleanup()


# SHARED STATE for Flask
_shared = {
    "weights_g": [0] * 4,
    "mean_g": 0,
    "status": "idle",
    "countdown": 0,
    "last_photo": "",
    "stable": True,
    "diag": ["no_data"] * 4,
    "uart_ok": False,
    "autocapture_enabled": True,
}
_shared_lock = threading.Lock()


def update_shared(**kw):
    with _shared_lock:
        _shared.update(kw)


def get_shared() -> dict:
    with _shared_lock:
        return dict(_shared)


_scale_manager = None


def get_scale_manager():
    return _scale_manager


# HARDWARE CAPTURE BUTTON — GPIO17, grounded = pressed
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    log.warning("RPi.GPIO not available — hardware capture button disabled")


class CaptureButton:
    def __init__(self, pin: int):
        self.pin = pin
        self.event = threading.Event()
        self._enabled = False
        if GPIO_AVAILABLE:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                GPIO.add_event_detect(pin, GPIO.FALLING,
                                       callback=self._on_press, bouncetime=250)
                self._enabled = True
                log.info(f"Capture button armed on GPIO{pin}")
            except Exception as e:
                log.error(f"Capture button setup failed on GPIO{pin}: {e} — "
                          f"hardware button disabled, on-screen button still works. "
                          f"See context.md troubleshooting if this persists "
                          f"(often a gpio-group or RPi.GPIO/kernel compatibility issue).")

    def _on_press(self, channel):
        self.event.set()

    def consume(self) -> bool:
        """Returns True exactly once per press."""
        if self.event.is_set():
            self.event.clear()
            return True
        return False

    def cleanup(self):
        if self._enabled:
            try:
                GPIO.remove_event_detect(self.pin)
            except Exception:
                pass


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
# PHOTO SAVE + STORAGE CAP  (grams, no decimals, in the caption)
# ═══════════════════════════════════════════════════════════════════════════════

def save_photo(frame, vals, labels, strip_h) -> str:
    photo   = frame.copy()
    h, w    = photo.shape[:2]
    strip_y = h - strip_h

    cv2.rectangle(photo, (0, strip_y), (w, h), (22, 22, 22), -1)
    cv2.line(photo, (0, strip_y), (w, strip_y), (65, 65, 65), 1)

    disp_g = vals["disp_g"]
    mean_g = vals["disp_mean_g"]
    parts = [f"{labels[i]}: {disp_g[i]}g" for i in range(4)]
    parts.append(f"MEAN: {mean_g}g")
    cv2.putText(photo, "   |   ".join(parts),
                (10, strip_y + strip_h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                (255, 255, 255), 1, cv2.LINE_AA)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTOS_DIR, f"capture_{ts}.jpg")
    cv2.imwrite(path, photo, [cv2.IMWRITE_JPEG_QUALITY, 95])
    log.info(f"Photo saved: {path} ({w}x{h})")
    return path


def enforce_storage_cap(max_mb: int):
    """Deletes the oldest photos until the photos/ folder is back under
    the configured cap. Runs after every save; cheap (folder is small)."""
    try:
        cap_bytes = max_mb * 1024 * 1024
        files = sorted(Path(PHOTOS_DIR).glob("*.jpg"), key=os.path.getmtime)
        total = sum(f.stat().st_size for f in files)
        removed = 0
        while total > cap_bytes and files:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink()
            removed += 1
        if removed:
            log.info(f"Storage cap: removed {removed} oldest photo(s), "
                      f"now {total/1024/1024:.1f}MB / {max_mb}MB")
    except Exception as e:
        log.error(f"Storage cap check failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# USB EXPORT  — zips photos/ and copies it to an inserted USB drive
# ═══════════════════════════════════════════════════════════════════════════════

def _find_usb_partition():
    """Returns a /dev/sdXN path for the first USB-attached partition with a
    filesystem, or None if nothing suitable is found."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TRAN,TYPE,FSTYPE,MOUNTPOINT"],
            capture_output=True, text=True, timeout=5, check=True
        ).stdout
        data = json.loads(out)
    except Exception as e:
        log.error(f"USB export: lsblk failed: {e}")
        return None

    for dev in data.get("blockdevices", []):
        if dev.get("tran") != "usb":
            continue
        for child in dev.get("children", []) or []:
            if child.get("type") == "part" and child.get("fstype"):
                return f"/dev/{child['name']}"
    return None


def _clean_udisks_path(raw: str) -> str:
    """udisksctl wraps paths in old-style Unix quoting on some message
    types — e.g. an "already mounted" error prints `/media/x/Y'. (backtick
    ... apostrophe-period), while a fresh "Mounted at" message doesn't.
    Strip any of that decorative punctuation so both forms parse the same."""
    return raw.strip("`'\".,")


def _udisks_mount(devpath: str):
    try:
        r = subprocess.run(["udisksctl", "mount", "-b", devpath],
                            capture_output=True, text=True, timeout=15)
        combined = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"at\s+(\S+)", combined)
        if m:
            return _clean_udisks_path(m.group(1))
        return None
    except Exception as e:
        log.error(f"USB export: mount failed: {e}")
        return None


def _udisks_unmount(devpath: str):
    try:
        subprocess.run(["udisksctl", "unmount", "-b", devpath],
                        capture_output=True, text=True, timeout=15)
    except Exception as e:
        log.warning(f"USB export: unmount failed (drive can still be removed): {e}")


def export_photos_to_usb(status_cb):
    """Zips PHOTOS_DIR and copies the zip to a plugged-in USB drive.
    status_cb(text) is called with short progress strings the caller can
    show on screen. Never deletes anything from the Pi.

    Requires passwordless udisks2 mount for the current user — see the
    polkit rule in SETUP.md (Part 6). Without it, mounting will hang
    waiting for a password prompt no one can answer from the touchscreen."""
    status_cb("Looking for USB drive...")
    dev = _find_usb_partition()
    if not dev:
        status_cb("No USB drive found")
        return False

    status_cb("Mounting drive...")
    mount_point = _udisks_mount(dev)
    if not mount_point:
        status_cb("Mount failed (check polkit rule — see SETUP.md)")
        return False

    try:
        status_cb("Zipping photos...")
        photos = list(Path(PHOTOS_DIR).glob("*.jpg"))
        if not photos:
            status_cb("No photos to export")
            return False

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"smartscale_photos_{ts}.zip"
        tmp_zip = os.path.join(tempfile.gettempdir(), zip_name)
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in photos:
                zf.write(p, p.name)

        status_cb("Copying to USB...")
        shutil.copy(tmp_zip, os.path.join(mount_point, zip_name))
        os.remove(tmp_zip)

        status_cb("Transfer complete!")
        log.info(f"USB export: {len(photos)} photos -> {mount_point}/{zip_name}")
        return True
    except Exception as e:
        log.error(f"USB export failed: {e}")
        status_cb(f"Export failed: {e}")
        return False
    finally:
        _udisks_unmount(dev)


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
COL_BTN    = (45,  45,  45 )
COL_BTN_ON = (35,  110, 35 )
COL_BTN_WARN   = (110, 40, 40)
COL_BTN_BORDER = (90, 90, 90)

def make_fonts():
    return {
        "small":  pygame.font.SysFont("monospace", 17),
        "medium": pygame.font.SysFont("monospace", 22),
        "large":  pygame.font.SysFont("monospace", 68, bold=True),
    }

def blit_camera_frame(screen, frame, area: pygame.Rect):
    """Draws the camera frame INSIDE `area`, preserving its aspect ratio
    (letterboxed with black bars if the frame's aspect doesn't match the
    area's) instead of stretching it to fill a mismatched rectangle."""
    fh, fw = frame.shape[:2]
    if fw == 0 or fh == 0:
        return

    scale = min(area.w / fw, area.h / fh)
    draw_w, draw_h = int(fw * scale), int(fh * scale)
    off_x = area.x + (area.w - draw_w) // 2
    off_y = area.y + (area.h - draw_h) // 2

    pygame.draw.rect(screen, COL_BLACK, area)  # letterbox background

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (draw_w, draw_h), interpolation=cv2.INTER_LINEAR)
    surf = pygame.image.frombuffer(rgb.tobytes(), (draw_w, draw_h), "RGB")
    screen.blit(surf, (off_x, off_y))

def draw_strip(screen, fonts, vals, labels, strip_y, dw, sh):
    pygame.draw.rect(screen, COL_STRIP, (0, strip_y, dw, sh))
    pygame.draw.line(screen, COL_LINE,  (0, strip_y), (dw, strip_y), 1)

    disp_g = vals["disp_g"]
    mean_g = vals["disp_mean_g"]
    stbl   = vals["stable"]
    uart_ok = vals.get("uart_ok", True)

    parts = [f"{labels[i]}: {disp_g[i]}g" for i in range(4)]
    mean_txt = f"MEAN: {mean_g}g" + (" \u2713" if stbl else "")
    text = "   |   ".join(parts) + "   |   " + mean_txt
    if not uart_ok:
        text = "ESP32 LINK LOST — " + text

    col  = COL_RED if not uart_ok else ((0, 210, 0) if stbl else COL_WHITE)
    surf = fonts["small"].render(text, True, col)
    tx   = max(8, (dw - surf.get_width()) // 2)
    ty   = strip_y + (sh - surf.get_height()) // 2
    screen.blit(surf, (tx, ty))

def draw_countdown(screen, fonts, secs, area: pygame.Rect):
    txt    = f"Photo in {secs:.1f}s"
    shadow = fonts["large"].render(txt, True, COL_BLACK)
    surf   = fonts["large"].render(txt, True, COL_YELLOW)
    cx = area.x + (area.w - surf.get_width())  // 2
    cy = area.y + (area.h - surf.get_height()) // 2
    screen.blit(shadow, (cx+2, cy+2))
    screen.blit(surf,   (cx,   cy  ))

def draw_msg(screen, fonts, text, color, x, y):
    surf = fonts["medium"].render(text, True, color)
    screen.blit(surf, (x, y))


# ── Control bar (top strip): TARE | CAPTURE | AUTO [slider] | EXPORT | QUIT | SHUTDOWN
BUTTON_ORDER = ("tare", "capture", "auto", "export", "quit", "shutdown")

def make_control_rects(dw, bar_h):
    margin = 6
    n = len(BUTTON_ORDER)
    w = (dw - margin * (n + 1)) // n
    rects = {}
    x = margin
    for name in BUTTON_ORDER:
        rects[name] = pygame.Rect(x, 4, w, bar_h - 8)
        x += w + margin
    return rects

def draw_control_bar(screen, fonts, rects, dw, bar_h, autocapture_on,
                      busy_name, status_text, shutdown_armed):
    pygame.draw.rect(screen, COL_STRIP, (0, 0, dw, bar_h))
    pygame.draw.line(screen, COL_LINE, (0, bar_h), (dw, bar_h), 1)

    def draw_btn(rect, label, on=False, busy=False, warn=False):
        color = COL_BTN_WARN if warn else (COL_BTN_ON if on else COL_BTN)
        pygame.draw.rect(screen, color, rect, border_radius=6)
        pygame.draw.rect(screen, COL_BTN_BORDER, rect, 1, border_radius=6)
        txt = "..." if busy else label
        surf = fonts["small"].render(txt, True, COL_WHITE)
        screen.blit(surf, (rect.x + (rect.w - surf.get_width()) // 2,
                            rect.y + (rect.h - surf.get_height()) // 2))

    draw_btn(rects["tare"],     "TARE")
    draw_btn(rects["capture"],  "CAPTURE", busy=(busy_name == "capture"))
    draw_btn(rects["auto"],     f"AUTO:{'ON' if autocapture_on else 'OFF'}",
             on=autocapture_on)
    draw_btn(rects["export"],   "EXPORT", busy=(busy_name == "export"))
    draw_btn(rects["quit"],     "QUIT")
    draw_btn(rects["shutdown"], "CONFIRM?" if shutdown_armed else "SHUTDOWN",
             warn=True)

    if status_text:
        surf = fonts["small"].render(status_text, True, COL_YELLOW)
        screen.blit(surf, (max(8, dw - surf.get_width() - 10), bar_h + 4))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global _scale_manager
    cfg = load_config()

    # ── Scale: start acquisition, then Pi-side tare (nothing sent to ESP32) ─
    scale = ScaleManager(cfg)
    _scale_manager = scale
    scale.start()
    log.info("Startup tare — platform must be EMPTY. Waiting 2s for UART data...")
    time.sleep(2)
    scale.tare()

    # ── Hardware capture button ─────────────────────────────────────────────
    button = CaptureButton(cfg["button_gpio"])

    # ── Camera ─────────────────────────────────────────────────────────────
    cam    = Camera(cfg["camera_index"],
                    cfg["stream_width"], cfg["stream_height"],
                    cfg["photo_width"],  cfg["photo_height"])
    cam_ok = cam.start()

    # ── Pygame ─────────────────────────────────────────────────────────────
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")
    pygame.init()
    DW, DH  = cfg["display_width"], cfg["display_height"]
    flags   = pygame.FULLSCREEN if cfg.get("fullscreen") else 0
    screen  = pygame.display.set_mode((DW, DH), flags)
    pygame.display.set_caption("SmartScale")
    pygame.mouse.set_visible(True)
    fonts   = make_fonts()
    clock   = pygame.time.Clock()

    strip_h = cfg["weight_strip_height"]
    strip_y = DH - strip_h
    bar_h   = cfg["control_bar_height"]
    ctrl_rects = make_control_rects(DW, bar_h)
    cam_area   = pygame.Rect(0, bar_h, DW, DH - bar_h - strip_h)

    trigger_g   = cfg["trigger_weight_g"]
    stabilise_s = cfg["stabilise_seconds"]
    labels      = cfg["cell_labels"]
    autocapture = cfg["autocapture_enabled"]

    state         = "idle"
    countdown_end = 0.0
    cooldown_end  = 0.0
    last_photo    = ""
    remaining     = 0.0
    cfg_mtime     = 0.0

    busy_name       = None    # "capture" / "export" / "tare" while a background job runs
    status_text     = ""
    status_until    = 0.0
    shutdown_armed_until = 0.0

    def set_status(text, seconds=4.0):
        nonlocal status_text, status_until
        status_text = text
        status_until = time.time() + seconds

    def do_manual_capture():
        nonlocal state, last_photo, cooldown_end, busy_name
        busy_name = "capture"
        vals = scale.get_values()
        frame = cam.capture_photo() if cam_ok else None
        if frame is not None:
            last_photo = save_photo(frame, vals, labels, strip_h)
            enforce_storage_cap(cfg["photos_max_mb"])
            set_status("Photo saved!")
        else:
            set_status("Capture failed — no camera frame")
        state = "cooldown"
        cooldown_end = time.time() + 3.0
        busy_name = None

    def do_tare():
        nonlocal busy_name
        busy_name = "tare"
        ok = scale.tare()
        set_status("Tare complete" if ok else "Tare failed — no data from ESP32 yet")
        busy_name = None

    def do_export():
        nonlocal busy_name
        busy_name = "export"
        def cb(text):
            set_status(text, 6)
        export_photos_to_usb(cb)
        busy_name = None

    def do_shutdown():
        set_status("Shutting down...", 30)
        log.info("Shutdown requested from touchscreen")
        time.sleep(0.5)
        subprocess.run(["sudo", "shutdown", "-h", "now"])

    log.info("Main loop started")

    while True:
        # ── Events ─────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cam.stop(); button.cleanup(); pygame.quit(); scale.cleanup(); return
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    cam.stop(); button.cleanup(); pygame.quit(); scale.cleanup()
                    log.info("Stopped"); return
                elif event.key == pygame.K_t:
                    threading.Thread(target=do_tare, daemon=True).start()
            elif event.type == pygame.MOUSEBUTTONDOWN:
                pos = event.pos
                now_click = time.time()
                if ctrl_rects["shutdown"].collidepoint(pos):
                    if now_click < shutdown_armed_until:
                        threading.Thread(target=do_shutdown, daemon=True).start()
                        shutdown_armed_until = 0.0
                    else:
                        shutdown_armed_until = now_click + 3.0
                        set_status("Tap SHUTDOWN again to confirm", 3)
                elif busy_name is None:
                    if ctrl_rects["tare"].collidepoint(pos):
                        threading.Thread(target=do_tare, daemon=True).start()
                    elif ctrl_rects["capture"].collidepoint(pos):
                        threading.Thread(target=do_manual_capture, daemon=True).start()
                    elif ctrl_rects["auto"].collidepoint(pos):
                        autocapture = not autocapture
                        cfg["autocapture_enabled"] = autocapture
                        save_config(cfg)
                        set_status(f"Autocapture {'ON' if autocapture else 'OFF'}", 2)
                    elif ctrl_rects["export"].collidepoint(pos):
                        threading.Thread(target=do_export, daemon=True).start()
                    elif ctrl_rects["quit"].collidepoint(pos):
                        pygame.event.post(pygame.event.Event(pygame.QUIT))

        # ── Hardware button ──────────────────────────────────────────────
        if button.consume() and busy_name is None:
            threading.Thread(target=do_manual_capture, daemon=True).start()

        # ── Config hot-reload ───────────────────────────────────────────
        try:
            mt = os.path.getmtime(CONFIG_FILE)
            if mt != cfg_mtime:
                cfg_mtime   = mt
                cfg         = load_config()
                scale.cfg   = cfg
                trigger_g   = cfg["trigger_weight_g"]
                stabilise_s = cfg["stabilise_seconds"]
                labels      = cfg["cell_labels"]
                strip_h     = cfg["weight_strip_height"]
                strip_y     = DH - strip_h
                cam_area    = pygame.Rect(0, bar_h, DW, DH - bar_h - strip_h)
                autocapture = cfg["autocapture_enabled"]
                log.info("Config reloaded")
        except Exception:
            pass

        # ── Weights ─────────────────────────────────────────────────────
        vals   = scale.get_values()
        mean_g = vals["ema_mean_g"]
        now    = time.time()

        update_shared(autocapture_enabled=autocapture, uart_ok=vals.get("uart_ok", False))

        # ── State machine (only auto-triggers when autocapture is ON) ────
        if state == "idle":
            if autocapture and mean_g >= trigger_g:
                state         = "countdown"
                countdown_end = now + stabilise_s
                log.info(f"Trigger: {mean_g:.0f}g")
            update_shared(weights_g=vals["disp_g"], mean_g=vals["disp_mean_g"],
                          status="idle", stable=vals["stable"], diag=vals["diag"])

        elif state == "countdown":
            remaining = countdown_end - now
            if not autocapture:
                state = "idle"
            elif mean_g < trigger_g:
                state = "idle"
                log.info("Weight removed — cancelled")
            elif remaining <= 0:
                photo_frame = cam.capture_photo() if cam_ok else None
                if photo_frame is not None:
                    last_photo = save_photo(photo_frame, vals, labels, strip_h)
                    enforce_storage_cap(cfg["photos_max_mb"])
                state        = "cooldown"
                cooldown_end = now + 3.0
            update_shared(weights_g=vals["disp_g"], mean_g=vals["disp_mean_g"],
                          status="countdown", countdown=max(0, remaining),
                          last_photo=last_photo, stable=vals["stable"], diag=vals["diag"])

        elif state == "cooldown":
            if now >= cooldown_end and mean_g < trigger_g:
                state = "idle"
            update_shared(weights_g=vals["disp_g"], mean_g=vals["disp_mean_g"],
                          status="cooldown", last_photo=last_photo,
                          stable=vals["stable"], diag=vals["diag"])

        # ── Draw ────────────────────────────────────────────────────────
        screen.fill(COL_BLACK)
        frame = cam.get_frame() if cam_ok else None
        if frame is not None:
            blit_camera_frame(screen, frame, cam_area)
        elif cam_ok:
            draw_msg(screen, fonts, "Waiting for camera...", COL_ORANGE,
                     cam_area.x + 10, cam_area.y + 10)

        if state == "countdown" and autocapture:
            draw_countdown(screen, fonts, remaining, cam_area)
        elif state == "cooldown" and status_text == "":
            draw_msg(screen, fonts, "Photo saved!", COL_GREEN,
                     cam_area.x + 10, cam_area.y + 10)

        if status_text and now > status_until:
            status_text = ""
        shutdown_armed = now < shutdown_armed_until
        if shutdown_armed_until and not shutdown_armed:
            shutdown_armed_until = 0.0

        draw_strip(screen, fonts, vals, labels, strip_y, DW, strip_h)
        draw_control_bar(screen, fonts, ctrl_rects, DW, bar_h, autocapture,
                          busy_name, status_text, shutdown_armed)
        pygame.display.flip()
        clock.tick(20)


if __name__ == "__main__":
    main()
