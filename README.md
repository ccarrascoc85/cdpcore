# CDPcore

Dedicated CD playback appliance for Raspberry Pi.

*CDPcore is a CD player with network-based control, designed for bit-perfect playback and full control over the audio pipeline.*

Audio flows directly from the CD drive to the DAC:

CD drive -> **mpv** -> ALSA -> USB DAC (USB Audio Class compliant DAC)

No network transport, no resampling, no intermediate layers or audio middleware.

A web UI (served by the backend) provides full playback control from any browser on the local network.

A Roon extension pauses the active zone when the CD starts playing and can
optionally resume it when playback stops or the disc is ejected, all configurable
per-session in the Roon Settings -> Extensions panel.

```
CD Drive (/dev/cdrom)
   |
   v mpv (cdda:///dev/cdrom)
      IPC: /var/cache/cd-player/mpv.sock
   |
   v ALSA  hw:N,0 (auto-detected)
      USB DAC (USB Audio Class compliant)

Python FastAPI  (0.0.0.0:8000)
   |- Serves web UI  (GET /)
   |- REST API  (playback commands + status)
   |- WebSocket /ws  (pushes state to all connected browsers)
   |- _disc_monitor  (asyncio task - polls disc, stall watchdog)
   `- /cd/inserted · /cd/ejected  (optional udev webhooks)

libdiscid  -->  MusicBrainz API  -->  Cover Art Archive
   |               | (404)             |
   TOC        GnuDB (CDDB)        iTunes API
              album/artist/tracks  cover JPEG
                                  (cached in /var/cache/cd-player/)

Browser  <--  WebSocket ws://cdpcore.local:8000/ws  (state push, ~1 s)
   `- REST POST /play · /pause · /stop · /next · /prev · /eject

CDPcore Roon Extension <--> Roon Core (mDNS)
   `- polls backend /status every 1 s -> pauses / resumes Roon zone

avahi-daemon  ->  cdpcore.local  (mDNS hostname + HTTP service announcement)
```

---

## Features

- Bit-perfect CD playback (no resampling, no DSP)
- Direct CDDA -> ALSA -> USB DAC pipeline
- Silent 1x drive operation (low noise)
- Real-time Web UI (WebSocket)
- Automatic metadata + cover art lookup
- Roon integration (auto pause/resume)
- Automatic DAC detection on plug/unplug (udev + reconciliation, no manual rescan)
- First-boot setup for admin posture (PIN-protected or explicit LAN-Trust)
- Appliance-management pages separated from the listening surface
- Zero audio middleware (no PulseAudio / PipeWire)

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| CD playback | **mpv** (`cdda://` input, IPC JSON socket control) |
| Playback control | mpv IPC socket (`/var/cache/cd-player/mpv.sock`) |
| Backend | **Python 3**, **FastAPI**, **uvicorn** (asyncio) |
| Disc ID / TOC | **libdiscid** (`python-discid`), cdparanoia fallback |
| Metadata (primary) | **MusicBrainz** (`musicbrainzngs`) |
| Metadata (fallback) | **GnuDB** (CDDB protocol, `httpx`) |
| Cover art (primary) | **Cover Art Archive** (via MusicBrainz release ID) |
| Cover art (fallback) | **iTunes Search API** (free, no key required) |
| ALSA enumeration | `/proc/asound/cards` + `aplay -l` fallback |
| Real-time UI updates | **WebSocket** (`/ws`, FastAPI native, server-push) |
| Service discovery | **avahi-daemon** (mDNS, `cdpcore.local`) |
| Web UI | Single-file HTML/CSS/JS served at `/` (no framework) |
| Extension | **Node.js**, `node-roon-api` |

---

## How It Works

### Disc detection

A background asyncio task (`_disc_monitor`) polls the drive via `libdiscid`
every **5 s** while the drive is idle, and every **15 s** once a disc is
confirmed loaded (reducing unnecessary disc head seeks). Polling via
`discid.read()` is suspended while mpv holds the drive open, since concurrent
reads would compete with mpv for the device. Instead, a **stall watchdog**
monitors `time-pos` advancement via the IPC socket every few seconds - if
playback stalls (disc ejected, drive unreadable), it detects the condition
within ~8 s, kills mpv, and resets state immediately.

This handles four scenarios without relying on udev:

