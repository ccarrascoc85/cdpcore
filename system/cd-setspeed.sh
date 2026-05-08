#!/bin/bash
# /usr/local/bin/cd-setspeed.sh
# Forces the optical drive to 1x read speed on every disc insertion.
# Called by udev via 99-cdrom.rules before the backend is notified.
#
# Many drives reset to their maximum speed on each disc insert, so
# this must run on every ID_CDROM_MEDIA==1 event, not just at boot.
#
# If the drive does not support CDROM_SELECT_SPEED (some USB drives),
# eject -x exits with an error and the drive keeps its default speed.

DEVICE="${DEVNAME:-/dev/cdrom}"
LOG_FILE="/var/log/cd-player-udev.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [cd-setspeed] $*" >> "$LOG_FILE"
}

log "Setting drive speed to 1x on $DEVICE"

if eject -x 1 "$DEVICE" 2>/dev/null; then
    log "Speed set to 1x OK"
else
    log "Speed set failed (drive may not support CDROM_SELECT_SPEED — ignored)"
fi
