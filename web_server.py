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
app.secret_key = os.urandom(24)   # re-generates on restart; sessions are short-lived

CONFIG_FILE = "/home/pi/smartscale/config.json"
PHOTOS_DIR  = "/home/pi/smartscale/photos"
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
    "weight_strip_height": 50,
    "trigger_weight_g": 500.0,
    "stabilise_seconds": 5.0,
    "unit": "g",
    "cell_labels": ["C1", "C2", "C3", "C4"],
    "clk_pin": 6,
    "dout_pins": [5, 13, 19, 26],
    "offsets": [0, 0, 0, 0],
    "cal_factors": [1.0, 1.0, 1.0, 1.0],
    "camera_index": 0,
    "cam_width": 1280,
    "cam_height": 720,
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


# ── Live weight state (updated by scale_app via shared module) ─────────────────
# Flask reads from shared dict imported from scale_app when running together,
# or falls back to reading the log / last-known values if run standalone.
def get_live_weights():
    try:
        from scale_app import get_shared
        return get_shared()
    except Exception:
        return {"weights": [0,0,0,0], "mean": 0, "status": "unknown",
                "countdown": 0, "last_photo": ""}


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    cfg = load_config()
    live = get_live_weights()
    photos = sorted(Path(PHOTOS_DIR).glob("*.jpg"), key=os.path.getmtime, reverse=True)
    photo_list = [{"name": p.name,
                   "size_kb": round(p.stat().st_size / 1024),
                   "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %Y  %H:%M:%S")}
                  for p in photos]
    return render_template("index.html", cfg=cfg, live=live, photos=photo_list)


# ── Scale settings ──────────────────────────────────────────────────────────────
@app.route("/save_scale", methods=["POST"])
@login_required
def save_scale():
    cfg = load_config()
    try:
        cfg["trigger_weight_g"]  = float(request.form["trigger_weight_g"])
        cfg["stabilise_seconds"] = float(request.form["stabilise_seconds"])
        cfg["unit"]              = request.form["unit"]
        cfg["cell_labels"]       = [
            request.form.get("label0","C1"), request.form.get("label1","C2"),
            request.form.get("label2","C3"), request.form.get("label3","C4"),
        ]
        save_config(cfg)
        flash("Scale settings saved.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#scale")


# ── GPIO settings ───────────────────────────────────────────────────────────────
@app.route("/save_gpio", methods=["POST"])
@login_required
def save_gpio():
    cfg = load_config()
    try:
        cfg["clk_pin"]   = int(request.form["clk_pin"])
        cfg["dout_pins"] = [
            int(request.form["dout0"]), int(request.form["dout1"]),
            int(request.form["dout2"]), int(request.form["dout3"]),
        ]
        save_config(cfg)
        flash("GPIO settings saved. Restart the scale app to apply.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#gpio")


# ── Display settings ────────────────────────────────────────────────────────────
@app.route("/save_display", methods=["POST"])
@login_required
def save_display():
    cfg = load_config()
    try:
        cfg["display_width"]       = int(request.form["display_width"])
        cfg["display_height"]      = int(request.form["display_height"])
        cfg["weight_strip_height"] = int(request.form["weight_strip_height"])
        save_config(cfg)
        flash("Display settings saved. Restart the scale app to apply.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#display")


# ── Calibration ─────────────────────────────────────────────────────────────────
@app.route("/tare", methods=["POST"])
@login_required
def do_tare():
    try:
        from scale_app import ScaleManager
        cfg   = load_config()
        scale = ScaleManager(cfg)
        scale.tare()
        flash("Tare complete — platform zeroed.", "ok")
    except Exception as e:
        flash(f"Tare failed: {e}", "err")
    return redirect(url_for("index") + "#calibration")

