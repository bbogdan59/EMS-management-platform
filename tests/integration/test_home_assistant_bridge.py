import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.rate_limit import reset_key
from app.core.security import hash_token, utcnow
from app.models.home_assistant_bridge import HomeAssistantBridge
from app.models.station import Station
from app.models.telemetry import TelemetryRaw
from app.schemas.home_assistant_bridge import Ingest, MappingUpdate, PairingRedeem
from app.services import home_assistant_bridge as service
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.web_helpers import login

API = "/api/v1/home-assistant"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    reset_key("login_attempts:testclient")
    monkeypatch.setattr(get_settings(), "home_assistant_bridge_enabled", True)
    monkeypatch.setattr("app.api.v1.home_assistant_bridge.check_fixed_window", lambda *a: 1)


def setup(db, client=None, role="organization_admin"):
    user = make_user(db, email=f"bridge-{uuid4().hex}@test.local")
    org = make_org(db, "Bridge " + uuid4().hex)
    make_membership(db, user, org, role)
    station = make_station(db, org, user)
    db.commit()
    if client:
        login(client, user.email, "TestPass1234")
    return user, org, station


def mapping(entity="sensor.power", **changes):
    return {
        "entity_id": entity,
        "kind": "power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "source_validated": True,
        **changes,
    }


def configured(db, station=None, user=None):
    if station is None:
        user, _, station = setup(db)
    bridge, code = service.issue_pairing(db, station, user)
    bridge, token = service.redeem(
        db, PairingRedeem(code=code, instance_id=uuid4(), instance_name="Test home")
    )
    service.configure(
        db,
        bridge,
        MappingUpdate(expected_version=bridge.mapping_version, consent=True, mappings=[mapping()]),
    )
    db.flush()
    return bridge, token


def sample(**changes):
    now = utcnow().isoformat()
    return {
        "entity_id": "sensor.power",
        "kind": "power",
        "unit": "W",
        "source": "home_assistant",
        "observed_at": now,
        "sample_id": now,
        "value": "0",
        "available": True,
        "quality": "estimated",
        **changes,
    }


def ingest_data(bridge, samples):
    return Ingest.model_validate_json(
        json.dumps({"mapping_version": bridge.mapping_version, "samples": samples})
    )


def test_cookie_pairing_csrf_one_time_and_secret_redaction(db, client):
    user, _, station = setup(db, client)
    path = f"/api/v1/stations/{station.id}/home-assistant"
    assert client.post(path + "/pairing").status_code == 403
    result = client.post(
        path + "/pairing", headers={"X-CSRF-Token": client.cookies.get("ems_csrf")}
    )
    assert result.status_code == 201 and result.headers["cache-control"] == "no-store"
    code = result.json()["code"]
    bridge = service.get_bridge(db, station.id)
    assert bridge.pairing_hash == hash_token(code) and code not in repr(bridge.__dict__)
    payload = {"code": code, "instance_id": str(uuid4()), "instance_name": "House"}
    redeemed = client.post(API + "/pairing/redeem", json=payload)
    assert redeemed.status_code == 200
    token = redeemed.json()["token"]
    assert bridge.token_hash == hash_token(token)
    assert client.post(API + "/pairing/redeem", json=payload).status_code == 400
    snapshot = client.get(path).json()
    assert code not in str(snapshot) and token not in str(snapshot)
    bad = client.post(API + "/pairing/redeem", json={**payload, "extra": "secret"})
    assert bad.status_code == 422 and code not in bad.text and "secret" not in bad.text


def test_pairing_and_tokens_expire_reinstall_revokes_old(db):
    user, _, station = setup(db)
    bridge, code = service.issue_pairing(db, station, user)
    bridge.pairing_expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    with pytest.raises(ValueError):
        service.redeem(db, PairingRedeem(code=code, instance_id=uuid4(), instance_name="Expired"))
    bridge, token = configured(db, station, user)
    assert service.authenticate(db, token) is bridge
    bridge.token_expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    assert service.authenticate(db, token) is None
    service.issue_pairing(db, station, user)
    assert service.authenticate(db, token) is None
    assert not bridge.enabled and not bridge.observations and not bridge.mappings


