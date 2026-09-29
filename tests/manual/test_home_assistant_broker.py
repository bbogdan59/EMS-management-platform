"""Opt-in protocol test against an isolated real Mosquitto broker with TLS/auth.

EMS_MQTT_REAL_BROKER_TEST=1 .venv/bin/pytest tests/manual/test_home_assistant_broker.py -q
Requires Docker and eclipse-mosquitto:2.0 already pulled. No existing broker is used.
"""

import json
import os
import secrets
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.services import home_assistant_mqtt as transport
from app.services import home_assistant_service as service
from tests.unit.test_home_assistant_contract import context, sample

pytestmark = pytest.mark.skipif(
    os.getenv("EMS_MQTT_REAL_BROKER_TEST") != "1", reason="Explicit opt-in real TLS broker test"
)


@pytest.fixture()
def broker(tmp_path):
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (tmp_path / "key.pem").chmod(0o600)
    (tmp_path / "mosquitto.conf").write_text(
        "user root\nlistener 8883\nallow_anonymous false\npassword_file /mqtt/passwords\n"
        "acl_file /mqtt/acl\ncertfile /mqtt/cert.pem\nkeyfile /mqtt/key.pem\n"
        "persistence false\nlog_type error\n"
    )
    (tmp_path / "acl").write_text("user ems-test\ntopic readwrite ems/v1/#\n")
    password = secrets.token_urlsafe(24)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{tmp_path}:/mqtt",
            "--entrypoint",
            "mosquitto_passwd",
            "eclipse-mosquitto:2.0",
            "-b",
            "-c",
            "/mqtt/passwords",
            "ems-test",
            password,
        ],
        check=True,
        capture_output=True,
    )
    container = "ems-mqtt-test-" + uuid4().hex[:10]
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "--user",
            "0:0",
            "-v",
            f"{tmp_path}:/mqtt:ro",
            "-p",
            "127.0.0.1::8883",
            "eclipse-mosquitto:2.0",
            "mosquitto",
            "-c",
            "/mqtt/mosquitto.conf",
        ],
        check=True,
        capture_output=True,
    )
    try:
        binding = subprocess.check_output(
            ["docker", "port", container, "8883/tcp"], text=True
        ).strip()
        endpoint = ("127.0.0.1", int(binding.rsplit(":", 1)[1]))
        until = time.monotonic() + 15
        while True:
            try:
                with socket.create_connection(endpoint, timeout=1):
                    break
            except OSError:
                if time.monotonic() > until:
                    raise
                time.sleep(0.2)
        yield {
            "endpoint": endpoint,
            "credentials": {"username": "ems-test", "password": password},
            "ca_file": str(tmp_path / "cert.pem"),
        }
    finally:
        subprocess.run(
            ["docker", "stop", "--time", "2", container], check=True, capture_output=True
        )


def test_real_tls_retained_reconnect_auth_and_acl(broker):
    connection, mapping = context()
    topic = service.context_topic(connection, mapping)
    now = datetime.now(UTC)
    payload = sample(observed_at=now.isoformat(), quality="simulated", value="0")
    transport.exchange(
        **broker, client_id="ha-test-publisher", topics=[], publication=(topic, payload)
    )
    messages = transport.exchange(**broker, client_id="ems-test-reader", topics=[topic])
    assert len(messages) == 1 and json.loads(messages[0][1]) == payload
    assert service.ingest(connection, [mapping], *messages[0], now=now) == "accepted"
    assert service.observation(mapping, now=now)["is_simulated"]
    replay = transport.exchange(**broker, client_id="ems-test-reader", topics=[topic])
    assert len(replay) == 1
    assert service.ingest(connection, [mapping], *replay[0], now=now) == "duplicate_or_older"
    assert service.observation(mapping, now=now + timedelta(minutes=4))["value"] is None
    with pytest.raises(transport.MQTTFailure, match="auth_failed"):
        transport.exchange(
            **{**broker, "credentials": {"username": "ems-test", "password": "invalid"}},
            client_id="denied",
            topics=[],
        )
    with pytest.raises(transport.MQTTFailure, match="tls_failed"):
        transport.exchange(**{**broker, "ca_file": None}, client_id="untrusted", topics=[])
    with pytest.raises(transport.MQTTFailure, match="access_denied"):
        transport.exchange(
            **broker,
            client_id="forbidden",
            topics=[],
            publication=("forbidden/command", {"value": "on"}),
        )
