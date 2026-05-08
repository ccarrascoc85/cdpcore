#!/bin/bash
# /usr/local/bin/cd-ejected.sh
# Called by udev when a CD is ejected.
# Notifies the Python backend via HTTP.

BACKEND_URL="${CD_BACKEND_URL:-http://localhost:8000}"
LOG_FILE="/var/log/cd-player-udev.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [cd-ejected] $*" >> "$LOG_FILE"
}

log "CD ejection detected (device: ${DEVNAME:-unknown})"

RESULT=$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 3 \
    --max-time 10 \
    -X POST \
    "${BACKEND_URL}/cd/ejected")

if [ "$RESULT" = "200" ]; then
    log "Backend notified successfully"
else
    log "Backend notification failed (HTTP $RESULT)"
    exit 1
fi
