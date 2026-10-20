"""
CD TOC reading using discid (libdiscid bindings).
Also handles ALSA device enumeration.
"""
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default CD-ROM device
CDROM_DEVICE = os.environ.get("CDROM_DEVICE", "/dev/cdrom")

# Drivers that are definitively non-USB — includes RPi SoC audio
# (bcm2835 headphone/HDMI, vc4 HDMI) and common x86 internal audio.
# These are never candidates for auto-selection even if usbid lookup fails.
_NON_USB_DRIVERS = frozenset({
    "bcm2835_headpho",  # RPi 3/4/5 onboard 3.5mm jack
    "bcm2835_alsa",     # older RPi kernels
    "vc4-hdmi",         # RPi vc4 HDMI audio (vc4-hdmi-0, vc4-hdmi-1)
    "HDA-Intel",        # x86 Intel HDA
    "HDA-ATI",          # AMD/ATI HDMI
    "HDA-NVidia",       # Nvidia HDMI
})


def disc_media_present(device: str = CDROM_DEVICE) -> bool:
    """
    Check whether a disc is physically present using CDROM_DRIVE_STATUS ioctl.

    Kernel return values:
      CDS_NO_INFO       = 0  (unknown)
      CDS_NO_DISC       = 1  (confirmed empty)
      CDS_TRAY_OPEN     = 2  (tray open)
      CDS_DRIVE_NOT_READY = 3  (ambiguous: spin-up OR ejection transition)
      CDS_DISC_OK       = 4  (disc ready)

    CDS_DRIVE_NOT_READY is ambiguous: it appears both during spin-up (disc present)
    and briefly during physical ejection (disc absent).  A second read after 300 ms
    resolves the ambiguity without meaningfully delaying ejection detection.
    """
    import fcntl, time as _time
    CDROM_DRIVE_STATUS  = 0x5326
    CDS_NO_DISC         = 1
    CDS_TRAY_OPEN       = 2
    CDS_DRIVE_NOT_READY = 3
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            result = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
            if result in (CDS_NO_DISC, CDS_TRAY_OPEN):
                return False            # definitively absent
            if result == CDS_DRIVE_NOT_READY:
                # Ambiguous — confirm with a second read after a brief pause.
                # Ejection: 300 ms later → TRAY_OPEN / NO_DISC → False (fast detect).
                # Spin-up:  300 ms later → NOT_READY / DISC_OK  → True (no false stop).
                _time.sleep(0.3)
                result = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
                return result not in (CDS_NO_DISC, CDS_TRAY_OPEN)
            return True                 # CDS_DISC_OK = 4 or CDS_NO_INFO = 0
        finally:
            os.close(fd)
    except Exception:
        return True  # assume present on error


def raw_drive_status(device: str = CDROM_DEVICE) -> int:
    """
    Return the raw CDROM_DRIVE_STATUS ioctl value, or 0 on error.

      1 = CDS_NO_DISC       (confirmed empty)
      2 = CDS_TRAY_OPEN     (tray open)
      3 = CDS_DRIVE_NOT_READY (spinning up — disc present but TOC not yet read)
      4 = CDS_DISC_OK       (disc ready for data access)
    """
    import fcntl
    CDROM_DRIVE_STATUS = 0x5326
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
        finally:
            os.close(fd)
    except Exception:
        return 0


# Last result of set_drive_speed — None until a disc has been inserted.
# True  = CDROM_SELECT_SPEED ioctl accepted by the kernel driver.
# False = ioctl failed (drive does not support speed selection).
speed_control_supported: Optional[bool] = None


def set_drive_speed(speed: int = 1, device: str = CDROM_DEVICE) -> bool:
    """
    Request the drive to run at `speed`× (1 = 1×, 0 = max).

    Uses CDROM_SELECT_SPEED ioctl — the same mechanism as `eject -x`.
    Returns True if the kernel accepted the command, False if the drive
    does not support speed selection.

    Note: a True return means the ioctl was acknowledged by the kernel driver,
    not necessarily that the drive firmware honoured it.  Drives that silently
    ignore the command will still return True.
    """
    import fcntl
    global speed_control_supported
    CDROM_SELECT_SPEED = 0x5322
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, CDROM_SELECT_SPEED, speed)
            speed_control_supported = True
            return True
        finally:
            os.close(fd)
    except Exception:
        speed_control_supported = False
        return False


