# Smart Scale — Full Setup & Deployment Guide (UART / ESP32 Edition)
**Raspberry Pi 4B | ESP32-CAM (HX711 bridge) | USB Webcam | 1024×600 Touchscreen**

This is the rebuilt version of the project: the Raspberry Pi no longer talks
to the HX711 ADCs directly (that was unreliable). An ESP32-CAM board now
reads all 4 HX711 chips and streams raw values to the Pi over a UART link.
The Pi owns all calibration/tare math, the camera, the touchscreen UI, and
the web config server — exactly as before, just fed by UART instead of GPIO.

Pi account used throughout this guide: **user `melody`, hostname `raspi`**
(fresh-flashed Pi OS). If you used different values, replace them everywhere
below and in `config.json`.

---

## HARDWARE WIRING

### 1. Load cells → HX711 → ESP32-CAM

Each of the 4 load cells still goes to its own HX711 module, exactly as
before. What's new is that **all 4 HX711 modules now wire to the ESP32-CAM**,
not the Pi.

```
HX711 #1  DOUT ──► ESP32 GPIO 2    (Top-Left)
HX711 #2  DOUT ──► ESP32 GPIO 14   (Top-Right)
HX711 #3  DOUT ──► ESP32 GPIO 13   (Bottom-Left)
HX711 #4  DOUT ──► ESP32 GPIO 4    (Bottom-Right)

ALL HX711  SCK  ──► ESP32 GPIO 15   ← shared clock line

HX711 VCC ──► ESP32 3.3V
HX711 GND ──► ESP32 GND
```

> These pins were deliberately chosen to avoid every ESP32-CAM
> camera-header pin and boot-strapping pin (GPIO0), even though this
> firmware doesn't use the camera — it keeps the board's other headers free.

### 2. ESP32-CAM → Raspberry Pi (UART)

The ESP32-CAM has no onboard USB-serial chip — it needs either an external
USB-TTL adapter (for flashing) or a direct wire link to the Pi's hardware
UART (for normal operation). We use the Pi's GPIO14/15 UART pins:

```
ESP32-CAM GPIO1 (U0TXD) ──► Pi GPIO15 / RXD  (physical pin 10)
ESP32-CAM GPIO3 (U0RXD) ◄── Pi GPIO14 / TXD  (physical pin 8)
ESP32-CAM GND            ── Pi GND           (physical pin 6, 9, 14, 20, 25, 30, 34, or 39)
```

> **Important:** this is the SAME pair of ESP32 pins used for flashing via
> an external USB-TTL adapter. Disconnect the Pi link before flashing new
> firmware onto the ESP32, and reconnect it afterward. Trying to drive TX
> from both the Pi and a USB-TTL adapter at once will not work.

### 3. Hardware capture button

```
Button pin A ──► Pi GPIO17 (physical pin 11)
Button pin B ──► Pi GND (any GND pin)
```

The Pi enables an internal pull-up on GPIO17, so the button just needs to
short it to GND — a simple momentary push-button works fine. No external
resistor is required.

### 4. USB Webcam
Plug into any USB port on the Pi. It will appear as `/dev/video0`.

### 5. Display
Connect via HDMI. Touchscreen USB cable goes into any USB port — touch
input is handled by the OS automatically (shows up to pygame as mouse
events, which this project relies on).

### 6. ESP32-CAM power
Power the ESP32-CAM from a stable 5V source (its own onboard 3.3V
regulator feeds the chip and the HX711s). Don't power it from the Pi's
3.3V rail — it draws more current than the Pi's GPIO header should supply,
especially during Wi-Fi/brownout-prone moments even though Wi-Fi is
disabled in this firmware.

---

## PART 1 — RASPBERRY PI OS SETUP

### 1.1 Flash the OS
Use **Raspberry Pi Imager** on your PC/Mac.
- Choose: **Raspberry Pi OS (64-bit) with Desktop**
- In Imager settings (gear icon), set:
  - Hostname: `raspi`
  - Username: `melody`
  - Password: *(your choice)*
  - WiFi SSID + password
  - Enable SSH

