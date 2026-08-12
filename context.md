# Smart Scale — Project Context & Architecture Reference

This document is the single source of truth for how this project fits
together: what each file does, how data flows from a load cell to a photo
on the touchscreen, every GPIO pin in use, the UART packet format, the
calibration math, the on-screen controls, and known trade-offs. Read this
before making changes — it should answer "why does X work this way" for
almost everything in the codebase.

**This is revision 3.** Revisions 1 and 2 had the ESP32 doing its own tare
on request from the Pi; that turned out to be the root cause of several
real bugs (§4, §5) and has been removed entirely. If you're reading old
notes or an old copy of `ESP32_Code.cpp` that mentions a `'T'` command or
`A,TARE_OK`/`A,TARE_FAIL`, that's superseded — the current firmware only
ever reads sensors and sends data, nothing else.

---

## 1. WHAT THIS PROJECT IS

A weighing station with a camera: an object is placed on a 4-corner load
cell platform, its weight is measured, and (automatically or manually) a
photo is taken with the weight burned into the bottom of the image. Photos
are stored on the Pi's SD card and can be retrieved either over WiFi (a
password-protected web page) or copied to a USB flash drive at the device
itself.

**Physical components:**
- 4x load cells (one per corner of a plate)
- 4x HX711 24-bit ADC modules (one per load cell)
- 1x ESP32-CAM board (camera unused — only used as an HX711-to-UART bridge)
- 1x Raspberry Pi 4B
- 1x USB webcam
- 1x 1024x600 HDMI touchscreen (app runs windowed by default — see §12)
- 1x momentary push-button (hardware capture button)
- 1x USB flash drive (used only during export, not permanently attached)

---

## 2. WHY THE ARCHITECTURE CHANGED (history, for context)

**Generation 1:** the Pi bit-banged all 4 HX711 chips directly via
`RPi.GPIO`. This didn't work reliably — Python on a general-purpose Linux
scheduler can't guarantee the tight, sub-millisecond clock-pulse timing
HX711 chips expect, so reads were noisy/corrupted under load.

**Generation 2:** an ESP32-CAM took over HX711 reading and streamed raw
values to the Pi over UART — but used a hand-rolled bit-bang read
function instead of a proven library, AND had a tare command that could
pause the ESP32's main loop for several seconds. Both were mistakes: the
custom bit-bang occasionally produced wildly wrong readings (a single
flipped bit in a 24-bit value is a multi-million-count error), and the
multi-second pause during tare looked exactly like a dead UART link from
the Pi's side.

**Generation 3 (this version):** the ESP32 uses the actual `HX711` Arduino
library (the same one the very first working prototype used) for reading,
lightly averages on-device, and does absolutely nothing else — no
commands accepted, no tare, no pausing, ever. It is now a pure,
continuous sensor-to-UART bridge. All tare and calibration logic lives
entirely on the Pi, coordinating nothing with the ESP32.

---

## 3. SYSTEM ARCHITECTURE DIAGRAM (textual)

```
Load Cells+HX711 (x4)  --DOUT x4 + shared SCK-->  ESP32-CAM
                                                    (read-only bridge,
                                                     no commands, no tare)
                                                         |
                                                         | UART, ESP32->Pi ONLY
                                                         | (GPIO14/15 <-> GPIO1/3)
                                                         | 115200 baud
                                                         v
                     Raspberry Pi 4B
   UARTReader (thread, read-only) --raw counts--> ScaleManager proc thread
                                                    (Pi-side tare)
                                                         |
                                                         v gram values
   USB Webcam --frames-->  scale_app main loop (pygame)
   GPIO17 button --press event--> scale_app main loop
                                                         |
                                                         v photo
                                          /home/melody/smartscale/photos
                                                         ^
   web_server.py (Flask) <--reads shared state + config-+
   serves index.html, handles tare/calibrate, WiFi add,
   reboot/shutdown, photo download
       |
       | HTTP :5000
       v
   Any browser on the same WiFi (phone/laptop)
```

