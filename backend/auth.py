"""
Admin authentication for CDPcore backend.

Model (see docs/agent_reasoning.md D-2):
- CDPCORE_ADMIN_AUTH=pin  -> PIN gate on /system/* (default)
- CDPCORE_ADMIN_AUTH=off  -> gate disabled (private-install opt-out)

PIN:  6-digit numeric, stored as pbkdf2_sha256(hash + salt).
Token: HMAC-signed short-lived cookie. Payload is the expiry timestamp.

Files (mode 0600):
- <CONFIG_DIR>/admin.pin   -> PIN hash material
- <CONFIG_DIR>/session.key -> HMAC signing key
- <CONFIG_DIR>/auth.conf   -> mode override (overrides CDPCORE_ADMIN_AUTH)

CONFIG_DIR defaults to ~/.config/cdpcore-backend and can be overridden
via CDPCORE_CONFIG_DIR (used by tests and install-time bootstrap).

CLI:
    python -m auth init          # generate PIN if missing, print if new
    python -m auth rotate        # force-generate a new PIN, print it
    python -m auth set <pin>     # set PIN to user-chosen 6-digit value
    python -m auth status        # report whether a PIN is configured
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

COOKIE_NAME  = "cdpcore_admin"
SESSION_TTL  = 15 * 60          # seconds — D-2 recommendation
PIN_DIGITS   = 6
PBKDF2_ITER  = 200_000
PBKDF2_ALGO  = "pbkdf2_sha256"

MODE_PIN = "pin"
MODE_OFF = "off"
_VALID_MODES = (MODE_PIN, MODE_OFF)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    override = os.environ.get("CDPCORE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "cdpcore-backend"


def _pin_file() -> Path:
    return _config_dir() / "admin.pin"


def _key_file() -> Path:
    return _config_dir() / "session.key"


def _mode_file() -> Path:
    return _config_dir() / "auth.conf"


def _ensure_dir() -> None:
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

def get_mode() -> str:
    """File override (persistent, runtime-toggleable) wins over env var."""
    try:
        data = json.loads(_mode_file().read_text())
        raw = (data.get("mode") or "").strip().lower()
        if raw in _VALID_MODES:
            return raw
    except (OSError, ValueError, TypeError):
        pass
    raw = (os.environ.get("CDPCORE_ADMIN_AUTH") or MODE_PIN).strip().lower()
    return raw if raw in _VALID_MODES else MODE_PIN


def set_mode(mode: str) -> None:
    """Persist the admin-auth mode. Raises ValueError on invalid input."""
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}")
    _ensure_dir()
    p = _mode_file()
    p.write_text(json.dumps({"mode": mode, "updated_at": int(time.time())}))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# PIN
# ---------------------------------------------------------------------------

def _hash_pin(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITER)


def _write_pin_record(pin: str) -> None:
    _ensure_dir()
    salt = secrets.token_bytes(16)
    payload = {
        "algorithm":  PBKDF2_ALGO,
        "iterations": PBKDF2_ITER,
        "salt":       salt.hex(),
        "hash":       _hash_pin(pin, salt).hex(),
        "created_at": int(time.time()),
    }
    p = _pin_file()
    p.write_text(json.dumps(payload))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _read_pin_record() -> Optional[dict]:
    try:
        return json.loads(_pin_file().read_text())
    except (OSError, ValueError):
        return None


def generate_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(PIN_DIGITS))


def validate_pin_shape(pin: str) -> None:
    """Raise ValueError if pin is not a 6-digit numeric string."""
    if not isinstance(pin, str) or len(pin) != PIN_DIGITS or not pin.isdigit():
        raise ValueError(f"PIN must be exactly {PIN_DIGITS} digits")


def initialize_pin_if_missing() -> Optional[str]:
    """Called at startup. Returns the plaintext PIN if one was just generated."""
    if _read_pin_record() is not None:
        return None
    pin = generate_pin()
    _write_pin_record(pin)
    return pin


def rotate_pin() -> str:
    pin = generate_pin()
    _write_pin_record(pin)
    return pin


def set_pin(new_pin: str) -> None:
    """Persist a user-chosen PIN. Raises ValueError on invalid shape."""
    validate_pin_shape(new_pin)
    _write_pin_record(new_pin)


def is_pin_configured() -> bool:
    return _read_pin_record() is not None


def setup_required() -> bool:
    """True iff the appliance has not yet made an explicit auth choice.

    Cleared by either persisting a PIN (`admin.pin`) or persisting a mode
    (`auth.conf`). The env var `CDPCORE_ADMIN_AUTH` remains a fallback for
    get_mode() but does NOT satisfy setup — a fresh install always prompts.
    """
    if is_pin_configured():
        return False
    if _mode_file().exists():
        return False
    return True


def verify_pin(submitted: str) -> bool:
    rec = _read_pin_record()
    if not rec:
        return False
    try:
        salt     = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
    except (KeyError, ValueError):
        return False
    candidate = _hash_pin(submitted or "", salt)
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# Session token
# ---------------------------------------------------------------------------

def _session_key() -> bytes:
    _ensure_dir()
    p = _key_file()
    if p.exists():
        try:
            return bytes.fromhex(p.read_text().strip())
        except ValueError:
            pass
    key = secrets.token_bytes(32)
    p.write_text(key.hex())
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key


def issue_token(ttl: int = SESSION_TTL) -> Tuple[str, int]:
    exp = int(time.time()) + ttl
    msg = str(exp).encode("ascii")
    sig = hmac.new(_session_key(), msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}", exp


def verify_token(token: Optional[str]) -> Optional[int]:
    """Return the expiry unix-ts if valid and not expired; None otherwise."""
    if not token or "." not in token:
        return None
    exp_str, sig = token.split(".", 1)
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    expected = hmac.new(_session_key(), exp_str.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if exp < int(time.time()):
        return None
    return exp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="auth", description="CDPcore admin-auth helpers")
    p.add_argument("cmd", choices=["init", "rotate", "set", "status"])
    p.add_argument("pin", nargs="?", help="6-digit PIN (only for 'set')")
    args = p.parse_args()

    if args.cmd == "init":
        new = initialize_pin_if_missing()
        if new:
            print(f"Generated admin PIN: {new}")
        else:
            rec = _read_pin_record()
            created = time.ctime(rec.get("created_at", 0)) if rec else "?"
            print(f"PIN already configured (created {created}). Use 'rotate' to replace it.")
        return 0

    if args.cmd == "rotate":
        print(f"New admin PIN: {rotate_pin()}")
        return 0

    if args.cmd == "set":
        if not args.pin:
            print("Usage: python -m auth set <6-digit-pin>")
            return 2
        try:
            set_pin(args.pin)
        except ValueError as e:
            print(f"Error: {e}")
            return 2
        print("PIN updated.")
        return 0

    if args.cmd == "status":
        rec = _read_pin_record()
        if rec:
            print(f"PIN configured. Created: {time.ctime(rec.get('created_at', 0))}")
        else:
            print("No PIN configured.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
