#!/bin/bash
# /usr/local/bin/audio-device-change.sh
# Called by udev via systemd-run when a sound card is added or removed.
# Notifies the CDPcore backend so it can update its device list.

BACKEND_URL="${CD_BACKEND_URL:-http://localhost:8000}"
LOG_FILE="/var/log/cd-player-udev.log"
ACTION="${1:-unknown}"
CARD="${2:-unknown}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [audio-change] $*" >> "$LOG_FILE"
}

log "Sound card event: action=${ACTION} card=${CARD}"

for i in 1 2; do
    RESULT=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 2 \
        --max-time 3 \
        -X POST \
        -H "Content-Type: application/json" \
        -d "{\"action\": \"${ACTION}\", \"name\": \"${CARD}\"}" \
        "${BACKEND_URL}/audio/device-change")

    if [ "$RESULT" = "200" ]; then
        log "Backend notified OK (attempt $i)"
        exit 0
    fi

    log "Attempt $i failed (HTTP $RESULT)"
    [ $i -lt 2 ] && sleep 1
done

log "Notification failed after 2 attempts - 30s reconciliation will cover the event"
exit 1