Flash to SD card and boot the Pi.

### 1.2 First boot — connect via SSH or open a terminal on the Pi

```bash
ping raspi.local
ssh melody@raspi.local
```

### 1.3 Disable the serial console, keep the serial PORT enabled

By default, Raspberry Pi OS puts a login console on the same UART pins
we need for the ESP32 link. Free them up:

```bash
sudo raspi-config
```

Navigate: **3 Interface Options → I6 Serial Port**
- "Would you like a login shell to be accessible over serial?" → **No**
- "Would you like the serial port hardware to be enabled?" → **Yes**

Reboot when prompted:

```bash
sudo reboot
```

After reboot, `/dev/serial0` will be the ESP32 link, free of console noise.

Verify:
```bash
ls -la /dev/serial0
# should symlink to /dev/ttyAMA0 or /dev/ttyS0 depending on Pi model
```

---

## PART 2 — SYSTEM DEPENDENCIES (fresh Pi — run all of this)

Run all of these in order. Takes ~10–15 minutes depending on your internet
speed.

### 2.1 Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 2.2 Install system packages

```bash
sudo apt install -y \
    python3-pip \
    python3-opencv \
    python3-flask \
    python3-rpi.gpio \
    python3-numpy \
    python3-serial \
    libatlas-base-dev \
    libopenblas-dev \
    libjpeg-dev \
    wpasupplicant \
    udisks2 \
    policykit-1 \
    ntfs-3g \
    lxterminal
```

What each new package is for versus the old GPIO-only version:
- `python3-serial` — talks to the ESP32 over UART (the `serial` module)
- `udisks2` + `policykit-1` — lets the scale app mount/unmount a USB drive
  from the touchscreen app without a password prompt (desktop session
  polkit rule covers this)
- `ntfs-3g` — so an NTFS-formatted USB drive also works for export, not
  just FAT32/exFAT
- `lxterminal` — used by the desktop launcher icon (Part 8) so you can see
  the app's log output if you start it manually

### 2.3 Install Python packages via pip

```bash
pip3 install --break-system-packages flask opencv-python-headless RPi.GPIO numpy pyserial
```

> `opencv-python-headless` is the pip version. The system `python3-opencv`
> from apt is also installed above as a fallback — either will work.
> Likewise `python3-serial` (apt) and `pyserial` (pip) are the same library
> under two install paths — having both installed is harmless.

### 2.4 Verify everything

```bash
python3 -c "import cv2; print('OpenCV OK:', cv2.__version__)"
python3 -c "import RPi.GPIO as G; print('GPIO OK')"
python3 -c "import serial; print('pyserial OK:', serial.__version__)"
python3 -c "import pygame; print('pygame OK')"
```

If `pygame` is missing:
```bash
pip3 install --break-system-packages pygame
```

---

## PART 3 — ESP32-CAM FIRMWARE

