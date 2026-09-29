import ssl
from types import SimpleNamespace

import pytest

from app.services import home_assistant_mqtt as transport


class FakeBrokerClient:
    auth_code = 0
    deny_subscribe = False
    deny_publish = False
    disconnect_early = False

    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.events = []
        self.closed = False
        self.messages = [("expected/topic", b'{"value":"0"}')]

    def tls_set_context(self, context):
        self.tls_context = context

    def username_pw_set(self, username, password):
        self.username, self.password = username, password

    def connect(self, host, port, **kwargs):
        self.connect_options = kwargs
        self.events.append(
            lambda: self.on_connect(self, None, None, self.reason(self.auth_code), None)
        )

    @staticmethod
    def reason(code):
        return SimpleNamespace(value=code, is_failure=code >= 128)

    def subscribe(self, topics):
        self.topics = topics
        self.events.append(
            lambda: self.on_subscribe(
                self,
                None,
                1,
                [self.reason(135 if self.deny_subscribe else 1) for _ in topics],
                None,
            )
        )
        for topic, payload in self.messages:
            self.events.append(
                lambda t=topic, p=payload: self.on_message(
                    self, None, SimpleNamespace(topic=t, payload=p)
                )
            )
        return 0, 1

    def publish(self, topic, payload, **kwargs):
        self.publication = (topic, payload, kwargs)
        self.events.append(
            lambda: self.on_publish(
                self, None, 2, self.reason(135 if self.deny_publish else 0), None
            )
        )
        return SimpleNamespace(rc=0)

    def loop(self, timeout):
        if self.events:
            self.events.pop(0)()
        return 7 if self.disconnect_early or self.auth_code else 0

    def disconnect(self):
        self.closed = True


@pytest.fixture()
def fake(monkeypatch):
    client = FakeBrokerClient()
    monkeypatch.setattr(transport.mqtt, "Client", lambda *a, **kw: client)
    clock = iter(i / 10 for i in range(10000))
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))
    return client


def exchange():
    return transport.exchange(
        endpoint=("broker.example", 8883),
        credentials={"username": "private-user", "password": "secret"},
        topics=["expected/topic"],
        client_id="test",
        publication=("ems/state", {"schema_version": 1}),
    )


def test_tls_exact_subscriptions_and_qos1_acknowledged_publication(fake):
    assert exchange() == fake.messages
    assert fake.tls_context.check_hostname
    assert fake.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert fake.tls_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert fake.topics == [("expected/topic", 1)]
    assert fake.publication[2]["qos"] == 1 and fake.publication[2]["retain"]
    assert fake.publication[2]["properties"].MessageExpiryInterval == 120
    assert fake.connect_options["properties"].MaximumPacketSize == 8192
    assert fake.closed


@pytest.mark.parametrize(
    "attribute,value,code",
    [
        ("auth_code", 134, "auth_failed"),
        ("deny_subscribe", True, "access_denied"),
        ("deny_publish", True, "access_denied"),
        ("disconnect_early", True, "unavailable"),
    ],
)
def test_broker_auth_acl_and_reconnect_failures_are_redacted(fake, attribute, value, code):
    setattr(fake, attribute, value)
    with pytest.raises(transport.MQTTFailure, match=code) as caught:
        exchange()
    assert "secret" not in str(caught.value) and "private-user" not in str(caught.value)
    assert fake.closed


def test_invalid_tls_fails_closed(fake):
    def fail(*a, **kw):
        raise ssl.SSLCertVerificationError("private certificate detail")

    fake.connect = fail
    with pytest.raises(transport.MQTTFailure, match="tls_failed"):
        exchange()


def test_message_flood_is_bounded(fake):
    fake.messages = [("expected/topic", b"{}") for _ in range(300)]

    # Deliver the flood in one loop, as a broker can do with a single network read.
    def flood(timeout):
        while fake.events:
            fake.events.pop(0)()
        return 0

    fake.loop = flood
    with pytest.raises(transport.MQTTFailure, match="message_limit"):
        exchange()
