"""SIMULATED sensor data generator - for demos and testing ONLY.

Publishes to its own device id (default 'simulator01') with "simulated": true, so the
data is never mixed up with the real ESP32 (device01) and the dashboard labels it
clearly as simulated.

    python simulator.py                 # uses MQTT_* settings from .env
    python simulator.py --interval 2    # faster
"""
import argparse
import json
import math
import random
import ssl
import time

import paho.mqtt.client as mqtt

from app import load_config


def main():
    p = argparse.ArgumentParser(description="Publish SIMULATED irrigation sensor data")
    p.add_argument("--device", default="simulator01")
    p.add_argument("--interval", type=float, default=5.0)
    args = p.parse_args()
    cfg = load_config()
    root, dev, thr = cfg["MQTT_TOPIC_ROOT"], args.device, cfg["SOIL_THRESHOLD"]

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{dev}-sim")
    if cfg["MQTT_USERNAME"]:
        c.username_pw_set(cfg["MQTT_USERNAME"], cfg["MQTT_PASSWORD"] or None)
    if cfg["MQTT_TLS"]:
        c.tls_set(ca_certs=cfg["MQTT_CA_CERT"] or None, cert_reqs=ssl.CERT_REQUIRED)
    c.will_set(f"{root}/{dev}/status", "offline", qos=1, retain=True)
    c.connect(cfg["MQTT_HOST"], cfg["MQTT_PORT"], keepalive=30)
    c.loop_start()
    c.publish(f"{root}/{dev}/status", "online", qos=1, retain=True)
    print(f"*** SIMULATED DATA *** publishing as '{dev}' every {args.interval}s. Ctrl+C to stop.")

    t0 = time.time()
    try:
        while True:
            t = time.time() - t0
            soil = int(2900 + 700 * math.sin(t / 120) + random.randint(-40, 40))  # slow wet/dry cycle
            water_low = math.sin(t / 300) > 0.85
            dry = soil >= thr
            payload = {"device_id": dev, "soil": max(0, min(4095, soil)), "soil_dry": dry,
                       "water_low": water_low, "buzzer": dry or water_low,
                       "rssi": random.randint(-75, -50), "uptime_s": int(t), "simulated": True}
            c.publish(f"{root}/{dev}/sensors", json.dumps(payload), qos=0)
            print(f"[SIMULATED] soil={payload['soil']} dry={dry} water_low={water_low}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        c.publish(f"{root}/{dev}/status", "offline", qos=1, retain=True).wait_for_publish(2)
        c.loop_stop()


if __name__ == "__main__":
    main()