@app.route("/calibrate", methods=["POST"])
@login_required
def do_calibrate():
    try:
        from scale_app import ScaleManager
        cfg   = load_config()
        known = float(request.form["known_weight_g"])
        scale = ScaleManager(cfg)
        scale.calibrate(known)
        flash(f"Calibration done with {known}g reference.", "ok")
    except Exception as e:
        flash(f"Calibration failed: {e}", "err")
    return redirect(url_for("index") + "#calibration")

@app.route("/save_cal_manual", methods=["POST"])
@login_required
def save_cal_manual():
    """Allow directly editing offsets and cal_factors from the web UI."""
    cfg = load_config()
    try:
        cfg["offsets"]    = [float(request.form[f"offset{i}"]) for i in range(4)]
        cfg["cal_factors"]= [float(request.form[f"factor{i}"]) for i in range(4)]
        save_config(cfg)
        flash("Manual calibration values saved.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "err")
    return redirect(url_for("index") + "#calibration")


# ── WiFi ────────────────────────────────────────────────────────────────────────
@app.route("/add_wifi", methods=["POST"])
@login_required
def add_wifi():
    ssid     = request.form.get("ssid","").strip()
    password = request.form.get("wifi_pass","").strip()
    if not ssid:
        flash("SSID cannot be empty.", "err")
        return redirect(url_for("index") + "#wifi")
    try:
        _add_wifi_network(ssid, password)
        flash(f"Network '{ssid}' saved as fallback. Pi will connect if current network is unavailable.", "ok")
    except Exception as e:
        flash(f"WiFi error: {e}", "err")
    return redirect(url_for("index") + "#wifi")

def _add_wifi_network(ssid, password):
    """Append a new network block to wpa_supplicant.conf (fallback priority)."""
    wpa_file = "/etc/wpa_supplicant/wpa_supplicant.conf"
    # Read existing file
    with open(wpa_file, "r") as f:
        content = f.read()

    # Check if SSID already exists
    if f'ssid="{ssid}"' in content:
        log.info(f"SSID {ssid} already in wpa_supplicant — updating")
        # Remove old block (simple approach: warn user to remove manually)
        raise ValueError(f"Network '{ssid}' already exists. Remove it first or edit /etc/wpa_supplicant/wpa_supplicant.conf directly.")

    # Find highest existing priority
    import re
    priorities = [int(m) for m in re.findall(r"priority=(\d+)", content)]
    new_priority = (max(priorities) - 1) if priorities else 0   # lower = fallback

    block = f"""
network={{
    ssid="{ssid}"
    psk="{password}"
    priority={new_priority}
    id_str="fallback_{ssid}"
}}
"""
    with open(wpa_file, "a") as f:
        f.write(block)

    # Reload without full disconnect
    subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"], check=True)
    log.info(f"Added fallback network: {ssid} (priority {new_priority})")


# ── Photos ──────────────────────────────────────────────────────────────────────
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
    count = 0
    for p in Path(PHOTOS_DIR).glob("*.jpg"):
        p.unlink()
        count += 1
    flash(f"Deleted {count} photo(s).", "ok")
    return redirect(url_for("index") + "#photos")


# ── System ──────────────────────────────────────────────────────────────────────
@app.route("/reboot", methods=["POST"])
@login_required
def reboot():
    flash("Rebooting in 3 seconds…", "ok")
    threading.Thread(target=lambda: (time.sleep(3), subprocess.run(["sudo","reboot"])),
                     daemon=True).start()
    return redirect(url_for("index") + "#system")

@app.route("/shutdown", methods=["POST"])
@login_required
def shutdown():
    flash("Shutting down in 3 seconds…", "ok")
    threading.Thread(target=lambda: (time.sleep(3), subprocess.run(["sudo","shutdown","-h","now"])),
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


# ── Live status API (polled by JS) ──────────────────────────────────────────────
@app.route("/api/live")
@login_required
def api_live():
    return jsonify(get_live_weights())


if __name__ == "__main__":
    # Bind to all interfaces so it's reachable over the network
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
