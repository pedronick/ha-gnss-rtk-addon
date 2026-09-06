"""Thin wrapper around the paho MQTT client that additionally remembers
the last payload published to every topic, so the skyplot web page's
own controls/status section (see webui.py) can expose the exact same
states published via MQTT Discovery without a second connection back to
the broker. Every other call (publish() aside) is transparently
delegated to the real client, including callback assignment
(mqtt.on_connect = ..., mqtt.on_message = ...) - paho reads those
straight off the client instance it's actually running, so they have to
land on the real object, not the wrapper."""


class ShadowMqttClient:
    def __init__(self, real_client):
        object.__setattr__(self, "_real", real_client)
        object.__setattr__(self, "shadow", {})

    def publish(self, topic, payload=None, retain=False, **kwargs):
        self.shadow[topic] = payload
        return self._real.publish(topic, payload, retain=retain, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)
