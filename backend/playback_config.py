"""Persistent playback policy, applied at the next segment start."""
import json
import os
import threading
from pathlib import Path

DEFAULTS = {"read_mode": "continuous", "gap_treatment": "mirror"}
CHOICES = {"read_mode": ("continuous", "recovery"), "gap_treatment": ("mirror", "silence")}
_lock = threading.RLock()


def config_file():
    override = os.environ.get("CDPCORE_CONFIG_DIR")
    directory = Path(override) if override else Path.home() / ".config" / "cdpcore-backend"
    return directory / "playback.conf"


def get_persisted():
    with _lock:
        result = DEFAULTS.copy()
        try:
            data = json.loads(config_file().read_text(encoding="ascii"))
            if isinstance(data, dict):
                for key, values in CHOICES.items():
                    if data.get(key) in values:
                        result[key] = data[key]
        except (OSError, ValueError):
            pass
        return result


def get_settings():
    settings = get_persisted()
    override = os.environ.get("CDPCORE_READ_MODE", "").strip().lower()
    if override in CHOICES["read_mode"]:
        settings["read_mode"] = override
    return settings


def save_settings(data):
    if not isinstance(data, dict) or not data or set(data) - set(CHOICES):
        raise ValueError("Supply read_mode and/or gap_treatment")
    for key, value in data.items():
        if value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {CHOICES[key]}")
    with _lock:
        settings = {**get_persisted(), **data}
        path = config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".conf.tmp")
        try:
            temporary.write_text(json.dumps(settings) + "\n", encoding="ascii")
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return settings