# Measured CDDA read throughput of the current disc as a multiple of 1x
# (75 frames x 2352 bytes = 176,400 B/s). None until measured. Set at disc
# detection right after the speed request, so the operator sees what the drive
# actually does: some USB drives accept CDROM_SELECT_SPEED and ignore it (the
# reference HL-DT-ST GP30NB20 reads ~4.9x whatever speed is requested).
drive_speed_measured: Optional[float] = None
CDDA_1X_BYTES_PER_SEC = 75 * 2352
SPEED_1X_CONFIRMED_MAX = 1.5   # measured x-factor at or below this counts as real 1x


def _sg_read_cd(fd: int, lba: int, nframes: int, timeout_ms: int) -> None:
    """One MMC READ CD (CD-DA, user data only) through SG_IO.

    Single attempt, no kernel retries: on a bad sector the drive answers with
    a sense error after its own internal timeout (~7 s on the reference
    drive) and this raises instead of looping. Allowed for the `cdrom` group
    without root (READ CD is on the kernel's unprivileged whitelist), which is
    the same path mpv/libcdio use.
    """
    import ctypes, fcntl
    SG_IO = 0x2285
    SG_DXFER_FROM_DEV = -3

    class _SgIoHdr(ctypes.Structure):
        _fields_ = [
            ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
            ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
            ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
            ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
            ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
            ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
            ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
            ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
            ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
            ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
            ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
        ]

    cdb = (ctypes.c_ubyte * 12)(
        0xBE, 0x04,                                   # READ CD, expected sector type CD-DA
        (lba >> 24) & 0xFF, (lba >> 16) & 0xFF, (lba >> 8) & 0xFF, lba & 0xFF,
        0, (nframes >> 8) & 0xFF, nframes & 0xFF,
        0x10, 0, 0,                                   # user data only
    )
    buf = ctypes.create_string_buffer(2352 * nframes)
    sense = ctypes.create_string_buffer(32)
    hdr = _SgIoHdr()
    hdr.interface_id = ord("S")
    hdr.dxfer_direction = SG_DXFER_FROM_DEV
    hdr.cmd_len = 12
    hdr.mx_sb_len = 32
    hdr.dxfer_len = 2352 * nframes
    hdr.dxferp = ctypes.cast(buf, ctypes.c_void_p)
    hdr.cmdp = ctypes.cast(cdb, ctypes.c_void_p)
    hdr.sbp = ctypes.cast(sense, ctypes.c_void_p)
    hdr.timeout = timeout_ms
    fcntl.ioctl(fd, SG_IO, hdr)
    if hdr.status != 0 or hdr.host_status != 0 or hdr.driver_status != 0 or hdr.resid != 0:
        raise OSError(
            f"READ CD failed at LBA {lba}: status={hdr.status} host={hdr.host_status} "
            f"driver={hdr.driver_status} resid={hdr.resid}"
        )