Both `scale_app.py` (touchscreen app) and `web_server.py` (Flask server)
run in **the same Python process**, started by `launcher.py`, on separate
threads. This matters: it's why `web_server.py` can reach into
`scale_app`'s live objects (`get_shared()`, `get_scale_manager()`) directly
via a plain Python import, instead of needing its own IPC mechanism.

---

## 4. THE UART PROTOCOL (ESP32 -> Pi, ONE DIRECTION ONLY)

This is the one piece of "custom wire protocol" in the whole project — kept
deliberately simple so it's easy to debug with nothing more than a serial
terminal. **As of revision 3, this protocol is strictly one-way**: the
ESP32 sends, the Pi only ever listens. The Pi never writes a single byte
to the ESP32.

### 4.1 Physical layer
- Pi hardware UART (`/dev/serial0`, mapped to GPIO14 TXD / GPIO15 RXD) —
  the Pi's TXD pin is technically unused by this protocol, but the wiring
  is still bidirectional in case a future revision needs it
- 115200 baud, 8N1, no flow control
- ESP32-CAM's UART0 (GPIO1 TXD / GPIO3 RXD) — the same pins used for
  flashing via an external USB-TTL adapter

### 4.2 ESP32 -> Pi: data packets (sent ~6.7x/second)

```
D,<raw1>,<raw2>,<raw3>,<raw4>,<checksum>
```

- `raw1..raw4` — signed 24-bit integers (HX711's native range,
  -8,388,608 to 8,388,607), ASCII decimal. Each is already the average of
  the last 4 raw samples taken on that channel (on-device smoothing —
  see §5), but has NO tare offset and NO scale factor applied — this is
  genuinely raw, uncalibrated data.
- `checksum` — 2 hex digits, uppercase. Computed as the XOR of every byte
  in the substring `"<raw1>,<raw2>,<raw3>,<raw4>"` (not including the
  leading `D,` or the checksum itself).

Example: `D,1234,-567,8901,-234,7A`

The Pi (`UARTReader._handle_data_line`) recomputes the same XOR and drops
the packet silently (counted in `_invalid_count` for diagnostics) if it
doesn't match. This catches truncated lines, dropped bytes, or noise from
a flaky wire.

**Send interval is 150ms (~6.7Hz), deliberately slower than revision 2's
50ms.** The HX711 itself only completes a conversion at ~10Hz per channel
— sending faster than that just repeats stale data and makes the Pi's
display update more often than the underlying measurement actually
changes, which reads as jitter even when the true weight is dead stable.

### 4.3 ESP32 -> Pi: boot banner (once, informational only)

```
A,READY
```

Sent once at boot. The Pi logs it if seen but doesn't depend on it for
anything — the data packets are the only thing that actually matters.

### 4.4 Pi -> ESP32: NOTHING

There is no command channel. `UARTReader` in `scale_app.py` never calls
`.write()` on the serial connection at all. This is intentional — see §5
for why a bidirectional tare handshake was removed.

### 4.5 Why ASCII, not binary
Chosen deliberately over a binary struct because you can literally
`cat /dev/serial0` or open a serial terminal at 115200 and read the data
with your eyes — no decoder needed. The tradeoff is a few more bytes on
the wire, which is irrelevant at ~7Hz/115200 baud.

---

## 5. WHY TARE IS PI-SIDE ONLY (and why that's not a compromise)

Earlier revisions had the ESP32 do its own tare on request from the Pi
(average some fresh samples, store the result, subtract it from future
readings). This was removed after real-world testing showed two related
problems:

1. **The ESP32's tare routine blocked its main loop.** Averaging 15
   samples across 4 channels, each waiting up to a 200ms timeout per
   sample, could take several seconds during which the ESP32 sent
   *nothing* over UART.
2. **That silence looked exactly like a dead link.** The Pi's own timeout
   for waiting on a tare acknowledgement was shorter than the ESP32's
   tare could actually take, so the Pi would give up and log "no ack from
   ESP32 within timeout" — every single time, even though the ESP32
   eventually did finish and reply, just too late to matter.

