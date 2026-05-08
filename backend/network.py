"""Network configuration management via NetworkManager (nmcli).

User-supplied parameters (connection names, IP addresses, SSIDs, hostnames)
are validated here before being passed to subprocess. This is the inner
defense layer paired with the verb-scoped /etc/sudoers.d/cdpcore rules:
sudoers prevents a foothold from running arbitrary nmcli verbs; the
validators below prevent argument injection within the verbs we do allow.
Together they keep the privileged surface tight even if the API handlers
above are loose.
"""
import re
import subprocess
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------
# Each validator raises ValueError on bad input. Callers (or the API
# handlers above them) translate that to a 4xx response. The shapes below
# are deliberately strict: nmcli would accept more, but the appliance UI
# never needs to.

_IPV4 = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"
)
_IPV4_CIDR = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?:/(?:[12]?\d|3[0-2]))?$"
)
# RFC 1123 single label: starts with letter, alnum/hyphen, 1-63 chars,
# does not end with hyphen.
_HOSTNAME = re.compile(r"^[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# NetworkManager connection name: visible-ASCII, no control chars or quotes,
# bounded length. Critically: must NOT start with '-' (sudoers wildcards
# match the whole tail, and a leading hyphen could be misread as a flag by
# a future helper that didn't use the -- separator).
_NM_CONN_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.-]{0,79}$")
# SSID: same anchoring against leading hyphen; allow most printable chars.
# nmcli wifi connect "<ssid>" handles spaces/punctuation fine via list args.
_SSID = re.compile(r"^[^\-\x00-\x1F][\x20-\x7E]{0,31}$")


