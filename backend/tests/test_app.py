import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from store import MemoryStore, StoreError  # noqa: E402
from validation import validate_payload  # noqa: E402

GOOD = {"device_id": "device01", "soil": 2800, "water_low": False}
LEGACY = {
    "device_id": "device01",
    "soil_raw_value": 3251,
    "soil_threshold_status": "NORMAL",
    "water_level_status": "HIGH",
    "buzzer_alert_status": "INACTIVE",
    "red_water_led": False,
    "green_water_led": True,
    "red_soil_led": False,
    "green_soil_led": True,
}


def msg(**kw):
    return json.dumps({**GOOD, **kw}).encode()


@pytest.fixture
def ctx():
    app = create_app({"MQTT_ENABLED": False, "STORE_BACKEND": "memory", "ADMIN_TOKEN": "x" * 24,
                      "STORE_INTERVAL": 0, "ALLOWED_ORIGINS": ["https://dash.example.com"]})
    return app, app.config["MONITOR"], app.test_client()


# ---- validation ----
def test_valid_payload_derives_indicators():
    r = validate_payload(msg(soil=3400, water_low=True), "device01", 3300)
    assert r["soil_dry"] and r["water_low"] and r["soil_red_led"] and r["water_red_led"] and r["buzzer_on"]
    assert not r["soil_green_led"] and r["is_simulated"] is False


def test_ok_payload_green():
    r = validate_payload(msg(), "device01", 3300)
    assert not r["soil_dry"] and r["soil_green_led"] and r["water_green_led"] and not r["buzzer_on"]


def test_legacy_payload_is_normalized():
    r = validate_payload(json.dumps(LEGACY).encode(), "device01", 3300)
    assert r == {
        "device_id": "device01",
        "soil_value": 3251,
        "soil_dry": False,
        "water_low": False,
        "water_red_led": False,
        "water_green_led": True,
        "soil_red_led": False,
        "soil_green_led": True,
        "buzzer_on": False,
        "is_simulated": False,
        "rssi": None,
        "uptime_s": None,
    }


def test_legacy_status_fields_map_to_normalized_flags():
    payload = {**LEGACY, "soil_raw_value": 3500, "soil_threshold_status": "DRY",
               "water_level_status": "LOW", "buzzer_alert_status": "ACTIVE"}
    r = validate_payload(json.dumps(payload).encode(), "device01", 3300)
    assert r["soil_dry"] and r["water_low"] and r["buzzer_on"]


def test_legacy_payload_ingests_and_marks_device_online(ctx):
    app, mon, c = ctx
    row = mon.handle_sensor("device01", json.dumps(LEGACY).encode())
    assert row["soil_value"] == 3251
    assert c.get("/api/latest").get_json()["online"] is True
    assert mon.store.readings[-1]["soil_value"] == 3251


def test_float_raw_high_means_low_water():
    r = validate_payload(json.dumps({"soil": 100, "float": 1}).encode(), "device01", 3300)
    assert r["water_low"] is True


@pytest.mark.parametrize("bad", [b"not json", b"[1,2]", msg(soil="wet"), msg(soil=5000), msg(soil=-1),
                                 json.dumps({"soil": 10}).encode(), msg(water_low="maybe"),
                                 msg(device_id="other"), b"x" * 5000])
def test_invalid_payloads_rejected(bad):
    with pytest.raises(ValueError):
        validate_payload(bad, "device01", 3300)


def test_bad_device_id_rejected():
    with pytest.raises(ValueError):
        validate_payload(msg(), "../etc", 3300)


# ---- ingestion + API ----
def test_ingest_and_latest(ctx):
    app, mon, c = ctx
    assert c.get("/api/latest").get_json()["reading"] is None
    mon.handle_sensor("device01", msg(soil=3500, water_low=True))
    j = c.get("/api/latest").get_json()
    assert j["online"] and j["reading"]["soil_value"] == 3500 and j["reading"]["soil_dry"]
    assert j["simulated"] is False and j["soil_threshold"] == 3300


def test_rejected_message_counted_not_stored(ctx):
    app, mon, c = ctx
    mon.handle_sensor("device01", b"garbage")
    assert mon.counters["rejected"] == 1 and mon.store.readings == []
    assert c.get("/api/status").get_json()["last_error"]["type"] == "rejected_message"


def test_alert_transitions(ctx):
    app, mon, c = ctx
    mon.handle_sensor("device01", msg(soil=2000))                  # ok, no alert
    assert c.get("/api/alerts").get_json()["events"] == []
    mon.handle_sensor("device01", msg(soil=3400))                  # dry raised
    mon.handle_sensor("device01", msg(soil=3450))                  # still dry, no duplicate
    mon.handle_sensor("device01", msg(soil=3400, water_low=True))  # water low raised
    mon.handle_sensor("device01", msg(soil=2000))                  # both cleared
    ev = c.get("/api/alerts").get_json()["events"]
    kinds = [(e["alert_type"], e["state"]) for e in reversed(ev)]
    assert kinds == [("soil_dry", "raised"), ("water_low", "raised"), ("water_low", "cleared"),
                     ("soil_dry", "cleared")] or sorted(kinds) == sorted(
        [("soil_dry", "raised"), ("water_low", "raised"), ("water_low", "cleared"), ("soil_dry", "cleared")])
    assert c.get("/api/alerts").get_json()["active"] == []


