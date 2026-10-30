(() => {
  "use strict";
  const API = ((window.APP_CONFIG && window.APP_CONFIG.API_BASE) || "").replace(/\/$/, "");
  const DEVICE = (window.APP_CONFIG && window.APP_CONFIG.DEFAULT_DEVICE_ID) || "device01";
  const $ = (id) => document.getElementById(id);
  let token = ""; // memory only

  async function call(path, opts = {}) {
    const r = await fetch(API + path, { ...opts, headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    return body;
  }
  const msg = (id, text, ok) => { $(id).textContent = text; $(id).className = "msg " + (ok ? "good" : "err"); };

  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    token = $("token").value;
    $("token").value = "";
    try {
      const s = await call("/api/admin/settings?device_id=" + DEVICE);
      $("devName").textContent = s.device_id;
      $("threshold").value = s.settings.soil_threshold;
      $("offlineAfter").value = s.settings.offline_after_seconds;
      const p = await call("/api/admin/pump");
      $("pumpMsg").textContent = p.message;
      $("settingsPanel").classList.remove("hidden");
      $("pumpPanel").classList.remove("hidden");
      msg("loginMsg", "Signed in.", true);
    } catch (err) { token = ""; msg("loginMsg", err.message, false); }
  });

  $("settingsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await call("/api/admin/settings?device_id=" + DEVICE, { method: "PUT", body: JSON.stringify({
        soil_threshold: parseInt($("threshold").value, 10), offline_after_seconds: parseInt($("offlineAfter").value, 10) }) });
      msg("settingsMsg", "Settings saved.", true);
    } catch (err) { msg("settingsMsg", err.message, false); }
  });
})();
