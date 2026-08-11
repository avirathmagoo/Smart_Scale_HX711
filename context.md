# Smart Scale — Project Context & Architecture Reference

This document is the single source of truth for how this project fits
together: what each file does, how data flows from a load cell to a photo
on the touchscreen, every GPIO pin in use, the UART packet format, the
calibration math, the on-screen controls, and known trade-offs. Read this
before making changes — it should answer "why does X work this way" for
almost everything in the codebase.

---

## 1. WHAT THIS PROJECT IS

A weighing station with a camera: an object is placed on a 4-corner load
cell platform, its weight is measured, and (automatically or manually) a
photo is taken with the weight burned into the bottom of the image. Photos
are stored on the Pi's SD card and can be retrieved either over WiFi (a
password-protected web page) or copied to a USB flash drive at the device
itself.

**Physical components:**
- 4× load cells (one per corner of a plate)
- 4× HX711 24-bit ADC modules (one per load cell)
- 1× ESP32-CAM board (camera unused — only used as an HX711-to-UART bridge)
- 1× Raspberry Pi 4B
- 1× USB webcam
- 1× 1024×600 HDMI touchscreen
- 1× momentary push-button (hardware capture button)
- 1× USB flash drive (used only during export, not permanently attached)

---

## 2. WHY THE ARCHITECTURE CHANGED (history, for context)

**Generation 1:** the Pi bit-banged all 4 HX711 chips directly via
`RPi.GPIO`. This didn't work reliably — Python on a general-purpose Linux
scheduler can't guarantee the tight, sub-millisecond clock-pulse timing
HX711 chips expect, so reads were noisy/corrupted under load.

**Generation 2 (this version):** an ESP32-CAM does the HX711 bit-banging
instead — a microcontroller running bare-metal C++ can hit that timing
reliably — and streams the results to the Pi over a simple UART link. The
Pi is now purely a *consumer* of load-cell data; it never touches HX711
hardware directly. Everything else (camera, touchscreen UI, web server,
calibration math, photo storage) stayed conceptually the same, just
re-pointed at the new data source.

---

## 3. SYSTEM ARCHITECTURE DIAGRAM (textual)

```
┌─────────────┐   HX711 DOUT×4 + shared SCK   ┌───────────────┐
│  4x Load     │ ─────────────────────────────► │   ESP32-CAM   │
│  Cells+HX711 │                                 │  (bridge only,│
└─────────────┘                                 │  camera/WiFi  │
                                                  │  code removed)│
                                                  └───────┬───────┘
                                                          │ UART
                                                          │ (GPIO14/15 <-> GPIO1/3)
                                                          │ 115200 baud
                                                          v
┌───────────────────────────── Raspberry Pi 4B ─────────────────────────────┐
│                                                                             │
│   ┌───────────────┐   raw counts    ┌────────────────┐   kg values        │
│   │  UARTReader    │ ──────────────► │  ScaleManager   │ ─────────┐        │
│   │  (thread)      │  ring buffers   │  proc thread    │          │        │
│   └───────────────┘                 └────────────────┘          │        │
│                                                                    v        │
│   ┌───────────────┐                                     ┌──────────────┐  │
│   │  USB Webcam    │ ─── frames ──────────────────────► │   scale_app  │  │
│   └───────────────┘                                     │  main loop   │  │
│                                                            │  (pygame)   │  │
│   ┌───────────────┐   button press event                │              │  │
│   │  GPIO17 button │ ─────────────────────────────────►  │              │  │
│   └───────────────┘                                     └──────┬───────┘  │
│                                                                   │ photo   │
│                                                                   v         │
│                                                          /home/melody/     │
│                                                          smartscale/photos │
│                                                                   ^         │
│   ┌───────────────┐         reads shared state + config          │         │
│   │  web_server.py │ ◄─────────────────────────────────────────┘         │
│   │  (Flask)       │ ── serves index.html, handles tare/calibrate,       │
│   └───────┬────────┘    WiFi add, reboot/shutdown, photo download        │
│           │ HTTP :5000                                                    │
└───────────┼─────────────────────────────────────────────────────────────┘
            v
      Any browser on the same WiFi (phone/laptop)
```