**The fix wasn't to tune the timeouts — it was to remove the coordination
entirely.** The ESP32 (`ESP32_Code.cpp`) now has no command handling of
any kind. It reads sensors, lightly averages, and sends — in a loop that
never pauses for anything. There is nothing that can make it go quiet
except an actual hardware/wiring fault, which is exactly what you want:
if the link drops now, it means something is actually wrong, not "the
ESP32 is busy averaging."

Tare, entirely on the Pi, is just: *remember what the current (already
averaged, already noise-filtered) raw stream reads as, and subtract that
from everything going forward.* No coordination, no waiting, no failure
mode where one side times out on the other.

```
ScaleManager.tare():
    1. Take the current ring-buffer snapshot (already-arrived UART data —
       no new samples are requested, nothing is sent to the ESP32)
    2. Compute a per-channel trimmed mean
    3. Store that as cfg["offsets"], save to config.json
    4. Reset the EMA/hysteresis display state to zero
```

This runs at app startup (after a 2-second wait for the first UART
packets to arrive), from the on-screen TARE button, from the `t` keyboard
shortcut, and from the web UI's "Run Tare Now" button — all four call the
exact same `ScaleManager.tare()` method.

### On-device averaging, not on-device tare
The ESP32 does still do *some* signal conditioning — each channel keeps a
small 4-sample circular buffer and sends the average, rather than a raw
single ADC conversion. This is smoothing, not calibration: it has no
concept of "zero" or "kilograms," it's purely "average my last few
readings before sending," which reduces the amount of pure sample-to-sample
ADC noise the Pi has to filter out. It requires no state that could get
out of sync with the Pi and nothing that can block.

### Calibration factor (unchanged from earlier revisions)
A known reference weight is placed on the platform; the Pi computes
`factor = (raw_avg - offset) / known_kg` per channel and stores it in
`cfg["cal_factors"]`. This, too, lives entirely on the Pi — the ESP32 has
no concept of "kilograms" at all, it only ever deals in raw ADC counts.

**Final formula**, applied every ~100ms in `ScaleManager._proc_loop`:

```
kg[i]    = (raw_avg[i] - offsets[i]) / cal_factors[i]
grams[i] = kg[i] * 1000
```

`raw_avg[i]` is a trimmed mean (drops the extreme 15% high/low) over the
last 12 raw samples in that channel's ring buffer (which are themselves
already 4-sample averages from the ESP32 — see §12 for the full filtering
chain and why it's tuned the way it is).

---

## 6. FILE-BY-FILE REFERENCE

### `ESP32_Code.cpp` (flashed to the ESP32-CAM, not part of the Pi's Python code)
- Uses the **`HX711` Arduino library** (install via Library Manager,
  search "HX711", author Bogdan Necula) for the actual bit-bang read —
  NOT a custom implementation. An earlier revision hand-rolled this and
  produced occasional wildly wrong readings (a single bit error in a
  24-bit value is a multi-million-count jump); the library is the same
  well-tested code the very first working prototype used.
- `pollScales()` — non-blocking round-robin: checks exactly one channel's
  `is_ready()` per call (instant, just a digitalRead), and only calls the
  library's `read()` once that channel is already known to be ready (so
  it returns immediately, no risk of the library's internal wait-loop
  blocking noticeably)
- Each channel keeps a small 4-sample circular buffer (`rawHistory`);
  `averagedRaw()` returns their mean — light on-device smoothing, no
  concept of tare or scale
- `sendDataPacket()` — builds and writes one `D,...` line every
  `SEND_INTERVAL_MS` (150ms, ~6.7Hz — matched to the HX711's own ~10Hz
  conversion rate, no point sending faster than the sensor actually updates)
- `loop()` does NOT check `Serial.available()` for anything — there is no
  command protocol in this revision. It only ever writes to Serial, never
  reads from it.
- Camera, WiFi, and WebSocket code from the original ESP32-CAM dashboard
  firmware was removed in revision 2 and stays removed — this board is a
  dedicated, single-purpose HX711-to-UART bridge

