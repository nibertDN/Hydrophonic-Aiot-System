# Smart AI Irrigation – Field Monitor

A lightweight soil-moisture and water-level **monitoring** system:

```
ESP32 ──MQTT──▶ Mosquitto / cloud broker ──▶ Flask backend ──▶ Supabase PostgreSQL
                                                  │
                                       JSON API ──┴──▶ Dashboard (HTML/CSS/JS + Chart.js)
```

## What works today, and what does not

| Works (tested) | Not included / needs you |
|---|---|
| ESP32 firmware: soil + float switch, LEDs, buzzer, MQTT with last-will and reconnect | Flashing the ESP32 and wiring (physical hardware) |
| Backend: MQTT subscribe, validation, Supabase inserts, retry buffer during DB outages, auto-reconnect | Your Supabase service key, MQTT passwords, admin token |
| API: latest, history, status, alerts, logs | A cloud MQTT account and hosting accounts for public access |
| Dashboard: live values, LEDs/buzzer, online/offline, charts, alerts, logs | |
| Admin page with token login and editable analysis settings | |
| Clearly labelled simulator for demos | |

**Pump and relay are not installed.** There is **no irrigation automation**. The admin page shows a disabled pump panel, and the pump API returns `501 Not Implemented`. The "AI" in the name is limited to a simple trend estimate on the dashboard (a linear fit, labelled as such); there is no machine-learning model.

## Quick start (Windows)

1. Install **Python 3.10+** (tick "Add Python to PATH") and, for local MQTT, **Mosquitto**.
2. Double-click **`start_windows.bat`**. It creates a virtual environment, installs dependencies, and opens `.env` for you to fill in.
3. Open <http://localhost:5000>. Admin page: <http://localhost:5000/admin.html>.

Other commands: `start_windows.bat test` runs the tests, `start_windows.bat simulate` publishes **simulated** data.

Try it without Supabase: set `STORE_BACKEND=memory` in `.env` (data disappears on restart).

On Linux/macOS: `python -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements.txt && cp .env.example .env && cd backend && python run_server.py`

## Setup steps

### 1. Supabase (existing project)
1. In **SQL Editor**, optionally run `supabase/00_inspect_existing_table.sql` to see your table.
2. Run `supabase/01_schema.sql` (adds any missing columns to `public.sensor_readings`, creates `irrigation_alerts`, `irrigation_settings` and a chart aggregation function; it never drops anything).
3. Run `supabase/02_security_rls.sql` (enables Row Level Security everywhere; no browser access).
4. Copy the **service-role / secret key** (Project Settings → API) into `SUPABASE_SERVICE_KEY` in `.env`. This key bypasses RLS, so it lives only on the backend. Never put it in `frontend/`, firmware or Git.

**Using your existing table.** The backend writes these columns: `device_id, soil_value, soil_dry, water_low, water_red_led, water_green_led, soil_red_led, soil_green_led, buzzer_on, is_simulated, rssi, uptime_s, created_at`. I could not see your table, so the script adds whichever are missing. If you already store the soil value in a differently named column (for example `soil_moisture`), either rename it (`alter table public.sensor_readings rename column soil_moisture to soil_value;`) or keep both by letting the script add `soil_value`. If an existing column has a conflicting type or a NOT NULL constraint without a default, the first insert will fail and the dashboard's Database field and `/api/status` will show the exact error.

### 2. MQTT broker (local)
Copy `mosquitto/mosquitto.conf.example`, create users with `mosquitto_passwd` (`irrigation_device` for the ESP32 and `irrigation_backend` for the server), then start Mosquitto. Keep port 1883 on your LAN only. Optional least-privilege rules are in `mosquitto/acl.example`. Note: the simulator publishes, so run it with a write-capable user.

### 3. ESP32
1. Arduino IDE → install the ESP32 board package and the libraries **PubSubClient** and **ArduinoJson (v7)**.
2. In `firmware/irrigation_esp32/`, copy `secrets.h.example` to `secrets.h` and enter Wi-Fi and MQTT details (host `192.168.8.35`, port `1883`, user `irrigation_device`, and your password).
3. Upload `irrigation_esp32.ino` and open the Serial Monitor at 115200 baud.

