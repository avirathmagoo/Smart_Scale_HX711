"""
Smart Scale — Web Config & Photo Retrieval Server
Access at http://192.168.1.100:5000
Login: melody / raspi
"""

import os
import json
import time
import logging
import zipfile
import subprocess
import threading
import io
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, send_file, send_from_directory, jsonify, flash)

log = logging.getLogger(__name__)

app        = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = "/home/melody/smartscale/config.json"
PHOTOS_DIR  = "/home/melody/smartscale/photos"
Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)

# ── Auth ───────────────────────────────────────────────────────────────────────
VALID_USER = "melody"
VALID_PASS = "raspi"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form.get("username") == VALID_USER and
                request.form.get("password") == VALID_PASS):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid credentials"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Config helpers ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "display_width": 1024, "display_height": 600,
    "weight_strip_height": 50, "control_bar_height": 50,
    "fullscreen": False,
    "trigger_weight_g": 500,
    "stabilise_seconds": 5.0,
    "cell_labels": ["C1", "C2", "C3", "C4"],
    "uart_port": "/dev/serial0", "uart_baud": 115200,
    "button_gpio": 17,
    "offsets": [0, 0, 0, 0],
    "cal_factors": [1.0, 1.0, 1.0, 1.0],
    "camera_index": 0,
    "stream_width": 640, "stream_height": 480,
    "photo_width": 1280, "photo_height": 720,
    "autocapture_enabled": True,
    "photos_max_mb": 20000,
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
            log.error(f"Config read error: {e}")
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Live state (from scale_app) ────────────────────────────────────────────────
def get_live():
    try:
        from scale_app import get_shared
        return get_shared()
    except Exception:
        return {
            "weights_g": [0,0,0,0], "mean_g": 0,
            "status": "unknown", "countdown": 0,
            "last_photo": "", "stable": False,
            "diag": ["no_data"]*4, "uart_ok": False,
            "autocapture_enabled": True,
        }


def _photos_folder_size_mb():
    total = sum(p.stat().st_size for p in Path(PHOTOS_DIR).glob("*.jpg"))
    return round(total / 1024 / 1024, 1)


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    cfg    = load_config()
    live   = get_live()
    photos = sorted(Path(PHOTOS_DIR).glob("*.jpg"), key=os.path.getmtime, reverse=True)
    photo_list = [{
        "name":    p.name,
        "size_kb": round(p.stat().st_size / 1024),
        "time":    datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %Y  %H:%M:%S")
    } for p in photos]
    return render_template("index.html", cfg=cfg, live=live, photos=photo_list,
                            photos_size_mb=_photos_folder_size_mb())


# ── Scale settings ─────────────────────────────────────────────────────────────
@app.route("/save_scale", methods=["POST"])
@login_required
def save_scale():
    cfg = load_config()
    try:
        cfg["trigger_weight_g"] = int(float(request.form["trigger_weight_g"]))
        cfg["stabilise_seconds"] = float(request.form["stabilise_seconds"])
        cfg["cell_labels"] = [
            request.form.get(f"label{i}", f"C{i+1}") for i in range(4)
        ]
        save_config(cfg)
        flash("Scale settings saved.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#scale")


# ── UART / hardware button settings (replaces old GPIO/HX711 pin settings) ─────
@app.route("/save_uart", methods=["POST"])
@login_required
def save_uart():
    cfg = load_config()
    try:
        cfg["uart_port"]   = request.form["uart_port"].strip()
        cfg["uart_baud"]   = int(request.form["uart_baud"])
        cfg["button_gpio"] = int(request.form["button_gpio"])
        save_config(cfg)
        flash("UART/button settings saved. Restart the scale app to apply.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#uart")


# ── Display settings ───────────────────────────────────────────────────────────
@app.route("/save_display", methods=["POST"])
@login_required
def save_display():
    cfg = load_config()
    try:
        cfg["display_width"]       = int(request.form["display_width"])
        cfg["display_height"]      = int(request.form["display_height"])
        cfg["weight_strip_height"] = int(request.form["weight_strip_height"])
        cfg["fullscreen"]          = request.form.get("fullscreen") == "on"
        save_config(cfg)
        flash("Display settings saved. Restart the scale app to apply.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#display")


# ── Storage settings ────────────────────────────────────────────────────────────
@app.route("/save_storage", methods=["POST"])
@login_required
def save_storage():
    cfg = load_config()
    try:
        cfg["photos_max_mb"] = int(request.form["photos_max_mb"])
        save_config(cfg)
        flash("Storage cap saved.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#storage")


# ── Calibration ────────────────────────────────────────────────────────────────
# IMPORTANT: these routes drive the SAME ScaleManager/UARTReader instance the
# running scale_app.py owns (launcher.py runs both in one process, so the
# import below reaches the live object). They must NOT open a second serial
# connection to the ESP32 — two readers on one UART port will corrupt both.

def _get_live_scale():
    from scale_app import get_scale_manager
    scale = get_scale_manager()
    if scale is None:
        raise RuntimeError("Scale app is not running — start/restart the "
                            "smartscale service first (System tab).")
    return scale

