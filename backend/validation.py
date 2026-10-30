"""Validation and normalisation of incoming MQTT sensor messages.

Expected payload published by the newer ESP32 firmware in this project:

    {"device_id": "device01", "soil": 2950, "soil_dry": false, "water_low": false,
     "leds": {"water_red": false, "water_green": true, "soil_red": false, "soil_green": true},
     "buzzer": false, "rssi": -61, "uptime_s": 1234, "simulated": false}

Only `soil` and `water_low` are required. Everything else is optional and, when
missing, is derived (and the derivation is documented in the README).

The earlier firmware format is also accepted:

    {"device_id": "device01", "soil_raw_value": 3251,
     "soil_threshold_status": "NORMAL", "water_level_status": "HIGH",
     "buzzer_alert_status": "INACTIVE", "red_water_led": false,
     "green_water_led": true, "red_soil_led": false, "green_soil_led": true}
"""
import json
import re

MAX_PAYLOAD_BYTES = 2048
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ADC_MAX = 4095  # ESP32 12-bit ADC

SOIL_KEYS = ("soil", "soil_value", "soil_raw", "soil_moisture", "soil_raw_value")
WATER_LOW_KEYS = ("water_low", "low_water")
FLOAT_RAW_KEYS = ("float", "float_state", "float_switch")  # raw pin level: 1 (HIGH) = low water


def valid_device_id(device_id):
    return bool(isinstance(device_id, str) and DEVICE_ID_RE.match(device_id))


def _to_bool(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    raise ValueError(f"'{name}' must be a boolean")


def _first(d, keys):
    for k in keys:
        if k in d and d[k] is not None:
            return k, d[k]
    return None, None


def _status_bool(value, name, true_value, false_value):
    if not isinstance(value, str):
        raise ValueError(f"'{name}' must be '{true_value}' or '{false_value}'")
    normalized = value.strip().upper()
    if normalized == true_value:
        return True
    if normalized == false_value:
        return False
    raise ValueError(f"'{name}' must be '{true_value}' or '{false_value}'")


def _opt_int(d, key, lo, hi):
    if key not in d or d[key] is None:
        return None
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"'{key}' must be a number")
    v = int(v)
    if not lo <= v <= hi:
        raise ValueError(f"'{key}' out of range {lo}..{hi}")
    return v


def validate_payload(raw, device_id, soil_threshold):
    """Return a normalised row dict (without timestamp). Raises ValueError if invalid."""
    if not valid_device_id(device_id):
        raise ValueError("invalid device id")
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload too large")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"payload is not valid JSON: {exc}") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")

    # The topic is the source of truth for device id; a mismatching body id is rejected.
    body_id = data.get("device_id")
    if body_id is not None and body_id != device_id:
        raise ValueError("device_id in payload does not match topic")

    key, soil = _first(data, SOIL_KEYS)
    if key is None:
        raise ValueError("missing soil reading ('soil')")
    if isinstance(soil, bool) or not isinstance(soil, (int, float)):
        raise ValueError("'soil' must be a number")
    soil = int(soil)
    if not 0 <= soil <= ADC_MAX:
        raise ValueError(f"'soil' out of ADC range 0..{ADC_MAX}")

    key, wl = _first(data, WATER_LOW_KEYS)
    if key is not None:
        water_low = _to_bool(wl, key)
    elif "water_level_status" in data and data["water_level_status"] is not None:
        water_low = _status_bool(data["water_level_status"], "water_level_status", "LOW", "HIGH")
    else:
        key, raw_level = _first(data, FLOAT_RAW_KEYS)
        if key is None:
            raise ValueError("missing water level ('water_low')")
        water_low = _to_bool(raw_level, key)  # HIGH (1) = low water

    if "soil_dry" in data and data["soil_dry"] is not None:
        soil_dry = _to_bool(data["soil_dry"], "soil_dry")
    else:
        # Validate the legacy status when present, but keep the configured ADC
        # threshold authoritative for the normalized schema.
        if "soil_threshold_status" in data and data["soil_threshold_status"] is not None:
            _status_bool(data["soil_threshold_status"], "soil_threshold_status", "DRY", "NORMAL")
        soil_dry = soil >= soil_threshold  # higher ADC value = drier soil

    leds = data.get("leds") if isinstance(data.get("leds"), dict) else {}

    def led(name, default):
        legacy_names = {
            "water_red": "red_water_led",
            "water_green": "green_water_led",
            "soil_red": "red_soil_led",
            "soil_green": "green_soil_led",
        }
        v = leds.get(name, data.get(name + "_led"))
        if v is None:
            v = data.get(legacy_names[name])
        return default if v is None else _to_bool(v, name)

    if data.get("buzzer") is not None:
        buzzer_on = _to_bool(data["buzzer"], "buzzer")
    elif data.get("buzzer_alert_status") is not None:
        buzzer_on = _status_bool(data["buzzer_alert_status"], "buzzer_alert_status", "ACTIVE", "INACTIVE")
    else:
        buzzer_on = water_low or soil_dry

    row = {
        "device_id": device_id,
        "soil_value": soil,
        "soil_dry": soil_dry,
        "water_low": water_low,
        "water_red_led": led("water_red", water_low),
        "water_green_led": led("water_green", not water_low),
        "soil_red_led": led("soil_red", soil_dry),
        "soil_green_led": led("soil_green", not soil_dry),
        "buzzer_on": buzzer_on,
        "is_simulated": _to_bool(data.get("simulated", False), "simulated"),
        "rssi": _opt_int(data, "rssi", -127, 0),
        "uptime_s": _opt_int(data, "uptime_s", 0, 2**31 - 1),
    }
    return row