def _max_frames_per_read(default: int = 45) -> int:
    """Largest READ CD transfer the block layer accepts for sr0, in frames.

    SG_IO rejects (EIO) any transfer above the queue's max_hw_sectors; for
    usb-storage that is 120 KiB by default, i.e. 52 frames, so 75-frame reads
    (a full second, 172 KB) never leave the host. Read the limit from sysfs;
    fall back to 45 frames when unavailable.
    """
    try:
        kb = int(Path("/sys/block/sr0/queue/max_hw_sectors_kb").read_text().strip())
        return max(1, min(75, (kb * 1024) // 2352))
    except Exception:
        return default


def measure_read_speed(device: str = CDROM_DEVICE, lba: int = 1500,
                       frames: Optional[int] = None, chunks: Optional[int] = None,
                       timeout_ms: int = 5000, _reader=None) -> Optional[float]:
    """Measure the drive's real CDDA read throughput and return it as an x-factor.

    Reads one warm-up run (absorbs spin-up and seek) and then `chunks` runs of
    `frames` audio frames each, timing only the latter. By default the run
    size is the largest transfer the device accepts (see
    `_max_frames_per_read`) and the runs cover ~3 s of audio: ~0.6 s at 4.9x,
    3 s at a true 1x. Runs only when the drive is idle (disc detection). Any
    read failure or timeout returns None rather than retrying: a bad sector
    must never turn this into a stall.

    Records the result in `drive_speed_measured` for /system/info.
    `_reader(lba, nframes)` is injectable for tests.
    """
    import time as _time
    global drive_speed_measured
    if frames is None:
        frames = _max_frames_per_read()
    if chunks is None:
        chunks = max(1, -(-225 // frames))         # ceil(3 s of audio / frames)
    fd = None
    try:
        if _reader is None:
            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)

            def _reader(start, n):
                _sg_read_cd(fd, start, n, timeout_ms)
        _reader(lba, frames)                       # warm-up: spin-up + seek, not timed
        t0 = _time.monotonic()
        for i in range(1, chunks + 1):
            _reader(lba + i * frames, frames)
        elapsed = _time.monotonic() - t0
        if elapsed <= 0:
            return None
        factor = (chunks * frames * 2352) / elapsed / CDDA_1X_BYTES_PER_SEC
        drive_speed_measured = round(factor, 1)
        return drive_speed_measured
    except Exception as e:
        logger.warning(f"Read-speed measurement failed: {e}")
        drive_speed_measured = None
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def read_disc_id() -> Optional[str]:
    """Read the MusicBrainz disc ID from the inserted CD (lightweight poll)."""
    try:
        import discid
        disc = discid.read(CDROM_DEVICE)
        logger.debug(f"Disc ID: {disc.id}")
        return disc.id
    except Exception:
        # No disc, tray open, or drive error — normal during polling
        return None


def read_disc_full() -> tuple:
    """
    Read the disc once and return (disc_id, toc, cddb_info).

    Consolidates the three separate discid.read() calls that would otherwise
    happen during metadata loading into a single physical disc read.

    Returns:
        disc_id   — MusicBrainz disc ID string, or None on failure
        toc       — list of (track_number, duration_seconds)
        cddb_info — dict with FreeDB fields, or None on failure
    """
    try:
        import discid
        disc = discid.read(CDROM_DEVICE)
        disc_id = disc.id
        toc = [(t.number, t.length // 75) for t in disc.tracks]
        cddb_info = {
            "freedb_id":   disc.freedb_id,
            "track_count": disc.last_track_num,
            "offsets":     [t.offset for t in disc.tracks],
            "seconds":     disc.sectors // 75,
        }
        logger.debug(f"Full disc read: id={disc_id}, {len(toc)} tracks")
        return disc_id, toc, cddb_info
    except Exception as e:
        logger.warning(f"discid full read failed: {e}")
        toc = _read_toc_cdparanoia()
        return None, toc, None


def read_toc() -> List[Tuple[int, int]]:
    """
    Return list of (track_number, duration_seconds) from CD TOC.
    Falls back to cdparanoia -Q if discid fails.
    """
    try:
        import discid
        disc = discid.read(CDROM_DEVICE)
        tracks = []
        for track in disc.tracks:
            # track.length is in sectors; 75 sectors/second for CD audio
            duration_sec = track.length // 75
            tracks.append((track.number, duration_sec))
        return tracks
    except Exception as e:
        logger.warning(f"discid read failed, trying cdparanoia: {e}")
        return _read_toc_cdparanoia()


def _read_toc_cdparanoia() -> List[Tuple[int, int]]:
    """Parse track list from cdparanoia -Q output."""
    try:
        result = subprocess.run(
            ["cdparanoia", "-Q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stderr  # cdparanoia prints TOC to stderr
        tracks = []
        for line in output.splitlines():
            # Example: "  1.   37027 [08:13.52]    0 [00:00.00]"
            m = re.match(r"\s+(\d+)\.\s+(\d+)\s+\[(\d+):(\d+)\.(\d+)\]", line)
            if m:
                track_num = int(m.group(1))
                minutes = int(m.group(3))
                seconds = int(m.group(4))
                duration = minutes * 60 + seconds
                tracks.append((track_num, duration))
        return tracks
    except Exception as e:
        logger.error(f"cdparanoia TOC read failed: {e}")
        return []


# ---------------------------------------------------------------------------
# ALSA device enumeration
# ---------------------------------------------------------------------------

def _parse_proc_asound_cards() -> List[dict]:
    """
    Parse /proc/asound/cards for a list of sound cards.
    Returns list of dicts with keys: index, name, longname, is_usb
    """
    cards = []
    cards_path = Path("/proc/asound/cards")
    if not cards_path.exists():
        return cards

    content = cards_path.read_text()
    # Each card entry looks like:
    #  0 [PCH            ]: HDA-Intel - HDA Intel PCH
    #                       HDA Intel PCH at 0x...
    #  1 [E50            ]: USB-Audio - Topping E50
    #                       Topping E50 at usb-...
    pattern = re.compile(
        r"^\s*(\d+)\s+\[(\S+)\s*\]:\s+(\S+)\s+-\s+(.+)$", re.MULTILINE
    )
    for m in pattern.finditer(content):
        index = int(m.group(1))
        short_name = m.group(2).strip()
        driver = m.group(3).strip()
        long_name = m.group(4).strip()

        # Only cards that can play audio are outputs. A USB webcam or a
        # microphone registers a capture-only card; counting it as a DAC would
        # put the appliance in "multiple DACs" for no reason.
        if not _card_has_playback(index):
            continue

        # Check USB by looking at the usb device symlink or driver name
        is_usb = _is_card_usb(index, driver)

        cards.append({
            "index": index,
            "short_name": short_name,
            "driver": driver,
            "long_name": long_name,
            "is_usb": is_usb,
        })
    return cards


def _card_has_playback(card_index: int) -> bool:
    """True unless the card's procfs directory is readable and lists no
    playback PCM (pcmNp). Unknown means playback-capable."""
    try:
        entries = os.listdir(f"/proc/asound/card{card_index}")
    except OSError:
        return True
    pcms = [e for e in entries if e.startswith("pcm")]
    if not pcms:
        return True
    return any(e.endswith("p") for e in pcms)


def _is_card_usb(card_index: int, driver: str) -> bool:
    """
    Determine if a sound card is USB-based.
    Checks driver name and /proc/asound/card<n>/usbid existence.
    Explicitly rejects known SoC/HDA drivers (RPi bcm2835, vc4-hdmi, etc.).
    """
    # Fast reject: known non-USB drivers (handles RPi SoC audio cleanly)
    driver_upper = driver.upper()
    for non_usb in _NON_USB_DRIVERS:
        if non_usb.upper() in driver_upper:
            return False

    if "USB" in driver_upper:
        return True

    # Check for usbid file which only exists for USB audio devices
    usbid_path = Path(f"/proc/asound/card{card_index}/usbid")
    return usbid_path.exists()


def enumerate_alsa_devices() -> List[dict]:
    """
    Enumerate ALSA playback devices and classify USB vs non-USB.
    Returns list of device dicts suitable for /devices endpoint.
    """
    cards = _parse_proc_asound_cards()
    if not cards:
        # Fallback: parse aplay -l
        cards = _parse_aplay_l()

    devices = []
    usb_devices = [c for c in cards if c["is_usb"]]

    for card in cards:
        device_id = f"hw:{card['index']},0"
        devices.append({
            "id": device_id,
            "card_id": card["short_name"],
            "name": card["long_name"],
            "is_usb": card["is_usb"],
            "auto_selected": False,
        })
        if card["is_usb"]:
            try:
                usb_id = Path(f"/proc/asound/card{card['index']}/usbid").read_text().strip()
                if usb_id:
                    devices[-1]["usb_id"] = usb_id
            except (OSError, UnicodeError):
                pass

    # Auto-select logic
    if len(usb_devices) == 1:
        selected_index = usb_devices[0]["index"]
        for d in devices:
            if d["id"] == f"hw:{selected_index},0":
                d["auto_selected"] = True
        logger.debug(f"Single USB audio device: hw:{selected_index},0 ({usb_devices[0]['long_name']})")
    elif len(usb_devices) > 1:
        logger.debug("Multiple USB audio devices found; selection is applied by the backend policy")
    else:
        logger.debug("No USB audio devices found")
        if devices:
            devices[0]["auto_selected"] = True

    return devices


def _parse_aplay_l() -> List[dict]:
    """Fallback parser for `aplay -l` output."""
    try:
        result = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        cards = []
        # card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
        pattern = re.compile(
            r"^card (\d+): (\S+) \[(.+?)\], device (\d+): .+$", re.MULTILINE
        )
        seen = set()
        for m in pattern.finditer(result.stdout):
            index = int(m.group(1))
            if index in seen:
                continue
            seen.add(index)
            short_name = m.group(2)
            long_name = m.group(3)
            # Also reject by long name for RPi cards not caught by driver name
            is_non_usb_name = any(
                s in long_name.upper()
                for s in ("BCM2835", "VC4-HDMI", "VC4HDMI", "HDMI")
            )
            is_usb = (
                not is_non_usb_name
                and ("USB" in long_name.upper() or "USB" in short_name.upper())
            )
            cards.append({
                "index": index,
                "short_name": short_name,
                "driver": "USB-Audio" if is_usb else "internal",
                "long_name": long_name,
                "is_usb": is_usb,
            })
        return cards
    except Exception as e:
        logger.error(f"aplay -l failed: {e}")
        return []


def get_auto_selected_device(devices: List[dict]) -> Optional[str]:
    """Return the device ID that was auto-selected, or None."""
    for d in devices:
        if d["auto_selected"]:
            return d["id"]
    return None
