"""
Smart Scale — Unified Launcher
Starts both the Flask web server and the OpenCV scale display in one process.
Run this file directly: python3 launcher.py
"""

import threading
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("/home/pi/smartscale/scale.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("launcher")


def run_web():
    log.info("Starting web server on 0.0.0.0:5000")
    try:
        from web_server import app
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
    except Exception as e:
        log.error(f"Web server crashed: {e}", exc_info=True)


def run_scale():
    log.info("Starting scale display app")
    try:
        from scale_app import main
        main()
    except Exception as e:
        log.error(f"Scale app crashed: {e}", exc_info=True)


if __name__ == "__main__":
    log.info("=== Smart Scale Launcher ===")

    web_thread = threading.Thread(target=run_web, daemon=True, name="web-server")
    web_thread.start()

    # Scale app runs on main thread (OpenCV requires main thread on some systems)
    run_scale()

    log.info("Scale app exited — launcher stopping")
