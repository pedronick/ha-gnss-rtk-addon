from mqtt_shadow import ShadowMqttClient


class FakeRealClient:
    def __init__(self):
        self.published = []
        self.on_connect = None
        self.on_message = None

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    def connect(self, host, port, keepalive):
        return (host, port, keepalive)


def test_publish_records_into_shadow_and_delegates_to_the_real_client():
    real = FakeRealClient()
    shadow = ShadowMqttClient(real)

    shadow.publish("gnssbase/ppp_status/state", "processing", retain=True)

    assert shadow.shadow == {"gnssbase/ppp_status/state": "processing"}
    assert real.published == [("gnssbase/ppp_status/state", "processing", True)]


def test_shadow_keeps_only_the_latest_payload_per_topic():
    shadow = ShadowMqttClient(FakeRealClient())
    shadow.publish("gnssbase/ppp_status/state", "logging")
    shadow.publish("gnssbase/ppp_status/state", "processing")
    assert shadow.shadow == {"gnssbase/ppp_status/state": "processing"}


def test_other_calls_and_callback_assignment_delegate_to_the_real_client():
    real = FakeRealClient()
    shadow = ShadowMqttClient(real)

    shadow.on_connect = "a callback"  # paho reads this straight off the real client
    shadow.on_message = "another callback"
    assert real.on_connect == "a callback"
    assert real.on_message == "another callback"

    assert shadow.connect("host", 1883, 30) == ("host", 1883, 30)
