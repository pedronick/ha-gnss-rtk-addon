"""Helper per pubblicare configurazioni MQTT Discovery raggruppate come un
unico dispositivo "RTK Base Station" in Home Assistant, indipendentemente
dal ricevitore GNSS effettivamente usato (vedi drivers/)."""

import json

DEVICE = {
    "identifiers": ["gnss_rtk_base"],
    "name": "RTK Base Station",
    "manufacturer": "GNSS RTK",
    "model": "generico",  # sovrascritto a runtime da main.py col driver selezionato
}


def sensor_config(object_id, name, state_topic, unit=None, icon=None, device_class=None,
                   json_attributes_topic=None):
    cfg = {
        "name": name,
        "unique_id": f"gnssbase_{object_id}",
        "state_topic": state_topic,
        "device": DEVICE,
    }
    if unit:
        cfg["unit_of_measurement"] = unit
    if icon:
        cfg["icon"] = icon
    if device_class:
        cfg["device_class"] = device_class
    if json_attributes_topic:
        cfg["json_attributes_topic"] = json_attributes_topic
    return cfg


def binary_sensor_config(object_id, name, state_topic, device_class=None, icon=None):
    cfg = {
        "name": name,
        "unique_id": f"gnssbase_{object_id}",
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": DEVICE,
    }
    if device_class:
        cfg["device_class"] = device_class
    if icon:
        cfg["icon"] = icon
    return cfg


def button_config(object_id, name, command_topic, icon=None):
    cfg = {
        "name": name,
        "unique_id": f"gnssbase_{object_id}",
        "command_topic": command_topic,
        "device": DEVICE,
    }
    if icon:
        cfg["icon"] = icon
    return cfg


def number_config(object_id, name, state_topic, command_topic, min_v, max_v, step, unit=None):
    cfg = {
        "name": name,
        "unique_id": f"gnssbase_{object_id}",
        "state_topic": state_topic,
        "command_topic": command_topic,
        "min": min_v,
        "max": max_v,
        "step": step,
        "mode": "box",
        "device": DEVICE,
    }
    if unit:
        cfg["unit_of_measurement"] = unit
    return cfg


def publish_discovery(mqtt_client, component, object_id, config):
    topic = f"homeassistant/{component}/gnssbase/{object_id}/config"
    mqtt_client.publish(topic, json.dumps(config), retain=True)
