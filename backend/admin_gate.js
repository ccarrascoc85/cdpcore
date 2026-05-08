// CDPcore — admin gate for /system pages.
// Loaded as <script src="/admin_gate.js"></script> in the <head> of each
// admin HTML shell. Intercepts window.fetch, shows a PIN modal on 401, and
// proactively probes /admin/status on load so the user is prompted before
// the page fires its data calls.
(function () {
  "use strict";

  const STATUS_URL = "/admin/status";
  const UNLOCK_URL = "/admin/unlock";

  let modalOpen = false;
  let queued = [];       // resolvers waiting for unlock
  const origFetch = window.fetch.bind(window);

  // Hide the admin surface until the probe confirms the session. The script
  // is loaded synchronously in <head> so this style lands before <body> is
  // parsed — no flash of empty admin cards behind the PIN modal.
  (function installGateGuard() {
    const s = document.createElement("style");
    s.id = "cdp-admin-gate-guard";
    s.textContent =
      "html.cdp-gated body { visibility: hidden; }" +
      "html.cdp-gated .cdp-gate-backdrop { visibility: visible; }";
    (document.head || document.documentElement).appendChild(s);
    document.documentElement.classList.add("cdp-gated");
  })();

  function reveal() {
    document.documentElement.classList.remove("cdp-gated");
  }

  function isAdminPath(url) {
    try {
      const path = new URL(url, window.location.origin).pathname;
      if (path === "/admin/unlock" || path === "/admin/status" || path === "/admin/lock") {
        return false;
      }
      return path.startsWith("/system");
    } catch {
      return false;
    }
  }

  function injectStyles() {
    if (document.getElementById("cdp-admin-gate-css")) return;
    const s = document.createElement("style");
    s.id = "cdp-admin-gate-css";
    s.textContent = `
      .cdp-gate-backdrop {
        position: fixed; inset: 0; background: rgba(0,0,0,.82);
        display: flex; align-items: center; justify-content: center;
        z-index: 99999; font-family: system-ui, -apple-system, sans-serif;
      }
      .cdp-gate {
        background: #111008; color: #c8c0ac;
        border: 1px solid #2a2820; border-radius: 6px;
        padding: 26px 24px; width: min(360px, 90vw);
        box-shadow: 0 10px 40px rgba(0,0,0,.6);
      }
      .cdp-gate h3 {
        margin: 0 0 12px; color: #e8a028; font-weight: 500;
        font-size: 13px; letter-spacing: .12em; text-transform: uppercase;
      }
      .cdp-gate p { margin: 0 0 14px; font-size: 12px; color: #6a6050; line-height: 1.5; }
      .cdp-gate input {
        width: 100%; padding: 12px 14px; background: #04060a;
        border: 1px solid #28241c; color: #e8a028; font-size: 18px;
        letter-spacing: .4em; text-align: center; border-radius: 4px;
        font-family: "Menlo", monospace; outline: none;
      }
      .cdp-gate input:focus { border-color: #e8a028; }
      .cdp-gate-row { display: flex; gap: 10px; margin-top: 14px; }
      .cdp-gate button {
        flex: 1; padding: 10px; background: #1c1a14;
        border: 1px solid #28241c; color: #c8c0ac; cursor: pointer;
        font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
        border-radius: 4px; font-family: inherit;
      }
      .cdp-gate button.primary { color: #e8a028; border-color: #e8a028; }
      .cdp-gate button:hover { background: #28241c; }
      .cdp-gate-err {
        color: #b83c3c; font-size: 12px; min-height: 16px;
        margin-top: 8px; text-align: center;
      }
    `;
    document.head.appendChild(s);
  }

  function showModal() {
    if (modalOpen) return;
    modalOpen = true;
    injectStyles();

    const wrap = document.createElement("div");
    wrap.className = "cdp-gate-backdrop";
    wrap.innerHTML = `
      <div class="cdp-gate" role="dialog" aria-modal="true">
        <h3>Admin PIN</h3>
        <p>This page manages the appliance. Enter the 6-digit admin PIN to continue.</p>
        <input type="password" inputmode="numeric" pattern="[0-9]*"
               maxlength="6" autocomplete="off" id="cdp-gate-input" />
        <div class="cdp-gate-err" id="cdp-gate-err">&nbsp;</div>
        <div class="cdp-gate-row">
          <button type="button" id="cdp-gate-cancel">Cancel</button>
          <button type="button" class="primary" id="cdp-gate-submit">Unlock</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    const input = wrap.querySelector("#cdp-gate-input");
    const err   = wrap.querySelector("#cdp-gate-err");
    input.focus();

    async function submit() {
      const pin = (input.value || "").trim();
      if (!pin) return;
      err.textContent = " ";
      try {
        const r = await origFetch(UNLOCK_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pin }),
          credentials: "same-origin",
        });
        if (r.ok) {
          close(true);
        } else {
          err.textContent = "Incorrect PIN.";
          input.value = "";
          input.focus();
        }
      } catch {
        err.textContent = "Network error.";
      }
    }

    // Cancel leaves the admin surface entirely. Staying on /system/* with no
    // PIN just causes every data fetch to 401 and re-open this modal, which
    // feels like a loop to the user. Sending them back to the player keeps
    // the "no PIN, no admin" decision visible and final.
    function cancel() {
      close(false);
      window.location.href = "/";
    }

    function close(ok) {
      modalOpen = false;
      wrap.remove();
      if (ok) reveal();
      const fns = queued; queued = [];
      fns.forEach((fn) => fn(ok));
    }

    wrap.querySelector("#cdp-gate-submit").addEventListener("click", submit);
    wrap.querySelector("#cdp-gate-cancel").addEventListener("click", cancel);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
    });
  }

  function waitForUnlock() {
    return new Promise((resolve) => {
      queued.push(resolve);
      showModal();
    });
  }

  // Intercept fetch: retry once after unlock on 401 for admin paths.
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const resp = await origFetch(input, init);
    if (resp.status === 401 && isAdminPath(url)) {
      const ok = await waitForUnlock();
      if (ok) return origFetch(input, init);
    }
    return resp;
  };

  // Proactive check so the user sees the modal immediately instead of
  // waiting for the first data call to 401 (which can feel laggy).
  // Also intercepts the pre-setup state: on a fresh appliance, bounce to
  // /setup instead of showing a modal the user can't clear.
  function probe() {
    origFetch("/admin/setup_required", { credentials: "same-origin" })
      .then((r) => r.ok ? r.json() : null)
      .then((d) => {
        if (d && d.required) {
          // Navigating away — keep the surface hidden so the empty shell
          // doesn't flash before the welcome screen mounts.
          window.location.replace("/setup");
          return null;
        }
        return origFetch(STATUS_URL, { credentials: "same-origin" })
          .then((r) => r.ok ? r.json() : null);
      })
      .then((data) => {
        if (!data) { reveal(); return; }         // status unavailable — let page load
        if (data.mode === "off") { reveal(); return; }
        if (data.unlocked) { reveal(); return; }
        showModal();                              // stay hidden behind the modal
      })
      .catch(() => { reveal(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", probe);
  } else {
    probe();
  }
})();
