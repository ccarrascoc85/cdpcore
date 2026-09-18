# Changelog

All notable changes to CDPcore are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versioning policy (appliance-oriented):

- **MAJOR** - breaks the install/config/hardware contract or the on-disk data
  layout (operator action required to upgrade).
- **MINOR** - new operator-visible feature or capability.
- **PATCH** - bug fix, metadata, or documentation with no behavior change.

Deployed appliances track tagged releases, not the `main` branch HEAD.

## [Unreleased]

## [1.3.2] - 2026-09-18

Critical corrective release: an unreadable disc sector could take the whole
appliance down (fake PLAYING, unresponsive drive, reboot required).

### Fixed

- A cold start whose disc TOC never arrives is now reported instead of faked.
  When the optical drive stops answering while mpv opens it, the player used
  to wait out its 60 s deadline, log "Playing track N" and unpause an mpv
  with no file loaded: the UI showed PLAYING with a frozen 0:00 counter and
  no way to recover. It now tears mpv down, `POST /play` and `POST /play/{n}`
  return HTTP 503 `drive_stalled`, the state goes back to LOADED with
  `error: "drive_stalled"` in `/status`, and the status line shows
  **DRIVE ERROR**. Playback of the same disc can be retried; the error clears
  on the next play attempt or disc change.
- Drive status ioctls (media-present polls, speed set) run on their own
  two-thread pool instead of the event loop's default executor. A wedged
  drive leaves each such call stuck in uninterruptible I/O; on the shared
  pool those leaked threads starved audio-device refresh and metadata work.
- The disc monitor no longer polls `CDROM_DRIVE_STATUS` while mpv is opening
  the drive on a cold start, so the backend does not interleave status
  commands with mpv's TOC read on the same USB bridge in that window. mpv's
  own events still catch a removal there.
- The udev disc-insertion rule matches again on current Raspberry Pi OS.
  `cdrom_id` from systemd 252 no longer sets `ID_CDROM_MEDIA_CD_AUDIO`, so
  `cd-setspeed.sh` and `cd-inserted.sh` had not run since June; disc
  detection worked only through the backend's polling fallback. The rule now
  matches `ID_CDROM_MEDIA_TRACK_COUNT_AUDIO`. udev rules are not covered by
  the in-app updater: re-run `install.sh` or copy `system/99-cdrom.rules` to
  `/etc/udev/rules.d/` and reload udev.

## [1.3.1] - 2026-09-18

### Fixed

- A track selected while the player shows STARTING is now honoured. Previously
  `POST /play/{n}` issued during mpv spin-up sent a chapter seek to an IPC
  socket that had not read the TOC yet; the command was lost, playback began
  on track 1 while the UI showed the requested track, and a second tap was
  needed. The request is now queued and applied once the TOC is available.
  Only the latest request is kept: tapping 9 and then 8 during STARTING starts
  on 8.
- The progress bar no longer slides for ~1 s to catch up when the page is
  reloaded or the tab returns to the foreground. The CSS transition on the
  bar predated client-side interpolation and had become a visual lag; the bar
  now jumps to the real position immediately and still moves continuously
  during playback.

## [1.3.0] - 2026-07-31

### Added

- The status line shows **NO DAC** when a disc is loaded but no usable USB DAC
  is present, so a play blocked by the audio gate has a visible reason instead
  of appearing to do nothing.

### Fixed

- Transport commands (`/play`, `/next`, `/prev`, `/eject`) stay responsive when
  a disc is ejected during playback. mpv teardown, which can block on
  uninterruptible drive I/O, was moved off the request path so the API no
  longer hangs (deadlocks) during eject-in-playback.
- Disc detection stays reliable when the optical drive wedges on I/O; a stuck,
  uninterruptible read no longer stalls the monitor loop.
