from datetime import timedelta

import pytest

from app.config import get_settings
from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.schemas.home_assistant_bridge import MappingUpdate
from app.services import home_assistant_bridge as service
from tests.factories import make_membership, make_user
from tests.integration.test_home_assistant_bridge import (
    configured,
    ingest_data,
    mapping,
    sample,
    setup,
)
from tests.web_helpers import login


@pytest.fixture()
def shared_temperatures(db, client, monkeypatch):
    reset_key("login_attempts:testclient")
    monkeypatch.setattr(get_settings(), "home_assistant_bridge_enabled", True)
    user, org, station = setup(db, client)
    bridge, _ = configured(db, station, user)
    service.configure(
        db,
        bridge,
        MappingUpdate(
            expected_version=bridge.mapping_version,
            consent=True,
            mappings=[
                mapping(
                    "sensor.living_temperature",
                    kind="temperature",
                    unit="°C",
                    device_class="temperature",
                ),
                mapping(
                    "sensor.bedroom_temperature",
                    kind="temperature",
                    unit="°F",
                    device_class="temperature",
                    quality="simulated",
                ),
            ],
        ),
    )
    service.ingest(
        bridge,
        ingest_data(
            bridge,
            [
                sample(
                    entity_id="sensor.living_temperature",
                    kind="temperature",
                    unit="°C",
                    value="21.5",
                ),
                sample(
                    entity_id="sensor.bedroom_temperature",
                    kind="temperature",
                    unit="°F",
                    value="32",
                    quality="simulated",
                ),
            ],
        ),
    )
    db.commit()
    yield user, org, station, bridge
    reset_key("login_attempts:testclient")


def paths(station):
    page = f"/stations/{station.id}/integrations/home-assistant-bridge"
    return [page, page + "/status", f"/?station_id={station.id}"]


def test_shared_temperatures_render_on_page_dashboard_and_refresh(db, client, shared_temperatures):
    _, _, station, bridge = shared_temperatures
    bridge.instance_name = '<img src=x onerror="alert(1)">'
    db.commit()
    for path in paths(station):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert 'data-ha-entity="sensor.living_temperature"' in response.text
        assert 'data-ha-entity="sensor.bedroom_temperature"' in response.text
        assert "21,50 <span>°C</span>" in response.text
        assert "0,00 <span>°C</span>" in response.text
        assert "Home Assistant · simulat" in response.text
        assert "Temperatura interioara" in response.text
        assert station.timezone in response.text
        assert "&lt;img" in response.text and bridge.instance_name not in response.text
    page = client.get(paths(station)[0]).text
    assert f'href="{paths(station)[0]}" aria-current="page"' in page
    assert f'href="/stations/{station.id}/integrations/home-assistant"' in page


@pytest.mark.parametrize("state", ["stale", "unknown", "offline", "expired", "disabled", "revoked"])
def test_sensor_values_are_hidden_when_no_longer_available(
    db, client, monkeypatch, shared_temperatures, state
):
    _, _, station, bridge = shared_temperatures
    if state == "stale":
        bridge.observations = {
            key: {**value, "observed_at": (utcnow() - timedelta(hours=2)).isoformat()}
            for key, value in bridge.observations.items()
        }
    elif state == "unknown":
        bridge.observations = {
            key: {**value, "value": None, "available": False, "quality": "unknown"}
            for key, value in bridge.observations.items()
        }
    elif state == "offline":
        bridge.last_seen_at = utcnow() - timedelta(minutes=6)
    elif state == "expired":
        bridge.token_expires_at = utcnow() - timedelta(seconds=1)
    elif state == "disabled":
        monkeypatch.setattr(get_settings(), "home_assistant_bridge_enabled", False)
    else:
        service.revoke(bridge)
    db.commit()
    for path in paths(station):
        response = client.get(path)
        assert response.status_code == 200
        assert "21,50 <span>°C</span>" not in response.text
        assert "0,00 <span>°C</span>" not in response.text
        if state not in ("disabled", "revoked"):
            assert "Indisponibil" in response.text
            assert "Home Assistant · simulat" in response.text
        else:
            assert "sensor.living_temperature" not in response.text


def test_viewer_can_read_but_other_tenant_cannot_read_sensor_fragment(
    db, client, shared_temperatures
):
    _, org, station, _ = shared_temperatures
    viewer = make_user(db)
    make_membership(db, viewer, org, "viewer")
    db.commit()
    login(client, viewer.email, "TestPass1234")
    page = client.get(paths(station)[0])
    assert page.status_code == 200 and "21,50 <span>°C</span>" in page.text
    assert "data-ha-pair" not in page.text and "data-ha-disconnect" not in page.text
    assert client.get(paths(station)[1]).status_code == 200
    setup(db, client)
    denied = client.get(paths(station)[1])
    assert denied.status_code == 403 and "sensor.living_temperature" not in denied.text


def test_no_samples_are_explained_without_becoming_zero(db, client, shared_temperatures):
    _, _, station, bridge = shared_temperatures
    bridge.observations = {}
    db.commit()
    response = client.get(paths(station)[1])
    assert response.status_code == 200
    assert "In asteptarea primei citiri" in response.text
    assert response.text.count("Indisponibil") == 2
    assert "0,00" not in response.text