- **Service started with disc already in drive** - detected within 7 s of startup
- **Disc inserted while running** - detected within 5 s of insertion
- **Disc swapped** - old playback stopped, new metadata loaded automatically
- **Disc ejected during playback** - ioctl detects removal immediately; state resets and music stops in under 1 s

The disc is read **once per load** via `read_disc_full()`, which returns the
MusicBrainz disc ID, the full TOC, and the FreeDB/CDDB fingerprint in a single
pass - avoiding multiple physical disc reads during metadata loading.

The REST endpoints `/cd/inserted` and `/cd/ejected` still accept udev webhook
calls but are secondary to the polling mechanism.

### Silent play - 1x read speed

The drive runs at **1x speed** during the entire session, i.e. real-time CDDA
playback speed. This is enforced by **udev** - `cd-setspeed.sh` calls
`eject -x 1` on every disc insertion, setting the drive speed before the
backend even detects the disc. mpv does not override the drive speed; it
inherits the 1x setting from udev.

At 1x, the drive motor runs at its lowest RPM - eliminating the audible
spin-up whine and high-frequency bearing noise typical of 24-48x ripping
speeds. Audio data is read in real time (a standard CD plays at exactly 1x), so
there is no intentional high-speed ripping phase. This reduces noise,
vibration, and speed-related read instability.

mpv's demuxer buffer is capped at **512 KiB** (~3 s of audio). Combined with
the ALSA and decoder pipeline, the total in-flight buffer is roughly 11 s -
enough to absorb the drive's natural read/idle cycle without audio dropouts,
while keeping the tail after ejection short.

Track-to-track seeks cause a brief spin-up as the laser repositions, but the
drive settles back to its operating speed within a few seconds.

### Metadata lookup

When a disc is detected, the following cascade runs until metadata is found:

1. `libdiscid` reads the TOC once - producing the MusicBrainz disc ID, track
   durations (in sectors), and the CDDB fingerprint
2. **MusicBrainz** is queried first for album, artist, track list, and release ID
3. If MusicBrainz returns a release ID, cover art is fetched from **Cover Art Archive**
4. If MusicBrainz has no match, **GnuDB** (free CDDB server) is tried using the
   FreeDB fingerprint - it returns album, artist, and track titles (no durations)
5. Track durations for GnuDB results are filled from the TOC (sector-accurate)
6. If GnuDB resolves the disc, cover art is fetched from the **iTunes Search API**
   (free, no API key, returns up to 1000x1000 px JPEG)
7. All results are cached in `/var/cache/cd-player/` - subsequent insertions of
   the same disc are instant

### Playback (mpv IPC)

mpv is started once per disc (`cdda:///dev/cdrom`) in paused mode. Track navigation
uses the `chapter` property (CDDA tracks map to mpv chapters, 0-indexed).
Elapsed time is read from `time-pos` (disc-absolute seconds) and converted to
track-relative using the cached chapter-start list.

**Time position fallback hierarchy:**
1. **Primary:** Chapter-list cached from mpv (actual disc TOC sectors) - most accurate, available after ~8 s
2. **Secondary:** TOC-based cumulative track durations - available immediately, <1 s error
3. **Tertiary:** Monotonic timer - used when IPC is unavailable; frozen during mpv spin-up

**Startup sequence for first play:**
1. mpv process starts (`--pause` flag, whole disc as `cdda:///dev/cdrom`)
2. Backend waits for IPC socket to appear (~1-2 s)
3. Backend polls until mpv has read the CDDA TOC (`chapter` property non-null, up to 60 s)
4. If a specific track was requested, `set_property chapter N` is sent before unpausing
5. `buffering=true` is set while mpv spins up; the VFD displays **STARTING**
6. Chapter-list is cached in background (up to 10 retries over 10 s) for precise elapsed time

**Track seek correctness:**
The monitor thread only fires `_on_track_end` for **sequential** chapter advances
(`new == prev + 1`). User-initiated seeks jump arbitrary chapters and are handled
exclusively by `play()` updating `_current_track` before sending the IPC command -
preventing any monitor event from overwriting state during a seek.

### State machine

