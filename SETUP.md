# Smart Scale — Full Setup & Deployment Guide
**Raspberry Pi 4B | 4× HX711 | USB Webcam | 1024×600 Touchscreen**

---

## HARDWARE WIRING

### GPIO Pin Assignment (BCM numbering)

```
HX711 #1  DOUT ──► GPIO 5   (Physical pin 29)
HX711 #2  DOUT ──► GPIO 13  (Physical pin 33)
HX711 #3  DOUT ──► GPIO 19  (Physical pin 35)
HX711 #4  DOUT ──► GPIO 26  (Physical pin 37)

ALL HX711  CLK ──► GPIO 6   (Physical pin 31)   ← shared

HX711 VCC ──► 3.3V (Physical pin 1 or 17)
HX711 GND ──► GND  (Physical pin 6, 9, 14, 20, 25, 30, 34, or 39)
```

> Wire all 4 CLK pins together to GPIO 6. Each HX711 gets its own DOUT line.
> All 4 HX711 VCC and GND can share the same 3.3V and GND rail.

### USB Webcam
Plug into any USB port. It will appear as `/dev/video0`.

### Display
Connect via HDMI. Touchscreen USB cable goes into any USB port — touch input is handled by the OS automatically.

---

## PART 1 — RASPBERRY PI OS SETUP

### 1.1 Flash the OS
Use **Raspberry Pi Imager** on your PC/Mac.
- Choose: **Raspberry Pi OS (64-bit) with Desktop**
- In Imager settings (gear icon), set:
  - Hostname: `smartscale`
  - Username: `pi`
  - Password: *(your choice)*
  - WiFi SSID + password
  - Enable SSH

Flash to SD card and boot the Pi.

### 1.2 First boot — connect via SSH or open a terminal on the Pi

```bash
# Verify you can reach the Pi
ping smartscale.local
ssh pi@smartscale.local
```

---

## PART 2 — SYSTEM DEPENDENCIES

Run all of these in order. Takes ~5–10 minutes depending on your internet speed.

### 2.1 Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 2.2 Install Python packages and system libs

```bash
sudo apt install -y \
    python3-pip \
    python3-opencv \
    python3-flask \
    python3-rpi.gpio \
    python3-numpy \
    libatlas-base-dev \
    libopenblas-dev \
    libjpeg-dev \
    wpasupplicant
```

### 2.3 Install Python packages via pip

```bash
pip3 install --break-system-packages flask opencv-python-headless RPi.GPIO numpy
```

> `opencv-python-headless` is the pip version. The system `python3-opencv` from apt
> is also installed above as a fallback — either will work.

### 2.4 Verify OpenCV

```bash
python3 -c "import cv2; print('OpenCV OK:', cv2.__version__)"
```

### 2.5 Verify GPIO

```bash
python3 -c "import RPi.GPIO as G; print('GPIO OK')"
```

---

## PART 3 — COPY PROJECT FILES

### 3.1 Create the project directory

```bash
mkdir -p /home/pi/smartscale/photos
mkdir -p /home/pi/smartscale/templates
mkdir -p /home/pi/smartscale/static
```

### 3.2 Copy files to the Pi

**Option A — From your PC via SCP (run on your PC, not the Pi):**

```bash
scp scale_app.py   pi@smartscale.local:/home/pi/smartscale/
scp web_server.py  pi@smartscale.local:/home/pi/smartscale/
scp launcher.py    pi@smartscale.local:/home/pi/smartscale/
scp templates/login.html pi@smartscale.local:/home/pi/smartscale/templates/
scp templates/index.html pi@smartscale.local:/home/pi/smartscale/templates/
```

**Option B — Using a USB drive:**
Copy files to a USB stick, plug into Pi, then:

```bash
cp /media/pi/YOURDRIVENAM/smartscale/* /home/pi/smartscale/
cp /media/pi/YOURDRIVENAM/smartscale/templates/* /home/pi/smartscale/templates/
```

**Option C — Paste directly (for small edits):**

```bash
nano /home/pi/smartscale/scale_app.py
# paste content, Ctrl+O to save, Ctrl+X to exit
```

### 3.3 Verify file structure

```bash
ls -la /home/pi/smartscale/
ls -la /home/pi/smartscale/templates/
```

Expected output:
```
/home/pi/smartscale/
├── launcher.py
├── scale_app.py
├── web_server.py
├── config.json          ← auto-created on first run
├── scale.log            ← auto-created on first run
├── photos/
└── templates/
    ├── login.html
    └── index.html
```

---

## PART 4 — STATIC IP