### 3.1 Arduino IDE setup
1. Install the [Arduino IDE](https://www.arduino.cc/en/software) on your PC/Mac.
2. File → Preferences → Additional Board URLs, add:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
3. Tools → Board → Boards Manager → search "esp32" → install the Espressif package.
4. Tools → Board → select **AI Thinker ESP32-CAM**.
5. Tools → Manage Libraries → search **"HX711"** → install the one by
   **Bogdan Necula** (sometimes listed as "HX711 by Bogdan Necula / Andreas
   Motl"). This firmware uses that library's read functions directly rather
   than a hand-rolled bit-bang implementation — an earlier draft of this
   firmware used a custom implementation and produced occasional wild,
   wrong readings; the library is the same well-tested code your original
   working ESP32 sketch used.

### 3.2 Flashing connection (temporary, USB-TTL adapter — NOT the Pi)

```
USB-TTL Adapter    ESP32-CAM
────────────────    ─────────
5V / VCC       ──►  5V
GND            ──►  GND
TX             ──►  GPIO 3 (U0RXD)
RX             ──►  GPIO 1 (U0TXD)
                     GPIO 0 ──► GND (only while flashing — puts the board in
                                 flash mode; remove after flashing)
```

1. Wire as above, GPIO0 to GND.
2. Press the ESP32-CAM's reset button (or power-cycle it).
3. In Arduino IDE: select the correct COM/serial port, upload `ESP32_Code.cpp`
   (rename to `ESP32_Code.ino` if the IDE requires the `.ino` extension —
   just a filename change, no code changes needed).
4. After a successful upload, disconnect GPIO0 from GND and reset the board
   again — it will now boot into normal run mode.
5. Open the Arduino Serial Monitor at **115200 baud** — you should see:
   ```
   A,READY
   D,123,-45,67,890,3C
   D,...
   ```
   (raw numbers will vary — this confirms all 4 HX711 channels are being
   read). A new line should appear roughly every 150ms. With the platform
   empty and stationary, watch the numbers for a few seconds — they should
   drift only slightly, never jump wildly. If any channel jumps by huge
   amounts (thousands+) randomly, that's a wiring/connection problem on
   that specific HX711, not a Pi-side issue — check its DOUT/SCK/VCC/GND
   connections before moving on.

> **Note:** this firmware has no tare command and accepts nothing from the
> Pi at all — it only ever sends data. There's nothing to test from the Pi
> side of the link except confirming data arrives; all tare/calibration
> testing happens on the Pi (Part 9).

### 3.3 Switch to the Pi link

Disconnect the USB-TTL adapter, wire the ESP32 to the Pi's GPIO14/15 as
described in the Hardware Wiring section, and power the ESP32 from a
standalone 5V source. From here on, the ESP32 only talks to the Pi.

---

## PART 4 — COPY PROJECT FILES TO THE PI

### 4.1 Create the project directory

```bash
mkdir -p /home/melody/smartscale/photos
mkdir -p /home/melody/smartscale/templates
mkdir -p /home/melody/smartscale/static
```

### 4.2 Copy files to the Pi

**Option A — From your PC via SCP (run on your PC, not the Pi):**

```bash
scp scale_app.py   melody@raspi.local:/home/melody/smartscale/
scp web_server.py  melody@raspi.local:/home/melody/smartscale/
scp launcher.py    melody@raspi.local:/home/melody/smartscale/
scp SmartScale.desktop melody@raspi.local:/home/melody/smartscale/
scp templates/login.html melody@raspi.local:/home/melody/smartscale/templates/
scp templates/index.html melody@raspi.local:/home/melody/smartscale/templates/
```

**Option B — Using a USB drive:**
Copy files to a USB stick, plug into the Pi, then:

```bash
cp /media/melody/YOURDRIVENAME/smartscale/*.py /home/melody/smartscale/
cp /media/melody/YOURDRIVENAME/smartscale/templates/* /home/melody/smartscale/templates/
```

**Option C — Paste directly (for small edits):**

```bash
nano /home/melody/smartscale/scale_app.py
# paste content, Ctrl+O to save, Ctrl+X to exit
```

### 4.3 Verify file structure

```bash
ls -la /home/melody/smartscale/
ls -la /home/melody/smartscale/templates/
```

Expected:
```
/home/melody/smartscale/
├── launcher.py
├── scale_app.py
├── web_server.py
├── SmartScale.desktop
├── config.json          ← auto-created on first run
├── scale.log             ← auto-created on first run
├── photos/
└── templates/
    ├── login.html
    └── index.html
```

---

## PART 5 — STATIC IP

### 5.1 Set static IP for wlan0

```bash
sudo nano /etc/dhcpcd.conf
```

Scroll to the bottom and add (contents also in `dhcpcd_static_ip.conf`):

```
interface wlan0
static ip_address=192.168.1.100/24
static routers=192.168.1.1
static domain_name_servers=8.8.8.8 8.8.4.4
```

> Check your router's gateway IP first — it may be `192.168.0.1` instead of
> `192.168.1.1`. Adjust the subnet to match.

### 5.2 Apply and verify

```bash
sudo systemctl restart dhcpcd
sleep 3
ip addr show wlan0
```

You should see `192.168.1.100` listed. If not, reboot:

```bash
sudo reboot
```

---

## PART 6 — SUDO PERMISSIONS FOR WEB CONTROLS

The web UI needs to run `reboot`/`shutdown` and edit `wpa_supplicant.conf`
without a password prompt.

```bash
sudo visudo -f /etc/sudoers.d/smartscale
```

Add these lines exactly:

```
melody ALL=(ALL) NOPASSWD: /sbin/reboot
melody ALL=(ALL) NOPASSWD: /sbin/shutdown
melody ALL=(ALL) NOPASSWD: /sbin/halt
melody ALL=(ALL) NOPASSWD: /usr/sbin/wpa_cli
melody ALL=(ALL) NOPASSWD: /bin/systemctl restart smartscale
```

Verify the file is valid:

```bash
sudo visudo -c
```

### 6.1 Passwordless USB mounting (required for the Export button)

Relying on polkit's "active session" detection for password-free
`udisksctl` mounting turned out to be unreliable in practice (it can prompt
for a password depending on exactly how the session was started). A
polkit rule fixes this explicitly instead:

```bash
sudo cp /home/melody/smartscale/99-udisks2-melody.rules /etc/polkit-1/rules.d/
sudo systemctl restart polkit
```

Test it directly (no app needed) — plug in a USB drive, find its device
name, then:

```bash
lsblk -o NAME,TRAN,FSTYPE
udisksctl mount -b /dev/sda1     # use the actual partition name from lsblk
```

This should mount immediately with no password prompt. If it still asks
for a password, double-check the rule file copied correctly and that
polkit actually restarted (`sudo systemctl status polkit`).

---

## PART 7 — TEST RUN (before setting up autostart)

### 7.1 Test the web server alone first

```bash
cd /home/melody/smartscale
python3 web_server.py
```

Open a browser and go to `http://192.168.1.100:5000` (or `http://raspi.local:5000`).
Login with `melody` / `raspi`. If the page loads, the web server is working —
weight values will show as 0 until the scale app is also running.
Press `Ctrl+C` to stop.

### 7.2 Test the scale app alone (in the Pi's desktop, not over SSH)

pygame's fullscreen display needs an actual desktop session — run this at
the Pi itself (or via VNC), not a headless SSH session.

```bash
cd /home/melody/smartscale
python3 scale_app.py
```

Expect, in order:
1. `ScaleManager init — UART=/dev/serial0@115200`
2. `Startup tare — platform must be EMPTY. Waiting 3s for UART link...`
3. The touchscreen fills with the camera feed, a top control bar
   (`TARE | CAPTURE | AUTO: ON | EXPORT USB`), and the bottom weight strip.

Test each control:
- Tap **TARE** — status text should read "Tare complete" within a couple
  of seconds.
- Tap **CAPTURE** — takes a photo immediately, shows "Photo saved!".
- Tap **AUTO** — toggles between ON/OFF; when OFF, placing weight on the
  platform does nothing (no countdown).
- Tap **EXPORT USB** (with a drive plugged in) — should progress through
  "Looking for USB drive... → Mounting... → Zipping... → Copying... →
  Transfer complete!"
- Press the physical GPIO17 button — same as CAPTURE.
- Press `t` on a keyboard — same as TARE.
- Press `q` — quits cleanly.

### 7.3 Test the launcher (both together)

```bash
cd /home/melody/smartscale
python3 launcher.py
```

Camera feed + controls appear on screen AND the web UI is available at
port 5000, both reading from the same live scale data. Press `q` on the
display to stop both.

---

## PART 8 — AUTOSTART ON BOOT (systemd) + DESKTOP ICON

### 8.1 Install the systemd service

```bash
sudo cp /home/melody/smartscale/smartscale.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smartscale
sudo systemctl start smartscale
```

Check it's running:
```bash
sudo systemctl status smartscale
```
You should see `Active: active (running)`.

View live logs:
```bash
journalctl -u smartscale -f
# or
tail -f /home/melody/smartscale/scale.log
```

### 8.2 Desktop icon

A `.desktop` launcher file is included so you (or anyone at the Pi) can
start/restart the app manually from the desktop, without SSH — useful if
you quit the app with `q` for debugging and want to relaunch it without
a full reboot.

```bash
mkdir -p /home/melody/Desktop
cp /home/melody/smartscale/SmartScale.desktop /home/melody/Desktop/
cp /home/melody/smartscale/SmartScale.desktop /home/melody/.local/share/applications/
chmod +x /home/melody/Desktop/SmartScale.desktop
```

Raspberry Pi OS (Bookworm+) treats new desktop icons as "untrusted" until
you explicitly allow them — right-click the icon on the Desktop and choose
**"Allow Launching"** (or **"Trust"**), otherwise double-clicking does nothing.

Because `smartscale.service` already autostarts the app on boot, double-
clicking this icon while the service is already running will just open a
second instance and fail to grab the camera/UART (both are already in use).
Stop the service first if you want to run it manually for debugging:

```bash
sudo systemctl stop smartscale
# then double-click the desktop icon, or run python3 launcher.py yourself
```

---

## PART 9 — CALIBRATION PROCEDURE

Do this once the hardware is wired and the app is running.

### Step 1 — Tare
1. Make sure the scale platform is completely empty.
2. Either wait for the automatic startup tare (happens ~2 seconds after the
   app launches), or open the web UI → **Calibration** → **Run Tare Now**,
   or tap **TARE** on the touchscreen.
3. This is Pi-side only — the ESP32 never does anything but stream raw
   data, so there's nothing to wait on for an ESP32 acknowledgement. Tare
   should complete in under a second.

### Step 2 — Calibrate
1. Place a known reference weight on the platform (e.g. a 1kg object whose
   weight you know precisely).
2. In the web UI's **Calibration** section, enter the exact weight in kg.
3. Click **Run Calibration**.
4. The factors are saved to `config.json` automatically.
5. Remove the reference weight — the mean reading should drop to ~0.

### Step 3 — Verify
Place the same reference weight back and check the MEAN reading matches.
A small variation is normal.

> Re-run tare + calibration any time the scale is moved, load cells are
> re-wired, or readings drift.

---

## PART 10 — USING THE EXPORT-TO-USB FEATURE

1. Format a USB drive as FAT32 or exFAT (NTFS also works via `ntfs-3g`).
2. Plug it into any Pi USB port.
3. Tap **EXPORT USB** on the touchscreen.
4. Watch the status text in the top-right of the control bar:
   `Looking for USB drive... → Mounting... → Zipping photos... →
   Copying to USB... → Transfer complete!`
5. Wait for "Transfer complete!" before removing the drive — the app
   unmounts it automatically right after the copy finishes.

This **copies** a zip of the entire `photos/` folder — it never deletes
anything from the Pi. Nothing on the drive is touched except the new zip
file it writes.

---

## PART 11 — USEFUL COMMANDS REFERENCE

```bash
# ── Service control ────────────────────────────────────────────────
sudo systemctl start smartscale       # start
sudo systemctl stop smartscale        # stop
sudo systemctl restart smartscale     # restart
sudo systemctl status smartscale      # status

# ── Logs ───────────────────────────────────────────────────────────
tail -f /home/melody/smartscale/scale.log      # follow log
journalctl -u smartscale -n 50                 # last 50 lines via journald
journalctl -u smartscale --since "10 min ago"  # last 10 minutes

# ── Manual tare from the touchscreen/keyboard ──────────────────────
# Tap TARE on screen, or press 't' key while the display window is focused

# ── Check camera ───────────────────────────────────────────────────
ls /dev/video*         # should show /dev/video0
v4l2-ctl --list-devices

# ── Check UART link to ESP32 ───────────────────────────────────────
ls -la /dev/serial0
python3 -c "import serial; s=serial.Serial('/dev/serial0',115200,timeout=2); print(s.readline())"

# ── Check GPIO ─────────────────────────────────────────────────────
gpio readall           # install: sudo apt install wiringpi (if you want this tool)

# ── Check static IP ────────────────────────────────────────────────
ip addr show wlan0
ping 192.168.1.100

# ── Check USB drives (for export debugging) ────────────────────────
lsblk -o NAME,TRAN,TYPE,FSTYPE,MOUNTPOINT,SIZE
udisksctl status

# ── Edit config directly ───────────────────────────────────────────
nano /home/melody/smartscale/config.json

# ── Reboot / shutdown ──────────────────────────────────────────────
sudo reboot
sudo shutdown -h now

# ── Check disk space (photos) ──────────────────────────────────────
du -sh /home/melody/smartscale/photos/
df -h /
```

---

## TROUBLESHOOTING

| Problem | Check / Fix |
|---|---|
| Web UI not reachable | `sudo systemctl status smartscale` — check for errors. Verify IP: `ip addr show wlan0` |
| **Login page shows "Internal Server Error" / `TemplateNotFound: login.html`** | The template file is missing on disk, not a code bug. Run `ls -la /home/melody/smartscale/templates/` — you need to see BOTH `login.html` and `index.html` there. If missing, re-copy/re-pull; check your git repo actually committed the `templates/` folder (a common gotcha: only the root `.py` files get added and the templates subfolder is forgotten) |
| Camera feed black / not opening | `ls /dev/video*` — if missing, replug webcam. Try `camera_index: 1` in config |
| "ESP32 LINK LOST" on the weight strip | Check the ESP32 is powered and the GPIO14/15↔GPIO1/3 wiring is correct (TX↔RX crossed, not straight-through). Check `/dev/serial0` exists. Check the serial console was disabled (Part 1.3). This firmware never pauses its own loop (no tare command anymore), so a link that drops after being fine for a while points to a wiring/power issue, not a firmware stall |
| All weight readings are 0 or `no_data` | Check HX711↔ESP32 wiring first (not Pi wiring — that's gone now). Reflash the ESP32 and watch its Serial Monitor for `D,...` lines before wiring it to the Pi |
| Weight readings jump wildly / look like random noise, not just "a bit jittery" | Confirm you're running the current firmware (uses the `HX711` library, not a hand-rolled bit-bang) and that the HX711 library is actually installed (Part 3.1). Watch the ESP32's own Serial Monitor directly — if raw numbers are already wild there with an empty, stationary platform, it's a wiring/power/ground issue on that HX711 module, not something fixable in software |
| Readings drift after tare | Normal if the platform vibrates. Re-tare on a stable surface |
| Photos not appearing in gallery | Check `ls /home/melody/smartscale/photos/` — verify write permissions |
| Storage cap seems to be deleting photos too aggressively | Raise `photos_max_mb` in the web UI's Storage section |
| Export USB says "No USB drive found" | `lsblk -o NAME,TRAN` — confirm the drive shows `TRAN=usb`. Try a different USB port or drive |
| Export USB asks for a password / hangs on mount | You're missing the polkit rule — see Part 6.1. This is required, not optional; without it, mounting can silently wait for a password prompt nothing on the touchscreen can answer |
| Export USB says "Mount failed" (after installing the polkit rule) | Run `udisksctl mount -b /dev/sdX1` manually over SSH to see the real error |
| Service fails to start | `journalctl -u smartscale -n 30` to see the actual error |
| Static IP not working | Verify your router gateway — it may be `192.168.0.1` not `192.168.1.1`. Edit `/etc/dhcpcd.conf` |
| WiFi fallback not connecting | Run `wpa_cli -i wlan0 reconfigure` manually and check `wpa_cli status` |
| Scale app crashes on start (pygame) | Missing display? Set `DISPLAY=:0` env variable. Confirm the desktop session is actually running (not a headless SSH shell) |
| Desktop icon does nothing when double-clicked | Right-click → "Allow Launching" / "Trust" (Bookworm marks new .desktop files untrusted by default) |
| GPIO17 button does nothing | Check wiring (button between GPIO17 and GND). Check nothing else already claims GPIO17 (`gpio readall`). Check `scale.log` for "Capture button setup failed" |

---

## FILE LOCATIONS SUMMARY

| File | Location |
|---|---|
| Main scale app | `/home/melody/smartscale/scale_app.py` |
| Web server | `/home/melody/smartscale/web_server.py` |
| Launcher | `/home/melody/smartscale/launcher.py` |
| Desktop icon | `/home/melody/smartscale/SmartScale.desktop` |
| ESP32 firmware (flashed separately) | `ESP32_Code.cpp` (via Arduino IDE) |
| Config (JSON) | `/home/melody/smartscale/config.json` |
| Log file | `/home/melody/smartscale/scale.log` |
| Captured photos | `/home/melody/smartscale/photos/` |
| HTML templates | `/home/melody/smartscale/templates/` |
| Systemd service | `/etc/systemd/system/smartscale.service` |
| Static IP config | `/etc/dhcpcd.conf` |
| WiFi networks | `/etc/wpa_supplicant/wpa_supplicant.conf` |
| Sudoers rule | `/etc/sudoers.d/smartscale` |
| Polkit rule (passwordless USB mount) | `/etc/polkit-1/rules.d/99-udisks2-melody.rules` |

## GPIO / PIN SUMMARY

| Board | Pin | Function |
|---|---|---|
| ESP32-CAM | GPIO15 | HX711 shared SCK |
| ESP32-CAM | GPIO2 | HX711 #1 DOUT (Top-Left) |
| ESP32-CAM | GPIO14 | HX711 #2 DOUT (Top-Right) |
| ESP32-CAM | GPIO13 | HX711 #3 DOUT (Bottom-Left) |
| ESP32-CAM | GPIO4 | HX711 #4 DOUT (Bottom-Right) |
| ESP32-CAM | GPIO1 (U0TXD) | UART to Pi GPIO15 (RXD) |
| ESP32-CAM | GPIO3 (U0RXD) | UART from Pi GPIO14 (TXD) |
| Pi | GPIO14 (physical 8) | UART TXD → ESP32 GPIO3 |
| Pi | GPIO15 (physical 10) | UART RXD ← ESP32 GPIO1 |
| Pi | GPIO17 (physical 11) | Hardware capture button (active-low, internal pull-up) |

---

## QUICK-START CHECKLIST

- [ ] Pi OS flashed (hostname `raspi`, user `melody`), booted, SSH working
- [ ] Serial console disabled, serial port hardware enabled (`raspi-config`)
- [ ] `sudo apt update && sudo apt upgrade -y` done
- [ ] All apt and pip packages installed (Part 2)
- [ ] ESP32-CAM firmware flashed and verified over USB-TTL Serial Monitor
- [ ] ESP32↔4×HX711 wiring done
- [ ] ESP32↔Pi UART wiring done (TX↔RX crossed, common GND)
- [ ] GPIO17 hardware button wired
- [ ] Project files copied to `/home/melody/smartscale/`
- [ ] Static IP set in `/etc/dhcpcd.conf` and verified
- [ ] Sudoers file created for reboot/shutdown/wpa_cli
- [ ] Manual test of `web_server.py` — web UI loads
- [ ] Manual test of `scale_app.py` — camera feed + control bar show, ESP32 link shows connected
- [ ] Tare + calibration done via web UI or on-screen TARE button
- [ ] Systemd service installed, enabled, started
- [ ] Desktop icon installed and trusted
- [ ] Manual capture (button + on-screen + hardware GPIO17) tested
- [ ] Autocapture toggle tested (ON and OFF)
- [ ] USB export tested with a real drive, confirmed "Transfer complete!"
- [ ] Storage cap value reviewed for your SD card size