- A disc removed the instant it is recognized now reaches IDLE within the
  drive's physical settling time (~1 s) instead of up to 15 s. The monitor
  polls for removal immediately after a disc loads, rather than waiting out the
  slow idle poll interval. Other conditions (eject while stopped or during
  playback) are unchanged.
- The elapsed counter begins at 0:00 in sync with the audio on a cold start and
  ticks up in real time, instead of holding at 0 and then jumping ~1-2 s once
  playback is confirmed. Cold starts anchor the timer to mpv's real playback
  position on the first valid sample; the counter still never leads the audio.
- `POST /play` and `POST /play/{n}` re-enumerate audio devices before starting.
  A DAC unplugged since the last periodic refresh is detected immediately:
  playback is refused with a clear reason and the UI reflects the missing DAC,
  instead of mpv failing silently on a device that is gone.

## [1.2.1] - 2026-06-09

### Fixed

- `POST /play` now resumes playback from the paused position instead of
  restarting the current track from the beginning. Previously, pressing play
  after a pause reset to track 1 at elapsed 0, discarding the paused position;
  it now routes to the existing resume path when the state is `PAUSED`,
  preserving track number and elapsed time. Playing from idle/stopped and
  `POST /play/{n}` are unchanged.

## [1.2.0] - 2026-06-01

### Added

- Updater self-updates its script, one-shot unit, and sudoers metadata from
  release tarballs, with staged sudoers validation and rollback coverage.
- `CDPCORE_RELEASE_REPO` override lets operators point release checks and
  self-updates at an alternate `<owner>/<repo>` via systemd drop-ins.

### Fixed

- The post-restart extension health gate now actually rolls back on failure.
  Previously `RESTORE_READY` was cleared before `wait_service_active` ran, so
  the rollback path introduced in 1.1.2 silently no-op'd when the extension
  failed to start after an update; it now arms rollback until the extension is
  confirmed `active`.
- The updater now consumes `update_request.json` immediately after reading the
  target tag. Previously the request file persisted, so any subsequent
  `systemctl start cdpcore-update` (operator debug or maintenance, or any
  future trigger of the unit) would silently replay the last requested tag -
  including downgrades, as observed during v1.2.0 smoke validation.

## [1.1.2] - 2026-05-31

### Fixed

- Updater no longer wipes the Node extension's `node_modules` when applying a
  release whose `extension/package.json` is unchanged. The release tarball does
  not ship `node_modules`, so the previous `rsync -a --delete` removed it and
  the conditional `npm ci` did not re-create it. Snapshot, restore, and install
  rsyncs now exclude `node_modules/`; `npm ci` still runs when `package.json`
  changes.
- Updater now waits for `cdpcore-extension` to reach `active` after restart; if
  it does not, the update is treated as failed and rolled back. The failure
  path now restarts both the extension and the backend so a failed update
  leaves both services running on the previous code.

## [1.1.1] - 2026-05-28

### Changed

- Maintenance release validating the operator-initiated update path.

## [1.1.0] - 2026-05-28

### Added

- Operator-initiated appliance updater on the system management page. Updates
  are applied from the latest tagged GitHub Release through a decoupled
  one-shot executor outside the backend sandbox.

## [1.0.1] - 2026-05-28

### Added

- `VERSION` file as the single source of truth for the running version,
  exposed via `GET /health`.
- README documents PWA installability.

### Fixed

- PWA install metadata: valid web app manifest (name, id, start_url, scope,
  standalone display, corrected icon paths) plus the `application/manifest+json`
  MIME type, so the player installs as a standalone app via Add to Home Screen
  (Samsung Internet on Android, Safari on iOS).

## [1.0.0] - 2026-05-08

### Added

- Initial appliance baseline: bit-perfect CD playback (mpv + ALSA + USB DAC),
  metadata cascade (MusicBrainz / GnuDB / Cover Art Archive / iTunes), real-time
  WebSocket UI, PIN-gated system management, first-boot trust-posture setup,
  Roon zone pause/resume extension, and automatic USB DAC detection.
