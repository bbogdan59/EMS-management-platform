"""Bounded MQTT 5 sessions; retained context is sampled by the optional worker."""

from __future__ import annotations

import json
import ssl
import time

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties


class MQTTFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def exchange(*, endpoint, credentials, topics, client_id, publication=None, ca_file=None):
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
        reconnect_on_failure=False,
    )
    client.connect_timeout = 3
    try:
        context = ssl.create_default_context(cafile=ca_file)
    except (ssl.SSLError, OSError):
        raise MQTTFailure("tls_failed") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    client.tls_set_context(context)
    client.username_pw_set(credentials["username"], credentials["password"])
    received = []
    state = {"connected": False, "subscribed": False, "published": False, "error": None}

    def on_connect(_client, _userdata, _flags, reason, _properties):
        if reason.is_failure:
            state["error"] = "auth_failed" if reason.value in (134, 135) else "unavailable"
        else:
            state["connected"] = True

    def on_subscribe(_client, _userdata, _mid, reasons, _properties):
        if len(reasons) != len(topics) or any(r.is_failure for r in reasons):
            state["error"] = "access_denied"
        else:
            state["subscribed"] = True

    def on_publish(_client, _userdata, _mid, reason, _properties):
        if reason.is_failure:
            state["error"] = "access_denied"
        else:
            state["published"] = True

    def on_message(_client, _userdata, message):
        if len(received) >= 200:
            state["error"] = "message_limit"
        else:
            received.append((message.topic, message.payload))

    def pump_until(predicate, seconds):
        end = time.monotonic() + seconds
        while not predicate():
            if state["error"]:
                raise MQTTFailure(state["error"])
            if time.monotonic() >= end:
                raise MQTTFailure("unavailable")
            result = client.loop(timeout=0.1)
            # CONNACK/PUBACK can carry the precise denial before loop reports disconnect.
            if state["error"]:
                raise MQTTFailure(state["error"])
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise MQTTFailure("unavailable")
        if state["error"]:
            raise MQTTFailure(state["error"])

    client.on_connect, client.on_subscribe = on_connect, on_subscribe
    client.on_message, client.on_publish = on_message, on_publish
    try:
        properties = Properties(PacketTypes.CONNECT)
        properties.MaximumPacketSize = 8192
        properties.ReceiveMaximum = 20
        properties.SessionExpiryInterval = 0
        client.connect(*endpoint, keepalive=20, clean_start=True, properties=properties)
        pump_until(lambda: state["connected"], 3)
        if topics:
            result, _ = client.subscribe([(topic, 1) for topic in topics])
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise MQTTFailure("unavailable")
            pump_until(lambda: state["subscribed"], 3)
            until = time.monotonic() + 1
            pump_until(lambda: time.monotonic() >= until, 2)
        if publication:
            topic, payload = publication
            properties = Properties(PacketTypes.PUBLISH)
            properties.MessageExpiryInterval = 120
            info = client.publish(
                topic,
                json.dumps(payload, separators=(",", ":")),
                qos=1,
                retain=True,
                properties=properties,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise MQTTFailure("unavailable")
            pump_until(lambda: state["published"], 3)
        return received
    except ssl.SSLError:
        raise MQTTFailure("tls_failed") from None
    except (OSError, ValueError, RuntimeError):
        # Provider exceptions may contain credentials, certificate paths or network details.
        raise MQTTFailure("unavailable") from None
    finally:
        client.disconnect()
