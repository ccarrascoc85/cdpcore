#!/usr/bin/env bash
# CDPcore installer — run as a regular user with sudo privileges
# Usage: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/cdpcore"
CACHE_DIR="/var/cache/cd-player"
SERVICE_USER="cdplayer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing system dependencies"
apt-get update -qq
apt-get install -y \
    python3 python3-pip python3-venv \
    mpv \
    libdiscid0 libdiscid-dev \
    cdparanoia \
    alsa-utils \
    libasound2-dev \
    curl ca-certificates gnupg eject \
    usbutils \
    avahi-daemon

echo "==> Installing Node.js 20 (NodeSource apt repo, GPG-pinned)"
# Avoid `curl | bash` of the NodeSource setup script: write the apt source
# explicitly with a signed-by keyring. The keyring is fetched once and the
# repository definition remains auditable in /etc/apt/sources.list.d/.
if ! command -v node &>/dev/null || [[ "$(node --version | cut -d. -f1)" != "v20" ]]; then
    NODE_KEYRING="/etc/apt/keyrings/nodesource.gpg"
    NODE_LIST="/etc/apt/sources.list.d/nodesource.list"
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o "$NODE_KEYRING"
    chmod 0644 "$NODE_KEYRING"
    echo "deb [signed-by=$NODE_KEYRING] https://deb.nodesource.com/node_20.x nodistro main" \
        > "$NODE_LIST"
    apt-get update -qq
    apt-get install -y nodejs
fi
echo "    Node: $(node --version)  npm: $(npm --version)"

echo "==> Creating service user: $SERVICE_USER"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -m -d "/home/$SERVICE_USER" -s /bin/bash "$SERVICE_USER"
fi
usermod -aG audio,cdrom "$SERVICE_USER"

echo "==> Installing project to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
    "$SCRIPT_DIR/backend/" "$INSTALL_DIR/backend/" \
    --exclude "__pycache__"
rsync -a --delete \
    "$SCRIPT_DIR/extension/" "$INSTALL_DIR/extension/" \
    --exclude "node_modules"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Creating Python virtual environment"
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q \
    -r "$INSTALL_DIR/backend/requirements.txt"

echo "==> Installing Node.js dependencies (deterministic via package-lock.json)"
# `npm ci` enforces the lockfile: fails if package.json and package-lock.json
# disagree, never mutates the lockfile, never reaches the registry to
# re-resolve loose ranges. This is the deterministic equivalent of pip-tools.
sudo -u "$SERVICE_USER" npm ci --prefix "$INSTALL_DIR/extension" --no-fund --no-audit

echo "==> Creating cache directory: $CACHE_DIR"
mkdir -p "$CACHE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$CACHE_DIR"

echo "==> Preparing admin-auth config directory"
CONFIG_DIR="/home/$SERVICE_USER/.config/cdpcore-backend"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR"

echo "==> Configuring sudo permissions for system management"
install -m 440 "$SCRIPT_DIR/system/cdpcore-sudoers" /etc/sudoers.d/cdpcore

echo "==> Installing udev rules"
cp "$SCRIPT_DIR/system/99-cdrom.rules"          /etc/udev/rules.d/
cp "$SCRIPT_DIR/system/99-audio.rules"          /etc/udev/rules.d/
cp "$SCRIPT_DIR/system/cd-inserted.sh"          /usr/local/bin/
cp "$SCRIPT_DIR/system/cd-ejected.sh"           /usr/local/bin/
cp "$SCRIPT_DIR/system/cd-setspeed.sh"          /usr/local/bin/
cp "$SCRIPT_DIR/system/audio-device-change.sh"  /usr/local/bin/
chmod +x /usr/local/bin/cd-inserted.sh \
         /usr/local/bin/cd-ejected.sh \
         /usr/local/bin/cd-setspeed.sh \
         /usr/local/bin/audio-device-change.sh
udevadm control --reload-rules

echo "==> Registering mDNS service (avahi)"
cp "$SCRIPT_DIR/system/cdpcore.avahi" /etc/avahi/services/cdpcore.service
systemctl reload-or-restart avahi-daemon 2>/dev/null || true

echo "==> Installing and enabling systemd services"
cp "$SCRIPT_DIR/system/cdpcore-backend.service"   /etc/systemd/system/
cp "$SCRIPT_DIR/system/cdpcore-extension.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cdpcore-backend cdpcore-extension

echo ""
echo "==> Done. Waiting for backend..."
sleep 6
if curl -sf http://localhost:8000/health | grep -q ok; then
    echo "    Backend is up:"
    echo "      http://$(hostname).local:8000"
    echo "      http://$(hostname -I | awk '{print $1}'):8000"
else
    echo "    Backend not responding — check: journalctl -u cdpcore-backend -n 30"
fi

echo ""
echo "    First-time setup: open the URL above in a browser to choose"
echo "    PIN protection or LAN-Trust. The welcome screen appears until"
echo "    one option is saved."
echo ""
echo "    Recovery (forgot PIN):"
echo "      sudo -u $SERVICE_USER bash -c \"cd $INSTALL_DIR/backend && $INSTALL_DIR/venv/bin/python -m auth rotate\""
