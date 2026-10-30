(() => {
  "use strict";
  const API = ((window.APP_CONFIG && window.APP_CONFIG.API_BASE) || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const S = { device: null, range: "24h", threshold: 3300, charts: {} };
  const fmtTime = (iso) => new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });

  async function api(path) {
    const r = await fetch(API + path, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "HTTP " + r.status);
    return r.json();
  }
  const q = (path, extra = "") => `${path}?device_id=${encodeURIComponent(S.device)}${extra}`;
  const setPill = (id, ok, text) => {
    $(id).classList.toggle("ok", ok); $(id).classList.toggle("bad", !ok); $(id + "Text").textContent = text;
  };
  const setBadge = (el, cls, text) => { el.className = "badge " + cls; el.textContent = text; };
  const ago = (s) => (s == null ? "never" : s < 90 ? Math.round(s) + " s ago" : s < 5400 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago");
  const uptimeText = (s) => (s == null ? "–" : s >= 86400 ? Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h" : s >= 3600 ? Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min" : Math.floor(s / 60) + " min");

  // ---------- live ----------
  async function refreshLatest() {
    try {
      const d = await api(q("/api/latest"));
      setPill("pillBackend", true, "connected");
      S.threshold = d.soil_threshold;
      renderLatest(d);
    } catch (e) {
      setPill("pillBackend", false, "unreachable");
      $("bannerStale").textContent = "Cannot reach the backend (" + e.message + "). Showing the last known values; retrying automatically.";
      $("bannerStale").classList.remove("hidden");
    }
  }

  function renderLatest(d) {
    const r = d.reading;
    $("bannerSim").classList.toggle("hidden", !d.simulated);
    setPill("pillDevice", d.online, d.online ? "online" : "offline");
    setBadge($("devBadge"), d.online ? "ok" : "bad", d.online ? "Online" : "Offline");
    $("liveCards").classList.toggle("stale", !d.online && !!r);
    const stale = $("bannerStale");
    if (!r) { stale.textContent = "No readings received from this device yet. Check that the ESP32 is powered and connected to the MQTT broker."; stale.classList.remove("hidden"); }
    else if (!d.online) { stale.textContent = "Device is offline. Values below are the last known readings from " + fmtTime(r.created_at) + "."; stale.classList.remove("hidden"); }
    else stale.classList.add("hidden");
    $("lastSeen").textContent = d.age_seconds == null ? "never" : ago(d.age_seconds);
    if (!r) return;

    $("soilValue").textContent = r.soil_value;
    setBadge($("soilBadge"), r.soil_dry ? "warn" : "ok", r.soil_dry ? "Dry" : "Moisture OK");
    $("soilFill").style.width = (100 - (r.soil_value / 4095) * 100) + "%";
    const mark = $("soilMark");
    mark.style.left = (S.threshold / 4095) * 100 + "%";
    $("soilMarkLabel").textContent = "dry ≥ " + S.threshold;

    setBadge($("waterBadge"), r.water_low ? "bad" : "ok", r.water_low ? "Low" : "Sufficient");
    $("tankFill").style.height = r.water_low ? "14%" : "78%";
    $("tankFill").parentElement.classList.toggle("low", r.water_low);
    $("waterText").textContent = r.water_low ? "Low water – refill the tank" : "Water level sufficient";

    for (const [id, key] of [["ledWaterRed", "water_red_led"], ["ledWaterGreen", "water_green_led"], ["ledSoilRed", "soil_red_led"], ["ledSoilGreen", "soil_green_led"]])
      $(id).classList.toggle("on", !!r[key]);
    $("buzzer").classList.toggle("on", !!r.buzzer_on);
    $("buzzerText").textContent = r.buzzer_on ? "Buzzer sounding" : "Buzzer off";
    $("rssi").textContent = r.rssi == null ? "–" : r.rssi + " dBm";
    $("uptime").textContent = uptimeText(r.uptime_s);
  }

  async function refreshStatus() {
    try {
      const s = await api(q("/api/status"));
      $("mqttState").textContent = s.mqtt_enabled ? (s.mqtt_connected ? "connected" : "reconnecting…") : "disabled";
      const db = s.database;
      $("dbState").textContent = db.ok === false ? "unavailable (" + db.pending_writes + " writes queued)" : "connected";
    } catch (e) { /* the backend pill already shows the problem */ }
  }

  // ---------- history ----------
  const label = (iso) => { const d = new Date(iso); return S.range === "7d" ? d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };

  function chart(id, cfg) {
    if (S.charts[id]) { S.charts[id].destroy(); }
    S.charts[id] = new Chart($(id), cfg);
  }

  async function refreshHistory() {
    let h;
    try { h = await api(q("/api/history", "&range=" + S.range)); } catch (e) { return; }
    const pts = h.points, s = h.summary;
    const stat = (v, l) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`;
    $("stats").innerHTML = s.samples
      ? stat(s.soil_avg, "average soil reading") + stat(s.soil_min + " – " + s.soil_max, "min – max") + stat(s.percent_dry + "%", "time soil was dry") + stat(s.percent_water_low + "%", "time water was low") + stat(s.samples, "stored readings")
      : '<div class="empty">No stored readings in this period yet.</div>';
    const labels = pts.map((p) => label(p.bucket));
    const common = { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } }, scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } } } };
    chart("chartSoil", { type: "line", data: { labels, datasets: [
      { label: "Soil reading", data: pts.map((p) => Math.round(p.soil_avg)), borderColor: "#3e7b4f", backgroundColor: "rgba(95,165,106,.18)", fill: true, tension: .25, pointRadius: 0, borderWidth: 2 },
      { label: "Dry threshold (" + h.soil_threshold + ")", data: pts.map(() => h.soil_threshold), borderColor: "#c2443a", borderDash: [6, 5], pointRadius: 0, borderWidth: 1.5, fill: false } ] },
      options: { ...common, scales: { ...common.scales, y: { min: 0, max: 4095, title: { display: true, text: "ADC value (higher = drier)" } } } } });
    chart("chartAlerts", { type: "bar", data: { labels, datasets: [
      { label: "Soil dry", data: pts.map((p) => Math.round(p.soil_dry_ratio * 100)), backgroundColor: "#c98a12" },
      { label: "Water low", data: pts.map((p) => Math.round(p.water_low_ratio * 100)), backgroundColor: "#4a9fcf" } ] },
      options: { ...common, scales: { ...common.scales, y: { min: 0, max: 100, title: { display: true, text: "% of readings" } } } } });
    renderTrend(pts, h.soil_threshold);
  }

  // Simple least-squares slope over the most recent buckets. A heuristic, not a trained model.
  function renderTrend(pts, thr) {
    const recent = pts.slice(-12);
    const el = $("trendText");
    if (recent.length < 4) { el.textContent = ""; return; }
    const t = recent.map((p) => new Date(p.bucket).getTime() / 3.6e6), y = recent.map((p) => p.soil_avg);
    const n = t.length, mt = t.reduce((a, b) => a + b) / n, my = y.reduce((a, b) => a + b) / n;
    const den = t.reduce((a, b) => a + (b - mt) ** 2, 0);
    if (!den) { el.textContent = ""; return; }
    const slope = t.reduce((a, b, i) => a + (b - mt) * (y[i] - my), 0) / den; // units per hour
    const last = y[n - 1];
    let msg = "Trend (simple linear estimate, not a forecast): ";
    if (Math.abs(slope) < 5) msg += "soil reading is stable.";
    else if (slope > 0) msg += last >= thr ? "soil is already dry and still drying." : `soil is drying by about ${Math.round(slope)} units/hour; it could reach the dry threshold in roughly ${Math.max(0.1, (thr - last) / slope).toFixed(1)} h.`;
    else msg += "soil is getting wetter.";
    el.textContent = msg;
  }

  // ---------- alerts + logs ----------
  const NAMES = { water_low: "Water level", soil_dry: "Soil moisture", device_offline: "Device connection" };

  async function refreshAlerts() {
    try {
      const a = await api(q("/api/alerts", "&limit=40"));
      $("activeAlerts").textContent = a.active.length ? "Active now: " + a.active.map((x) => NAMES[x]).join(", ") : "No active alerts.";
      const ol = $("alertList");
      ol.replaceChildren();
      if (!a.events.length) { ol.innerHTML = '<li class="empty">No alerts recorded yet.</li>'; return; }
      for (const e of a.events) {
        const li = document.createElement("li");
        li.className = e.state + " " + e.severity;
        const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = e.state === "raised" ? "ALERT" : "CLEARED";
        const msg = document.createElement("span"); msg.textContent = e.message;
        if (e.is_simulated) { const s = document.createElement("span"); s.className = "tag sim-tag"; s.textContent = "SIMULATED"; msg.append(s); }
        const t = document.createElement("span"); t.className = "t"; t.textContent = fmtTime(e.created_at);
        li.append(tag, msg, t); ol.append(li);
      }
    } catch (e) { /* keep previous list */ }
  }

  async function refreshLogs() {
    try {
      const d = await api(q("/api/readings", "&limit=40"));
      const body = $("logBody");
      body.replaceChildren();
      if (!d.readings.length) { body.innerHTML = '<tr><td colspan="5" class="empty">No readings yet.</td></tr>'; return; }
      for (const r of d.readings) {
        const tr = document.createElement("tr");
        const cells = [fmtTime(r.created_at) + (r.is_simulated ? " (SIMULATED)" : ""), r.soil_value, r.soil_dry ? "Dry" : "OK", r.water_low ? "Low" : "Sufficient", r.buzzer_on ? "On" : "Off"];
        cells.forEach((c, i) => { const td = document.createElement("td"); td.textContent = c; if ((i === 2 && r.soil_dry) || (i === 3 && r.water_low)) td.className = "bad"; if (i === 0 && r.is_simulated) td.className = "sim"; tr.append(td); });
        body.append(tr);
      }
    } catch (e) { /* keep previous table */ }
  }

  // ---------- init ----------
  async function loadDevices() {
    let list = ["device01"], def = "device01";
    try { const d = await api("/api/devices"); list = d.devices; def = d.default; } catch (e) { /* use defaults */ }
    const sel = $("deviceSelect");
    sel.replaceChildren(...list.map((id) => new Option(id.startsWith("sim") ? id + " (simulated)" : id, id)));
    S.device = list.includes(S.device) ? S.device : def;
    sel.value = S.device;
  }

  function refreshAll() { refreshLatest(); refreshStatus(); refreshHistory(); refreshAlerts(); refreshLogs(); }

  document.querySelectorAll(".range button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".range button").forEach((x) => x.classList.toggle("on", x === b));
    S.range = b.dataset.range; refreshHistory();
  }));
  $("deviceSelect").addEventListener("change", (e) => { S.device = e.target.value; refreshAll(); });

  loadDevices().then(() => {
    refreshAll();
    setInterval(refreshLatest, 5000);
    setInterval(refreshStatus, 15000);
    setInterval(() => { refreshAlerts(); refreshLogs(); }, 15000);
    setInterval(refreshHistory, 60000);
    setInterval(loadDevices, 120000);
  });
})();
