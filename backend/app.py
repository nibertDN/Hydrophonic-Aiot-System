"""Smart AI Irrigation - Flask backend (API + optional static dashboard hosting)."""
import hmac
import logging
import os
import time
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

from service import Monitor
from store import MemoryStore, StoreError, SupabaseStore, iso
from validation import valid_device_id

log = logging.getLogger("irrigation.app")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

RANGES = {"1h": (3600, 30), "6h": (21600, 120), "24h": (86400, 300), "7d": (604800, 1800)}


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config(overrides=None):
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    e = os.environ.get
    cfg = {
        "STORE_BACKEND": e("STORE_BACKEND", "supabase"),
        "SUPABASE_URL": e("SUPABASE_URL", ""),
        "SUPABASE_SERVICE_KEY": e("SUPABASE_SERVICE_KEY", ""),
        "SUPABASE_TABLE": e("SUPABASE_TABLE", "sensor_readings"),
        "MQTT_ENABLED": _bool(e("MQTT_ENABLED", "true")),
        "MQTT_HOST": e("MQTT_HOST", "127.0.0.1"),
        "MQTT_PORT": int(e("MQTT_PORT", "1883")),
        "MQTT_USERNAME": e("MQTT_USERNAME", ""),
        "MQTT_PASSWORD": e("MQTT_PASSWORD", ""),
        "MQTT_TLS": _bool(e("MQTT_TLS", "false")),
        "MQTT_CA_CERT": e("MQTT_CA_CERT", ""),
        "MQTT_TOPIC_ROOT": e("MQTT_TOPIC_ROOT", "irrigation"),
        "DEFAULT_DEVICE_ID": e("DEFAULT_DEVICE_ID", "device01"),
        "SOIL_THRESHOLD": int(e("SOIL_THRESHOLD", "3300")),
        "OFFLINE_AFTER": int(e("OFFLINE_AFTER_SECONDS", "30")),
        "STORE_INTERVAL": int(e("STORE_INTERVAL_SECONDS", "10")),
        "ADMIN_TOKEN": e("ADMIN_TOKEN", ""),
        "ALLOWED_ORIGINS": [o.strip().rstrip("/") for o in e("ALLOWED_ORIGINS", "").split(",") if o.strip()],
        "SERVE_FRONTEND": _bool(e("SERVE_FRONTEND", "true")),
    }
    cfg.update(overrides or {})
    return cfg