@app.route("/tare", methods=["POST"])
@login_required
def do_tare():
    try:
        scale = _get_live_scale()
        ok = scale.tare()
        flash("Tare complete — platform zeroed." if ok else
              "Tare failed — no data from ESP32 yet, check the UART link.", "ok" if ok else "err")
    except Exception as e:
        flash(f"Tare error: {e}", "err")
    return redirect(url_for("index") + "#calibration")


@app.route("/calibrate", methods=["POST"])
@login_required
def do_calibrate():
    try:
        known_kg = float(request.form["known_weight_kg"])
        if known_kg <= 0:
            flash("Known weight must be greater than zero.", "err")
            return redirect(url_for("index") + "#calibration")
        scale = _get_live_scale()
        ok = scale.calibrate(known_kg)
        if ok:
            flash(f"Calibration done with {known_kg:.4f} kg reference.", "ok")
        else:
            flash("Calibration failed — no valid factors. Check wiring and weight placement.", "err")
    except Exception as e:
        flash(f"Calibration error: {e}", "err")
    return redirect(url_for("index") + "#calibration")


@app.route("/save_cal_manual", methods=["POST"])
@login_required
def save_cal_manual():
    cfg = load_config()
    try:
        cfg["offsets"]     = [float(request.form[f"offset{i}"]) for i in range(4)]
        cfg["cal_factors"] = [float(request.form[f"factor{i}"]) for i in range(4)]
        save_config(cfg)
        flash("Manual calibration values saved.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#calibration")


# ── WiFi ───────────────────────────────────────────────────────────────────────
@app.route("/add_wifi", methods=["POST"])
@login_required
def add_wifi():
    ssid     = request.form.get("ssid", "").strip()
    password = request.form.get("wifi_pass", "").strip()
    if not ssid:
        flash("SSID cannot be empty.", "err")
        return redirect(url_for("index") + "#wifi")
    try:
        _add_wifi_network(ssid, password)
        flash(f"Network '{ssid}' saved as fallback.", "ok")
    except Exception as e:
        flash(f"WiFi error: {e}", "err")
    return redirect(url_for("index") + "#wifi")

def _add_wifi_network(ssid, password):
    wpa_file = "/etc/wpa_supplicant/wpa_supplicant.conf"
    with open(wpa_file, "r") as f:
        content = f.read()
    if f'ssid="{ssid}"' in content:
        raise ValueError(f"Network '{ssid}' already exists.")
    import re
    priorities = [int(m) for m in re.findall(r"priority=(\d+)", content)]
    new_priority = (max(priorities) - 1) if priorities else 0
    block = f'\nnetwork={{\n    ssid="{ssid}"\n    psk="{password}"\n    priority={new_priority}\n}}\n'
    with open(wpa_file, "a") as f:
        f.write(block)
    subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"], check=True)


# ── Photos ─────────────────────────────────────────────────────────────────────
@app.route("/photos/<filename>")
@login_required
def serve_photo(filename):
    return send_from_directory(PHOTOS_DIR, filename)

@app.route("/download/<filename>")
@login_required
def download_photo(filename):
    return send_from_directory(PHOTOS_DIR, filename, as_attachment=True)

@app.route("/download_all")
@login_required
def download_all():
    photos = list(Path(PHOTOS_DIR).glob("*.jpg"))
    if not photos:
        flash("No photos to download.", "err")
        return redirect(url_for("index") + "#photos")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in photos:
            zf.write(p, p.name)
    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=f"scale_photos_{ts}.zip")

@app.route("/delete_all", methods=["POST"])
@login_required
def delete_all():
    count = sum(1 for p in Path(PHOTOS_DIR).glob("*.jpg") if p.unlink() is None)
    flash(f"Deleted {count} photo(s).", "ok")
    return redirect(url_for("index") + "#photos")


# ── System ─────────────────────────────────────────────────────────────────────
@app.route("/reboot", methods=["POST"])
@login_required
def reboot():
    flash("Rebooting in 3 seconds…", "ok")
    threading.Thread(
        target=lambda: (time.sleep(3), subprocess.run(["sudo","reboot"])),
        daemon=True).start()
    return redirect(url_for("index") + "#system")

@app.route("/shutdown", methods=["POST"])
@login_required
def shutdown():
    flash("Shutting down in 3 seconds…", "ok")
    threading.Thread(
        target=lambda: (time.sleep(3), subprocess.run(["sudo","shutdown","-h","now"])),
        daemon=True).start()
    return redirect(url_for("index") + "#system")

@app.route("/restart_scale", methods=["POST"])
@login_required
def restart_scale():
    try:
        subprocess.run(["sudo","systemctl","restart","smartscale"], check=True)
        flash("Scale app restarted.", "ok")
    except Exception as e:
        flash(f"Restart failed: {e}", "err")
    return redirect(url_for("index") + "#system")


# ── Live API ────────────────────────────────────────────────────────────────────
@app.route("/api/live")
@login_required
def api_live():
    live = get_live()
    live["photos_size_mb"] = _photos_folder_size_mb()
    return jsonify(live)

@app.route("/api/diagnostics")
@login_required
def api_diagnostics():
    try:
        scale = _get_live_scale()
        return jsonify(scale.diagnostics())
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