def test_lwt_offline_and_back(ctx):
    app, mon, c = ctx
    mon.handle_sensor("device01", msg())
    mon.handle_status("device01", b"offline")
    assert c.get("/api/latest").get_json()["online"] is False
    assert "device_offline" in c.get("/api/alerts").get_json()["active"]
    mon.handle_status("device01", b'{"status":"online"}')
    mon.handle_sensor("device01", msg())
    assert c.get("/api/latest").get_json()["online"] is True
    types = [(e["alert_type"], e["state"]) for e in c.get("/api/alerts").get_json()["events"]]
    assert ("device_offline", "raised") in types and ("device_offline", "cleared") in types


def test_timeout_marks_offline(ctx):
    app, mon, c = ctx
    mon.handle_sensor("device01", msg())
    mon.devices["device01"]["last_seen"] -= 100
    mon._presence("device01", time.time())
    assert c.get("/api/latest").get_json()["online"] is False


def test_history_and_summary(ctx):
    app, mon, c = ctx
    for s in (2000, 2500, 3400):
        mon.handle_sensor("device01", msg(soil=s))
    j = c.get("/api/history?range=1h").get_json()
    assert j["summary"]["samples"] == 3 and j["summary"]["soil_max"] == 3400
    assert c.get("/api/history?range=bogus").status_code == 400
    assert c.get("/api/latest?device_id=../x").status_code == 400


def test_simulated_flag_propagates(ctx):
    app, mon, c = ctx
    mon.handle_sensor("sim01", json.dumps({"soil": 1000, "water_low": False, "simulated": True}).encode())
    assert c.get("/api/latest?device_id=sim01").get_json()["simulated"] is True
    assert "sim01" in c.get("/api/devices").get_json()["devices"]


def test_database_outage_buffers_and_recovers(ctx):
    app, mon, c = ctx
    real = mon.store.insert_reading

    def boom(row):
        raise StoreError("down")
    mon.store.insert_reading = boom
    mon.handle_sensor("device01", msg(soil=2100))
    assert len(mon.pending) == 1 and mon.db_ok is False
    assert c.get("/api/latest").get_json()["reading"]["soil_value"] == 2100  # live data still served
    mon.store.insert_reading = real
    assert mon.flush() and len(mon.pending) == 0 and len(mon.store.readings) == 1


# ---- admin / security ----
def test_admin_requires_token(ctx):
    app, mon, c = ctx
    assert c.get("/api/admin/settings").status_code == 401
    assert c.get("/api/admin/settings", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = {"Authorization": "Bearer " + "x" * 24}
    assert c.get("/api/admin/settings", headers=ok).status_code == 200
    r = c.put("/api/admin/settings", json={"soil_threshold": 3000}, headers=ok)
    assert r.status_code == 200 and r.get_json()["settings"]["soil_threshold"] == 3000
    assert c.put("/api/admin/settings", json={"soil_threshold": 99999}, headers=ok).status_code == 400


def test_pump_not_implemented(ctx):
    app, mon, c = ctx
    ok = {"Authorization": "Bearer " + "x" * 24}
    assert c.get("/api/admin/pump", headers=ok).get_json()["installed"] is False
    assert c.post("/api/admin/pump", json={"on": True}, headers=ok).status_code == 501
    assert c.get("/api/status").get_json()["capabilities"]["pump_installed"] is False


def test_admin_disabled_without_token():
    app = create_app({"MQTT_ENABLED": False, "STORE_BACKEND": "memory", "ADMIN_TOKEN": ""})
    assert app.test_client().get("/api/admin/settings").status_code == 503


def test_admin_bruteforce_lockout(ctx):
    app, mon, c = ctx
    for _ in range(10):
        c.get("/api/admin/settings", headers={"Authorization": "Bearer nope"})
    ok = {"Authorization": "Bearer " + "x" * 24}
    assert c.get("/api/admin/settings", headers=ok).status_code == 429


def test_cors_and_security_headers(ctx):
    app, mon, c = ctx
    r = c.get("/api/health", headers={"Origin": "https://dash.example.com"})
    assert r.headers["Access-Control-Allow-Origin"] == "https://dash.example.com"
    assert "Access-Control-Allow-Origin" not in c.get("/api/health", headers={"Origin": "https://evil.com"}).headers
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_no_secrets_in_frontend():
    root = Path(__file__).resolve().parents[2] / "frontend"
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".js", ".html", ".css") and "vendor" not in f.parts:
            t = f.read_text(encoding="utf-8").lower()
            for bad in ("service_role", "supabase_service_key", "mqtt_password", "eyj"):
                assert bad not in t, f"{bad} found in {f}"


def test_frontend_served(ctx):
    app, mon, c = ctx
    assert c.get("/").status_code == 200
    assert c.get("/js/app.js").status_code == 200
    assert c.get("/../backend/app.py").status_code in (400, 404)