### `scale_app.py` (runs on the Pi, the touchscreen application)
Threads:
- **`uart-reader`** (inside `UARTReader`) — owns the actual `serial.Serial`
  connection, reads lines continuously, validates checksums, fills 4
  per-channel ring buffers (`collections.deque(maxlen=20)`), tracks
  connection health (`connected` = a valid packet arrived in the last 2s).
  **Read-only** — never calls `.write()`.
- **`hx711-proc`** (inside `ScaleManager`) — every ~100ms, reads a snapshot
  of the ring buffers, applies the trimmed-mean filter, applies
  offset/cal_factor, computes an EMA-smoothed per-channel and mean gram
  value, applies display hysteresis, and determines "stable"
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
- `UARTReader` — see §4 for protocol details. Purely a reader; has no
  method that writes to the serial port at all in this revision.
- `ScaleManager` — owns a `UARTReader`, runs the `hx711-proc` thread,
  exposes `get_values()` (non-blocking, returns the latest computed dict),
  `tare()` (Pi-side only, see §5), `calibrate()`, and `diagnostics()`
  (used by the web UI's `/api/diagnostics`)
- `CaptureButton` — wraps the GPIO17 interrupt; `consume()` returns `True`
  exactly once per press (clears its internal event), so the main loop's
  polling of it never double-fires
- `Camera` — two resolutions (streaming vs. photo) because pulling full
  photo resolution at video framerate would be too slow
- `blit_camera_frame()` — draws the camera frame aspect-correct inside a
  given screen area (letterboxed, not stretched — see §12)
- `save_photo()` — draws the weight strip onto a copy of the captured
  frame with OpenCV (`cv2.putText`), writes a JPEG to `photos/` named
  `capture_YYYYMMDD_HHMMSS.jpg`, using whole-gram values with no decimals
- `enforce_storage_cap()` — called after every save; sums the `photos/`
  folder, deletes oldest-first until back under `cfg["photos_max_mb"]`
- `_find_usb_partition()` / `_udisks_mount()` / `_udisks_unmount()` /
  `export_photos_to_usb()` — the USB export pipeline (see §8)
- `_clean_udisks_path()` — strips old-style Unix quote punctuation
  (backtick and apostrophe) that `udisksctl` includes in some message
  formats (notably "already mounted" errors) but not others — a real bug
  in an earlier revision that caused a genuine export failure, fixed by
  stripping this punctuation from any parsed mount path regardless of
  which message format produced it
- `_scale_manager` (module-level global) + `get_scale_manager()` — lets
  `web_server.py`, running in the same process, reach the live
  `ScaleManager` instance instead of opening a second UART connection
- `main()` — orchestrates startup order: create `ScaleManager` -> start it
  -> wait 2s for the first UART packets to arrive -> `tare()` -> create
  `CaptureButton` -> start `Camera` -> init pygame (windowed by default,
  see §12) -> enter the main loop

### `web_server.py` (runs on the Pi, in the same process as `scale_app.py`)
- Flask app, session-based login (`melody`/`raspi`, hardcoded — see §10
  for why this is a known limitation, not an oversight)
- `load_config()`/`save_config()` — same `config.json` file `scale_app.py`
  uses; `scale_app.py`'s main loop polls the file's mtime and hot-reloads
  most settings without a restart (a few, like display resolution/mode
  and the UART port, need a restart — the web UI says so next to those
  forms)
- `get_live()` — pulls `scale_app.get_shared()`, the small dict of
  "what should the web page show right now" (gram weights, status, UART
  link health, autocapture state, last photo)
- `_get_live_scale()` — pulls `scale_app.get_scale_manager()` so tare/
  calibrate routes drive the SAME live `ScaleManager`/`UARTReader`
  instance `scale_app.py` already created, instead of opening a second
  serial connection (which would corrupt both streams — the fix for a
  bug caught before it ever shipped)
- Routes:
  - `/`, `/login`, `/logout` — page + auth
  - `/save_scale`, `/save_uart`, `/save_display`, `/save_storage`,
    `/save_cal_manual` — config form handlers, each touches only its own
    slice of `config.json`
  - `/tare` — calls `scale.tare()` directly (Pi-side only, instant, no
    ESP32 coordination — see §5)
  - `/calibrate` — calls `scale.calibrate(known_kg)`
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
polls `/api/live` every second via `fetch()` and updates gram values,
status, ESP32 link state, and autocapture state without a page reload.
Display section includes a fullscreen on/off checkbox (§12).

Note: this page shows autocapture's *state* but does not let you toggle
it — that's intentionally only reachable from the on-screen slider at the
scale itself, so it's changeable without needing a phone/laptop open.

**If this page 500s with `TemplateNotFound: login.html` or
`TemplateNotFound: index.html`**, that's a deployment issue, not a code
bug — Flask's template loader looks for these files at
`/home/melody/smartscale/templates/`. Verify both files actually exist
there (`ls -la /home/melody/smartscale/templates/`); a common cause is a
git repo that only tracked the top-level `.py` files and never committed
the `templates/` subfolder.

### `login.html`
Simple username/password form, dark theme matching `index.html`. No
special logic — if this fails to load, see the `index.html` note above,
it's the same root cause.

### `launcher.py`
Single entry point. Starts `web_server.py`'s Flask app on a daemon thread,
then runs `scale_app.py`'s `main()` on the main thread (pygame + GPIO
interrupts are best kept on the main thread). This is the file
`smartscale.service` and the desktop icon both actually execute. Cannot
be run over a plain SSH session without `DISPLAY` set — pygame needs an
actual X11 display, see §11.

