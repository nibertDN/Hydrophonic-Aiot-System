"""MQTT subscriber (paho-mqtt 2.x). Reconnects automatically with exponential backoff."""
import logging
import ssl
import uuid

import paho.mqtt.client as mqtt

log = logging.getLogger("irrigation.mqtt")


def start_mqtt(cfg, monitor):
    root = cfg["MQTT_TOPIC_ROOT"]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"irrigation-backend-{uuid.uuid4().hex[:8]}")
    if cfg["MQTT_USERNAME"]:
        client.username_pw_set(cfg["MQTT_USERNAME"], cfg["MQTT_PASSWORD"] or None)
    if cfg["MQTT_TLS"]:
        client.tls_set(ca_certs=cfg["MQTT_CA_CERT"] or None, cert_reqs=ssl.CERT_REQUIRED)
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            monitor.mqtt_connected = False
            log.error("MQTT connection refused: %s (check username/password/TLS settings)", reason_code)
            return
        monitor.mqtt_connected = True
        log.info("Connected to MQTT broker %s:%s (tls=%s)", cfg["MQTT_HOST"], cfg["MQTT_PORT"], cfg["MQTT_TLS"])
        c.subscribe([(f"{root}/+/sensors", 1), (f"{root}/+/status", 1)])  # re-subscribe on every reconnect

    def on_disconnect(c, userdata, flags, reason_code, properties):
        monitor.mqtt_connected = False
        log.warning("MQTT disconnected (%s); retrying automatically", reason_code)

    def on_message(c, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) != 3 or parts[0] != root:
                return
            if parts[2] == "sensors":
                monitor.handle_sensor(parts[1], msg.payload)
            elif parts[2] == "status":
                monitor.handle_status(parts[1], msg.payload)
        except Exception:
            log.exception("Error while handling MQTT message on %s", msg.topic)

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.connect_async(cfg["MQTT_HOST"], cfg["MQTT_PORT"], keepalive=30)
    client.loop_start()
    return client
