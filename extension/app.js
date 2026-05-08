"use strict";
/**
 * Roon CD Player Extension — Main entry point
 *
 * Connects to Roon Core via mDNS auto-discovery.
 * Polls Python backend for CD status.
 * Exposes transport controls + settings in Roon UI.
 */

const path = require("path");
const fs = require("fs");
const fetch = require("node-fetch");

const RoonApi = require("node-roon-api");
const RoonApiTransport = require("node-roon-api-transport");
const RoonApiImage = require("node-roon-api-image");
const RoonApiSettings = require("node-roon-api-settings");

const CDPoller = require("./poller");
const RoonTransportManager = require("./transport");

// ---------------------------------------------------------------------------
// Paths & constants
// ---------------------------------------------------------------------------
const BACKEND_URL = process.env.CD_BACKEND_URL || "http://localhost:8000";
const SETTINGS_PATH = path.join(
  process.env.HOME || "/home/cdplayer",
  ".config/cdpcore-extension/settings.json"
);
const STATE_PATH    = "/var/cache/cd-player/roon-state.json";
const ROON_RETRY_MS = 5000;

// ---------------------------------------------------------------------------
// Settings persistence
// ---------------------------------------------------------------------------
function loadSettings() {
  const defaults = {
    zone_name:       "",
    alsa_device:     "",
    mb_lookup:       true,
    pause_on_insert: false,
    pause_on_play:   true,
    resume_on_stop:  false,
    resume_on_eject: false,
  };
  try {
    if (fs.existsSync(SETTINGS_PATH)) {
      return { ...defaults, ...JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf8")) };
    }
  } catch (e) {
    console.warn("[settings] Could not load settings:", e.message);
  }
  return defaults;
}

function saveSettings(s) {
  try {
    fs.mkdirSync(path.dirname(SETTINGS_PATH), { recursive: true });
    fs.writeFileSync(SETTINGS_PATH, JSON.stringify(s, null, 2));
  } catch (e) {
    console.error("[settings] Could not save settings:", e.message);
  }
}

function writeRoonState(paired, coreName) {
  try {
    fs.writeFileSync(STATE_PATH, JSON.stringify({
      paired,
      core_name:  coreName || null,
      updated_at: new Date().toISOString(),
    }, null, 2));
  } catch (e) {
    console.warn("[state] Could not write roon-state:", e.message);
  }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let settings = loadSettings();
let zones = [];
let currentStatus = null;
let svc_settings = null;

const poller = new CDPoller();
const transport_mgr = new RoonTransportManager();

// ---------------------------------------------------------------------------
// Roon API setup
// ---------------------------------------------------------------------------
const roon = new RoonApi({
  extension_id:    "com.local.cd-player",
  display_name:    "CDPcore",
  display_version: "1.0.0",
  publisher:       "ccarrascoc85",
  email:           "ccarrascoc85@gmail.com",
  website:         "https://github.com/ccarrascoc85/cdpcore",

  core_paired(core) {
    console.log("[roon] Paired with core:", core.display_name);
    writeRoonState(true, core.display_name);
    onCorePaired(core);
  },
  core_unpaired(core) {
    console.log("[roon] Unpaired from core:", core.display_name);
    writeRoonState(false, null);
    zones = [];
    transport_mgr.setTransport(null);
  },
});

// ---------------------------------------------------------------------------
// Services
// ---------------------------------------------------------------------------

// Settings service
svc_settings = new RoonApiSettings(roon, {
  get_settings(cb) {
    cb(buildSettingsLayout(settings));
  },

  save_settings(req, isdryrun, s) {
    let has_error = false;
    let l = buildSettingsLayout(s.values);

    if (!isdryrun && !has_error) {
      settings = s.values;
      saveSettings(settings);
      transport_mgr.setTargetZone(settings.zone_name);

      // Apply ALSA device selection if changed
      if (settings.alsa_device) {
        poller.selectDevice(settings.alsa_device)
          .then(() => poller.rescanDevices())
          .catch((e) => console.error("[settings] Device selection failed:", e.message));
      }

      svc_settings.update_settings(l);
    }
    req.send_complete(has_error ? "NotValid" : "Success", { settings: l });
  },
});

function buildSettingsLayout(values) {
  // Fetch device list synchronously-ish via cached data
  return {
    values,
    layout: [
      {
        type: "group",
        title: "CDPcore Settings",
        items: [
          {
            type: "string",
            title: "ALSA device override",
            subtitle: "Leave blank for auto-detection (e.g. hw:1,0)",
            setting: "alsa_device",
          },
          {
            type: "dropdown",
            title: "MusicBrainz metadata lookup",
            values: [
              { title: "Enabled", value: true },
              { title: "Disabled", value: false },
            ],
            setting: "mb_lookup",
          },
          {
            type: "dropdown",
            title: "Pause zone when disc is inserted",
            subtitle: "Pause the Roon zone as soon as a disc is detected",
            values: [
              { title: "Off", value: false },
              { title: "On", value: true },
            ],
            setting: "pause_on_insert",
          },
          {
            type: "dropdown",
            title: "Pause zone when CD plays",
            subtitle: "Pause the Roon zone when playback starts",
            values: [
              { title: "On", value: true },
              { title: "Off", value: false },
            ],
            setting: "pause_on_play",
          },
          {
            type: "dropdown",
            title: "Resume zone after CD stops",
            subtitle: "Resume the Roon zone when playback is stopped",
            values: [
              { title: "Off", value: false },
              { title: "On", value: true },
            ],
            setting: "resume_on_stop",
          },
          {
            type: "dropdown",
            title: "Resume zone after disc is ejected",
            subtitle: "Resume the Roon zone when the disc is ejected",
            values: [
              { title: "Off", value: false },
              { title: "On", value: true },
            ],
            setting: "resume_on_eject",
          },
        ],
      },
    ],
    has_error: false,
  };
}

// Transport service
const svc_transport = new RoonApiTransport(roon);

// Image service
const svc_image = new RoonApiImage(roon);

roon.init_services({
  required_services:  [RoonApiTransport],
  optional_services:  [RoonApiImage],
  provided_services:  [svc_settings],
});

// ---------------------------------------------------------------------------
// Core paired handler
// ---------------------------------------------------------------------------
function onCorePaired(core) {
  const transport = core.services.RoonApiTransport;
  transport_mgr.setTransport(transport);
  transport_mgr.setTargetZone(settings.zone_name);

  transport.subscribe_zones((cmd, data) => {
    if (cmd === "Subscribed") {
      zones = data.zones || [];
    } else if (cmd === "Changed") {
      if (data.zones_added)   zones = [...zones, ...data.zones_added];
      if (data.zones_removed) {
        const removedIds = new Set(data.zones_removed.map((z) => z.zone_id));
        zones = zones.filter((z) => !removedIds.has(z.zone_id));
      }
      if (data.zones_changed) {
        data.zones_changed.forEach((changed) => {
          const idx = zones.findIndex((z) => z.zone_id === changed.zone_id);
          if (idx >= 0) zones[idx] = changed; else zones.push(changed);
        });
      }
    }
  });
}

// ---------------------------------------------------------------------------
// Poller event handlers
// ---------------------------------------------------------------------------
poller.on("status", (status) => {
  currentStatus = status;
});

poller.on("discLoaded", async (status) => {
  console.log(`[cd] Disc loaded: '${status.album}' by '${status.artist}'`);
  if (settings.pause_on_insert) await transport_mgr.pauseRoonZone(zones);
});

poller.on("playing", async (status) => {
  console.log(`[cd] Playing: Track ${status.track_number} — ${status.track_title}`);
  if (settings.pause_on_play) await transport_mgr.pauseRoonZone(zones);
});

poller.on("discStopped", async (_status) => {
  console.log("[cd] Playback stopped");
  if (settings.resume_on_stop) await transport_mgr.resumeRoonZone(zones);
});

poller.on("discEjected", async (_status) => {
  console.log("[cd] Disc ejected");
  if (settings.resume_on_eject) await transport_mgr.resumeRoonZone(zones);
});

poller.on("offline", () => {
  console.warn("[cd] Backend offline — retrying...");
  currentStatus = null;
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
console.log("[cd-player] Starting Roon CD Player Extension v1.0.0");
console.log(`[cd-player] Backend: ${BACKEND_URL}`);
console.log(`[cd-player] Settings: ${SETTINGS_PATH}`);

// Reload settings automatically when the file is changed externally (e.g. from /system/roon)
fs.watchFile(SETTINGS_PATH, { interval: 2000 }, () => {
  const updated = loadSettings();
  const prevZone = settings.zone_name;
  settings = updated;
  if (settings.zone_name !== prevZone) {
    transport_mgr.setTargetZone(settings.zone_name);
    console.log(`[settings] Zone updated to: "${settings.zone_name}"`);
  }
  if (svc_settings) svc_settings.update_settings(buildSettingsLayout(settings));
  console.log("[settings] Reloaded from disk");
});

// Start polling the Python backend
poller.start();

// Start Roon discovery (auto-retry built in via mDNS)
function startRoon() {
  try {
    roon.start_discovery();
    console.log("[roon] Discovery started — waiting for Roon Core...");
  } catch (e) {
    console.error("[roon] Discovery failed:", e.message);
    setTimeout(startRoon, ROON_RETRY_MS);
  }
}

startRoon();

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------
process.on("SIGTERM", () => {
  console.log("[cd-player] SIGTERM — shutting down");
  poller.stop();
  process.exit(0);
});

process.on("SIGINT", () => {
  poller.stop();
  process.exit(0);
});
