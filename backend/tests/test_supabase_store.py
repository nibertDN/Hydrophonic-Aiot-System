"""Checks the exact HTTP requests SupabaseStore sends, against a local mock of Supabase's REST API."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from store import StoreError, SupabaseStore  # noqa: E402

LOG = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        u = urlparse(self.path)
        LOG.append((self.command, u.path, parse_qs(u.query), body, dict(self.headers)))
        code, out = 200, []
        if u.path.endswith("/boom"):
            code = 500
        if self.command == "POST" and "rpc" not in u.path:
            code = 201
        if "rpc" in u.path:
            out = [{"bucket": "2026-01-01T00:00:00Z", "n": 2, "soil_avg": 3000.0, "soil_min": 2900, "soil_max": 3100,
                    "water_low_ratio": 0.0, "soil_dry_ratio": 0.5}]
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    do_GET = do_POST = _go


@pytest.fixture(scope="module")
def store():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SupabaseStore(f"http://127.0.0.1:{srv.server_port}", "SECRET-KEY", "sensor_readings", timeout=3)
    srv.shutdown()


def test_requests_are_well_formed(store):
    LOG.clear()
    store.insert_reading({"device_id": "device01", "soil_value": 1})
    store.insert_alert({"device_id": "device01", "alert_type": "soil_dry", "state": "raised"})
    store.latest_reading("device01")
    store.history_buckets("device01", 1_700_000_000, 300)
    store.save_settings("device01", {"soil_threshold": 3000})
    m = {(c, p): (q, b, h) for c, p, q, b, h in LOG}
    assert m[("POST", "/rest/v1/sensor_readings")][1]["soil_value"] == 1
    assert ("POST", "/rest/v1/irrigation_alerts") in m
    q = m[("GET", "/rest/v1/sensor_readings")][0]
    assert q["device_id"] == ["eq.device01"] and q["order"] == ["created_at.desc"] and q["limit"] == ["1"]
    rpc = m[("POST", "/rest/v1/rpc/sensor_history_buckets")][1]
    assert rpc == {"p_device": "device01", "p_since": "2023-11-14T22:13:20Z", "p_bucket_seconds": 300}
    assert m[("POST", "/rest/v1/irrigation_settings")][0]["on_conflict"] == ["device_id"]
    h = m[("POST", "/rest/v1/sensor_readings")][2]
    assert h["apikey"] == "SECRET-KEY" and h["Authorization"] == "Bearer SECRET-KEY"


def test_http_error_becomes_store_error(store):
    with pytest.raises(StoreError):
        store._req("GET", "boom")


def test_unreachable_becomes_store_error():
    s = SupabaseStore("http://127.0.0.1:1", "k", timeout=1)
    with pytest.raises(StoreError):
        s.ping()


def test_error_text_never_leaks_key():
    s = SupabaseStore("http://127.0.0.1:1", "TOPSECRET", timeout=1)
    with pytest.raises(StoreError) as e:
        s.ping()
    assert "TOPSECRET" not in str(e.value)