Both `scale_app.py` (touchscreen app) and `web_server.py` (Flask server)
run in **the same Python process**, started by `launcher.py`, on separate
threads. This matters: it's why `web_server.py` can reach into
`scale_app`'s live objects (`get_shared()`, `get_scale_manager()`) directly
via a plain Python import, instead of needing its own IPC mechanism.

---

## 4. THE UART PROTOCOL (ESP32 <-> Pi)

This is the one piece of "custom wire protocol" in the whole project — kept
deliberately simple so it's easy to debug with nothing more than a serial
terminal.

### 4.1 Physical layer
- Pi hardware UART (`/dev/serial0`, mapped to GPIO14 TXD / GPIO15 RXD)
- 115200 baud, 8N1, no flow control
- ESP32-CAM's UART0 (GPIO1 TXD / GPIO3 RXD) — the same pins used for
  flashing via an external USB-TTL adapter

### 4.2 ESP32 -> Pi: data packets (sent ~20x/second)

```
D,<raw1>,<raw2>,<raw3>,<raw4>,<checksum>\n
```

- `raw1..raw4` — signed 24-bit integers (HX711's native range,
  -8,388,608 to 8,388,607), ASCII decimal, **already minus the ESP32's own
  tare offset** (see §5), but with NO scale factor applied — this is
  intentionally still "raw" from the Pi's point of view.
- `checksum` — 2 hex digits, uppercase. Computed as the XOR of every byte
  in the substring `"<raw1>,<raw2>,<raw3>,<raw4>"` (not including the
  leading `D,` or the checksum itself).

Example: `D,1234,-567,8901,-234,7A`

The Pi (`UARTReader._handle_data_line`) recomputes the same XOR and drops
the packet silently (counted in `_invalid_count` for diagnostics) if it
doesn't match. This catches truncated lines, dropped bytes, or noise from
a flaky wire.

### 4.3 ESP32 -> Pi: status/ack lines (sent once, not on a timer)

```
A,READY        — sent once at boot
A,TARE_OK      — sent after a successful tare
A,TARE_FAIL    — sent if one or more channels timed out during tare
```

### 4.4 Pi -> ESP32: commands

```
T\n   (or just the byte 't'/'T', case-insensitive)
```

The only command in the protocol. Triggers a tare on the ESP32 side (see
§5). The Pi always waits for the `A,TARE_OK`/`A,TARE_FAIL` reply (with a
timeout) before considering the ESP32-side tare complete.

### 4.5 Why ASCII, not binary
Chosen deliberately over a binary struct because you can literally
`cat /dev/serial0` or open a serial terminal at 115200 and read the data
with your eyes — no decoder needed. The tradeoff is a few more bytes on
the wire, which is irrelevant at 20Hz/115200 baud.

---

## 5. WHERE CALIBRATION MATH LIVES (and why it's split this way)

Both the ESP32 and the Pi do a "tare" step, and they do different things —
this is intentional, not redundant:

- **ESP32-side tare** (`tareAll()` in `ESP32_Code.cpp`): averages ~15 fresh
  raw samples per channel and stores them as `tareOffset[i]`, subtracted
  from every future raw reading before it's sent to the Pi. This removes
  bulk zero-drift *at the source* — e.g. if a load cell's baseline has
  drifted a lot since last power-on, the numbers hitting the Pi's UART
  buffer are already close to zero, keeping them well within the Pi's
  jump/noise sanity checks.

- **Pi-side tare** (`ScaleManager.tare()` in `scale_app.py`): takes the
  current (already ESP32-zeroed) ring buffer, computes a per-channel
  trimmed mean, and stores that as `cfg["offsets"]` in `config.json`. This
  is the offset actually used in the kg conversion formula.

- **Pi-side calibration factor** (`cfg["cal_factors"]`): a known reference
  weight is placed on the platform; the Pi computes
  `factor = (raw_avg - offset) / known_kg` per channel. This, too, lives
  entirely on the Pi, in `config.json` — the ESP32 has no concept of
  "kilograms" at all, it only ever deals in raw ADC counts.