def test_two_tenants_and_station_binding(db, client):
    owner, _, station = setup(db)
    bridge, token = configured(db, station, owner)
    _, _, other = setup(db, client)
    path = f"/api/v1/stations/{station.id}/home-assistant"
    headers = {"X-CSRF-Token": client.cookies.get("ems_csrf")}
    assert client.get(path).status_code == 403
    assert client.post(path + "/pairing", headers=headers).status_code == 403
    assert client.delete(path, headers=headers).status_code == 403
    assert (
        client.post(
            API + "/bridge/samples",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "mapping_version": bridge.mapping_version,
                "station_id": str(other.id),
                "samples": [],
            },
        ).status_code
        == 422
    )
    station.organization_id = other.organization_id
    db.flush()
    assert service.authenticate(db, token) is None


def test_allowlist_metadata_consent_and_bounded_http(db, client):
    bridge, token = configured(db)
    headers = {"Authorization": f"Bearer {token}"}
    for bad in (
        mapping("person.alice"),
        mapping(unit="kWh"),
        mapping(state_class="total"),
        mapping(source_validated=False),
        mapping("binary_sensor.house", kind="occupancy", unit="boolean", device_class="occupancy"),
    ):
        result = client.put(
            API + "/bridge/mappings",
            headers=headers,
            json={"expected_version": bridge.mapping_version, "consent": True, "mappings": [bad]},
        )
        assert result.status_code == 422
    assert (
        client.post(API + "/bridge/samples", headers=headers, content=b"x" * 32769).status_code
        == 413
    )
    assert client.post(API + "/bridge/samples", json={"samples": []}).status_code == 401


def test_zero_unknown_duplicates_out_of_order_stale_and_atomic_rejection(db, client):
    bridge, token = configured(db)
    first = sample()
    assert service.ingest(bridge, ingest_data(bridge, [first]))["accepted"] == 1
    assert bridge.observations["sensor.power"]["value"] == "0"
    assert service.ingest(bridge, ingest_data(bridge, [first]))["ignored"] == 1
    older = sample(observed_at=(utcnow() - timedelta(seconds=1)).isoformat())
    assert service.ingest(bridge, ingest_data(bridge, [older]))["ignored"] == 1
    db.flush()
    response = client.post(
        API + "/bridge/samples",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "mapping_version": bridge.mapping_version,
            "samples": [sample(value="42"), sample(entity_id="sensor.private")],
        },
    )
    assert response.status_code == 422
    db.refresh(bridge)
    assert bridge.observations["sensor.power"]["value"] == "0"
    assert (
        service.ingest(
            bridge, ingest_data(bridge, [sample(value=None, available=False, quality="unknown")])
        )["accepted"]
        == 1
    )
    assert service.snapshot(bridge)["observations"][0]["value"] is None
    bridge.last_seen_at = utcnow() - timedelta(seconds=100)
    assert service.snapshot(bridge)["status"] == "stale"
    bridge.last_seen_at = utcnow() - timedelta(seconds=301)
    assert service.snapshot(bridge)["status"] == "offline"


def test_mapping_retry_version_change_and_revoke(db, client):
    bridge, token = configured(db)
    update = MappingUpdate(
        expected_version=bridge.mapping_version, consent=True, mappings=[mapping("sensor.new")]
    )
    old_version = bridge.mapping_version
    service.configure(db, bridge, update)
    service.configure(db, bridge, update)
    assert bridge.mapping_version == old_version + 1
    assert not bridge.observations
    with pytest.raises(ValueError):
        service.ingest(bridge, ingest_data(bridge, [sample()]))
    db.flush()
    headers = {"Authorization": f"Bearer {token}"}
    assert client.delete(API + "/bridge", headers=headers).status_code == 200
    assert not bridge.token_hash and not bridge.mappings and not bridge.observations
    assert (
        client.post(
            API + "/bridge/samples",
            headers=headers,
            json={"mapping_version": old_version, "samples": []},
        ).status_code
        == 401
    )


