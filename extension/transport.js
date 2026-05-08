"use strict";
/**
 * Roon zone pause/resume logic.
 * When CD starts playing, pauses the configured Roon zone.
 * When CD stops/ejects, resumes the previously paused zone.
 */

class RoonTransportManager {
  constructor() {
    this._transport = null;
    this._pausedZoneId = null;
    this._pausedZoneName = null;
    this._targetZoneName = null; // set from settings
  }

  setTransport(transport) {
    this._transport = transport;
  }

  setTargetZone(zoneName) {
    this._targetZoneName = zoneName;
  }

  /**
   * Find the target Roon zone by name from the current zones list.
   */
  _findZone(zones) {
    if (!this._targetZoneName) return null;
    return zones.find(
      (z) =>
        z.display_name &&
        z.display_name.toLowerCase() ===
          this._targetZoneName.toLowerCase()
    ) || null;
  }

  /**
   * Called when CD disc is loaded / starts playing.
   * Pauses the Roon zone if it's playing, so the DAC is released.
   */
  async pauseRoonZone(zones) {
    if (!this._transport || !this._targetZoneName) return;

    const zone = this._findZone(zones);
    if (!zone) {
      console.log(
        `[transport] Zone '${this._targetZoneName}' not found — skipping pause`
      );
      return;
    }

    const isPlaying =
      zone.state === "playing" ||
      (zone.now_playing && zone.now_playing.seek_position !== undefined);

    if (!isPlaying) {
      console.log(
        `[transport] Zone '${zone.display_name}' is not playing — nothing to pause`
      );
      return;
    }

    return new Promise((resolve) => {
      this._transport.control(zone, "pause", (err) => {
        if (err) {
          console.error("[transport] Pause zone failed:", err);
        } else {
          this._pausedZoneId = zone.zone_id;
          this._pausedZoneName = zone.display_name;
          console.log(`[transport] Paused Roon zone: '${zone.display_name}'`);
        }
        resolve();
      });
    });
  }

  /**
   * Called when CD stops or is ejected.
   * Resumes the previously paused Roon zone.
   */
  async resumeRoonZone(zones) {
    if (!this._transport || !this._pausedZoneId) return;

    const zone = zones.find((z) => z.zone_id === this._pausedZoneId);
    if (!zone) {
      console.log(
        `[transport] Previously paused zone '${this._pausedZoneName}' not found`
      );
      this._pausedZoneId = null;
      this._pausedZoneName = null;
      return;
    }

    return new Promise((resolve) => {
      this._transport.control(zone, "play", (err) => {
        if (err) {
          console.error("[transport] Resume zone failed:", err);
        } else {
          console.log(
            `[transport] Resumed Roon zone: '${zone.display_name}'`
          );
        }
        this._pausedZoneId = null;
        this._pausedZoneName = null;
        resolve();
      });
    });
  }

  isPaused() {
    return !!this._pausedZoneId;
  }
}

module.exports = RoonTransportManager;
