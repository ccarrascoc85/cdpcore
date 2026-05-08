#!/bin/bash
# /usr/local/bin/cd-inserted.sh
# Called by udev when an audio CD is inserted.
# Notifies the Python backend via HTTP.
#
# This script runs as root in the udev context.
# curl must be available on the system.

BACKEND_URL="${CD_BACKEND_URL:-http://localhost:8000}"
LOG_FILE="/var/log/cd-player-udev.log"
MAX_RETRIES=5
RETRY_DELAY=2

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [cd-inserted] $*" >> "$LOG_FILE"
}

log "CD insertion detected (device: ${DEVNAME:-unknown})"

# Wait for the backend to be ready (it may still be starting)
for i in $(seq 1 $MAX_RETRIES); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 2 \
        --max-time 3 \
        "${BACKEND_URL}/health")

    if [ "$HTTP_CODE" = "200" ]; then
        break
    fi

    log "Backend not ready (attempt $i/$MAX_RETRIES, HTTP $HTTP_CODE) — retrying in ${RETRY_DELAY}s"
    sleep $RETRY_DELAY
done

# Notify backend
RESULT=$(curl -s -o /tmp/cd-insert-response.json -w "%{http_code}" \
    --connect-timeout 3 \
    --max-time 10 \
    -X POST \
    "${BACKEND_URL}/cd/inserted")

if [ "$RESULT" = "200" ]; then
    log "Backend notified successfully"
else
    log "Backend notification failed (HTTP $RESULT)"
    exit 1
fi