**Final formula**, applied every ~80ms in `ScaleManager._proc_loop`:

```
kg[i] = (raw_avg[i] - offsets[i]) / cal_factors[i]
```

`raw_avg[i]` is a trimmed mean (drops the extreme 15% high/low) over the
last 16 raw samples in that channel's ring buffer — this rejects the
occasional single-sample spike without needing a full statistical filter.

**Why split it this way instead of putting it all on one side:**
putting calibration entirely on the Pi means the existing, already-tested
offset/factor math and the web UI's calibration form barely had to change
when the load-cell acquisition moved from GPIO to UART — only the *source*
of raw numbers changed. Putting the ESP32-side tare on top is a small
addition that meaningfully improves long-term drift tolerance for free.

### `combined_tare()` — what actually runs when you tap TARE
```
ScaleManager.combined_tare():
    1. Send 'T' to ESP32, wait for A,TARE_OK / A,TARE_FAIL (up to 5s)
    2. Sleep 0.3s (let a few freshly-zeroed packets arrive)
    3. Run Pi-side tare() using the current ring buffer
```
This is what runs at app startup, from the on-screen TARE button, from the
hardware `t` keyboard shortcut (desktop debugging), and from the web UI's
"Run Tare Now" button (via `get_scale_manager()`, see §7).

---

## 6. FILE-BY-FILE REFERENCE