```
IDLE
 |  disc detected by _disc_monitor
 v
LOADED  (loading=true while metadata lookup runs -> VFD: READING)
 |  POST /play or /play/{n}
 v
PLAYING  (buffering=true while mpv spins up -> VFD: STARTING)
 |  POST /pause
 v
PAUSED
 |  POST /stop or disc removed
 v
IDLE
```

### Web UI

The CDPcore interface is served at `http://cdpcore.local:8000/` and provides:
- VFD-style amber display (track, elapsed, remaining, track/total)
- Cover art with placeholder when unavailable
- Album / artist / track title
- Progress bar with per-track tick marks
- Transport controls (Prev, Stop, Play/Pause, Next)
- Collapsible track list with click-to-play (expanded by default)
- Light / dark mode toggle (persisted in localStorage)
- Responsive layout for mobile (<= 576 px breakpoint)
- Gear icon linking to the system management page
- Installable to the home screen as a standalone app (Samsung Internet on Android, Safari on iOS) via Add to Home Screen

The UI connects to `/ws` via WebSocket on load. The server pushes a state snapshot every second (for elapsed time) and immediately on any state change (disc insert, track advance, play/pause/stop, eject). If the WebSocket drops, the UI falls back to polling `/status` every 2 s and reconnects automatically.

### System management

A separate administrative surface at `http://cdpcore.local:8000/system` provides:

- **System** - Pi model, OS version, uptime
- **CPU** - temperature (via `vcgencmd`), load average (1 / 5 / 15 min) with visual bar
- **Memory & Storage** - RAM and disk usage with bars (used / total)
- **Network** - all interfaces with link state, IP, MAC, and link speed
- **CD Drive** - model and speed-control support (1x ioctl)
- **Services** - cdpcore-backend and cdpcore-extension status and PID
- **Roon Bridge / Extension** - service status, PID, settings, and extension actions
- **Admin Settings** - admin PIN change and auth-mode change (`PIN` or explicit `LAN-Trust`)
- **Audio** - current audio device state (USB DAC / Multiple DACs / No USB DAC / Audio device missing) with the selected DAC name when applicable, updated automatically
- **Updates** - operator-initiated update checks and release updates from the latest tagged GitHub Release
- **Backend Logs** - last 150 lines of the backend service journal, with a refresh button
- **Actions** - restart backend, restart all, reboot Pi, power off Pi

Actions that terminate the backend process (restart, reboot, poweroff) execute with a short delay so the HTTP response is delivered first. All destructive actions require confirmation. The `cdplayer` service user is granted minimal sudo rights for these operations via `/etc/sudoers.d/cdpcore`.

Updates are checked only from the system management page. Pressing **Update**
starts a root-owned one-shot executor that downloads the latest tagged GitHub
Release source tarball, updates `/opt/cdpcore/backend` and
`/opt/cdpcore/extension`, installs changed dependencies, and restarts CDPcore.
Operator state lives outside `/opt/cdpcore` and is not overwritten by this flow.

> **Roon Bridge coexistence:** CDPcore is designed to run alongside Roon Bridge on the same appliance. The system pages manage the `roonbridge.service` unit and extension behavior; the Roon extension pauses the active zone when CD playback starts and can resume it when playback stops - both systems share the same USB DAC without conflict.

### Admin PIN gate

Everything under the appliance-management surface (`/system`, `/system/network`, `/system/roon`, `/system/admin`, and their data/mutation endpoints) is protected by a 6-digit admin PIN in the default posture. Playback and status endpoints stay open on the LAN so normal listening is unaffected.

- **First-time setup.** Fresh installs open with a welcome flow at `/setup`. The operator must choose either PIN-protected admin mode or explicit LAN-Trust before the appliance is considered configured.
- **Storage.** Admin-auth state lives in `/home/cdplayer/.config/cdpcore-backend/`:
  - `admin.pin` - PBKDF2-SHA256 PIN hash material
  - `session.key` - admin-session signing key
  - `auth.conf` - persisted auth-mode override
