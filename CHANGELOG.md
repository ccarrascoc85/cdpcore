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
