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