- **Admin mode.** `POST /admin/unlock` with the PIN sets a signed `HttpOnly`, `SameSite=Strict` session cookie valid for 15 minutes. When it expires, the UI re-prompts.
- **Brute-force protection.** `/admin/unlock` enforces a per-IP failure tracker (in-process). The first three failures only carry the baseline 200 ms delay; after that the imposed delay grows exponentially up to 30 s. A successful unlock or 10 minutes of inactivity resets the counter for that IP.
- **Cross-origin defense.** `/admin/unlock` and `/admin/setup` reject browser POSTs whose `Origin` (or `Referer`, when `Origin` is absent) does not match the request's `Host`. CLI/script callers without those headers are still allowed.
- **Defensive headers.** Every response carries `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and a `Content-Security-Policy` that blocks framing and locks default sources to the appliance origin (cover-art `img-src` allows third-party HTTPS).
- **Privileged surface.** `system/cdpcore-sudoers` only authorises specific verb tails (`systemctl restart cdpcore-backend`, `systemctl restart cdpcore-extension`, `systemctl start cdpcore-update`, `nmcli connection modify *`, `nmcli connection up *`, `nmcli connection delete *`, `nmcli radio wifi on/off`, `nmcli device wifi rescan`, `nmcli device wifi connect *`, `hostnamectl set-hostname *`). Backend validators in `backend/network.py` further restrict argument shapes (IPv4, RFC 1123 hostname, NetworkManager connection-name whitelist) so the wildcards cannot be abused via API input.
- **Change PIN.** On the appliance:
  ```bash
  sudo -u cdplayer /opt/cdpcore/venv/bin/python -m auth rotate
  # (run from /opt/cdpcore/backend)
  ```
  Or set a chosen PIN explicitly:
  ```bash
  sudo -u cdplayer /opt/cdpcore/venv/bin/python -m auth set 123456
  ```
  The same can be done from the Admin Settings page via `/system/admin`.
- **Mode changes.** The active admin posture is persisted in `auth.conf`. `CDPCORE_ADMIN_AUTH` remains the bootstrap/default input, but once a mode is saved the persisted value is authoritative at runtime.
- **Opt out (private installs).** LAN-Trust / auth-off remains available for private installs, either during `/setup` or later from the Admin Settings page. Community-style deployments should keep the default protected posture.

---

## Hardware

CDPcore distinguishes between hardware that has been observed working under
real test runs and hardware that the design is targeted at. Claims about a
particular board are only made where there is repeated, real-hardware
evidence.

### Hardware posture

| Tier | Board | Status |
|------|-------|--------|
| Recommended reference | **Raspberry Pi 4** | Recommended for normal users. Comfortable margin for CDDA + metadata + Roon coexistence. |
| Supported floor | **Raspberry Pi 3 Model B** | Supported for the validated configuration: USB optical drive + USB DAC + local LAN operation. |
| Untested | Raspberry Pi 5 | No formal test evidence yet. |

### Validated configurations

| Date       | Board                  | DAC          | Drive        | Result |
|------------|------------------------|--------------|--------------|--------|
| 2026-04-22 | Raspberry Pi 3 Model B | Topping E50  | LG GP50NB40  | Pass (one transient buffer underrun, auto-recovered) |
| 2026-04-30 | Raspberry Pi 3 Model B | Topping E50  | LG GP50NB40  | Pass (canonical) |

### Reference setup

| Component | Value |
|-----------|-------|
| Board | Raspberry Pi 4 |
| OS | Raspberry Pi OS Bookworm (64-bit) |
| DAC | Any USB Audio Class compliant DAC (USB audio, auto-detected) |
| CD Drive | USB optical drive (`/dev/cdrom`) |
| Install path | `/opt/cdpcore/` |
| Cache | `/var/cache/cd-player/` |
| IPC socket | `/var/cache/cd-player/mpv.sock` |

---

## Installation

### Quick install (recommended)

Clone the repo and run the installer as root:

```bash
git clone https://github.com/ccarrascoc85/cdpcore.git
cd cdpcore
sudo bash install.sh
```

The script handles everything: apt packages, Node.js 20, service user, Python
venv, npm deps, cache directory, admin-auth config directory, udev rules,
avahi mDNS service, and systemd services.

After the services come up, open the appliance URL in a browser and complete
the first-time setup at `/setup`.

On Raspberry Pi, [disable PipeWire first](#step-a---disable-pipewire--pulseaudio)
before running the installer.

---

### Manual install

#### 1. System dependencies

```bash
sudo apt update
sudo apt install -y \
    python3 python3-pip python3-venv \
    mpv \
    libdiscid0 libdiscid-dev \
    cdparanoia \
    alsa-utils \
    libasound2-dev \
    curl eject usbutils \
    avahi-daemon