### `ESP32_Code.cpp` (flashed to the ESP32-CAM, not part of the Pi's Python code)
- Bit-bangs 4 HX711 chips in round-robin, non-blocking (`pollScales()`
  checks exactly one channel's `is_ready()` per call, never stalls the
  loop waiting on a channel that isn't ready yet)
- `hx711ReadRaw()` — the actual 24-clock-pulse + 1 gain-set-pulse bit
  sequence, with two's-complement sign extension. No third-party HX711
  library used — implemented directly for fewer dependencies and full
  control over timing.
- `tareAll()` — averages a fresh batch of samples per channel (up to a
  200ms timeout per sample) and stores the result as that channel's
  `tareOffset`
- `sendDataPacket()` — builds and writes one `D,...` line per send interval
  (`SEND_INTERVAL_MS`, default 50ms -> ~20Hz)
- `loop()` also watches `Serial.available()` for an incoming `'T'`/`'t'`
  byte from the Pi and runs `tareAll()` in response, replying with an ack
- Camera, WiFi, and WebSocket code from the original ESP32-CAM dashboard
  firmware has been fully removed — this board is now a dedicated,
  single-purpose HX711-to-UART bridge

### `scale_app.py` (runs on the Pi, the touchscreen application)
Threads:
- **`uart-reader`** (inside `UARTReader`) — owns the actual `serial.Serial`
  connection, reads lines continuously, validates checksums, fills 4
  per-channel ring buffers (`collections.deque(maxlen=24)`), tracks
  connection health (`connected` = a valid packet arrived in the last 2s)
- **`hx711-proc`** (inside `ScaleManager`) — every ~80ms, reads a snapshot
  of the ring buffers, applies the trimmed-mean filter, applies
  offset/cal_factor, computes an EMA-smoothed per-channel and mean weight,
  and determines "stable" (reading hasn't moved more than 0.008kg for 10
  consecutive checks)
- **`camera`** (inside `Camera`) — continuously grabs webcam frames for
  the live preview; on a capture request, briefly switches the camera to
  its higher photo resolution, grabs a few frames to flush the buffer,
  takes one, then switches back to the lower streaming resolution
- **GPIO17 interrupt callback** (inside `CaptureButton`) — not a polling
  thread; uses `RPi.GPIO.add_event_detect(..., GPIO.FALLING, ...)`, which
  runs the callback from a GPIO library-managed thread the instant the pin
  goes low, with a 250ms software debounce built into `add_event_detect`'s
  `bouncetime` parameter
- **main thread** — pygame event loop, state machine, drawing, and the
  background-thread launches for tare/capture/export actions (so none of
  those block the UI)

Key classes/functions:
- `UARTReader` — see §4 for protocol details. `send_tare()` is the only
  way the Pi ever writes to the ESP32.
- `ScaleManager` — owns a `UARTReader`, runs the `hx711-proc` thread,
  exposes `get_values()` (non-blocking, returns the latest computed dict),
  `tare()`/`combined_tare()`/`calibrate()`, and `diagnostics()` (used by
  the web UI's `/api/diagnostics`)
- `CaptureButton` — wraps the GPIO17 interrupt; `consume()` returns `True`
  exactly once per press (clears its internal event), so the main loop's
  polling of it never double-fires
- `Camera` — unchanged from the original GPIO-era version; two resolutions
  (streaming vs. photo) because pulling full photo resolution at video
  framerate would be too slow
- `save_photo()` — draws the weight strip onto a copy of the captured
  frame with OpenCV (`cv2.putText`), writes a JPEG to `photos/` named
  `capture_YYYYMMDD_HHMMSS.jpg`
- `enforce_storage_cap()` — called after every save; sums the `photos/`
  folder, deletes oldest-first until back under `cfg["photos_max_mb"]`
- `_find_usb_partition()` / `_udisks_mount()` / `_udisks_unmount()` /
  `export_photos_to_usb()` — the USB export pipeline (see §8)
- `_scale_manager` (module-level global) + `get_scale_manager()` — lets
  `web_server.py`, running in the same process, reach the live
  `ScaleManager` instance instead of opening a second UART connection
  (see §7 for why this matters)
- `main()` — orchestrates startup order: create `ScaleManager` -> start it ->
  wait 3s for the UART link to come up -> `combined_tare()` -> create
  `CaptureButton` -> start `Camera` -> init pygame -> enter the main loop

### `web_server.py` (runs on the Pi, in the same process as `scale_app.py`)
- Flask app, session-based login (`melody`/`raspi`, hardcoded — see §10
  for why this is a known limitation, not an oversight)
- `load_config()`/`save_config()` — same `config.json` file `scale_app.py`
  uses; `scale_app.py`'s main loop polls the file's mtime and hot-reloads
  most settings without a restart (a few, like display resolution and the
  UART port, need a restart — the web UI says so next to those forms)
- `get_live()` — pulls `scale_app.get_shared()`, the small dict of
  "what should the web page show right now" (weights, status, UART link
  health, autocapture state, last photo)
- `_get_live_scale()` — pulls `scale_app.get_scale_manager()`; **this is
  the fix for a real bug that almost shipped**: an earlier draft had the
  web routes open their *own* `UARTReader` (i.e. a second serial
  connection to the same port the running scale app already has open).
  Two readers on one UART device would race for bytes and corrupt both
  streams. The final version always drives tare/calibrate through the
  *one* live `ScaleManager` object that `scale_app.py`'s `main()` already
  created, via the `_scale_manager` module global.
- Routes:
  - `/`, `/login`, `/logout` — page + auth
  - `/save_scale`, `/save_uart`, `/save_display`, `/save_storage`,
    `/save_cal_manual` — config form handlers, each touches only its own
    slice of `config.json`
  - `/tare`, `/calibrate` — call into the live `ScaleManager` (see above)
  - `/add_wifi` — appends a `network={}` block to
    `/etc/wpa_supplicant/wpa_supplicant.conf` and asks `wpa_cli` to
    reconfigure
  - `/photos/<file>`, `/download/<file>`, `/download_all` (zip),
    `/delete_all` — photo retrieval/management
  - `/reboot`, `/shutdown`, `/restart_scale` — system controls (need the
    sudoers rules from `SETUP.md` Part 6)
  - `/api/live` — polled by `index.html`'s JS every second for the live
    weight bar
  - `/api/diagnostics` — UART packet counts, connection state, current
    offsets/factors

### `index.html` (Jinja2 template, served by `web_server.py`)
Sections (anchor-linked from the top nav): Scale, Calibration, UART/Button,
Display, Storage, WiFi, Photos, System. The live weight bar at the top
polls `/api/live` every second via `fetch()` and updates weight values,
status, ESP32 link state, and autocapture state without a page reload.

Note: this page shows autocapture's *state* but does not let you toggle
it — that's intentionally only reachable from the on-screen slider at the
scale itself, so it's changeable without needing a phone/laptop open.

### `login.html`
Unchanged from the original — simple username/password form, dark theme
matching `index.html`.

### `launcher.py`
Single entry point. Starts `web_server.py`'s Flask app on a daemon thread,
then runs `scale_app.py`'s `main()` on the main thread (pygame + GPIO
interrupts are best kept on the main thread). This is the file
`smartscale.service` and the desktop icon both actually execute.

### `smartscale.service`
systemd unit. Runs as `melody`, depends on `graphical.target` (needs an
X11 desktop session for pygame's fullscreen window and for the USB-export
feature's polkit-based mounting, which is tied to an active desktop
session). Auto-restarts on failure with a 5s backoff.

### `SmartScale.desktop`
Manual-launch icon for the desktop, opens a terminal running
`launcher.py` so you can see log output live. Mainly useful when you've
stopped the systemd service for debugging and want to relaunch by hand.

### `dhcpcd_static_ip.conf`
Reference block to paste into `/etc/dhcpcd.conf` — pins the Pi's WiFi
interface to a known IP so the web UI is always reachable at the same
address.

### `config.json` (auto-created, not checked in)
See §9 for the full schema.

---

## 7. WHY scale_app AND web_server SHARE ONE PROCESS

`launcher.py` imports and runs both in one Python process (Flask on a
thread, pygame on the main thread). This was true in the original GPIO-era
version too, but it matters even more now:

- The UART connection to the ESP32 is a **single serial port** — only one
  file descriptor should be reading/writing it at a time. Because both
  "sides" of the app live in one process, `web_server.py` can call methods
  directly on the *same* `ScaleManager`/`UARTReader` object `scale_app.py`
  created, instead of needing a second connection (which would corrupt the
  stream) or an IPC layer (sockets/files) to coordinate two processes.
- The tradeoff: if `scale_app.py`'s main loop hangs (e.g. pygame stuck),
  the Flask thread is still technically alive but any route touching the
  live `ScaleManager` will hang too, since it's a plain Python function
  call in a shared process, not an independent service. `smartscale.service`'s
  `Restart=on-failure` only helps if the process actually crashes/exits,
  not if it hangs — worth knowing if you ever see the web UI go
  unresponsive without a corresponding process restart in the logs.

---

## 8. USB EXPORT — HOW IT WORKS UNDER THE HOOD

Triggered only from the on-screen **EXPORT USB** button (not the hardware
button, not the web UI — the web UI has its own separate `/download_all`
zip-download route for retrieval over WiFi instead).

```
1. _find_usb_partition()
   -> runs `lsblk -J -o NAME,TRAN,TYPE,FSTYPE,MOUNTPOINT`
   -> walks the JSON tree for a top-level device with TRAN=="usb"
   -> returns the first child partition that has a filesystem
   -> returns None if nothing matches ("No USB drive found")

2. _udisks_mount(devpath)
   -> runs `udisksctl mount -b /dev/sdX1`
   -> udisksctl handles this WITHOUT sudo, authorized via polkit for the
      active desktop session (this is why smartscale.service depends on
      graphical.target)
   -> parses the mount point out of udisksctl's stdout/stderr with a regex

3. Zip photos/*.jpg into a temp file (Python's zipfile module)

4. shutil.copy() the zip onto the mounted drive

5. _udisks_unmount(devpath)
   -> always runs, even on error (in a `finally` block), so the drive is
      never left mounted after a failed export
```

**Deliberately NOT supported** (kept out for reliability/simplicity):
multiple drives at once, deleting from the Pi as part of export, resuming
a partial copy, non-FAT/exFAT/NTFS filesystems. If any step fails, the
on-screen status text says so in plain language and the drive is still
safely unmounted.

---

## 9. `config.json` SCHEMA

Auto-created with these defaults on first run if the file doesn't exist;
any missing keys are backfilled from defaults on every load (so upgrading
this file's shape over time doesn't require manual migration).

| Key | Default | Meaning |
|---|---|---|
| `display_width` / `display_height` | 1024 / 600 | Touchscreen resolution |
| `weight_strip_height` | 50 | Bottom strip height (px), on screen AND baked into photos |
| `control_bar_height` | 50 | Top control-bar height (px) |
| `trigger_weight_kg` | 0.5 | Autocapture fires above this |
| `stabilise_seconds` | 5.0 | Countdown before an autocapture photo (manual capture ignores this) |
| `unit` | "kg" | Display unit (kg math is internal regardless; this is display-only in the web UI) |
| `cell_labels` | ["C1","C2","C3","C4"] | Shown on screen, in photos, and in the calibration table |
| `uart_port` | "/dev/serial0" | Pi's serial device talking to the ESP32 |
| `uart_baud` | 115200 | Must match `ESP32_Code.cpp`'s `BAUD_RATE` |
| `button_gpio` | 17 | BCM pin for the hardware capture button |
| `offsets` | [0,0,0,0] | Pi-side tare offsets (raw counts), per channel |
| `cal_factors` | [1.0,1.0,1.0,1.0] | Raw-counts-per-kg, per channel |
| `camera_index` | 0 | `/dev/videoN` |
| `stream_width/height` | 640x480 | Live preview resolution |
| `photo_width/height` | 1280x720 | Captured photo resolution |
| `autocapture_enabled` | true | Weight-triggered auto-capture on/off (toggled on-screen) |
| `photos_max_mb` | 20000 | Storage cap — oldest photos deleted past this |

---

## 10. KNOWN LIMITATIONS / DELIBERATE TRADE-OFFS

These are conscious "simplicity over completeness" choices, not bugs:

- **Hardcoded web login** (`melody`/`raspi`) — fine for a device on a
  private home/workshop WiFi network; would need real user management for
  anything more exposed.
- **Single ESP32, single UART port** — no redundancy; if the ESP32 crashes
  or the wire comes loose, the Pi shows "ESP32 LINK LOST" and holds the
  last known weight values rather than guessing. It does NOT auto-reconnect
  beyond pyserial's normal read retry loop — a wedged ESP32 needs a
  power-cycle.
- **No database, CSV log, or capture history beyond the JPEGs themselves**
  — by explicit request, keeping this to "just photos with weight baked
  in," same as the original design.
- **USB export is copy-only, single-drive** — no multi-drive selection UI,
  no incremental/delta export (always zips everything currently in
  `photos/`).
- **Same-process web+display app** (§7) — simplest to build and to keep
  the UART port single-owner, at the cost of the two being unable to fail
  fully independently of each other.
- **Manual capture always overrides state** — pressing CAPTURE (button or
  on-screen) mid-countdown or mid-cooldown immediately takes a photo and
  resets to "cooldown," rather than queuing or blocking. This favors
  predictability (button always does something immediately) over strict
  state-machine purity.
- **Photo storage cap is a blunt oldest-first deletion**, not
  size-per-day or any smarter retention policy.

---

## 11. WHERE TO LOOK WHEN SOMETHING BREAKS

Rough triage order, cheapest checks first:

1. **`scale.log`** (`/home/melody/smartscale/scale.log`) — almost every
   component logs here (UART errors, tare/calibrate results, camera
   errors, storage cap deletions, USB export progress).
2. **"ESP32 LINK LOST" on the weight strip** -> it's a UART/wiring problem,
   not a Pi software problem. Check the ESP32 is powered, check TX/RX
   aren't swapped, check the serial console was actually disabled
   (`raspi-config`).
3. **Weight reads `no_data` per-channel but the link is fine** -> that
   specific HX711/load-cell wiring to the ESP32, not the Pi. Flash-test the
   ESP32 alone with a USB-TTL adapter and watch its Serial Monitor.
4. **Web UI unreachable** -> `systemctl status smartscale`, then
   `journalctl -u smartscale -n 50`.
5. **USB export fails** -> re-run its steps manually over SSH
   (`lsblk`, `udisksctl mount -b ...`) to see the real error the on-screen
   status text is summarizing.

See also the Troubleshooting table in `SETUP.md`, which maps specific
symptoms to specific fixes.
