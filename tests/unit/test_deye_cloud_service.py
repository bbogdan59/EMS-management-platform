"""Teste de contract pentru adaptorul Deye Cloud (issue #43) -- deterministe,
FARA niciun apel de retea real. Raspunsurile mock reflecta forma documentata
oficial a API-ului (endpoint-uri/campuri de request verificate impotriva
specificatiei OpenAPI bundle-uite, vezi `deye_cloud_service` si
docs/LIMITATIONS.md); eroarea de autentificare foloseste EXACT plicul
observat direct impotriva serverului real Deye Cloud (cod/mesaj), nu o
presupunere."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from app.core.security import utcnow
from app.models.deye_integration import DeyeCloudConnection
from app.models.enums import DeyeCloudConnectionStatus, TelemetrySource
from app.models.station import Station
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services import aggregation_service, dashboard_service
from app.services import deye_cloud_service as svc
from tests.factories import make_device, make_org, make_station, make_user

BASE = "https://eu1-developer.deyecloud.com"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(svc.settings, "deye_cloud_max_retries", 3)


def _connection(db, station, user, **overrides) -> DeyeCloudConnection:
    defaults = {
        "station_id": station.id,
        "created_by_user_id": user.id,
        "status": DeyeCloudConnectionStatus.connected.value,
        "app_id": "test-app-id",
        "encrypted_app_secret": encrypt_secret("test-app-secret"),
        "account_email": "client@example.com",
        "encrypted_account_password": encrypt_secret("s3cret"),
        "encrypted_access_token": encrypt_secret("cached-token"),
        "access_token_expires_at": utcnow() + timedelta(hours=1),
        "remote_station_id": 322,
        "remote_station_name": "Casa Test",
        "consent_accepted_at": utcnow(),
    }
    defaults.update(overrides)
    conn = DeyeCloudConnection(**defaults)
    db.add(conn)
    db.flush()
    return conn


# --- Config si hashing ------------------------------------------------------


def test_require_app_credentials_raises_when_unconfigured():
    with pytest.raises(svc.DeyeCloudConfigError):
        svc._require_app_credentials("", None)


def test_password_is_hashed_sha256_never_sent_in_clear():
    assert svc._hash_password("hunter2") == hashlib.sha256(b"hunter2").hexdigest()


def test_valid_access_token_requires_station_app_credentials():
    conn = DeyeCloudConnection(
        station_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        status=DeyeCloudConnectionStatus.connected.value,
        account_email="client@example.com",
        encrypted_account_password=encrypt_secret("hunter2"),
        consent_accepted_at=utcnow(),
    )

    with pytest.raises(svc.DeyeCloudConfigError):
        svc._valid_access_token(conn)


# --- Autentificare (plic real de eroare, verificat live) --------------------


@respx.mock
def test_authenticate_success():
    route = respx.post(f"{BASE}/v1.0/account/token").mock(
        return_value=httpx.Response(200, json={"access_token": "abc123", "expires_in": 86400, "token_type": "bearer"})
    )
    data = svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")
    assert data["access_token"] == "abc123"
    request = route.calls[0].request
    assert request.url.params["appId"] == "station-app-id"
    payload = json.loads(request.content)
    assert payload["appSecret"] == "station-app-secret"
    assert payload["email"] == "client@example.com"
    assert payload["password"] == hashlib.sha256(b"hunter2").hexdigest()
    assert "hunter2" not in request.content.decode("utf-8")


@respx.mock
def test_authenticate_accepts_camel_case_token_response():
    respx.post(f"{BASE}/v1.0/account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "abc123", "expiresIn": 86400, "tokenType": "bearer"})
    )

    data = svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")

    assert data["access_token"] == "abc123"
    assert data["expires_in"] == 86400


@respx.mock
def test_authenticate_invalid_app_id_raises_auth_error():
    # Plicul EXACT observat live impotriva serverului real Deye Cloud
    # (cod/mesaj), nu o presupunere -- vezi docs/LIMITATIONS.md.
    respx.post(f"{BASE}/v1.0/account/token").mock(
        return_value=httpx.Response(
            200, json={"code": "2101021", "msg": "auth invalid appId", "success": False, "requestId": "abc"}
        )
    )
    with pytest.raises(svc.DeyeCloudAuthError):
        svc.authenticate("bad-app-id", "station-app-secret", "client@example.com", "hunter2")


@respx.mock
def test_unrecognized_api_error_is_not_treated_as_auth_error():
    respx.post(f"{BASE}/v1.0/account/token").mock(
        return_value=httpx.Response(200, json={"code": "9999999", "msg": "eroare necunoscuta", "success": False})
    )
    with pytest.raises(svc.DeyeCloudApiError) as exc_info:
        svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")
    assert exc_info.value.code == "9999999"


@respx.mock
def test_transient_5xx_is_retried_then_succeeds():
    route = respx.post(f"{BASE}/v1.0/account/token")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json={"access_token": "ok-after-retry", "expires_in": 3600}),
    ]
    data = svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")
    assert data["access_token"] == "ok-after-retry"
    assert route.call_count == 2


@respx.mock
def test_business_error_is_not_retried():
    route = respx.post(f"{BASE}/v1.0/account/token").mock(
        return_value=httpx.Response(200, json={"code": "9999999", "msg": "x", "success": False})
    )
    with pytest.raises(svc.DeyeCloudApiError):
        svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")
    assert route.call_count == 1


@respx.mock
def test_non_json_success_response_fails_explicitly_without_retry():
    route = respx.post(f"{BASE}/v1.0/account/token").mock(
        return_value=httpx.Response(200, text="not-json")
    )
    with pytest.raises(svc.DeyeCloudApiError, match="JSON invalid"):
        svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")
    assert route.call_count == 1


@respx.mock
def test_persistent_5xx_raises_unavailable_after_max_retries(monkeypatch):
    monkeypatch.setattr(svc.settings, "deye_cloud_max_retries", 2)  # limiteaza timpul real de asteptare al testului
    respx.post(f"{BASE}/v1.0/account/token").mock(return_value=httpx.Response(500))
    with pytest.raises(svc.DeyeCloudUnavailableError):
        svc.authenticate("station-app-id", "station-app-secret", "client@example.com", "hunter2")


# --- Mapare Deye -> model canonic (defensiva, unitati/semne) ----------------


def test_map_station_latest_inverts_battery_and_grid_power_signs():
    """Campurile Deye/Solarman sunt in WATI (issue #118, confirmat de un
    raport real de utilizator -- valori de ordinul miilor pe un grafic
    etichetat kW), nu kW -- maparea NU mai inmulteste cu 1000.

    Verificat pe date reale: `batteryPower` si `wirePower` sunt opuse
    conventiei platformei, deci semnul se inverseaza pentru ambele."""
    mapped = svc.map_station_latest_to_telemetry(
        {"batteryPower": -1500, "generationPower": 2000, "consumptionPower": 1000, "wirePower": -500, "batterySOC": 80}
    )
    assert mapped["battery_power_w"] == 1500  # incarcare (Deye: negativ) -> pozitiv, conventia platformei
    assert mapped["grid_power_w"] == 500  # Deye wirePower negativ -> import pozitiv in platforma
    assert mapped["pv_power_w"] == 2000
    assert mapped["load_power_w"] == 1000
    assert mapped["battery_soc_percent"] == 80


def test_map_station_latest_discharge_is_negative_battery_power():
    mapped = svc.map_station_latest_to_telemetry({"batteryPower": 2000})
    assert mapped["battery_power_w"] == -2000


def test_map_station_latest_treats_deye_power_fields_as_watts_not_kw():
    mapped = svc.map_station_latest_to_telemetry(
        {
            "generationPower": 1263,
            "consumptionPower": 1110,
            "wirePower": 20,
            "batteryPower": 500,
        }
    )

    assert mapped["pv_power_w"] == Decimal("1263")
    assert mapped["load_power_w"] == Decimal("1110")
    assert mapped["grid_power_w"] == Decimal("-20")  # wirePower Deye pozitiv -> export negativ in platforma
    assert mapped["battery_power_w"] == Decimal("-500")  # batteryPower descarcare (+) -> semn inversat


def test_map_station_latest_missing_keys_are_none_not_zero():
    mapped = svc.map_station_latest_to_telemetry({})
    assert mapped["pv_power_w"] is None
    assert mapped["battery_power_w"] is None
    assert mapped["grid_power_w"] is None
    assert mapped["battery_soc_percent"] is None
    assert mapped["measured_at"] is None


def test_map_station_latest_rejects_garbage_numeric_fields():
    mapped = svc.map_station_latest_to_telemetry({"generationPower": "not-a-number", "batterySOC": float("nan")})
    assert mapped["pv_power_w"] is None
    assert mapped["battery_soc_percent"] is None


# --- Regula de prioritate: dispozitiv local activ ---------------------------


def test_local_device_active_true_when_recent_rs485_row_exists(db):
    org = make_org(db, "Org LDA")
    user = make_user(db, email="lda@test.local")
    station = make_station(db, org, user, name="Statie LDA")
    device = make_device(db, station)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="boot-1", sequence=1,
            measured_at=utcnow(), received_at=utcnow(), source=TelemetrySource.device_rs485.value,
        )
    )
    db.flush()
    assert svc.local_device_active(db, station.id, exclude_device_id=None) is True


def test_local_device_active_false_when_only_stale_rs485_row(db):
    org = make_org(db, "Org LDA2")
    user = make_user(db, email="lda2@test.local")
    station = make_station(db, org, user, name="Statie LDA2")
    device = make_device(db, station)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="boot-1", sequence=1,
            measured_at=utcnow() - timedelta(hours=2), received_at=utcnow(), source=TelemetrySource.device_rs485.value,
        )
    )
    db.flush()
    assert svc.local_device_active(db, station.id, exclude_device_id=None) is False


def test_local_device_active_ignores_cloud_sourced_rows(db):
    org = make_org(db, "Org LDA3")
    user = make_user(db, email="lda3@test.local")
    station = make_station(db, org, user, name="Statie LDA3")
    device = make_device(db, station)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id=svc.CLOUD_BOOT_ID, sequence=1,
            measured_at=utcnow(), received_at=utcnow(), source=TelemetrySource.deye_cloud.value,
        )
    )
    db.flush()
    assert svc.local_device_active(db, station.id, exclude_device_id=device.id) is False


# --- poll_connection: idempotenta, backoff, prioritate ----------------------


def _setup_station(db, suffix: str):
    org = make_org(db, f"Org Poll {suffix}")
    user = make_user(db, email=f"poll{suffix}@test.local")
    station = make_station(db, org, user, name=f"Statie Poll {suffix}")
    cloud_device = make_device(db, station, name="Deye Cloud (Casa Test)")
    cloud_device.capabilities = {"deye_cloud": True, "read_only": True}
    db.add(cloud_device)
    db.flush()
    return org, user, station, cloud_device


def test_poll_connection_skips_when_not_connected(db):
    _, user, station, cloud_device = _setup_station(db, "1")
    conn = _connection(db, station, user, status=DeyeCloudConnectionStatus.pending_selection.value, device_id=cloud_device.id)
    result = svc.poll_connection(db, conn)
    assert result == {"status": "skipped", "reason": "not_connected"}


def test_poll_connection_skips_when_local_device_active(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "2")
    local_device = make_device(db, station, name="EMS local")
    db.add(
        TelemetryRaw(
            device_id=local_device.id, station_id=station.id, boot_id="boot-1", sequence=1,
            measured_at=utcnow(), received_at=utcnow(), source=TelemetrySource.device_rs485.value,
        )
    )
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    def _boom(*a, **k):
        raise AssertionError("nu ar trebui apelat cand dispozitivul local e activ")

    monkeypatch.setattr(svc, "fetch_station_latest", _boom)
    result = svc.poll_connection(db, conn)
    assert result == {"status": "skipped", "reason": "local_device_active"}
    assert conn.last_sync_status == "skipped"


def test_poll_connection_success_creates_telemetry_row_with_cloud_source(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "3")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    raw = {"generationPower": 3200, "consumptionPower": 1100, "batteryPower": -500, "wirePower": 2100, "batterySOC": 76, "lastUpdateTime": 1757721600}
    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(svc, "fetch_station_latest", lambda token, station_id: raw)

    result = svc.poll_connection(db, conn)
    assert result["status"] == "succeeded"
    assert result["telemetry_created"] is True
    row = db.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == cloud_device.id))
    assert row is not None
    assert row.source == TelemetrySource.deye_cloud.value
    assert row.boot_id == svc.CLOUD_BOOT_ID
    assert float(row.pv_power_w) == 3200
    assert conn.last_sync_status == "succeeded"
    assert conn.consecutive_failure_count == 0


def test_poll_connection_is_idempotent_on_identical_reading(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "4")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    raw = {"generationPower": 1.0, "lastUpdateTime": 1757721600}
    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(svc, "fetch_station_latest", lambda token, station_id: raw)

    first = svc.poll_connection(db, conn)
    second = svc.poll_connection(db, conn)
    assert first["telemetry_created"] is True
    assert second["telemetry_created"] is False

    count = db.scalar(select(func.count(TelemetryRaw.id)).where(TelemetryRaw.device_id == cloud_device.id))
    assert count == 1


@respx.mock
def test_fetch_station_history_power_posts_timestamp_window():
    route = respx.post(f"{BASE}/v1.0/station/history/power").mock(
        return_value=httpx.Response(200, json={"stationDataItems": []})
    )
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    data = svc.fetch_station_history_power("token", 322, start, end, page=2, size=50)

    assert data == {"stationDataItems": []}
    request = route.calls[0].request
    assert request.url.params["page"] == "2"
    assert request.url.params["size"] == "50"
    payload = json.loads(request.content)
    assert payload == {
        "stationId": 322,
        "startTimestamp": int(start.timestamp()),
        "endTimestamp": int(end.timestamp()),
    }


def test_import_station_history_creates_rows_deduplicates_and_reaggregates(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "history-import")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    existing_at = datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    new_at = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    end = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    db.add(
        TelemetryRaw(
            device_id=cloud_device.id, station_id=station.id, boot_id=svc.CLOUD_BOOT_ID,
            sequence=int(existing_at.timestamp()), measured_at=existing_at, received_at=existing_at,
            source=TelemetrySource.deye_cloud.value, pv_power_w=Decimal("100"),
        )
    )
    db.flush()

    calls = []

    def _history(token, remote_station_id, range_start, range_end, *, page, size):
        calls.append((token, remote_station_id, range_start, range_end, page, size))
        if page == 1:
            return {
                "stationDataItems": [
                    {"timeStamp": int(existing_at.timestamp()), "generationPower": 999},
                    {
                        "timeStamp": int(new_at.timestamp()),
                        "generationPower": 1500,
                        "consumptionPower": 700,
                        "batteryPower": -200,
                        "wirePower": -600,
                        "batterySOC": 61,
                    },
                    {"generationPower": 1},
                ],
                "total": 3,
            }
        return {"stationDataItems": []}

    reaggregated = []
    monkeypatch.setattr(svc, "fetch_station_history_power", _history)
    monkeypatch.setattr(
        svc.aggregation_service,
        "reaggregate_range",
        lambda db_arg, station_arg, start_arg, end_arg: reaggregated.append((station_arg.id, start_arg, end_arg)),
    )

    result = svc.import_station_history(db, conn, start, end)

    assert result["created"] == 1
    assert result["skipped_existing"] == 1
    assert result["skipped_invalid_timestamp"] == 1
    assert result["implausible_power_rows"] == 0
    assert calls == [("cached-token", 322, start, end, 1, svc.HISTORY_IMPORT_PAGE_SIZE)]
    imported = db.scalar(
        select(TelemetryRaw).where(
            TelemetryRaw.device_id == cloud_device.id,
            TelemetryRaw.sequence == int(new_at.timestamp()),
        )
    )
    assert imported is not None
    assert imported.source == TelemetrySource.deye_cloud.value
    assert imported.pv_power_w == Decimal("1500")
    assert imported.load_power_w == Decimal("700")
    assert imported.battery_power_w == Decimal("200")
    assert imported.grid_power_w == Decimal("600")
    assert imported.battery_soc_percent == Decimal("61")
    assert reaggregated == [(station.id, new_at, new_at + timedelta(minutes=15))]
    assert conn.last_sync_status == "succeeded"


def test_import_station_history_rejects_large_window(db):
    _, user, station, cloud_device = _setup_station(db, "history-window")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="7 zile"):
        svc.import_station_history(db, conn, start, start + timedelta(days=8))


def test_poll_connection_auth_failure_marks_connection_error(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "5")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    def _raise_auth(connection):
        raise svc.DeyeCloudAuthError("cont revocat")

    monkeypatch.setattr(svc, "_valid_access_token", _raise_auth)
    result = svc.poll_connection(db, conn)
    assert result == {"status": "failed", "reason": "auth_error"}
    assert conn.status == DeyeCloudConnectionStatus.error.value
    assert conn.consecutive_failure_count == 1


def test_poll_connection_backoff_skips_before_wait_elapses(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "6")
    conn = _connection(
        db, station, user, device_id=cloud_device.id,
        consecutive_failure_count=1, last_sync_at=utcnow(),
    )
    db.flush()

    def _boom(*a, **k):
        raise AssertionError("nu ar trebui apelat in fereastra de backoff")

    monkeypatch.setattr(svc, "fetch_station_latest", _boom)
    result = svc.poll_connection(db, conn)
    assert result == {"status": "skipped", "reason": "backoff"}


# --- Plauzibilitate de unitate pe putere (issue #118) ------------------------


def test_implausible_power_fields_empty_when_within_ceiling():
    mapped = {"pv_power_w": Decimal(4000), "load_power_w": Decimal(3000), "battery_power_w": Decimal(-1000), "grid_power_w": Decimal(500)}
    assert svc._implausible_power_fields(mapped, ceiling_w=Decimal(15000)) == []


def test_implausible_power_fields_flags_values_over_ceiling_by_absolute_value():
    mapped = {"pv_power_w": Decimal(4_000_000), "load_power_w": Decimal(1000), "battery_power_w": Decimal(-9_000_000), "grid_power_w": None}
    offenders = svc._implausible_power_fields(mapped, ceiling_w=Decimal(15000))
    assert offenders == ["pv_power_w", "battery_power_w"]


def test_power_plausibility_ceiling_uses_station_rated_capacity(db):
    org = make_org(db, "Org Ceiling")
    user = make_user(db, email="ceiling@test.local")
    station = make_station(db, org, user, name="Statie Ceiling", pv_installed_power_kw=Decimal("8"), inverter_power_kw=Decimal("6"))
    db.commit()
    # max(8, 6) kW * 1000 * marja de 3x
    assert svc._power_plausibility_ceiling_w(db, station.id) == Decimal(24000)


def test_power_plausibility_ceiling_falls_back_when_station_has_no_config(db):
    org = make_org(db, "Org NoConfig")
    station = Station(organization_id=org.id, name="Statie fara config", timezone="Europe/Bucharest")
    db.add(station)
    db.commit()
    assert svc._power_plausibility_ceiling_w(db, station.id) == svc._FALLBACK_POWER_CEILING_W


def test_poll_connection_keeps_confirmed_deye_watts_plausible(db, monkeypatch):
    """Un raspuns Deye de 5000 inseamna 5000 W (5 kW), nu 5000 kW."""
    _, user, station, cloud_device = _setup_station(db, "implausible")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    raw = {"generationPower": 5000, "lastUpdateTime": 1757721600}
    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(svc, "fetch_station_latest", lambda token, station_id: raw)

    result = svc.poll_connection(db, conn)

    assert result["status"] == "succeeded"
    assert result["implausible_power_fields"] == []
    assert conn.last_sync_status == "succeeded"
    assert conn.last_sync_message is None
    row = db.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == cloud_device.id))
    assert row is not None
    assert float(row.pv_power_w) == 5000


def test_poll_connection_flags_truly_implausible_watt_values(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "implausible-w")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    raw = {"generationPower": 50_000_000, "lastUpdateTime": 1757721600}
    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(svc, "fetch_station_latest", lambda token, station_id: raw)

    result = svc.poll_connection(db, conn)

    assert result["status"] == "succeeded"
    assert result["implausible_power_fields"] == ["pv_power_w"]
    assert conn.last_sync_status == "warning"
    assert conn.last_sync_message is not None
    assert "pv_power_w" in conn.last_sync_message
    row = db.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == cloud_device.id))
    assert row is not None
    assert float(row.pv_power_w) == 50_000_000


def test_poll_connection_plausible_power_keeps_succeeded_status_without_message(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "plausible")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.flush()

    raw = {"generationPower": 3200, "lastUpdateTime": 1757721600}
    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(svc, "fetch_station_latest", lambda token, station_id: raw)

    result = svc.poll_connection(db, conn)
    assert result["implausible_power_fields"] == []
    assert conn.last_sync_status == "succeeded"
    assert conn.last_sync_message is None


def test_deye_watt_ingest_feeds_dashboard_kpis_and_aggregates_in_kw_kwh(db, monkeypatch):
    _, user, station, cloud_device = _setup_station(db, "dashboard-units")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    measured_at = utcnow().replace(minute=0, second=0, microsecond=0)
    raw = {
        "generationPower": 1263,
        "consumptionPower": 1110,
        "wirePower": 20,
        "lastUpdateTime": int(measured_at.timestamp()),
    }
    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(svc, "fetch_station_latest", lambda token, station_id: raw)

    svc.poll_connection(db, conn)
    summary = dashboard_service.get_summary(db, station)
    aggregation_service.reaggregate_range(db, station, measured_at, measured_at + timedelta(minutes=15))

    assert abs(summary["pv_power_kw"] - 1.263) < 0.001
    assert abs(summary["load_power_kw"] - 1.11) < 0.001
    assert abs(summary["grid_power_kw"] + 0.02) < 0.001  # Deye wirePower pozitiv -> export negativ in platforma
    raw_row = db.scalar(
        select(TelemetryRaw).where(
            TelemetryRaw.device_id == cloud_device.id,
            TelemetryRaw.source == TelemetrySource.deye_cloud.value,
        )
    )
    assert raw_row is not None
    assert raw_row.pv_power_w == Decimal("1263")

    interval = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start == measured_at,
        )
    )
    assert interval is not None
    assert interval.pv_energy_kwh == Decimal("0.1053")
    assert interval.load_energy_kwh == Decimal("0.0925")
    assert interval.grid_export_energy_kwh == Decimal("0.0017")


# --- Ciclul de viata al conexiunii: connect/select/disconnect ---------------


def test_start_connection_creates_pending_selection(db, monkeypatch):
    org = make_org(db, "Org Start")
    user = make_user(db, email="start@test.local")
    station = make_station(db, org, user, name="Statie Start")

    seen_credentials = {}

    def _auth(app_id, app_secret, email, password):
        seen_credentials.update(
            {"app_id": app_id, "app_secret": app_secret, "email": email, "password": password}
        )
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr(svc, "authenticate", _auth)
    monkeypatch.setattr(svc, "list_remote_stations", lambda token: [{"id": 322, "name": "Casa Test"}])

    conn, stations = svc.start_connection(db, station, user, "station-app-id", "station-app-secret", "client@example.com", "hunter2")
    assert conn.status == DeyeCloudConnectionStatus.pending_selection.value
    assert seen_credentials == {
        "app_id": "station-app-id",
        "app_secret": "station-app-secret",
        "email": "client@example.com",
        "password": "hunter2",
    }
    assert conn.app_id == "station-app-id"
    assert decrypt_secret(conn.encrypted_app_secret) == "station-app-secret"
    assert decrypt_secret(conn.encrypted_account_password) == "hunter2"
    assert stations == [{"id": 322, "name": "Casa Test"}]
    assert conn.pending_remote_stations == [{"id": 322, "name": "Casa Test"}]


def test_select_remote_station_creates_synthetic_device_and_links(db, monkeypatch):
    org = make_org(db, "Org Select")
    user = make_user(db, email="select@test.local")
    station = make_station(db, org, user, name="Statie Select")
    conn = _connection(
        db,
        station,
        user,
        status=DeyeCloudConnectionStatus.pending_selection.value,
        device_id=None,
        pending_remote_stations=[{"id": 322, "name": "Casa Test"}],
    )
    db.flush()

    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    monkeypatch.setattr(
        svc, "list_remote_station_devices",
        lambda token, remote_station_id: [{"deviceSn": "SN123", "deviceType": "INVERTER"}],
    )

    svc.select_remote_station(db, conn, 322)
    assert conn.status == DeyeCloudConnectionStatus.connected.value
    assert conn.device_id is not None
    assert len(conn.device_links) == 1
    assert conn.device_links[0].remote_device_sn == "SN123"
    assert conn.remote_station_name == "Casa Test"
    assert conn.pending_remote_stations == []


def test_select_remote_station_rejects_id_not_returned_for_account(db, monkeypatch):
    org = make_org(db, "Org Select Reject")
    user = make_user(db, email="select-reject@test.local")
    station = make_station(db, org, user, name="Statie Select Reject")
    conn = _connection(
        db,
        station,
        user,
        status=DeyeCloudConnectionStatus.pending_selection.value,
        device_id=None,
        pending_remote_stations=[{"id": 322, "name": "Casa Test"}],
    )

    monkeypatch.setattr(svc, "_valid_access_token", lambda c: "token")
    with pytest.raises(svc.DeyeCloudApiError, match="nu apartine"):
        svc.select_remote_station(db, conn, 999)

    assert conn.device_id is None


def test_disconnect_wipes_credentials_but_keeps_history(db):
    org = make_org(db, "Org Disc")
    user = make_user(db, email="disc@test.local")
    station = make_station(db, org, user, name="Statie Disc")
    cloud_device = make_device(db, station, name="Deye Cloud")
    conn = _connection(db, station, user, device_id=cloud_device.id)
    db.add(
        TelemetryRaw(
            device_id=cloud_device.id, station_id=station.id, boot_id=svc.CLOUD_BOOT_ID, sequence=1,
            measured_at=utcnow(), received_at=utcnow(), source=TelemetrySource.deye_cloud.value,
        )
    )
    db.flush()

    svc.disconnect(db, conn, user)
    assert conn.status == DeyeCloudConnectionStatus.disconnected.value
    assert conn.app_id is None
    assert conn.encrypted_app_secret is None
    assert conn.encrypted_access_token is None
    assert conn.encrypted_account_password == ""
    assert cloud_device.status == "revoked"
    assert cloud_device.revoked_at is not None
    row = db.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == cloud_device.id))
    assert row is not None  # istoricul ramane, nedistructiv


# --- Criptare la repaus ------------------------------------------------------


def test_encrypt_decrypt_round_trip():
    ciphertext = encrypt_secret("super-secret-password")
    assert ciphertext != "super-secret-password"
    assert decrypt_secret(ciphertext) == "super-secret-password"


def test_decrypt_fails_clearly_when_key_changes(monkeypatch):
    import app.core.crypto as crypto_module

    ciphertext = encrypt_secret("x")
    monkeypatch.setattr(crypto_module.settings, "secret_key", str(uuid.uuid4()))
    with pytest.raises(DecryptionError):
        decrypt_secret(ciphertext)