```

#### 2. Node.js 20 (via NodeSource apt repo, GPG-pinned)

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
sudo chmod 0644 /etc/apt/keyrings/nodesource.gpg
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
    | sudo tee /etc/apt/sources.list.d/nodesource.list
sudo apt update
sudo apt install -y nodejs
```

This avoids piping a remote shell script into root; the apt repo is signed
by the explicit keyring and remains auditable in `/etc/apt/sources.list.d/`.

#### 3. Service user

```bash
sudo useradd -r -m -d /home/cdplayer -s /bin/bash cdplayer
sudo usermod -aG audio,cdrom cdplayer
```

#### 4. Install project

```bash
sudo mkdir -p /opt/cdpcore
sudo cp -r backend extension system /opt/cdpcore/
sudo chown -R cdplayer:cdplayer /opt/cdpcore
```

#### 5. Python virtualenv + deps

```bash
sudo -u cdplayer python3 -m venv /opt/cdpcore/venv
sudo -u cdplayer /opt/cdpcore/venv/bin/pip install \
    -r /opt/cdpcore/backend/requirements.txt
```

#### 6. Node.js deps

```bash
sudo -u cdplayer npm install --prefix /opt/cdpcore/extension
```

#### 7. Cache directory

```bash
sudo mkdir -p /var/cache/cd-player
sudo chown cdplayer:cdplayer /var/cache/cd-player
```

#### 8. Admin-auth config directory

```bash
sudo install -d -m 0700 -o cdplayer -g cdplayer /home/cdplayer/.config/cdpcore-backend
```

#### 9. Sudo permissions (system management)

```bash
sudo install -m 440 /opt/cdpcore/system/cdpcore-sudoers /etc/sudoers.d/cdpcore
```

This grants `cdplayer` the ability to restart services, reboot/poweroff, and
start the bounded `cdpcore-update` one-shot via the system management page.

#### 10. udev rules

```bash
sudo cp /opt/cdpcore/system/99-cdrom.rules         /etc/udev/rules.d/
sudo cp /opt/cdpcore/system/99-audio.rules         /etc/udev/rules.d/
sudo cp /opt/cdpcore/system/cd-inserted.sh         /usr/local/bin/
sudo cp /opt/cdpcore/system/cd-ejected.sh          /usr/local/bin/
sudo cp /opt/cdpcore/system/cd-setspeed.sh         /usr/local/bin/
sudo cp /opt/cdpcore/system/audio-device-change.sh /usr/local/bin/
sudo cp /opt/cdpcore/system/cdpcore-update         /usr/local/bin/
sudo chmod +x /usr/local/bin/cd-inserted.sh \
              /usr/local/bin/cd-ejected.sh \
              /usr/local/bin/cd-setspeed.sh \
              /usr/local/bin/audio-device-change.sh \
              /usr/local/bin/cdpcore-update
sudo udevadm control --reload-rules
```

`99-cdrom.rules` triggers disc-insertion and speed-setting scripts. `99-audio.rules` notifies the backend whenever a sound card is added or removed (via `systemd-run --no-block --collect`), so DAC changes are detected without operator action. A 30 s reconciliation loop in the backend covers any missed events.

#### 11. mDNS service (avahi)

```bash
sudo cp /opt/cdpcore/system/cdpcore.avahi /etc/avahi/services/cdpcore.service
sudo systemctl reload-or-restart avahi-daemon
```

After this, the UI is reachable at `http://cdpcore.local:8000/` from any device on the local network (no need to know the IP address).

#### 12. systemd services

```bash
sudo cp /opt/cdpcore/system/cdpcore-backend.service   /etc/systemd/system/
sudo cp /opt/cdpcore/system/cdpcore-extension.service /etc/systemd/system/
sudo cp /opt/cdpcore/system/cdpcore-update.service    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cdpcore-backend cdpcore-extension
```

#### 13. First-time setup

Open the appliance URL in a browser:

```text
http://cdpcore.local:8000/setup
```

Choose one of:
- **PIN-protected admin mode** - recommended/default posture
- **LAN-Trust** - explicit private-install mode without the admin gate

---

## Raspberry Pi

### Step A - Disable PipeWire / PulseAudio

