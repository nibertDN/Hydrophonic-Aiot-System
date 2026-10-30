"""Core service: processes MQTT messages, keeps live device state, raises alerts,
stores data in the database and survives temporary database outages."""
import json
import logging
import threading
import time
from collections import deque

from store import StoreError, iso
from validation import valid_device_id, validate_payload

log = logging.getLogger("irrigation.service")

ALERT_TEXT = {
    ("water_low", "raised"): ("critical", "Water level is low (float switch reports low water)"),
    ("water_low", "cleared"): ("info", "Water level restored"),
    ("soil_dry", "raised"): ("warning", "Soil is dry (reading is at or above the dry threshold)"),
    ("soil_dry", "cleared"): ("info", "Soil moisture is back below the dry threshold"),
    ("device_offline", "raised"): ("critical", "Device went offline (no data received)"),
    ("device_offline", "cleared"): ("info", "Device is back online"),
}


class Monitor:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.lock = threading.RLock()
        self.devices = {}
        self.pending = deque(maxlen=2000)  # (kind, row) waiting for the database
        self.counters = {"received": 0, "rejected": 0, "stored": 0}
        self.last_error = None
        self.db_ok = None
        self.mqtt_connected = False
        self._settings_cache = {}
        self._stop = threading.Event()

    # ---------- settings ----------
    def settings(self, device_id):
        cached = self._settings_cache.get(device_id)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        values = {"soil_threshold": self.cfg["SOIL_THRESHOLD"], "offline_after_seconds": self.cfg["OFFLINE_AFTER"]}
        try:
            saved = self.store.get_settings(device_id)
            if saved:
                values.update({k: v for k, v in saved.items() if v is not None})
        except StoreError as exc:
            self._note_db_error(exc)
        self._settings_cache[device_id] = (time.time(), values)
        return values

    def invalidate_settings(self):
        self._settings_cache.clear()

    # ---------- device state ----------
    def _dev(self, device_id):
        return self.devices.setdefault(device_id, {
            "latest": None, "last_seen": None, "lwt": None, "lwt_at": 0, "online": None,
            "flags": {}, "last_store": 0, "offline_raised": False})

    def is_online(self, device_id, now=None):
        now = now or time.time()
        d = self.devices.get(device_id)
        if not d or d["last_seen"] is None:
            return False
        if d["lwt"] == "offline" and d["lwt_at"] >= d["last_seen"]:
            return False
        return now - d["last_seen"] <= self.settings(device_id)["offline_after_seconds"]

    def _emit(self, device_id, alert_type, state, simulated, now):
        severity, message = ALERT_TEXT[(alert_type, state)]
        row = {"device_id": device_id, "alert_type": alert_type, "state": state, "severity": severity,
               "message": message, "is_simulated": simulated, "created_at": iso(now)}
        log.info("ALERT %s %s %s", device_id, alert_type, state)
        self._persist("alert", row)

    def _presence(self, device_id, now):
        d = self._dev(device_id)
        online = self.is_online(device_id, now)
        prev, d["online"] = d["online"], online
        sim = bool(d["latest"] and d["latest"]["is_simulated"])
        if not online and prev is True and not d["offline_raised"]:
            d["offline_raised"] = True
            self._emit(device_id, "device_offline", "raised", sim, now)
        elif online and d["offline_raised"]:
            d["offline_raised"] = False
            self._emit(device_id, "device_offline", "cleared", sim, now)

    # ---------- MQTT handlers ----------
    def handle_sensor(self, device_id, payload):
        self.counters["received"] += 1
        try:
            row = validate_payload(payload, device_id, self.settings(device_id)["soil_threshold"])
        except ValueError as exc:
            self.counters["rejected"] += 1
            self.last_error = {"at": iso(time.time()), "type": "rejected_message", "detail": str(exc)}
            log.warning("Rejected message from %s: %s", device_id, exc)
            return None
        now = time.time()
        row["created_at"] = iso(now)
        with self.lock:
            d = self._dev(device_id)
            d["latest"], d["last_seen"] = row, now
            self._presence(device_id, now)
            changed = False
            for flag in ("water_low", "soil_dry"):
                prev, cur = d["flags"].get(flag), row[flag]
                if cur != prev and (cur or prev is not None):
                    changed = True
                    self._emit(device_id, flag, "raised" if cur else "cleared", row["is_simulated"], now)
                d["flags"][flag] = cur
            if changed or now - d["last_store"] >= self.cfg["STORE_INTERVAL"]:
                d["last_store"] = now
                self._persist("reading", row)
        return row

    def handle_status(self, device_id, payload):
        if not valid_device_id(device_id):
            return
        text = payload.decode("utf-8", "ignore").strip() if isinstance(payload, (bytes, bytearray)) else str(payload)
        status = text.lower()
        if text.startswith("{"):
            try:
                status = str(json.loads(text).get("status", "")).lower()
            except (ValueError, AttributeError):
                return
        if status not in ("online", "offline"):
            return
        now = time.time()
        with self.lock:
            d = self._dev(device_id)
            d["lwt"], d["lwt_at"] = status, now
            if status == "online" and d["last_seen"] is None:
                d["last_seen"] = now
            self._presence(device_id, now)

    # ---------- persistence with retry buffer ----------
    def _note_db_error(self, exc):
        self.db_ok = False
        self.last_error = {"at": iso(time.time()), "type": "database", "detail": str(exc)}
        log.error("Database problem: %s", exc)

    def _persist(self, kind, row):
        self.pending.append((kind, row))
        self.flush()

    def flush(self):
        with self.lock:
            while self.pending:
                kind, row = self.pending[0]
                try:
                    (self.store.insert_reading if kind == "reading" else self.store.insert_alert)(row)
                except StoreError as exc:
                    self._note_db_error(exc)
                    return False
                self.pending.popleft()
                self.db_ok = True
                if kind == "reading":
                    self.counters["stored"] += 1
        return True

    # ---------- queries ----------
    def latest(self, device_id):
        with self.lock:
            d = self.devices.get(device_id)
            if d and d["latest"]:
                return d["latest"], d["last_seen"]
        row = self.store.latest_reading(device_id)  # after a restart: last stored reading
        if row:
            from store import parse_iso
            return row, parse_iso(row["created_at"]).timestamp()
        return None, None

    def known_devices(self):
        ids = list(self.devices)
        try:
            ids += [i for i in self.store.device_ids() if i not in ids]
        except StoreError as exc:
            self._note_db_error(exc)
        if self.cfg["DEFAULT_DEVICE_ID"] not in ids:
            ids.insert(0, self.cfg["DEFAULT_DEVICE_ID"])
        return ids

    # ---------- watchdog ----------
    def start_watchdog(self):
        def loop():
            while not self._stop.wait(5):
                try:
                    with self.lock:
                        for dev in list(self.devices):
                            self._presence(dev, time.time())
                    self.flush()
                except Exception:  # never let the watchdog die
                    log.exception("watchdog error")
        threading.Thread(target=loop, name="watchdog", daemon=True).start()

    def stop(self):
        self._stop.set()
