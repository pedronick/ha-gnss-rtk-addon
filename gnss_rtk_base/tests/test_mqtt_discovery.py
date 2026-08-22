import json

import mqtt_discovery as disc


class FakeMqttClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


def test_sensor_config_has_expected_fields():
    cfg = disc.sensor_config("fix_status", "Fix Status", "gnssbase/fix_status/state", unit="m", icon="mdi:foo")
    assert cfg["unique_id"] == "gnssbase_fix_status"
    assert cfg["name"] == "Fix Status"
    assert cfg["state_topic"] == "gnssbase/fix_status/state"
    assert cfg["unit_of_measurement"] == "m"
    assert cfg["icon"] == "mdi:foo"
    assert cfg["device"] is disc.DEVICE


def test_binary_sensor_config_default_payloads():
    cfg = disc.binary_sensor_config("device_connected", "Dispositivo connesso",
                                     "gnssbase/device_connected/state", device_class="connectivity")
    assert cfg["payload_on"] == "ON"
    assert cfg["payload_off"] == "OFF"
    assert cfg["device_class"] == "connectivity"


def test_number_config_bounds():
    cfg = disc.number_config("manual_lat", "Lat", "s/state", "s/set", -90, 90, 0.0000001, "°")
    assert cfg["min"] == -90
    assert cfg["max"] == 90
    assert cfg["mode"] == "box"


def test_publish_discovery_topic_and_payload():
    client = FakeMqttClient()
    cfg = disc.sensor_config("fix_status", "Fix Status", "gnssbase/fix_status/state")
    disc.publish_discovery(client, "sensor", "fix_status", cfg)

    assert len(client.published) == 1
    topic, payload, retain = client.published[0]
    assert topic == "homeassistant/sensor/gnssbase/fix_status/config"
    assert retain is True
    assert json.loads(payload) == cfg