Raspberry Pi OS Bookworm runs PipeWire + WirePlumber as a user-session service.
This **will** conflict with mpv's exclusive ALSA access. Disable it before installing:

```bash
systemctl --user stop    pipewire pipewire-pulse wireplumber
systemctl --user disable pipewire pipewire-pulse wireplumber
systemctl --user mask    pipewire pipewire-pulse wireplumber

fuser /dev/snd/*   # expected: empty output
```

### Step B - CD drive device

```bash
ls -la /dev/sr* /dev/cdrom
```

If `/dev/cdrom` does not exist:

```bash
sudo ln -s /dev/sr0 /dev/cdrom
# or set in the service unit:
Environment="CDROM_DEVICE=/dev/sr0"
```

### Step C - Powered USB hub strongly recommended

For reliable operation, use a powered USB hub when connecting both a USB
DAC and a USB optical drive. Some combinations may work directly from the
Pi, but optical drives often exceed what the onboard USB ports can supply
reliably.

---

## Extension authorization

The first time the extension connects you will be prompted to authorize it:

1. Open Roon -> Settings -> Extensions
2. Find **"CDPcore"** in the pending list
3. Click **Enable**

The pairing token is stored in `/home/cdplayer/.config/cdpcore-extension/config.json`
and persists across service restarts - authorization is required only once.

Configure the extension behaviour in Settings -> Extensions -> CDPcore -> Settings.

| Setting | Default | Description |
|---------|---------|-------------|
| Roon Zone | - | Name of the zone connected to your USB DAC |
| ALSA device override | _(auto)_ | Leave blank for auto-detection |
| MusicBrainz lookup | On | Enable/disable MusicBrainz metadata |
| Pause zone when disc is inserted | **Off** | Pause the zone as soon as a disc is detected |
| Pause zone when CD plays | **On** | Pause the zone when playback starts |
| Resume zone after CD stops | **Off** | Resume the zone when Stop is pressed |
| Resume zone after disc is ejected | **Off** | Resume the zone when the disc is ejected |

Settings are persisted at `/home/cdplayer/.config/cdpcore-extension/settings.json`.

---

## Verification

```bash
# Health check
curl http://localhost:8000/health
# Expected: {"status":"ok","version":"1.0.1"}

# Check whether first-time setup is pending
curl http://localhost:8000/admin/setup_required

# List ALSA devices (DAC should be auto_selected: true)
curl http://localhost:8000/devices

# Insert a CD, wait ~5 s, check state
curl http://localhost:8000/status | python3 -m json.tool

# Service logs
sudo journalctl -u cdpcore-backend  -f
sudo journalctl -u cdpcore-extension -f
```

---

## Integrations

CDPcore exposes a REST API and WebSocket interface, enabling integration with external systems (e.g. Home Assistant, Node-RED, or custom automation).

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Player web UI |
| WS | `/ws` | WebSocket - server pushes state JSON on every change |
| GET | `/status` | Current playback state (REST fallback) |
| GET | `/tracks` | Track list for loaded disc |
| POST | `/play` | Play from track 1 |
| POST | `/play/{n}` | Play track number n |
| POST | `/pause` | Toggle pause/resume |
| POST | `/stop` | Stop playback |
| POST | `/next` | Next track |
| POST | `/prev` | Previous track (< 5 s -> restart current; >= 5 s -> previous track) |
| POST | `/eject` | Stop and eject disc |
| GET | `/cover` | Cover art image (JPEG, cached locally) |
| GET | `/devices` | List ALSA devices with auto-selection info |
| POST | `/devices/rescan` | Re-enumerate ALSA devices (diagnostic; runtime detection is automatic) |
| POST | `/devices/select?device_id=hw:2,0` | Select ALSA output device |
| POST | `/audio/device-change` | udev webhook - sound card add/remove (called by `audio-device-change.sh`) |
| POST | `/cd/inserted` | udev webhook - disc inserted |
| POST | `/cd/ejected` | udev webhook - disc ejected |
| GET | `/health` | Health check |
| GET | `/system` | System management page (HTML shell - open; its data endpoints are gated) |
| GET | `/system/info` | System metrics JSON (CPU, memory, disk, network, services) - admin-gated |
| GET | `/system/logs` | Last N lines of the backend service journal (`?n=150`) - admin-gated |
| POST | `/system/action` | Execute system action (`restart_backend`, `restart_extension`, `restart_all`, `reboot`, `poweroff`, `roon_bridge_start`, `roon_bridge_stop`, `roon_bridge_restart`, `roon_bridge_enable`, `roon_bridge_disable`) - admin-gated |
| GET | `/system/update/check` | Check the latest tagged GitHub Release - admin-gated |
| GET | `/system/update/status` | Read updater phase from local appliance state - admin-gated |
| POST | `/system/update/apply` | Resolve latest release server-side and start the updater one-shot - admin-gated |
| GET | `/system/network`, `/system/roon`, `/system/admin` | Admin config pages (HTML shells - open; their data endpoints are gated) |
| POST | `/system/network/*`, `/system/roon/*` | Network / Roon config writes - admin-gated |
| POST | `/system/admin/set_pin` | Change the admin PIN - admin-gated |
| POST | `/system/admin/set_mode` | Persist admin mode (`pin` or `off`) - admin-gated |
| GET | `/admin/setup_required` | Returns whether first-time setup is still pending |
| GET | `/setup` | First-time setup / welcome page |
| POST | `/admin/setup` | Persist initial admin posture during first-time setup |
| POST | `/admin/unlock` | Exchange PIN for an admin-mode session cookie (15 min TTL) |
| GET | `/admin/status` | Current admin-mode status (`mode`, `unlocked`, `expires_at`) |
| POST | `/admin/lock` | Clear the admin-mode cookie |

