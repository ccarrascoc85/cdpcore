"""System information gathering for the system management page."""
import os
import socket
import subprocess
from pathlib import Path


def _read(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default


def get_system_info() -> dict:
    info: dict = {}

    # Hostname
    info["hostname"] = socket.gethostname()

    # Pi model
    info["model"] = _read("/proc/device-tree/model").rstrip("\x00") or "Unknown"

    # OS version
    info["os"] = "Unknown"
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            info["os"] = line.split("=", 1)[1].strip('"')
            break

    # Uptime
    uptime_sec = float(_read("/proc/uptime", "0 0").split()[0])
    info["uptime_sec"] = uptime_sec
    h = int(uptime_sec // 3600)
    m = int((uptime_sec % 3600) // 60)
    s = int(uptime_sec % 60)
    info["uptime"] = f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"

    # CPU temperature
    cpu_temp = None
    try:
        r = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=2
        )
        cpu_temp = float(r.stdout.strip().replace("temp=", "").replace("'C", ""))
    except Exception:
        try:
            cpu_temp = int(_read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000
        except Exception:
            pass
    info["cpu_temp"] = cpu_temp

    # Load average
    parts = _read("/proc/loadavg", "0 0 0 0/0 0").split()
    load = [float(parts[0]), float(parts[1]), float(parts[2])]
    info["load"] = load
    cpu_count = os.cpu_count() or 1
    info["cpu_count"] = cpu_count
    info["load_pct"] = round(load[0] / cpu_count * 100, 1)

    # Memory
    mem: dict = {}
    for line in _read("/proc/meminfo").splitlines():
        p = line.split()
        if p[0] in ("MemTotal:", "MemFree:", "MemAvailable:"):
            mem[p[0][:-1]] = int(p[1]) * 1024
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    used = total - avail
    info["memory"] = {
        "total": total,
        "used": used,
        "free": avail,
        "pct": round(used / total * 100, 1) if total else 0,
    }

    # Disk
    try:
        r = subprocess.run(["df", "-B1", "/"], capture_output=True, text=True, timeout=3)
        p = r.stdout.strip().splitlines()[1].split()
        dtotal, dused, dfree = int(p[1]), int(p[2]), int(p[3])
        info["disk"] = {
            "total": dtotal,
            "used": dused,
            "free": dfree,
            "pct": round(dused / dtotal * 100, 1) if dtotal else 0,
        }
    except Exception:
        info["disk"] = {"total": 0, "used": 0, "free": 0, "pct": 0}

    # Network interfaces
    net: dict = {}
    try:
        for iface in sorted(Path("/sys/class/net").iterdir()):
            name = iface.name
            if name == "lo":
                continue
            state = _read(str(iface / "operstate"))
            mac = _read(str(iface / "address"))
            speed_raw = _read(str(iface / "speed"))
            speed = f"{speed_raw} Mbps" if speed_raw and speed_raw != "-1" else None
            r = subprocess.run(
                ["ip", "-4", "addr", "show", name],
                capture_output=True, text=True, timeout=2,
            )
            ips = [
                line.strip().split()[1].split("/")[0]
                for line in r.stdout.splitlines()
                if line.strip().startswith("inet ")
            ]
            net[name] = {
                "state": state,
                "mac": mac,
                "ip": ips[0] if ips else None,
                "speed": speed,
            }
    except Exception:
        pass
    info["network"] = net

    # CD drive
    try:
        import cd_reader as _cdr
        vendor = _read("/sys/block/sr0/device/vendor").strip()
        model  = _read("/sys/block/sr0/device/model").strip()
        info["drive"] = {
            "model":                 f"{vendor} {model}".strip() or "Unknown",
            "speed_control":         _cdr.speed_control_supported,  # None = not yet tested
        }
    except Exception:
        info["drive"] = {"model": "Unknown", "speed_control": None}

    # Roon Bridge (optional — only present if service unit exists)
    try:
        probe = subprocess.run(
            ["systemctl", "list-unit-files", "roonbridge.service"],
            capture_output=True, text=True, timeout=3,
        )
        if "roonbridge.service" in probe.stdout:
            active  = subprocess.run(
                ["systemctl", "is-active",  "roonbridge"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            enabled = subprocess.run(
                ["systemctl", "is-enabled", "roonbridge"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            props_raw = subprocess.run(
                ["systemctl", "show", "roonbridge", "--property=MainPID"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            pid = dict(l.split("=", 1) for l in props_raw.splitlines() if "=" in l).get("MainPID")
            info["roon_bridge"] = {
                "active":  active,
                "enabled": enabled,
                "pid":     pid,
            }
    except Exception:
        pass  # roon_bridge key absent = not installed

    # Services
    services: dict = {}
    for svc in ["cdpcore-backend", "cdpcore-extension"]:
        try:
            active = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            props_raw = subprocess.run(
                ["systemctl", "show", svc,
                 "--property=MainPID,ActiveEnterTimestamp,ActiveState"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            props = {
                k: v for k, v in
                (line.split("=", 1) for line in props_raw.splitlines() if "=" in line)
            }
            services[svc] = {
                "active": active,
                "pid": props.get("MainPID"),
                "since": props.get("ActiveEnterTimestamp", ""),
                "state": props.get("ActiveState", ""),
            }
        except Exception:
            services[svc] = {"active": "unknown", "pid": None, "since": "", "state": "unknown"}
    info["services"] = services

    return info


# ---------------------------------------------------------------------------
# Focused helpers (used by /system/roon/info — avoid running full get_system_info)
# ---------------------------------------------------------------------------

def get_roon_bridge_info() -> dict | None:
    """Return Roon Bridge service status, or None if not installed."""
    try:
        probe = subprocess.run(
            ["systemctl", "list-unit-files", "roonbridge.service"],
            capture_output=True, text=True, timeout=3,
        )
        if "roonbridge.service" not in probe.stdout:
            return None
        active = subprocess.run(
            ["systemctl", "is-active", "roonbridge"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        enabled = subprocess.run(
            ["systemctl", "is-enabled", "roonbridge"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        props_raw = subprocess.run(
            ["systemctl", "show", "roonbridge", "--property=MainPID"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        pid = dict(l.split("=", 1) for l in props_raw.splitlines() if "=" in l).get("MainPID")
        return {"active": active, "enabled": enabled, "pid": pid}
    except Exception:
        return None


def get_service_status(svc: str) -> dict:
    """Return basic status dict for a single systemd service."""
    try:
        active = subprocess.run(
            ["systemctl", "is-active", svc],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        props_raw = subprocess.run(
            ["systemctl", "show", svc, "--property=MainPID,ActiveState"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        props = {k: v for k, v in (l.split("=", 1) for l in props_raw.splitlines() if "=" in l)}
        return {"active": active, "pid": props.get("MainPID"), "state": props.get("ActiveState", "")}
    except Exception:
        return {"active": "unknown", "pid": None, "state": "unknown"}
