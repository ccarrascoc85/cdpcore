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
            "name": card["long_name"],
            "is_usb": card["is_usb"],
            "auto_selected": False,
        })

    # Auto-select logic
    if len(usb_devices) == 1:
        selected_index = usb_devices[0]["index"]
        for d in devices:
            if d["id"] == f"hw:{selected_index},0":
                d["auto_selected"] = True
        logger.info(f"Auto-selected USB audio device: hw:{selected_index},0 ({usb_devices[0]['long_name']})")
    elif len(usb_devices) > 1:
        logger.warning("Multiple USB audio devices found — manual selection required")
    else:
        logger.warning("No USB audio devices found — falling back to default ALSA device")
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