Firmware assumptions to check against your existing sketch: higher ADC value = drier soil (dry at ≥ 3300); float switch HIGH = low water; LEDs and buzzer are active-HIGH, and the buzzer is an *active* buzzer that beeps while any alert is on.

### 4. Backend `.env`
Copy `.env.example` to `.env` (the Windows script does this). Required: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `MQTT_*`, and `ADMIN_TOKEN` (16+ characters; generate with `python -c "import secrets;print(secrets.token_urlsafe(32))"`).

## MQTT message format

Topic `irrigation/device01/sensors` (JSON, published every 5 s) and `irrigation/device01/status` (`online` / `offline`, retained, offline sent by the broker as last-will):

```json
{"device_id":"device01","soil":2950,"soil_dry":false,"water_low":false,
 "leds":{"water_red":false,"water_green":true,"soil_red":false,"soil_green":true},
 "buzzer":false,"rssi":-61,"uptime_s":1234,"simulated":false}
```
Only `soil` (0–4095) and `water_low` are required. If your current firmware sends a different shape, the backend also accepts `soil_value`/`soil_raw`/`soil_moisture`, `low_water`, and a raw `float` pin level (1 = low water). Missing LED/buzzer/dry fields are derived from the rules above. Invalid messages are rejected, counted, and shown under "last error" in `/api/status`. The backend stores a reading every 10 s (and immediately on any alert change) to keep the database small.

The earlier ESP32 format is also supported: `soil_raw_value` is normalized to
the raw soil reading; `water_level_status` values `HIGH`/`LOW` become
sufficient/low water; `buzzer_alert_status` values `ACTIVE`/`INACTIVE` become
the buzzer flag; and the `red_water_led`, `green_water_led`,
`red_soil_led`, and `green_soil_led` fields are mapped to the stored LED
columns. `soil_threshold_status` is validated for compatibility while the
configured raw ADC threshold remains authoritative.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/latest?device_id=` | Latest reading, online state, `simulated` flag |
| `GET /api/history?range=1h\|6h\|24h\|7d` | Bucketed averages plus summary statistics |
| `GET /api/status` | Device, broker, database health, counters, capabilities |
| `GET /api/alerts` | Active alerts and timestamped alert history |
| `GET /api/readings?limit=` | Recent sensor log |
| `GET/PUT /api/admin/settings` | Admin only (`Authorization: Bearer <ADMIN_TOKEN>`) |
| `GET/POST /api/admin/pump` | Admin only. Reports "not installed"; POST returns 501 |

## Simulated data

`simulator.py` publishes as device **`simulator01`** with `"simulated": true`. The dashboard shows a purple **SIMULATED DATA** banner and tags log and alert rows. It never writes as `device01`, so real and simulated data are not mixed.

## Security notes

- Secrets exist only in backend environment variables (`.env` is git-ignored). A test fails the build if secret-like strings appear in `frontend/`.
- RLS is enabled on all tables with no browser policies; only the backend uses the service key.
- Admin endpoints need a constant-time-compared token and lock out an IP after 10 failures in 15 minutes. The token is held in page memory only.
- Security headers and a strict CSP are set; Chart.js is bundled locally (no CDN). The dashboard's read-only API is public by design; add authentication in front of it if your readings are sensitive.
- The ESP32 verifies the broker certificate when TLS is on (no `setInsecure`).

## Project layout

```
backend/     Flask app, MQTT bridge, storage, simulator, tests
frontend/    Dashboard (index.html), admin.html, css, js, vendored Chart.js
firmware/    ESP32 sketch + secrets.h.example + root CA bundle
supabase/    SQL scripts
mosquitto/   Example broker config and ACL
DEPLOYMENT.md   Cloud (HTTPS + TLS MQTT) guide
```

## Troubleshooting

- **Device shows offline:** check `/api/status` (broker connected?), the Serial Monitor, and that the ESP32 user/password match the broker.
- **"Database unavailable" in the dashboard:** the backend keeps up to 2000 queued writes and retries every 5 s; check `SUPABASE_URL`, the key, and that you ran the SQL scripts.
- **Admin says disabled:** set `ADMIN_TOKEN` (16+ characters) and restart.
