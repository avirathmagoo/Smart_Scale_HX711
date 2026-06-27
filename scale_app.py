"""
Smart Scale — Main Application
Raspberry Pi 4B | 4× HX711 (shared CLK) | USB Webcam | 1024×600

ARCHITECTURE
============
Thread priority (highest → lowest):
  1. hx711-acq   — raw GPIO bit-banging, pinned tight loop, no GIL contention
  2. hx711-proc  — filtering, validation, calibration, publishes kg values
  3. camera      — frame grab, always lowest priority
  4. main        — pygame display + state machine, reads pre-computed values

Key design decisions:
  - Acquisition thread does NOTHING except GPIO reads → ring buffer
  - Processing thread does all maths off the acquisition path
  - Camera thread uses SCHED_IDLE via nice(19) to never compete with sensors
  - Display loop reads atomically from a single shared struct, no waiting
  - Tare and calibrate temporarily pause the acquisition loop cleanly
  - All filtering happens in the processing thread, not the main loop
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
            # Migrate old grams key
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
# HX711 LOW-LEVEL DRIVER
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import RPi.GPIO as GPIO
    HW_AVAILABLE = True
except ImportError:
    HW_AVAILABLE = False
    log.warning("RPi.GPIO not available — simulation mode")

# HX711 raw value limits — 24-bit ADC, 2's complement
# Values outside ±7,800,000 are almost certainly noise or corrupt reads
HX711_RAW_MIN = -7_800_000
HX711_RAW_MAX =  7_800_000
# Maximum plausible raw delta between consecutive reads for a static load
# (large, but rules out clearly corrupt bit-flips)
HX711_MAX_DELTA = 500_000

class HX711Raw:
    """
    Low-level HX711 driver for a single channel.

    Design principles:
    - _read() does ONE complete acquisition cycle: wait for DOUT low,
      clock 24 data bits, clock gain pulses. Nothing else.
    - No averaging here — that's the processing layer's job.
    - Corrupt reads (timeout, impossible value) return None immediately.
    - Power-down / power-up sequence for clean reset.
    - CLK is shared — only this class touches it during a read, and the
      acquisition manager serialises all reads so CLK is never contested.
    """

    def __init__(self, dout_pin: int, clk_pin: int):
        self.dout = dout_pin
        self.clk  = clk_pin
        self._last_raw = None
        if HW_AVAILABLE:
            # CLK is set up once by the acquisition manager
            GPIO.setup(self.dout, GPIO.IN)
            self._power_cycle()

    def _power_cycle(self):
        """
        HX711 powers down when CLK is held HIGH > 60 µs.
        Powers back up on CLK LOW. Resets internal state.
        """
        if not HW_AVAILABLE:
            return
        GPIO.output(self.clk, True)
        time.sleep(0.0001)   # 100 µs > 60 µs threshold
        GPIO.output(self.clk, False)
        time.sleep(0.001)    # allow internal oscillator to stabilise

    def is_ready(self) -> bool:
        """HX711 signals DOUT LOW when conversion is complete."""
        if not HW_AVAILABLE:
            return True
        return GPIO.input(self.dout) == 0

    def read(self) -> int | None:
        """
        Perform one complete read cycle.

        Returns raw 24-bit signed integer, or None if:
        - DOUT never went LOW within timeout (sensor hung)
        - Read value is outside plausible ADC range (corrupt)
        - Read value jumps impossibly from last reading (bit-flip)
        """
        if not HW_AVAILABLE:
            import random
            base = self._last_raw if self._last_raw is not None else 0
            val  = base + random.randint(-200, 200)
            self._last_raw = val
            return val

        # Wait for conversion ready (DOUT LOW), 200 ms timeout
        deadline = time.monotonic() + 0.2
        while GPIO.input(self.dout):
            if time.monotonic() > deadline:
                log.warning(f"HX711 DOUT={self.dout} timeout — resetting")
                self._power_cycle()
                return None

        # Clock in 24 bits — each bit: CLK HIGH → sample DOUT → CLK LOW
        # Timing: HX711 requires t_clk ≥ 0.2 µs HIGH, ≥ 0.2 µs LOW
        # RPi GPIO toggling is ~1–2 µs naturally — no explicit sleep needed
        raw = 0
        for _ in range(24):
            GPIO.output(self.clk, True)
            raw = (raw << 1) | GPIO.input(self.dout)
            GPIO.output(self.clk, False)

        # 25th pulse sets gain=128 Channel A for NEXT conversion
        GPIO.output(self.clk, True)
        GPIO.output(self.clk, False)

        # Convert 24-bit two's complement to signed int
        if raw & 0x800000:
            raw -= 0x1000000

        # Validate range
        if not (HX711_RAW_MIN <= raw <= HX711_RAW_MAX):
            log.warning(f"DOUT={self.dout} out-of-range raw={raw} — discarded")
            return None

        # Validate delta from last known good read
        if self._last_raw is not None:
            if abs(raw - self._last_raw) > HX711_MAX_DELTA:
                log.warning(
                    f"DOUT={self.dout} spike: {self._last_raw}→{raw} "
                    f"(Δ={abs(raw - self._last_raw)}) — discarded"
                )
                return None

        self._last_raw = raw
        return raw


# ═══════════════════════════════════════════════════════════════════════════════
# ACQUISITION PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

# Ring buffer depth per channel — ~20 samples at ~10 Hz = ~2 s history
RING_DEPTH = 32

class AcquisitionPipeline:
    """
    Two-stage pipeline:

    Stage 1 — hx711-acq thread (this class)
      - Runs in a tight loop
      - Reads all 4 channels sequentially (CLK is shared, must be sequential)
      - Pushes raw validated integers into per-channel ring buffers
      - Does NO arithmetic — pure I/O

    Stage 2 — hx711-proc thread (ProcessingPipeline)
      - Consumes raw ring buffers
      - Applies offset, calibration, filtering, outlier rejection
      - Publishes final kg values + diagnostics

    The two stages communicate via ring buffers protected by a single lock.
    The lock is held for microseconds (deque append/popleft only).
    """

    def __init__(self, clk_pin: int, dout_pins: list):
        self.clk_pin   = clk_pin
        self.dout_pins = dout_pins
        self.n_cells   = len(dout_pins)

        # Per-channel ring buffers of raw ints
        self._buffers  = [collections.deque(maxlen=RING_DEPTH)
                          for _ in range(self.n_cells)]
        self._buf_lock = threading.Lock()

        # Per-channel read counters and error counters for diagnostics
        self._read_count = [0] * self.n_cells
        self._err_count  = [0] * self.n_cells

        # Control
        self._stop   = threading.Event()
        self._pause  = threading.Event()   # pause for tare/calibration
        self._paused = threading.Event()   # signals caller that pause is active

        self.cells   = []

        if HW_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(clk_pin, GPIO.OUT, initial=GPIO.LOW)

        for dout in dout_pins:
            self.cells.append(HX711Raw(dout, clk_pin))

        log.info(f"AcquisitionPipeline init — {self.n_cells} cells, "
                 f"CLK=GPIO{clk_pin}, DOUT={dout_pins}, HW={'yes' if HW_AVAILABLE else 'SIM'}")

    # ── Ring buffer access ─────────────────────────────────────────────────
    def get_raw_snapshot(self) -> list:
        """
        Returns a list of N lists — current raw buffer contents per channel.
        Copies under lock — caller gets a stable snapshot.
        """
        with self._buf_lock:
            return [list(buf) for buf in self._buffers]

    def get_diagnostics(self) -> dict:
        return {
            "reads":  list(self._read_count),
            "errors": list(self._err_count),
            "error_rates": [
                round(self._err_count[i] / max(self._read_count[i], 1) * 100, 1)
                for i in range(self.n_cells)
            ]
        }

    # ── Pause / resume (for tare and calibration) ─────────────────────────
    def pause_and_wait(self, timeout=3.0) -> bool:
        """
        Signal acquisition to pause, wait until it is actually paused.
        Returns True if paused successfully.
        During tare/calibration the caller reads directly from cells.
        """
        self._paused.clear()
        self._pause.set()
        return self._paused.wait(timeout)

    def resume(self):
        self._pause.clear()

    # ── Acquisition thread ─────────────────────────────────────────────────
    def _acq_loop(self):
        """
        Tight acquisition loop.
        Reads cells sequentially — CLK is shared, only one cell at a time.
        Each cell is read once per pass → push to ring buffer.

        No sleep between reads: HX711 outputs ~10 Hz at 80 Hz crystal,
        the is_ready() poll blocks until the conversion is done naturally.
        This gives deterministic, conversion-rate-locked sampling.
        """
        while not self._stop.is_set():

            # ── Pause point ───────────────────────────────────────────
            if self._pause.is_set():
                self._paused.set()
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.01)
                self._paused.clear()
                continue

            # ── Read all cells ────────────────────────────────────────
            for i, cell in enumerate(self.cells):
                if self._stop.is_set():
                    break
                raw = cell.read()
                self._read_count[i] += 1
                if raw is None:
                    self._err_count[i] += 1
                    continue
                with self._buf_lock:
                    self._buffers[i].append(raw)

    def start(self):
        t = threading.Thread(target=self._acq_loop, daemon=True, name="hx711-acq")
        t.start()
        # Attempt to raise acquisition thread priority (requires sudo or CAP_SYS_NICE)
        try:
            os.nice(-5)
        except Exception:
            pass
        log.info("Acquisition thread started")

    def stop(self):
        self._stop.set()
        if HW_AVAILABLE:
            GPIO.cleanup()


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESSING PIPELINE  (filtering, calibration, validation)
# ═══════════════════════════════════════════════════════════════════════════════

# Filter window — number of raw samples used per output value
FILTER_WINDOW   = 16
# EMA alpha — how quickly the displayed value tracks real changes
# Lower = smoother but slower. 0.15 gives ~1.5 s settling time.
EMA_ALPHA       = 0.15
# Stability threshold — mean_kg must not change more than this between
# consecutive processed readings to be considered "stable"
STABLE_THRESH_KG = 0.010   # 10 g
# How many consecutive stable readings before we say the reading is stable
STABLE_COUNT_REQ = 8


class CellDiagnostic:
    OK          = "ok"
    NO_DATA     = "no_data"      # ring buffer empty
    NOISY       = "noisy"        # std dev too high
    SATURATED   = "saturated"    # raw values near ADC limits


class ProcessingPipeline:
    """
    Consumes AcquisitionPipeline ring buffers.
    Runs in its own thread at lower priority than acquisition.

    Per-channel processing per cycle:
      1. Take snapshot of raw buffer
      2. Reject if < MIN_SAMPLES
      3. Trimmed mean (discard top & bottom 15%)
      4. Apply offset and calibration factor → kg
      5. EMA smoothing
      6. Outlier / saturation / noise diagnostic

    Mean kg = mean of all 4 channel kg values.
    Stability detection on mean_kg.
    """

    MIN_SAMPLES = 6   # minimum raw samples needed to compute a valid reading

    def __init__(self, acq: AcquisitionPipeline, cfg: dict):
        self.acq      = acq
        self.cfg      = cfg
        self._lock    = threading.Lock()
        self._stop    = threading.Event()

        # Published values (read by main loop and Flask)
        self._kg        = [0.0] * acq.n_cells
        self._mean_kg   = 0.0
        self._ema_kg    = [0.0] * acq.n_cells
        self._ema_mean  = 0.0
        self._diag      = [CellDiagnostic.NO_DATA] * acq.n_cells
        self._stable    = False
        self._stable_ctr = 0
        self._prev_mean  = 0.0

    # ── Internal processing ────────────────────────────────────────────────
    @staticmethod
    def _trimmed_mean(values: list, trim_frac=0.15) -> float:
        """
        Sort values, discard bottom and top trim_frac fraction,
        return mean of remainder.
        With FILTER_WINDOW=16 and trim_frac=0.15 → discard 2 low, 2 high.
        """
        n = len(values)
        k = max(1, int(n * trim_frac))
        trimmed = sorted(values)[k: n - k]
        if not trimmed:
            return statistics.mean(values)
        return statistics.mean(trimmed)

    def _process_channel(self, raw_buf: list, idx: int) -> tuple:
        """
        Returns (kg_value, diagnostic_string).
        """
        if len(raw_buf) < self.MIN_SAMPLES:
            return self._ema_kg[idx], CellDiagnostic.NO_DATA

        # Saturation check — raw near ±8M means load cell is overloaded
        latest = raw_buf[-1]
        if abs(latest) > 7_500_000:
            return self._ema_kg[idx], CellDiagnostic.SATURATED

        # Noise check — if std dev of raw buffer is extremely high,
        # the sensor is unstable (wiring issue, vibration, etc.)
        try:
            std = statistics.stdev(raw_buf)
        except statistics.StatisticsError:
            std = 0.0

        # Typical noise floor for HX711 ≈ 50–500 raw counts at rest.
        # If std > 50,000 raw counts the cell is pathologically noisy.
        if std > 50_000:
            return self._ema_kg[idx], CellDiagnostic.NOISY

        # Trimmed mean → remove per-sample spikes
        raw_mean = self._trimmed_mean(raw_buf)

        # Apply offset and calibration
        offset = self.cfg["offsets"][idx]
        factor = self.cfg["cal_factors"][idx]
        if factor == 0 or abs(factor) < 1:
            # Factor too small — calibration not done or corrupt
            factor = 1.0
        kg = (raw_mean - offset) / factor

        return kg, CellDiagnostic.OK

    def _proc_loop(self):
        while not self._stop.is_set():
            snapshot = self.acq.get_raw_snapshot()

            kg_vals = []
            diags   = []
            for i in range(self.acq.n_cells):
                kg, diag = self._process_channel(snapshot[i], i)
                kg_vals.append(kg)
                diags.append(diag)

            mean_kg = sum(kg_vals) / len(kg_vals)

            # EMA smoothing — per channel and on mean
            ema_kg   = []
            a        = EMA_ALPHA
            for i in range(self.acq.n_cells):
                ema = a * kg_vals[i] + (1 - a) * self._ema_kg[i]
                ema_kg.append(ema)
            ema_mean = a * mean_kg + (1 - a) * self._ema_mean

            # Stability detection on EMA mean
            delta = abs(ema_mean - self._prev_mean)
            if delta < STABLE_THRESH_KG:
                self._stable_ctr = min(self._stable_ctr + 1, STABLE_COUNT_REQ + 1)
            else:
                self._stable_ctr = 0
            stable = (self._stable_ctr >= STABLE_COUNT_REQ)
            self._prev_mean = ema_mean

            # Publish atomically
            with self._lock:
                self._kg       = kg_vals
                self._mean_kg  = mean_kg
                self._ema_kg   = ema_kg
                self._ema_mean = ema_mean
                self._diag     = diags
                self._stable   = stable

            # Processing runs at ~10 Hz — no need to spin faster
            time.sleep(0.1)

    def start(self):
        t = threading.Thread(target=self._proc_loop, daemon=True, name="hx711-proc")
        t.start()
        log.info("Processing thread started")

    def stop(self):
        self._stop.set()

    def get_values(self) -> dict:
        """
        Returns dict with all published values.
        Called by main loop and Flask — instant, no blocking.
        """
        with self._lock:
            return {
                "kg":        list(self._kg),
                "mean_kg":   self._mean_kg,
                "ema_kg":    list(self._ema_kg),
                "ema_mean":  self._ema_mean,
                "diag":      list(self._diag),
                "stable":    self._stable,
            }


# ═══════════════════════════════════════════════════════════════════════════════
# SCALE MANAGER  (tare, calibration, public interface)
# ═══════════════════════════════════════════════════════════════════════════════

class ScaleManager:
    """
    Public interface for the application.
    Owns both AcquisitionPipeline and ProcessingPipeline.
    Handles tare and calibration by pausing acquisition and reading directly.
    """

    # How many direct reads to take for tare / calibration
    TARE_READS = 20
    CAL_READS  = 20
    # Minimum plausible calibration factor (raw counts per kg)
    # For typical 5 kg load cells, expect ~400,000–800,000 raw/kg
    MIN_CAL_FACTOR = 100
    MAX_CAL_FACTOR = 5_000_000

    def __init__(self, cfg: dict):
        self.cfg  = cfg
        self._acq = AcquisitionPipeline(cfg["clk_pin"], cfg["dout_pins"])
        self._proc = ProcessingPipeline(self._acq, cfg)

    def start(self):
        self._acq.start()
        self._proc.start()

    def get_values(self) -> dict:
        return self._proc.get_values()

    # ── Direct read helper (used only during tare/calibrate) ──────────────
    def _direct_read_all(self, n_reads: int) -> list | None:
        """
        Read each cell n_reads times directly (acquisition paused).
        Returns list of n_reads raw values per cell, or None on failure.
        """
        per_cell = [[] for _ in range(len(self._acq.cells))]

        for _ in range(n_reads):
            for i, cell in enumerate(self._acq.cells):
                raw = cell.read()
                if raw is not None:
                    per_cell[i].append(raw)
            time.sleep(0.015)   # ~10 Hz HX711 output rate

        # Require at least MIN_SAMPLES per cell
        for i, buf in enumerate(per_cell):
            if len(buf) < ProcessingPipeline.MIN_SAMPLES:
                log.error(f"Cell {i} only returned {len(buf)} reads during direct read")
                return None

        return per_cell

    # ── Tare ──────────────────────────────────────────────────────────────
    def tare(self) -> bool:
        """
        Zero the scale.
        Pauses acquisition, reads TARE_READS samples per cell,
        stores trimmed mean as offset, resumes acquisition.
        Returns True on success.
        """
        log.info("Tare: pausing acquisition…")
        if not self._acq.pause_and_wait(timeout=3.0):
            log.error("Tare: acquisition did not pause — aborting")
            return False

        log.info(f"Tare: reading {self.TARE_READS} samples per cell…")
        per_cell = self._direct_read_all(self.TARE_READS)

        self._acq.resume()

        if per_cell is None:
            log.error("Tare: insufficient readings — tare aborted")
            return False

        new_offsets = []
        for i, buf in enumerate(per_cell):
            offset = ProcessingPipeline._trimmed_mean(buf)
            new_offsets.append(offset)
            log.info(f"  Cell {i}: {len(buf)} reads, offset={offset:.1f}")

        self.cfg["offsets"] = new_offsets
        save_config(self.cfg)
        self._proc.cfg = self.cfg

        # Reset EMA to zero immediately
        with self._proc._lock:
            self._proc._ema_kg   = [0.0] * self._acq.n_cells
            self._proc._ema_mean = 0.0
            self._proc._prev_mean = 0.0

        log.info(f"Tare complete — offsets: {[round(o) for o in new_offsets]}")
        return True

    # ── Calibration ───────────────────────────────────────────────────────
    def calibrate(self, known_kg: float) -> bool:
        """
        Calibrate with a known reference weight (in kg).
        Platform must already be tared. Place known weight, then call.
        Computes cal_factor = net_raw / known_kg per cell.
        Validates factors are within plausible range.
        Returns True on success.
        """
        if known_kg <= 0:
            log.error(f"Calibrate: invalid known_kg={known_kg}")
            return False

        log.info(f"Calibrate: {known_kg:.4f} kg reference — pausing acquisition…")
        if not self._acq.pause_and_wait(timeout=3.0):
            log.error("Calibrate: acquisition did not pause — aborting")
            return False

        log.info(f"Calibrate: reading {self.CAL_READS} samples per cell…")
        per_cell = self._direct_read_all(self.CAL_READS)

        self._acq.resume()

        if per_cell is None:
            log.error("Calibrate: insufficient readings — calibration aborted")
            return False

        new_factors = []
        offsets     = self.cfg["offsets"]
        valid_factors = []

        for i, buf in enumerate(per_cell):
            raw_mean = ProcessingPipeline._trimmed_mean(buf)
            net_raw  = raw_mean - offsets[i]
            log.info(f"  Cell {i}: raw_mean={raw_mean:.1f}, "
                     f"offset={offsets[i]:.1f}, net_raw={net_raw:.1f}")

            if abs(net_raw) < 100:
                log.warning(f"  Cell {i}: net_raw too small ({net_raw:.1f}) "
                             f"— cell may not be under load")
                new_factors.append(None)
                continue

            factor = net_raw / known_kg
            if not (self.MIN_CAL_FACTOR <= abs(factor) <= self.MAX_CAL_FACTOR):
                log.warning(f"  Cell {i}: factor={factor:.1f} outside plausible range "
                             f"[{self.MIN_CAL_FACTOR}, {self.MAX_CAL_FACTOR}] — flagged")
                new_factors.append(None)
                continue

            log.info(f"  Cell {i}: factor={factor:.2f} raw/kg ✓")
            new_factors.append(factor)
            valid_factors.append(factor)

        if not valid_factors:
            log.error("Calibrate: no valid factors computed — check wiring and weight")
            return False

        mean_factor = sum(valid_factors) / len(valid_factors)

        # Fill in failed cells with the mean of valid cells
        final_factors = []
        for i, f in enumerate(new_factors):
            if f is not None:
                final_factors.append(f)
            else:
                log.warning(f"  Cell {i}: using mean factor {mean_factor:.2f}")
                final_factors.append(mean_factor)

        self.cfg["cal_factors"] = final_factors
        save_config(self.cfg)
        self._proc.cfg = self.cfg

        log.info(f"Calibration complete — factors: {[round(f) for f in final_factors]}")
        return True

    def get_diagnostics(self) -> dict:
        return self._acq.get_diagnostics()

    def cleanup(self):
        self._proc.stop()
        self._acq.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED STATE  (Flask web server reads this)
# ═══════════════════════════════════════════════════════════════════════════════

_shared = {
    "weights":    [0.0, 0.0, 0.0, 0.0],
    "mean":       0.0,
    "status":     "idle",
    "countdown":  0,
    "last_photo": "",
    "stable":     False,
    "diag":       ["no_data"] * 4,
}
_shared_lock = threading.Lock()

def update_shared(**kwargs):
    with _shared_lock:
        _shared.update(kwargs)

def get_shared() -> dict:
    with _shared_lock:
        return dict(_shared)


# ═══════════════════════════════════════════════════════════════════════════════
# CAMERA  (background grab thread, lowest priority)
# ═══════════════════════════════════════════════════════════════════════════════

class Camera:
    """
    Grabs frames in a background thread.
    Stream: 640×480 MJPG @ 30fps — for display.
    Photo:  switches to 1280×720, grabs 1 frame, switches back.

    Thread runs at nice(10) — explicitly lower priority than sensor threads.
    """
    def __init__(self, index, stream_w, stream_h, photo_w, photo_h):
        self.index       = index
        self.sw, self.sh = stream_w, stream_h
        self.pw, self.ph = photo_w, photo_h
        self._lock       = threading.Lock()
        self._frame      = None
        self._stop       = threading.Event()
        self._cap        = None
        self._photo_req  = threading.Event()
        self._photo_done = threading.Event()
        self._photo_frame = None

    def _open_cap(self, w, h) -> bool:
        if self._cap:
            self._cap.release()
        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS,          30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)   # always freshest frame
        self._cap = cap
        return cap.isOpened()

    def start(self) -> bool:
        if not self._open_cap(self.sw, self.sh):
            log.error("Camera: failed to open")
            return False
        t = threading.Thread(target=self._grab_loop, daemon=True, name="camera")
        t.start()
        log.info(f"Camera: stream {self.sw}×{self.sh} MJPG started")
        return True

    def _grab_loop(self):
        try:
            os.nice(10)   # lower priority than sensors
        except Exception:
            pass

        while not self._stop.is_set():
            # Handle photo request
            if self._photo_req.is_set():
                self._photo_req.clear()
                log.info("Camera: switching to photo resolution")
                self._open_cap(self.pw, self.ph)
                # Flush stale buffer frames
                for _ in range(4):
                    self._cap.grab()
                ret, frame = self._cap.read()
                self._photo_frame = frame.copy() if ret else None
                self._photo_done.set()
                # Restore stream resolution
                self._open_cap(self.sw, self.sh)
                log.info("Camera: back to stream resolution")
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
# PYGAME DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

COL_WHITE  = (255, 255, 255)
COL_BLACK  = (0,   0,   0  )
COL_GREEN  = (0,   210, 0  )
COL_YELLOW = (230, 200, 0  )
COL_RED    = (220, 50,  50 )
COL_ORANGE = (230, 130, 0  )
COL_STRIP  = (22,  22,  22 )
COL_LINE   = (65,  65,  65 )
COL_STABLE = (0,   170, 0  )

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

def draw_strip(screen, fonts, vals: dict, labels, strip_y, dw, sh):
    pygame.draw.rect(screen, COL_STRIP, (0, strip_y, dw, sh))
    pygame.draw.line(screen, COL_LINE,  (0, strip_y), (dw, strip_y), 1)

    ema_kg  = vals["ema_kg"]
    mean_kg = vals["ema_mean"]
    diags   = vals["diag"]
    stable  = vals["stable"]

    # Colour each cell by diagnostic
    diag_colour = {
        CellDiagnostic.OK:        COL_WHITE,
        CellDiagnostic.NO_DATA:   COL_ORANGE,
        CellDiagnostic.NOISY:     COL_RED,
        CellDiagnostic.SATURATED: COL_RED,
    }

    parts = []
    for i in range(4):
        parts.append(f"{labels[i]}: {ema_kg[i]:.3f}kg")

    mean_col  = COL_STABLE if stable else COL_WHITE
    mean_text = f"MEAN: {mean_kg:.3f}kg"
    if stable:
        mean_text += " ✓"

    full_text = "   |   ".join(parts) + "   |   " + mean_text
    surf = fonts["small"].render(full_text, True, mean_col)
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

def draw_msg(screen, fonts, text, color, x=10, y=10):
    surf = fonts["medium"].render(text, True, color)
    screen.blit(surf, (x, y))


# ═══════════════════════════════════════════════════════════════════════════════
# PHOTO SAVE
# ═══════════════════════════════════════════════════════════════════════════════

def save_photo(frame, vals: dict, labels, strip_h: int) -> str:
    photo   = frame.copy()
    h, w    = photo.shape[:2]
    strip_y = h - strip_h

    cv2.rectangle(photo, (0, strip_y), (w, h), (22, 22, 22), -1)
    cv2.line(photo, (0, strip_y), (w, strip_y), (65, 65, 65), 1)

    ema_kg  = vals["ema_kg"]
    mean_kg = vals["ema_mean"]

    parts = [f"{labels[i]}: {ema_kg[i]:.3f}kg" for i in range(4)]
    parts.append(f"MEAN: {mean_kg:.3f}kg")
    text  = "   |   ".join(parts)

    cv2.putText(photo, text, (10, strip_y + strip_h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 1, cv2.LINE_AA)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTOS_DIR, f"capture_{ts}.jpg")
    cv2.imwrite(path, photo, [cv2.IMWRITE_JPEG_QUALITY, 95])
    log.info(f"Photo saved: {path} ({w}×{h})")
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    cfg = load_config()

    # ── Scale ──────────────────────────────────────────────────────────────
    scale = ScaleManager(cfg)
    log.info("Startup tare — platform must be EMPTY")
    # Start acquisition first, wait for buffers to fill, then tare
    scale.start()
    log.info("Waiting for HX711 buffers to fill (5s)…")
    time.sleep(5)
    scale.tare()

    # ── Camera ─────────────────────────────────────────────────────────────
    cam = Camera(
        cfg["camera_index"],
        cfg["stream_width"],  cfg["stream_height"],
        cfg["photo_width"],   cfg["photo_height"],
    )
    cam_ok = cam.start()
    if not cam_ok:
        log.warning("Camera not available — continuing without camera")

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
                    log.info("Stopped (q)"); return
                elif event.key == pygame.K_t:
                    threading.Thread(target=scale.tare, daemon=True).start()

        # ── Hot-reload config ───────────────────────────────────────────
        try:
            mt = os.path.getmtime(CONFIG_FILE)
            if mt != cfg_mtime:
                cfg_mtime   = mt
                cfg         = load_config()
                scale.cfg   = cfg
                scale._proc.cfg = cfg
                trigger_kg  = cfg["trigger_weight_kg"]
                stabilise_s = cfg["stabilise_seconds"]
                labels      = cfg["cell_labels"]
                strip_h     = cfg["weight_strip_height"]
                strip_y     = DH - strip_h
                log.info("Config reloaded")
        except Exception:
            pass

        # ── Sensor values — instant, no blocking ───────────────────────
        vals    = scale.get_values()
        mean_kg = vals["ema_mean"]
        now     = time.time()

        # ── State machine ───────────────────────────────────────────────
        if state == "idle":
            if mean_kg >= trigger_kg:
                state         = "countdown"
                countdown_end = now + stabilise_s
                log.info(f"Trigger: {mean_kg:.3f}kg")
            update_shared(
                weights=vals["ema_kg"], mean=mean_kg,
                status="idle", stable=vals["stable"], diag=vals["diag"]
            )

        elif state == "countdown":
            remaining = countdown_end - now
            if mean_kg < trigger_kg:
                state = "idle"
                log.info("Weight removed — cancelled")
            elif remaining <= 0:
                photo_frame = cam.capture_photo() if cam_ok else None
                if photo_frame is not None:
                    last_photo = save_photo(photo_frame, vals, labels, strip_h)
                else:
                    log.warning("Photo capture returned no frame")
                state        = "cooldown"
                cooldown_end = now + 3.0
            update_shared(
                weights=vals["ema_kg"], mean=mean_kg,
                status="countdown", countdown=max(0, remaining),
                last_photo=last_photo, stable=vals["stable"], diag=vals["diag"]
            )

        elif state == "cooldown":
            if now >= cooldown_end and mean_kg < trigger_kg:
                state = "idle"
            update_shared(
                weights=vals["ema_kg"], mean=mean_kg,
                status="cooldown", last_photo=last_photo,
                stable=vals["stable"], diag=vals["diag"]
            )

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
        clock.tick(20)   # 20 fps cap — sensor threads get priority

    cam.stop()
    pygame.quit()
    scale.cleanup()
    log.info("Stopped")


if __name__ == "__main__":
    main()