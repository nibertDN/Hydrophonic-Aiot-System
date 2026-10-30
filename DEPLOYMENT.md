# Cloud deployment (public HTTPS)

Goal: dashboard and API on HTTPS, ESP32 talking to a **cloud MQTT broker over TLS**, and your local Mosquitto (`192.168.8.35`) left **private on your LAN**. Do not port-forward 1883.

```
ESP32 ──TLS :8883──▶ Cloud broker ◀──TLS── Flask (hosted, HTTPS) ──▶ Supabase
                                                  ▲
                                      Browser ────┘  (dashboard served by Flask, or a separate static host)
```

Provider names below are examples. I have not tested your accounts, and free-tier limits change, so check each provider's current terms.

## 1. Cloud MQTT broker with TLS (example: HiveMQ Cloud)

1. Create a cluster. Note the host (`xxxx.s1.eu.hivemq.cloud`) and TLS port **8883**.
2. Create two credentials with least privilege:
   - `irrigation_device`: publish to `irrigation/device01/#` only.
   - `irrigation_backend`: subscribe to `irrigation/#`.
3. Choose strong, different passwords. Never reuse your local broker password.

HiveMQ Cloud certificates chain to **ISRG Root X1**; EMQX Cloud commonly uses **DigiCert Global Root G2**. Both are already in `firmware/irrigation_esp32/root_ca.h`. If your provider uses a different CA, replace the PEM there.

## 2. Point the ESP32 at the cloud broker

In `secrets.h`:

```cpp
#define MQTT_HOST     "xxxx.s1.eu.hivemq.cloud"
#define MQTT_PORT     8883
#undef  MQTT_USE_TLS
#define MQTT_USE_TLS  1
#define MQTT_USERNAME "irrigation_device"
#define MQTT_PASSWORD "…"
```

Re-upload. Serial should print `MQTT connected`. The sketch syncs its clock by NTP so certificate dates validate.

## 3. Host the Flask backend

Needs: Python 3.10+, **exactly one worker process** (each worker would run its own MQTT subscriber), and an instance that **stays awake**. Many free tiers sleep when idle, which would stop MQTT ingestion and create data gaps, so use an always-on plan.

**Render (blueprint provided):**
1. Push this folder to a private GitHub repo (`.env` is git-ignored; check `git status` shows no secrets).
2. Render → New → Blueprint → select the repo (`render.yaml`). Choose an always-on plan.
3. Fill in the secret variables (marked `sync: false`):

| Variable | Value |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | from Supabase → Project Settings → API |
| `MQTT_HOST` | cloud broker host |
| `MQTT_PORT` / `MQTT_TLS` | `8883` / `true` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | `irrigation_backend` credentials |
| `ADMIN_TOKEN` | 16+ random characters |
| `ALLOWED_ORIGINS` | only if the frontend is on a separate domain (step 4b) |

Render provides HTTPS automatically. Any host that runs `gunicorn wsgi:app --workers 1 --threads 4` from the `backend/` folder works (see `Procfile`) on Railway, Fly.io or a VPS. On a VPS, put Caddy or nginx with Let's Encrypt in front.

**Check it:** `https://YOUR-APP/api/health` returns `{"ok":true}`; `https://YOUR-APP/api/status` shows `"mqtt_connected": true` and `database.ok: true`.

## 4. Dashboard

**a) Simplest – served by the backend.** With `SERVE_FRONTEND=true` the dashboard is at the same HTTPS URL. No CORS, no extra config.

**b) Separate static host (Netlify, Cloudflare Pages, Vercel).**
1. Deploy the `frontend/` folder.
2. Edit `frontend/config.js`: `API_BASE: "https://YOUR-APP.onrender.com"` (public, non-secret).
3. Edit `frontend/_headers` and replace `YOUR-BACKEND.onrender.com` in `connect-src`.
4. On the backend set `ALLOWED_ORIGINS=https://your-site.netlify.app` and set `SERVE_FRONTEND=false`.

## 5. Go-live checklist

- [ ] SQL scripts 01 and 02 run; RLS shows `true` for all three tables.
- [ ] `.env` and `secrets.h` are not in Git. Rotate any key that was ever committed or pasted in chat.
- [ ] Backend uses the **service key**; no Supabase key appears in `frontend/`.
- [ ] Cloud broker credentials are separate from the local ones; the ESP32 user can only publish.
- [ ] Local Mosquitto is not reachable from the internet (no router port-forward).
- [ ] ESP32 online, dashboard shows live non-simulated readings, and no purple banner.
- [ ] `ADMIN_TOKEN` is set and tested at `/admin.html`.
- [ ] Optional: run `supabase/03_optional_retention.sql` to prune old rows.

## Still needs you or hardware

Supabase key, broker account and passwords, hosting account, a flashed ESP32 with sensors, and (for the future) the pump, relay and a driver circuit. Irrigation control is intentionally **not implemented**.