### 4.1 Set static IP for wlan0

```bash
sudo nano /etc/dhcpcd.conf
```

Scroll to the bottom and add:

```
interface wlan0
static ip_address=192.168.1.100/24
static routers=192.168.1.1
static domain_name_servers=8.8.8.8 8.8.4.4
```

Save with `Ctrl+O`, exit with `Ctrl+X`.

> **Important:** Check your router's gateway IP first.
> If your router is `192.168.0.1` instead of `192.168.1.1`, change the subnet to match.
> Check on any connected device: Settings → WiFi → Gateway

### 4.2 Apply and verify

```bash
sudo systemctl restart dhcpcd
sleep 3
ip addr show wlan0
```

You should see `192.168.1.100` listed. If not, reboot:

```bash
sudo reboot
```

After reboot, SSH back in using the static IP:

```bash
ssh pi@192.168.1.100
```

---

## PART 5 — SUDO PERMISSIONS FOR WEB CONTROLS

The web UI needs to run `reboot`, `shutdown`, and edit `wpa_supplicant.conf`
without a password prompt. Add a sudoers rule:

```bash
sudo visudo -f /etc/sudoers.d/smartscale
```

Add these lines exactly:

```
pi ALL=(ALL) NOPASSWD: /sbin/reboot
pi ALL=(ALL) NOPASSWD: /sbin/shutdown
pi ALL=(ALL) NOPASSWD: /sbin/halt
pi ALL=(ALL) NOPASSWD: /usr/sbin/wpa_cli
```

Save and exit (`Ctrl+X`, `Y`, `Enter` in nano).

Verify the file is valid:

```bash
sudo visudo -c
```

---

## PART 6 — TEST RUN (before setting up autostart)

### 6.1 Test the web server alone first

```bash
cd /home/pi/smartscale
python3 web_server.py
```

Open a browser on your phone or laptop and go to:
```
http://192.168.1.100:5000
```

Login with `melody` / `raspi`. If the page loads, web server is working.
Press `Ctrl+C` to stop.

### 6.2 Test the scale app alone (in the Pi's desktop terminal)

```bash
cd /home/pi/smartscale
python3 scale_app.py
```

The camera feed should appear fullscreen. The bottom strip shows weight readings.
Press `q` to quit, `t` to manually tare.

### 6.3 Test the launcher (both together)

```bash
cd /home/pi/smartscale
python3 launcher.py
```

Camera feed appears on screen AND web UI is available at port 5000. Press `q` on
the display window to stop both.

---

## PART 7 — AUTOSTART ON BOOT (systemd service)

### 7.1 Install the service file

```bash
sudo cp /home/pi/smartscale/smartscale.service /etc/systemd/system/
```

### 7.2 Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable smartscale
sudo systemctl start smartscale
```

### 7.3 Check it is running

```bash
sudo systemctl status smartscale
```

You should see `Active: active (running)`.

### 7.4 View live logs

```bash
# Follow live log output
journalctl -u smartscale -f

# Or read the log file directly
tail -f /home/pi/smartscale/scale.log
```

---

## PART 8 — CALIBRATION PROCEDURE

Do this on first startup after the hardware is wired.

### Step 1 — Tare
1. Make sure the scale platform is completely empty.
2. Open the web UI: `http://192.168.1.100:5000`
3. Go to **Calibration** section.
4. Click **Run Tare Now**.
5. Wait for the confirmation message.

### Step 2 — Calibrate
1. Place a known reference weight on the platform (e.g. a 500g or 1kg object
   whose weight you know precisely — a sealed 1 litre water bottle = ~1000g).
2. In the **Calibration** section, enter the exact weight in grams.
3. Click **Run Calibration**.
4. The factors are saved to `config.json` automatically.
5. Remove the reference weight — the mean reading on the live bar should drop to ~0g.

### Step 3 — Verify
Place the same reference weight back and check that the MEAN reading on the live
bar matches. A ±5g variation is normal.

> Re-run tare + calibration any time the scale is moved or the readings drift.
> Tare button is also available on the web UI and the `t` key on the display.

---

## PART 9 — ACCESSING PHOTOS

### From any browser on the same WiFi:
```
http://192.168.1.100:5000
```
Login → scroll to **Photos** section → individual download or Download All (ZIP).

### Direct file access via SCP (from PC/Mac terminal):
```bash
scp -r pi@192.168.1.100:/home/pi/smartscale/photos/ ./scale_photos/
```

### Via Windows File Explorer (Samba — optional):
If you want the photos folder to appear as a network drive:

```bash
sudo apt install -y samba
sudo nano /etc/samba/smb.conf
```

Add at the bottom:
```ini
[ScalePhotos]
   path = /home/pi/smartscale/photos
   browseable = yes
   read only = yes
   guest ok = no
   valid users = pi
```

Set Samba password:
```bash
sudo smbpasswd -a pi
```

Restart Samba:
```bash
sudo systemctl restart smbd
```

Then on Windows: File Explorer → `\\192.168.1.100\ScalePhotos`

---

## PART 10 — USEFUL COMMANDS REFERENCE

```bash
# ── Service control ────────────────────────────────────────────────
sudo systemctl start smartscale       # start
sudo systemctl stop smartscale        # stop
sudo systemctl restart smartscale     # restart
sudo systemctl status smartscale      # status

# ── Logs ───────────────────────────────────────────────────────────
tail -f /home/pi/smartscale/scale.log          # follow log
journalctl -u smartscale -n 50                 # last 50 lines via journald
journalctl -u smartscale --since "10 min ago"  # last 10 minutes

# ── Manual tare from terminal ──────────────────────────────────────
# Press 't' key while the display window is in focus

# ── Check camera ───────────────────────────────────────────────────
ls /dev/video*         # should show /dev/video0
v4l2-ctl --list-devices

# ── Check GPIO ─────────────────────────────────────────────────────
gpio readall           # shows all pin states (install: sudo apt install wiringpi)

# ── Check static IP ────────────────────────────────────────────────
ip addr show wlan0
ping 192.168.1.100

# ── Edit config directly ───────────────────────────────────────────
nano /home/pi/smartscale/config.json

# ── Reboot / shutdown ──────────────────────────────────────────────
sudo reboot
sudo shutdown -h now

# ── Check disk space (photos) ──────────────────────────────────────
du -sh /home/pi/smartscale/photos/
df -h /
```

---

## TROUBLESHOOTING

| Problem | Check / Fix |
|---|---|
| Web UI not reachable | `sudo systemctl status smartscale` — check for errors. Verify IP: `ip addr show wlan0` |
| Camera feed black / not opening | `ls /dev/video*` — if missing, replug webcam. Try `camera_index: 1` in config |
| All weight readings are 0 | Check GPIO wiring. Run `python3 scale_app.py` in terminal and watch for HX711 timeout warnings in log |
| Readings drift after tare | Normal if platform vibrates. Re-tare on a stable surface |
| Photos not appearing in gallery | Check `ls /home/pi/smartscale/photos/` — verify write permissions: `ls -la /home/pi/smartscale/` |
| Service fails to start | `journalctl -u smartscale -n 30` to see the actual error |
| Static IP not working | Verify your router gateway — it may be `192.168.0.1` not `192.168.1.1`. Edit `/etc/dhcpcd.conf` |
| WiFi fallback not connecting | Run `wpa_cli -i wlan0 reconfigure` manually and check `wpa_cli status` |
| Scale app crashes on start | Missing display? Set `DISPLAY=:0` env variable. Check desktop is running |

---

## FILE LOCATIONS SUMMARY

| File | Location |
|---|---|
| Main scale app | `/home/pi/smartscale/scale_app.py` |
| Web server | `/home/pi/smartscale/web_server.py` |
| Launcher | `/home/pi/smartscale/launcher.py` |
| Config (JSON) | `/home/pi/smartscale/config.json` |
| Log file | `/home/pi/smartscale/scale.log` |
| Captured photos | `/home/pi/smartscale/photos/` |
| HTML templates | `/home/pi/smartscale/templates/` |
| Systemd service | `/etc/systemd/system/smartscale.service` |
| Static IP config | `/etc/dhcpcd.conf` |
| WiFi networks | `/etc/wpa_supplicant/wpa_supplicant.conf` |

---

## QUICK-START CHECKLIST

- [ ] Pi OS flashed and booted, SSH working  
- [ ] `sudo apt update && sudo apt upgrade -y` done  
- [ ] All apt and pip packages installed  
- [ ] Project files copied to `/home/pi/smartscale/`  
- [ ] Static IP set in `/etc/dhcpcd.conf` and verified  
- [ ] Sudoers file created for reboot/shutdown/wpa_cli  
- [ ] Manual test of `web_server.py` — web UI loads  
- [ ] Manual test of `scale_app.py` — camera feed shows  
- [ ] Tare + calibration done via web UI  
- [ ] Systemd service installed, enabled, started  
- [ ] Photo capture tested — object placed, countdown fired, photo saved  
- [ ] Photo download tested from phone/laptop browser  