def _validate_ipv4(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IPV4.match(value):
        raise ValueError(f"{field} must be an IPv4 address (e.g. 192.168.1.10)")
    return value


def _validate_ipv4_cidr(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IPV4_CIDR.match(value):
        raise ValueError(f"{field} must be IPv4 or IPv4/CIDR (e.g. 192.168.1.10/24)")
    return value


def _validate_dns_list(value: str, field: str) -> str:
    """Comma-separated list of IPv4 addresses, or empty string."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a comma-separated list of IPv4 addresses")
    if not value.strip():
        return ""
    items = [v.strip() for v in value.split(",")]
    for v in items:
        if not _IPV4.match(v):
            raise ValueError(f"{field} contains invalid IPv4: {v!r}")
    return ",".join(items)


def _validate_hostname(value: str, field: str = "hostname") -> str:
    if not isinstance(value, str) or not _HOSTNAME.match(value):
        raise ValueError(
            f"{field} must be 1-63 chars, letters/digits/hyphen, "
            f"start with a letter, not end with a hyphen"
        )
    return value


def _validate_ssid(value: str) -> str:
    if not isinstance(value, str) or not _SSID.match(value):
        raise ValueError(
            "SSID must be 1-32 printable ASCII chars and not start with '-'"
        )
    return value


def _validate_connection_name(value: str, *, must_exist: bool = True) -> str:
    """Validate a connection name. When must_exist=True, also require the name
    to appear in `nmcli connection show` — bounds the surface of `connection
    delete` / `connection up` to known existing connections only."""
    if not isinstance(value, str) or not _NM_CONN_NAME.match(value):
        raise ValueError(
            "connection name must be 1-80 chars, alnum/space/_/./-, "
            "and not start with a hyphen"
        )
    if must_exist:
        out, _, rc = _run("nmcli", "-t", "-f", "NAME", "connection", "show")
        if rc != 0:
            raise ValueError("nmcli not available to verify connection name")
        names = {line.strip() for line in out.splitlines() if line.strip()}
        if value not in names:
            raise ValueError(f"connection {value!r} does not exist")
    return value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(*args: str, timeout: int = 8) -> tuple[str, str, int]:
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def _run_stdin(stdin_bytes: bytes, *args: str, timeout: int = 8) -> tuple[str, str, int]:
    """Like _run, but pipes stdin_bytes into the process.

    Used for the hidden-WiFi wrapper so the password never appears in
    the process's argv (which is briefly visible in /proc/<pid>/cmdline).
    """
    r = subprocess.run(
        list(args),
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
    )
    return (
        r.stdout.decode("utf-8", errors="replace").strip(),
        r.stderr.decode("utf-8", errors="replace").strip(),
        r.returncode,
    )


def _conn_props(name: str, *fields: str) -> list[str]:
    """Return one value per requested field for a NM connection (uses -g, no delimiter issues)."""
    out, _, rc = _run("nmcli", "-g", ",".join(fields), "connection", "show", name)
    if rc != 0:
        return [""] * len(fields)
    lines = out.splitlines()
    return (lines + [""] * len(fields))[: len(fields)]


# ---------------------------------------------------------------------------
# Ethernet
# ---------------------------------------------------------------------------

def get_ethernet_connections() -> list[dict]:
    """Return all 802-3-ethernet NM connections with current config."""
    out, _, rc = _run("nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show")
    if rc != 0:
        return []

    conns: list[dict] = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or parts[1] != "802-3-ethernet":
            continue
        name, _, device = parts

        vals = _conn_props(
            name,
            "ipv4.method",
            "ipv4.addresses",
            "ipv4.gateway",
            "ipv4.dns",
            "IP4.ADDRESS",
            "IP4.GATEWAY",
        )
        method       = vals[0] or "auto"
        address_cfg  = vals[1]   # configured static addr (may be empty for DHCP)
        gateway_cfg  = vals[2]
        dns_cfg      = vals[3]
        current_ip   = vals[4].split("/")[0] if vals[4] else ""   # strip prefix
        current_gw   = vals[5]

        conns.append({
            "name":       name,
            "device":     device,
            "method":     method,
            "address":    address_cfg,
            "gateway":    gateway_cfg,
            "dns":        dns_cfg,
            "current_ip": current_ip,
            "current_gw": current_gw,
        })
    return conns


def set_ethernet_dhcp(connection_name: str) -> tuple[bool, str]:
    """Switch a connection to DHCP."""
    connection_name = _validate_connection_name(connection_name)
    _, err, rc = _run(
        "sudo", "nmcli", "connection", "modify", connection_name,
        "ipv4.method", "auto",
        "ipv4.addresses", "",
        "ipv4.gateway",  "",
        "ipv4.dns",      "",
    )
    if rc != 0:
        return False, err
    _, err, rc = _run("sudo", "nmcli", "connection", "up", connection_name)
    return rc == 0, err


def set_ethernet_static(
    connection_name: str,
    address: str,
    gateway: str,
    dns: str,
) -> tuple[bool, str]:
    """Switch a connection to a static IP."""
    connection_name = _validate_connection_name(connection_name)
    address = _validate_ipv4_cidr(address, "address")
    gateway = _validate_ipv4(gateway, "gateway")
    dns = _validate_dns_list(dns, "dns")
    _, err, rc = _run(
        "sudo", "nmcli", "connection", "modify", connection_name,
        "ipv4.method",    "manual",
        "ipv4.addresses", address,
        "ipv4.gateway",   gateway,
        "ipv4.dns",       dns,
    )
    if rc != 0:
        return False, err
    _, err, rc = _run("sudo", "nmcli", "connection", "up", connection_name)
    return rc == 0, err


# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------

def _parse_wifi_list(raw: str) -> list[dict]:
    """Parse `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list` output."""
    networks: list[dict] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        # Format: IN-USE:SSID:SIGNAL:SECURITY  (colons inside SSID escaped as \:)
        parts = re.split(r"(?<!\\):", line, maxsplit=3)
        if len(parts) < 4:
            continue
        in_use, ssid, signal_s, security = parts
        ssid = ssid.replace("\\:", ":")
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(signal_s)
        except ValueError:
            signal = 0
        networks.append({
            "ssid":      ssid,
            "signal":    signal,
            "security":  security,
            "connected": in_use == "*",
        })
    networks.sort(key=lambda x: -x["signal"])
    return networks


def wifi_radio_state() -> bool:
    out, _, _ = _run("nmcli", "radio", "wifi")
    return out.lower() == "enabled"


def wifi_set_radio(on: bool) -> tuple[bool, str]:
    _, err, rc = _run("sudo", "nmcli", "radio", "wifi", "on" if on else "off")
    return rc == 0, err


def wifi_list() -> list[dict]:
    """Return the cached WiFi network list (no rescan, fast)."""
    out, _, rc = _run(
        "nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"
    )
    return _parse_wifi_list(out) if rc == 0 else []


def wifi_scan() -> list[dict]:
    """Force a rescan then return updated network list."""
    _run("sudo", "nmcli", "device", "wifi", "rescan", timeout=12)
    out, _, rc = _run(
        "nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"
    )
    return _parse_wifi_list(out) if rc == 0 else []


def wifi_connect(ssid: str, password: str, hidden: bool = False) -> tuple[bool, str]:
    ssid = _validate_ssid(ssid)
    if password and not isinstance(password, str):
        raise ValueError("password must be a string")

    if hidden:
        # Hidden APs don't broadcast SSID, so nmcli's `device wifi connect
        # hidden yes` cannot find them. The reliable path is the wrapper
        # script /usr/local/bin/cdpcore-wifi-hidden, which creates an NM
        # profile with 802-11-wireless.hidden=yes and activates it.
        # Password is piped on stdin so it never appears in argv.
        stdin = (password.encode("utf-8") + b"\n") if password else b""
        _, err, rc = _run_stdin(
            stdin,
            "sudo", "/usr/local/bin/cdpcore-wifi-hidden", ssid,
            timeout=35,
        )
        return rc == 0, err

    cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    _, err, rc = _run(*cmd, timeout=30)
    return rc == 0, err


def wifi_disconnect(connection_name: str) -> tuple[bool, str]:
    """Delete a WiFi connection profile (disconnects the device)."""
    connection_name = _validate_connection_name(connection_name)
    _, err, rc = _run("sudo", "nmcli", "connection", "delete", connection_name)
    return rc == 0, err


def get_wifi_current_ip() -> str | None:
    """Return the IPv4 address of the active WiFi interface, without prefix length."""
    out, _, rc = _run("nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) >= 3 and parts[1] in ("wifi", "802-11-wireless") and parts[2]:
            ip_out, _, ip_rc = _run("nmcli", "-g", "IP4.ADDRESS", "device", "show", parts[2])
            if ip_rc == 0 and ip_out.strip():
                return ip_out.strip().split("/")[0]
    return None


def get_current_wifi_connection() -> str | None:
    """Return the name of the active WiFi NM connection, or None.

    NM reports the connection type as '802-11-wireless' in terse output;
    connections created via `device wifi connect` may also show as 'wifi'
    depending on NM version. Accept both to cover the hidden-network path
    (which uses `connection add type wifi`) and the normal path.
    """
    out, _, rc = _run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active")
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":", 1)
        if len(parts) >= 2 and parts[1] in ("wifi", "802-11-wireless"):
            return parts[0]
    return None


# ---------------------------------------------------------------------------
# Hostname
# ---------------------------------------------------------------------------

def set_hostname(name: str) -> tuple[bool, str]:
    name = _validate_hostname(name)
    _, err, rc = _run("sudo", "hostnamectl", "set-hostname", name)
    return rc == 0, err
