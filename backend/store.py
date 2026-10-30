"""Storage backends.

SupabaseStore talks to Supabase's PostgREST API with the *service role* key.
That key bypasses Row Level Security, which is why it must only ever live in
backend environment variables. RLS stays ENABLED on every table and no policy
grants anything to the `anon` / `authenticated` roles, so the browser can never
read or write the database directly.

MemoryStore is used for automated tests and for trying the app without Supabase.
"""
import threading
from datetime import datetime, timedelta, timezone

import requests

READING_COLS = ("device_id,soil_value,soil_dry,water_low,water_red_led,water_green_led,"
                "soil_red_led,soil_green_led,buzzer_on,is_simulated,rssi,uptime_s,created_at")
ALERT_COLS = "device_id,alert_type,state,severity,message,is_simulated,created_at"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class StoreError(Exception):
    pass


class SupabaseStore:
    name = "supabase"

    def __init__(self, url, service_key, table="sensor_readings", timeout=10):
        if not url or not service_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        self.base = url.rstrip("/") + "/rest/v1"
        self.table = table
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"apikey": service_key, "Authorization": f"Bearer {service_key}"})

    def _req(self, method, path, **kw):
        try:
            r = self.s.request(method, f"{self.base}/{path}", timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise StoreError(f"Supabase unreachable: {exc.__class__.__name__}") from exc
        if r.status_code >= 300:
            raise StoreError(f"Supabase error {r.status_code}: {r.text[:200]}")
        return r

    def ping(self):
        self._req("GET", f"{self.table}?select=device_id&limit=1")
        return True

    def insert_reading(self, row):
        self._req("POST", self.table, json=row, headers={"Prefer": "return=minimal"})

    def insert_alert(self, row):
        self._req("POST", "irrigation_alerts", json=row, headers={"Prefer": "return=minimal"})

    def latest_reading(self, device_id):
        r = self._req("GET", self.table, params=[
            ("select", READING_COLS), ("device_id", f"eq.{device_id}"),
            ("order", "created_at.desc"), ("limit", "1")])
        rows = r.json()
        return rows[0] if rows else None

    def recent_readings(self, device_id, limit):
        r = self._req("GET", self.table, params=[
            ("select", READING_COLS), ("device_id", f"eq.{device_id}"),
            ("order", "created_at.desc"), ("limit", str(limit))])
        return r.json()

    def history_buckets(self, device_id, since_ts, bucket_seconds):
        r = self._req("POST", "rpc/sensor_history_buckets", json={
            "p_device": device_id, "p_since": iso(since_ts), "p_bucket_seconds": bucket_seconds})
        return r.json()

    def alerts(self, device_id, limit):
        r = self._req("GET", "irrigation_alerts", params=[
            ("select", ALERT_COLS), ("device_id", f"eq.{device_id}"),
            ("order", "created_at.desc"), ("limit", str(limit))])
        return r.json()

    def device_ids(self):
        r = self._req("GET", self.table, params=[
            ("select", "device_id"), ("order", "created_at.desc"), ("limit", "300")])
        return list(dict.fromkeys(x["device_id"] for x in r.json()))

    def get_settings(self, device_id):
        r = self._req("GET", "irrigation_settings", params=[
            ("select", "soil_threshold,offline_after_seconds"), ("device_id", f"eq.{device_id}")])
        rows = r.json()
        return rows[0] if rows else None

    def save_settings(self, device_id, values):
        row = {"device_id": device_id, **values, "updated_at": iso(datetime.now(timezone.utc).timestamp())}
        self._req("POST", "irrigation_settings?on_conflict=device_id", json=row,
                  headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


class MemoryStore:
    name = "memory"

    def __init__(self):
        self.lock = threading.Lock()
        self.readings, self.alerts_, self.settings = [], [], {}

    def ping(self):
        return True

    def insert_reading(self, row):
        with self.lock:
            self.readings.append(dict(row))

    def insert_alert(self, row):
        with self.lock:
            self.alerts_.append(dict(row))

    def _mine(self, device_id):
        return [r for r in self.readings if r["device_id"] == device_id]

    def latest_reading(self, device_id):
        with self.lock:
            m = self._mine(device_id)
            return dict(m[-1]) if m else None

    def recent_readings(self, device_id, limit):
        with self.lock:
            return [dict(r) for r in reversed(self._mine(device_id)[-limit:])]

    def history_buckets(self, device_id, since_ts, bucket_seconds):
        buckets = {}
        with self.lock:
            for r in self._mine(device_id):
                t = parse_iso(r["created_at"]).timestamp()
                if t < since_ts:
                    continue
                buckets.setdefault(int(t // bucket_seconds) * bucket_seconds, []).append(r)
        out = []
        for b, rows in sorted(buckets.items()):
            n = len(rows)
            soils = [x["soil_value"] for x in rows]
            out.append({"bucket": iso(b), "n": n, "soil_avg": sum(soils) / n,
                        "soil_min": min(soils), "soil_max": max(soils),
                        "water_low_ratio": sum(x["water_low"] for x in rows) / n,
                        "soil_dry_ratio": sum(x["soil_dry"] for x in rows) / n})
        return out

    def alerts(self, device_id, limit):
        with self.lock:
            return [dict(a) for a in reversed([a for a in self.alerts_ if a["device_id"] == device_id][-limit:])]

    def device_ids(self):
        with self.lock:
            return list(dict.fromkeys(r["device_id"] for r in reversed(self.readings)))

    def get_settings(self, device_id):
        return self.settings.get(device_id)

    def save_settings(self, device_id, values):
        self.settings.setdefault(device_id, {}).update(values)