def create_app(overrides=None, store=None):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(overrides)
    if store is None:
        store = MemoryStore() if cfg["STORE_BACKEND"] == "memory" else SupabaseStore(
            cfg["SUPABASE_URL"], cfg["SUPABASE_SERVICE_KEY"], cfg["SUPABASE_TABLE"])
    monitor = Monitor(cfg, store)
    monitor.start_watchdog()
    if cfg["MQTT_ENABLED"]:
        from mqtt_client import start_mqtt
        monitor.mqtt_client = start_mqtt(cfg, monitor)
    if store.name == "memory":
        log.warning("Using the IN-MEMORY store: data is lost on restart (testing only).")

    app = Flask(__name__, static_folder=None)
    app.config["MONITOR"], app.config["CFG"] = monitor, cfg
    failures = {}  # ip -> [timestamps] for admin brute-force protection

    # ---------- helpers ----------
    def device_arg():
        dev = request.args.get("device_id", cfg["DEFAULT_DEVICE_ID"])
        if not valid_device_id(dev):
            raise ValueError("invalid device_id")
        return dev

    def public_reading(row):
        return {k: v for k, v in row.items() if k != "raw"}

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            token = cfg["ADMIN_TOKEN"]
            if len(token) < 16:
                return jsonify(error="Admin interface disabled: set ADMIN_TOKEN (16+ characters) on the server."), 503
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
            recent = [t for t in failures.get(ip, []) if time.time() - t < 900]
            failures[ip] = recent
            if len(recent) >= 10:
                return jsonify(error="Too many failed attempts. Try again in 15 minutes."), 429
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                failures[ip].append(time.time())
                return jsonify(error="Invalid admin token."), 401
            return fn(*a, **kw)
        return wrapper

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(StoreError)
    def db_down(exc):
        monitor._note_db_error(exc)
        return jsonify(error="Database temporarily unavailable.", detail=str(exc)), 503

    @app.after_request
    def headers(resp):
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and ("*" in cfg["ALLOWED_ORIGINS"] or origin in cfg["ALLOWED_ORIGINS"]):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, OPTIONS"
            resp.headers["Vary"] = "Origin"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- public read API ----------
    @app.get("/api/health")
    def health():
        return jsonify(ok=True, time=iso(time.time()))

    @app.get("/api/devices")
    def devices():
        return jsonify(devices=monitor.known_devices(), default=cfg["DEFAULT_DEVICE_ID"])

    @app.get("/api/latest")
    def latest():
        dev = device_arg()
        row, seen = monitor.latest(dev)
        st = monitor.settings(dev)
        return jsonify(device_id=dev, online=monitor.is_online(dev),
                       age_seconds=None if seen is None else round(time.time() - seen, 1),
                       simulated=bool(row and row["is_simulated"]),
                       soil_threshold=st["soil_threshold"],
                       reading=public_reading(row) if row else None)

    @app.get("/api/status")
    def status():
        dev = device_arg()
        d = monitor.devices.get(dev, {})
        try:
            monitor.store.ping()
            monitor.db_ok = True
        except StoreError as exc:
            monitor._note_db_error(exc)
        return jsonify(
            device_id=dev, device_online=monitor.is_online(dev),
            device_last_seen=iso(d["last_seen"]) if d.get("last_seen") else None,
            device_lwt=d.get("lwt"),
            mqtt_connected=monitor.mqtt_connected, mqtt_enabled=cfg["MQTT_ENABLED"],
            database={"backend": store.name, "ok": monitor.db_ok, "pending_writes": len(monitor.pending)},
            counters=monitor.counters, last_error=monitor.last_error, server_time=iso(time.time()),
            capabilities={"pump_installed": False, "irrigation_automation": False,
                          "note": "Monitoring only. No pump/relay hardware is installed or controlled."})

    @app.get("/api/history")
    def history():
        dev = device_arg()
        rng = request.args.get("range", "24h")
        if rng not in RANGES:
            raise ValueError("range must be one of " + ", ".join(RANGES))
        span, bucket = RANGES[rng]
        pts = monitor.store.history_buckets(dev, time.time() - span, bucket)
        n = sum(p["n"] for p in pts)
        summary = {"samples": n}
        if n:
            summary.update(
                soil_min=min(p["soil_min"] for p in pts), soil_max=max(p["soil_max"] for p in pts),
                soil_avg=round(sum(p["soil_avg"] * p["n"] for p in pts) / n, 1),
                percent_dry=round(100 * sum(p["soil_dry_ratio"] * p["n"] for p in pts) / n, 1),
                percent_water_low=round(100 * sum(p["water_low_ratio"] * p["n"] for p in pts) / n, 1))
        return jsonify(device_id=dev, range=rng, bucket_seconds=bucket, points=pts, summary=summary,
                       soil_threshold=monitor.settings(dev)["soil_threshold"])

    @app.get("/api/alerts")
    def alerts():
        dev = device_arg()
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
        row, _ = monitor.latest(dev)
        active = []
        if row and monitor.is_online(dev):
            if row["water_low"]:
                active.append("water_low")
            if row["soil_dry"]:
                active.append("soil_dry")
        if monitor.devices.get(dev, {}).get("offline_raised"):
            active.append("device_offline")
        return jsonify(device_id=dev, active=active, events=monitor.store.alerts(dev, limit))

    @app.get("/api/readings")
    def readings():
        dev = device_arg()
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
        return jsonify(device_id=dev, readings=[public_reading(r) for r in monitor.store.recent_readings(dev, limit)])

    # ---------- admin API (token protected) ----------
    @app.get("/api/admin/settings")
    @admin_required
    def get_settings():
        dev = device_arg()
        return jsonify(device_id=dev, settings=monitor.settings(dev),
                       note="These values affect backend analysis and the dashboard only. "
                            "The ESP32 firmware threshold is set in the .ino file.")

    @app.put("/api/admin/settings")
    @admin_required
    def put_settings():
        dev = device_arg()
        body = request.get_json(silent=True) or {}
        values = {}
        for key, lo, hi in (("soil_threshold", 0, 4095), ("offline_after_seconds", 10, 3600)):
            if key in body:
                v = body[key]
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    raise ValueError(f"{key} must be an integer between {lo} and {hi}")
                values[key] = v
        if not values:
            raise ValueError("nothing to update")
        monitor.store.save_settings(dev, values)
        monitor.invalidate_settings()
        return jsonify(device_id=dev, settings=monitor.settings(dev))

    @app.get("/api/admin/pump")
    @admin_required
    def pump_state():
        return jsonify(installed=False, implemented=False,
                       message="Pump and relay are not installed. Manual pump control is not implemented yet.")

    @app.post("/api/admin/pump")
    @admin_required
    def pump_command():
        return jsonify(error="Pump control is not implemented: no pump or relay hardware is installed.",
                       implemented=False), 501

    # ---------- static dashboard ----------
    if cfg["SERVE_FRONTEND"] and FRONTEND_DIR.exists():
        @app.get("/")
        def index():
            return send_from_directory(FRONTEND_DIR, "index.html")

        @app.get("/<path:path>")
        def static_files(path):
            if path.startswith("api/"):
                return jsonify(error="not found"), 404
            return send_from_directory(FRONTEND_DIR, path)

    return app