### `smartscale.service`
systemd unit. Runs as `melody`, depends on `graphical.target` (needs an
X11 desktop session for pygame's fullscreen/windowed surface). Auto-restarts
on failure with a 5s backoff.

### `SmartScale.desktop`
Manual-launch icon for the desktop, opens a terminal running
`launcher.py` so you can see log output live. Mainly useful when you've
stopped the systemd service for debugging and want to relaunch by hand.

### `99-udisks2-melody.rules`
Polkit rule granting `melody` passwordless `udisksctl` mount/unmount for
removable filesystems specifically (not full disk/system control). Needed
because relying on polkit's "active session" auto-detection turned out to
be unreliable in practice — it would sometimes prompt for a password the
touchscreen app has no way to answer, causing the Export button to hang
until timeout. See §8 and `SETUP.md` Part 6.1 for installation.

### `dhcpcd_static_ip.conf`
Reference block to paste into `/etc/dhcpcd.conf` — pins the Pi's WiFi
interface to a known IP so the web UI is always reachable at the same
address.

### `config.json` (auto-created, not checked in)
See §9 for the full schema.

---

## 7. WHY scale_app AND web_server SHARE ONE PROCESS

`launcher.py` imports and runs both in one Python process (Flask on a
thread, pygame on the main thread).

- The UART connection to the ESP32 is a **single serial port** — only one
  file descriptor should be reading it at a time. Because both "sides" of
  the app live in one process, `web_server.py` can call methods directly
  on the *same* `ScaleManager`/`UARTReader` object `scale_app.py` created,
  instead of needing a second connection (which would corrupt the stream)
  or an IPC layer to coordinate two processes.
- The tradeoff: if `scale_app.py`'s main loop hangs (e.g. pygame stuck),
  the Flask thread is still technically alive but any route touching the
  live `ScaleManager` will hang too, since it's a plain Python function
  call in a shared process. `smartscale.service`'s `Restart=on-failure`
  only helps if the process actually crashes/exits, not if it hangs.

---

## 8. USB EXPORT — HOW IT WORKS UNDER THE HOOD

Triggered only from the on-screen **EXPORT** button (not the hardware
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
   -> requires the polkit rule (99-udisks2-melody.rules) to be installed
      for this to be password-free — without it, this call can hang
      waiting on an auth prompt nothing can answer (a real failure mode
      observed in testing before the rule was added)
   -> parses the mount point out of udisksctl's stdout/stderr with a
      regex, then _clean_udisks_path() strips old-style Unix quote
      punctuation that appears in some message formats (e.g. an
      "already mounted" error) but not others — this WAS a real bug that
      corrupted a mount path with a stray backtick/quote, now fixed

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
| `display_width` / `display_height` | 1024 / 600 | App window size (also the fullscreen resolution if `fullscreen` is true) |
| `weight_strip_height` | 50 | Bottom strip height (px), on screen AND baked into photos |
| `control_bar_height` | 50 | Top control-bar height (px) |
| `fullscreen` | false | Windowed by default — see §12 |
| `trigger_weight_g` | 500 | Autocapture fires above this (grams) |
| `stabilise_seconds` | 5.0 | Countdown before an autocapture photo (manual capture ignores this) |
| `cell_labels` | ["C1","C2","C3","C4"] | Shown on screen, in photos, and in the calibration table |
| `uart_port` | "/dev/serial0" | Pi's serial device listening to the ESP32 |
| `uart_baud` | 115200 | Must match `ESP32_Code.cpp`'s `BAUD_RATE` |
| `button_gpio` | 17 | BCM pin for the hardware capture button |
| `offsets` | [0,0,0,0] | Pi-side tare offsets (raw counts), per channel |
| `cal_factors` | [1.0,1.0,1.0,1.0] | Raw-counts-per-KG, per channel (calibration-time unit only — see §5) |
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
- **Single ESP32, single UART port, no reconnection logic beyond pyserial's
  normal retry** — if the ESP32 crashes or the wire comes loose, the Pi
  shows "ESP32 LINK LOST" and holds the last known weight values rather
  than guessing. A wedged ESP32 needs a power-cycle. This is a smaller
  risk than in earlier revisions since the ESP32 firmware never blocks
  its own loop for any reason now.
- **No database, CSV log, or capture history beyond the JPEGs themselves**
  — by explicit request, keeping this to "just photos with weight baked
  in."
- **USB export is copy-only, single-drive** — no multi-drive selection UI,
  no incremental/delta export (always zips everything currently in
  `photos/`).
- **Same-process web+display app** (§7) — simplest to build and to keep
  the UART port single-owner, at the cost of the two being unable to fail
  fully independently of each other.
- **Manual capture always overrides state** — pressing CAPTURE (button or
  on-screen) mid-countdown or mid-cooldown immediately takes a photo and
  resets to "cooldown," rather than queuing or blocking.
- **Photo storage cap is a blunt oldest-first deletion**, not
  size-per-day or any smarter retention policy.
- **On-device ESP32 averaging is a fixed 4-sample window**, not
  configurable from the Pi — if you need to change it, it's a firmware
  constant (`SMOOTH_N` in `ESP32_Code.cpp`), not a `config.json` setting.

---

## 11. WHERE TO LOOK WHEN SOMETHING BREAKS

Rough triage order, cheapest checks first:

1. **`scale.log`** (`/home/melody/smartscale/scale.log`) — almost every
   component logs here (UART errors, tare/calibrate results, camera
   errors, storage cap deletions, USB export progress).
2. **"ESP32 LINK LOST" on the weight strip** -> it's a UART/wiring problem,
   not a Pi software problem, and no longer a "the ESP32 is busy" false
   alarm (it never pauses itself in this revision). Check the ESP32 is
   powered, check TX/RX aren't swapped, check the serial console was
   actually disabled (`raspi-config`).
3. **Weight reads `no_data` per-channel but the link is fine** -> that
   specific HX711/load-cell wiring to the ESP32, not the Pi. Flash-test the
   ESP32 alone with a USB-TTL adapter and watch its Serial Monitor.
4. **Weight readings look wildly wrong, not just jittery** -> watch the
   ESP32's own Serial Monitor directly (bypassing the Pi entirely). If
   it's already wrong there with an empty, stationary platform, it's a
   wiring/power/ground issue on that specific HX711, not anything fixable
   in Pi-side software.
5. **Web UI unreachable** -> `systemctl status smartscale`, then
   `journalctl -u smartscale -n 50`.
6. **Login page / any page 500s with `TemplateNotFound`** -> a file is
   missing on disk, not a code bug — see the `index.html` entry in §6.
7. **USB export fails** -> if it's asking for a password or hanging, you
   need the polkit rule (§8, `SETUP.md` Part 6.1). Otherwise re-run the
   steps manually over SSH (`lsblk`, `udisksctl mount -b ...`) to see the
   real error the on-screen status text is summarizing.

See also the Troubleshooting table in `SETUP.md`, which maps specific
symptoms to specific fixes.

---

## 12. DISPLAY UNITS, FILTERING, AND WINDOW MODE

### Grams, always, no decimals
Every human-facing number — screen, photo caption, web page — is a whole
number of grams. Internally, `ScaleManager` still computes an intermediate
"kg" value from `(raw - offset) / cal_factor`, purely because
`cal_factors` is calibrated as "raw counts per kilogram" (a convenient
calibration-time unit — you calibrate with a 1kg reference weight, not a
1-gram one). That kg number is multiplied by 1000 and rounded the moment
it's about to be shown or stored anywhere; nothing outside
`ScaleManager._proc_loop` ever sees a kg value. `trigger_weight_g` in
`config.json` is genuinely grams.

### Filtering — now a three-stage chain, tuned for the current data rate
1. **On-device averaging (ESP32)** — each channel is already the mean of
   its last 4 raw samples before it ever reaches the Pi (`SMOOTH_N` in
   `ESP32_Code.cpp`). This is new in revision 3; the Pi used to receive
   pure single-sample ADC noise and had to do all the smoothing itself.
2. **Trimmed mean (Pi)** over a 12-sample window (`FILTER_WINDOW`) — drops
   the extreme 15% high/low before averaging.
3. **EMA smoothing (Pi)** at `EMA_ALPHA = 0.15` — faster than revision 2's
   0.06, because the incoming data is already cleaner (stage 1), so less
   aggressive smoothing is needed to get a calm result, and a faster EMA
   means less lag between "you place the object" and "the number settles."
4. **Display hysteresis (Pi)** (`HYSTERESIS_G = 2`) — the shown integer
   only moves if the smoothed value has drifted at least 2g from what's
   currently displayed. This is what stops the last digit from flickering
   when the true weight sits right on a rounding boundary.

If readings still feel too slow/fast for your load cells, these constants
(`FILTER_WINDOW`, `EMA_ALPHA`, `HYSTERESIS_G` near the top of
`scale_app.py`, and `SMOOTH_N`/`SEND_INTERVAL_MS` in `ESP32_Code.cpp`) are
the only places to tune.

**If numbers still look implausible after this tuning** (not just
jittery, but actually wrong — large random jumps), that's a hardware
diagnosis, not a filtering one: watch the ESP32's own Serial Monitor
directly and independently of the Pi. If the raw `D,...` numbers
themselves look wrong with the platform stationary and empty, the problem
is in the HX711<->ESP32 wiring or power, not anywhere in this code.

### Windowed mode by default, plus on-screen QUIT/SHUTDOWN
`fullscreen` defaults to `false` in `config.json` — the app opens as a
normal window at exactly `display_width`x`display_height`. This sidesteps
SDL-fullscreen/touchscreen-resolution mismatches that can shift the
mapping between touch coordinates and window coordinates (taps landing in
the wrong place) and letterboxing artifacts. Flip it back on in the web
UI's Display section if your setup doesn't have this problem and you
prefer true fullscreen.

The camera feed is drawn aspect-correct (`blit_camera_frame()`), not
stretched to fill a mismatched rectangle — it scales to fit the area
between the control bar and weight strip while preserving the camera's
native aspect ratio, letterboxing with black bars rather than distorting
the image.

The control bar has six buttons: TARE / CAPTURE / AUTO / EXPORT / QUIT /
SHUTDOWN — added because fullscreen kiosk-style touchscreens often have no
window manager chrome (no title bar close button), so there needs to be an
in-app way out regardless of window mode. SHUTDOWN requires two taps
within 3 seconds (button reads "CONFIRM?" after the first tap) since it
powers off the whole Pi, using the same passwordless `sudo shutdown`
sudoers rule already set up for the web UI's Shutdown button.
