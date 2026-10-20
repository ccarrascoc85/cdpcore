"""Persistent preferred USB output, applied at the next playback start."""
import json
import os
import threading
from pathlib import Path

_lock = threading.RLock()


def config_file():
    override = os.environ.get("CDPCORE_CONFIG_DIR")
    directory = Path(override) if override else Path.home() / ".config" / "cdpcore-backend"
    return directory / "audio.conf"


def get_preferred():
    with _lock:
        try:
            data = json.loads(config_file().read_text(encoding="ascii"))
            preferred = data.get("preferred") if isinstance(data, dict) else None
            if not isinstance(preferred, dict):
                return None
            card_id = preferred.get("card_id")
            usb_id = preferred.get("usb_id")
            name = preferred.get("name")
            if not isinstance(card_id, str) or not card_id.strip():
                return None
            if usb_id is not None and not isinstance(usb_id, str):
                return None
            if not isinstance(name, str):
                return None
            return {"card_id": card_id, "usb_id": usb_id, "name": name}
        except (OSError, ValueError):
            return None


def _write(preferred):
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".conf.tmp")
    try:
        temporary.write_text(json.dumps({"preferred": preferred}) + "\n", encoding="ascii")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def set_preferred(device_dict):
    preferred = {"card_id": device_dict["card_id"],
                 "usb_id": device_dict.get("usb_id"), "name": device_dict["name"]}
    with _lock:
        _write(preferred)
    return preferred


def clear_preferred():
    with _lock:
        _write(None)