### `/status` response

```json
{
  "state":        "playing",
  "track_number": 3,
  "track_title":  "Somewhere Only We Know",
  "elapsed":      42,
  "duration":     231,
  "album":        "Hopes and Fears",
  "artist":       "Keane",
  "cover_url":    "/cover",
  "disc_id":      "UX1aUjV5EP768c8J7ZU3rqWQVSc-",
  "tracks_total": 11,
  "loading":      false,
  "buffering":    false,
  "alsa_device":  "hw:1,0",
  "audio_state":  "usb_single"
}
```

- `loading` - `true` while metadata is being fetched (VFD shows READING)
- `buffering` - `true` while mpv is spinning up before first audio output (VFD shows STARTING)
- `alsa_device` - currently selected ALSA device id used by the next playback start
- `audio_state` - one of `usb_single`, `usb_multiple`, `no_usb`, `device_missing`, `default`. `/play` and `/play/{n}` return HTTP 409 when the state is `device_missing`, `no_usb`, or `usb_multiple` with `alsa_device` no longer in the device list. The appliance requires a valid USB DAC for playback; there is no fallback to the Pi internal audio output

---

## Project Structure

```
cdpcore/
|-- backend/
|   |-- main.py              FastAPI app, REST + WebSocket endpoints, setup flow
|   |-- auth.py              Admin PIN lifecycle, auth mode, setup state, signed sessions
|   |-- cd_reader.py         libdiscid TOC reading, ALSA device enumeration
|   |-- metadata.py          MusicBrainz / GnuDB lookup + cover art caching
|   |-- network.py           Network configuration helpers
|   |-- player.py            mpv process lifecycle + IPC socket control
|   |-- state.py             In-memory state machine (IDLE/LOADED/PLAYING/PAUSED)
|   |-- system_info.py       System metrics (CPU, memory, disk, network, services)
|   |-- admin_gate.js        Frontend admin-session gate for appliance pages
|   |-- ui.html              Player web UI (served at /)
|   |-- system.html          System management page (served at /system)
|   |-- network_config.html  Network configuration page
|   |-- roon_config.html     Roon configuration page
|   |-- admin_config.html    Admin settings page
|   |-- welcome.html         First-time setup page
|   |-- favicon/             Browser icons and web manifest
|   `-- requirements.txt
|-- extension/
|   |-- app.js               Extension entry point, settings management
|   |-- transport.js         Zone pause/resume logic
|   |-- poller.js            Backend polling + state events
|   `-- package.json
|-- system/
|   |-- 99-cdrom.rules             udev rules (disc event webhooks + speed)
|   |-- 99-audio.rules             udev rules (sound card add/remove via systemd-run)
|   |-- cd-inserted.sh             udev trigger script
|   |-- cd-ejected.sh              udev trigger script
|   |-- cd-setspeed.sh             udev trigger script - forces drive to 1x
|   |-- audio-device-change.sh     udev trigger script - notifies backend on DAC change
|   |-- cdpcore-update             privileged release updater executor
|   |-- cdpcore.avahi              avahi mDNS service (HTTP on port 8000)
|   |-- cdpcore-sudoers            sudo rules for system management actions
|   |-- cdpcore-backend.service    systemd unit (uvicorn, 0.0.0.0:8000)
|   |-- cdpcore-extension.service  systemd unit (node app.js)
|   `-- cdpcore-update.service     one-shot updater unit
|-- install.sh
`-- README.md
```

