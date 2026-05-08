"use strict";
/**
 * Polls the Python backend /status endpoint at 1-second intervals
 * when a disc is loaded. Emits events for state changes.
 */

const fetch = require("node-fetch");

const BACKEND_URL = process.env.CD_BACKEND_URL || "http://localhost:8000";
const POLL_INTERVAL_MS = 1000;

class CDPoller {
  constructor() {
    this._timer = null;
    this._lastState = null;
    this._listeners = {};
  }

  on(event, fn) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(fn);
    return this;
  }

  _emit(event, data) {
    (this._listeners[event] || []).forEach((fn) => {
      try {
        fn(data);
      } catch (e) {
        console.error(`[poller] listener error on '${event}':`, e.message);
      }
    });
  }

  start() {
    if (this._timer) return;
    this._timer = setInterval(() => this._poll(), POLL_INTERVAL_MS);
    console.log("[poller] Started polling", BACKEND_URL);
  }

  stop() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  async _poll() {
    try {
      const resp = await fetch(`${BACKEND_URL}/status`, {
        timeout: 800,
      });
      if (!resp.ok) return;
      const status = await resp.json();
      this._handleStatus(status);
    } catch (e) {
      // Backend unreachable — emit offline event once
      if (this._lastState !== "offline") {
        this._lastState = "offline";
        this._emit("offline", {});
      }
    }
  }

  _handleStatus(status) {
    const prev = this._lastState;

    // Always emit status for UI updates
    this._emit("status", status);

    // Transition events
    if (prev !== status.state) {
      this._emit("stateChange", { prev, current: status.state, status });

      if (status.state === "loaded" && prev !== "loaded") {
        this._emit("discLoaded", status);
      }
      if (status.state === "idle" && prev !== "idle" && prev !== null) {
        if (prev === "playing" || prev === "paused") {
          this._emit("discStopped", status);
        } else {
          this._emit("discEjected", status);
        }
      }
      if (status.state === "playing" && prev !== "playing") {
        this._emit("playing", status);
      }
      if (status.state === "paused" && prev === "playing") {
        this._emit("paused", status);
      }
    }

    this._lastState = status.state;
  }

  async getStatus() {
    try {
      const resp = await fetch(`${BACKEND_URL}/status`, { timeout: 2000 });
      if (resp.ok) return resp.json();
    } catch (_) {}
    return null;
  }

  async getTracks() {
    try {
      const resp = await fetch(`${BACKEND_URL}/tracks`, { timeout: 2000 });
      if (resp.ok) return resp.json();
    } catch (_) {}
    return [];
  }

  async getDevices() {
    try {
      const resp = await fetch(`${BACKEND_URL}/devices`, { timeout: 2000 });
      if (resp.ok) return resp.json();
    } catch (_) {}
    return [];
  }

  async post(path) {
    try {
      const resp = await fetch(`${BACKEND_URL}${path}`, {
        method: "POST",
        timeout: 3000,
      });
      if (resp.ok) return resp.json();
    } catch (e) {
      console.error(`[poller] POST ${path} failed:`, e.message);
    }
    return null;
  }

  async selectDevice(deviceId) {
    try {
      const url = new URL(`${BACKEND_URL}/devices/select`);
      url.searchParams.set("device_id", deviceId);
      const resp = await fetch(url.toString(), {
        method: "POST",
        timeout: 5000,
      });
      if (resp.ok) return resp.json();
    } catch (e) {
      console.error("[poller] selectDevice failed:", e.message);
    }
    return null;
  }

  async rescanDevices() {
    return this.post("/devices/rescan");
  }
}

module.exports = CDPoller;