def test_retention_preserves_replay_watermarks_and_migration_keeps_other_data(db, monkeypatch):
    bridge, _ = configured(db)
    old = sample(observed_at=(utcnow() - timedelta(hours=23)).isoformat(), value="15")
    service.ingest(bridge, ingest_data(bridge, [old]))
    bridge.observations = {
        "sensor.power": {
            **bridge.observations["sensor.power"],
            "observed_at": (utcnow() - timedelta(hours=25)).isoformat(),
        }
    }
    db.flush()
    service.purge_context(db)
    assert bridge.observations["sensor.power"]["value"] is None
    assert bridge.observations["sensor.power"]["sample_id"] == old["sample_id"]
    station_id = bridge.station_id
    device = make_device(db, db.get(Station, station_id))
    for sequence, value in enumerate((None, 0, "1234.567")):
        db.add(
            TelemetryRaw(
                station_id=station_id,
                device_id=device.id,
                boot_id="ha-migration",
                sequence=sequence,
                measured_at=utcnow(),
                received_at=utcnow(),
                pv_power_w=value,
            )
        )
    db.flush()
    from sqlalchemy import text

    query = text("SELECT pv_power_w FROM telemetry_raw WHERE device_id=:id ORDER BY sequence")
    before = db.execute(query, {"id": device.id}).all()
    spec = spec_from_file_location(
        "bridge_migration",
        Path(__file__).parents[2] / "alembic/versions/a216b40c912e_home_assistant_bridge.py",
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.flush()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db.connection())))
    migration.downgrade()
    assert db.execute(query, {"id": device.id}).all() == before
    migration.upgrade()
    assert db.execute(query, {"id": device.id}).all() == before
    assert db.get(Station, station_id) is not None
    assert migration.down_revision == "f189a27c803e"


def test_postgres_pairing_replay_race_only_one_winner(engine):
    with Session(engine) as db:
        user, _, station = setup(db)
        bridge, code = service.issue_pairing(db, station, user)
        bridge_id = bridge.id
        db.commit()

    def redeem():
        with Session(engine) as db:
            try:
                service.redeem(
                    db, PairingRedeem(code=code, instance_id=uuid4(), instance_name="Race")
                )
                db.commit()
                return "accepted"
            except ValueError:
                return "rejected"

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: redeem(), range(2)))
    assert sorted(outcomes) == ["accepted", "rejected"]
    with Session(engine) as db:
        assert db.get(HomeAssistantBridge, bridge_id).pairing_hash is None


def test_postgres_ingest_lock_fences_revoke(engine):
    with Session(engine) as db:
        bridge, token = configured(db)
        bridge_id = bridge.id
        db.commit()
    locked, release = Event(), Event()

    def ingest():
        with Session(engine) as db:
            bridge = service.authenticate(db, token)
            locked.set()
            assert release.wait(10)
            service.ingest(bridge, ingest_data(bridge, [sample()]))
            db.commit()

    def revoke():
        with Session(engine) as db:
            bridge = service.authenticate(db, token)
            service.revoke(bridge)
            db.commit()

    try:
        with ThreadPoolExecutor(2) as pool:
            ingesting = pool.submit(ingest)
            assert locked.wait(10)
            revoking = pool.submit(revoke)
            release.set()
            ingesting.result(10)
            revoking.result(10)
        with Session(engine) as db:
            assert service.authenticate(db, token) is None
            assert not db.get(HomeAssistantBridge, bridge_id).observations
    finally:
        release.set()


def test_ui_and_disabled_feature(db, client, monkeypatch):
    user, _, station = setup(db, client)
    page = f"/stations/{station.id}/integrations/home-assistant-bridge"
    assert client.get(page).status_code == 200
    assert (
        'aria-label="Senzori Home Assistant"'
        not in client.get(f"/?station_id={station.id}").text
    )
    bridge, token = configured(db, station, user)
    db.flush()
    assert (
        'aria-label="Senzori Home Assistant"' in client.get(f"/?station_id={station.id}").text
    )
    monkeypatch.setattr(get_settings(), "home_assistant_bridge_enabled", False)
    assert client.get(f"/api/v1/stations/{station.id}/home-assistant").json()["enabled"] is False
    assert (
        client.post(
            API + "/bridge/samples",
            headers={"Authorization": f"Bearer {token}"},
            json={"mapping_version": bridge.mapping_version, "samples": []},
        ).status_code
        == 503
    )
    assert (
        client.delete(API + "/bridge", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )


def test_simulated_provenance_survives_unknown_and_stale(db):
    bridge, _ = configured(db)
    bridge.mappings = [{**bridge.mappings[0], "quality": "simulated"}]
    service.ingest(bridge, ingest_data(bridge, [sample(quality="simulated")]))
    service.ingest(
        bridge, ingest_data(bridge, [sample(value=None, available=False, quality="unknown")])
    )
    observed = service.snapshot(bridge)["observations"][0]
    assert observed["value"] is None
    assert observed["is_simulated"] and observed["source_quality"] == "simulated"