---

## Troubleshooting

### Backend won't start

```bash
sudo journalctl -u cdpcore-backend --no-pager -n 50
sudo ls -la /var/cache/cd-player/
```

### First-time setup keeps appearing

```bash
curl http://localhost:8000/admin/setup_required
# Expected after setup: {"required":false}
```

If it remains `true`, verify that `/home/cdplayer/.config/cdpcore-backend/`
is writable by the service user and that either `admin.pin` or `auth.conf`
was persisted.

### mpv "Device busy"

```bash
fuser /dev/snd/*
# If something holds the device -> disable PipeWire (Step A)
sudo -u cdplayer mpv --no-video \
    --ao=alsa --audio-device=alsa/hw:1,0 cdda:///dev/cdrom
```

### No cover art

```bash
curl http://localhost:8000/status | python3 -m json.tool
# disc_id "unknown" -> libdiscid could not read the TOC -> check libdiscid0 install
# cover_url null -> disc not in Cover Art Archive or iTunes (uncommon)
ls /var/cache/cd-player/
```

### Tracks all show 0:00

GnuDB does not include track durations. Durations are filled from the disc TOC
automatically. If durations still show zero, the TOC read failed - check
`libdiscid0` is installed and `/dev/cdrom` is accessible.

### DAC not auto-detected

```bash
curl http://localhost:8000/devices
# The backend excludes bcm2835, vc4-hdmi, and HDA-Intel drivers automatically
# Check that /proc/asound/card<N>/usbid exists for your DAC

curl http://localhost:8000/status | python3 -m json.tool
# audio_state should be "usb_single" with a single USB DAC connected
# audio_state "usb_multiple" with no valid selection causes /play to return 409
# Use POST /devices/select?device_id=hw:N,0 to pick one explicitly

# Verify the udev path is wired up
sudo journalctl -u cdpcore-backend -f | grep "[audio]"
# Plug or unplug the DAC; you should see an "[audio] udev event" line within ~1 s.
# If nothing arrives, the 30 s reconciliation loop will still pick it up.
tail -n 20 /var/log/cd-player-udev.log
```

### Extension not appearing

```bash
sudo journalctl -u cdpcore-extension -f
# "ECONNREFUSED" -> backend is not running
# Not visible in app -> mDNS/UDP 9003 blocked -> sudo ss -tlnp | grep 9003
```

### Extension appears as new instance after restart

The Roon pairing token must survive restarts. Verify:

```bash
ls /home/cdplayer/.config/cdpcore-extension/
# Should contain: config.json  settings.json
```

If `config.json` is missing, the directory is not writable by the service user.
Check `ReadWritePaths` in `cdpcore-extension.service` includes `/home/cdplayer`.

| Issue | Cause | Fix |
|-------|-------|-----|
| Audio stutters | USB bus saturation (RPi 3) | Use RPi 4/5 + powered hub |
| `aplay` shows no USB DAC | PipeWire holds the device | Disable PipeWire (Step A) |
| `discid` returns empty | `/dev/cdrom` not found | Set `CDROM_DEVICE=/dev/sr0` |
| Extension not found | mDNS blocked | Allow UDP 9003 in firewall |
| VFD shows STARTING for a long time | Slow drive spin-up, weak USB power, or unreadable disc | Use a powered hub, try another disc, or inspect backend logs |
| UI lags after disc ejected during play | slow mpv stop blocking event loop | Update to latest - ejection is now detected immediately via ioctl |
| Disc not found in MusicBrainz | Different pressing / unknown disc | GnuDB fallback runs automatically |

---

## License

MIT
