import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.config import get_settings
from app.core.rate_limit import RateLimitExceeded, reset_key
from app.core.security import utcnow
from app.models.deye_integration import DeyeCloudConnection
from app.models.ev import EVSE, ChargingSession, EVConnector
from app.models.forecast import PvForecast, WeatherForecast
from app.models.notification import Notification
from app.models.organization import Membership
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.models.user import Session as UserSession
from app.services import dashboard_service as dashboard
from app.services import mobile_service as mobile
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.integration.test_home_assistant_bridge import configured, ingest_data, sample
from tests.web_helpers import login

API = "/api/v1/mobile"


@pytest.fixture()
def context(db, client, monkeypatch):
    now = utcnow().replace(second=0, microsecond=0)
    user = make_user(db, email=f"mobile-{uuid4()}@test.local")
    org = make_org(db, "Mobile " + uuid4().hex)
    make_membership(db, user, org, "organization_admin")
    station = make_station(db, org, user)
    device = make_device(db, station)
    db.commit()
    reset_key("login_attempts:testclient")
    assert login(client, user.email, "TestPass1234").status_code == 303
    monkeypatch.setattr(mobile, "evaluation_time", lambda: now)
    return user, org, station, device, now


def raw(db, station, device, now, **changes):
    data = {
        "station_id": station.id,
        "device_id": device.id,
        "boot_id": uuid4().hex,
        "sequence": 1,
        "measured_at": now,
        "received_at": now,
        "pv_power_w": Decimal(1200),
        "load_power_w": None,
        "battery_soc_percent": Decimal(0),
    }
    data.update(changes)
    result = TelemetryRaw(**data)
    db.add(result)
    db.flush()
    return result


def aggregate(db, station, start, end, period="day", **changes):
    data = {
        "station_id": station.id,
        "period_type": period,
        "period_start": start,
        "period_end": end,
        "pv_energy_kwh": Decimal("1.2345"),
        "load_energy_kwh": None,
        "grid_import_energy_kwh": Decimal(0),
        "grid_export_energy_kwh": None,
        "avg_battery_soc_percent": Decimal(0),
        "coverage": {"pv": 0.5, "grid": 0.5, "soc": 1},
        "data_quality": "simulated",
    }
    data.update(changes)
    row = TelemetryAggregate(**data)
    db.add(row)
    db.flush()
    return row


def notice(db, user, station, at, index):
    row = Notification(
        user_id=user.id,
        station_id=station.id,
        source_key=f"day:test-{index}",
        title=f"Notice {index}",
        severity="info",
        category="summary",
        link="/notifications",
        payload={"body": "Safe summary", "secret": "never-return"},
        created_at=at,
    )
    db.add(row)
    db.flush()
    return row


def test_overview_reuses_canonical_kpis_and_preserves_units_null_provenance(db, client, context):
    user, org, station, device, now = context
    raw(db, station, device, now, is_simulated=True, quality_flags={"stale": True})
    start = dashboard._period_start_utc(station, "today", now)
    end = dashboard._period_end_utc(station, "today", now)
    aggregate(db, station, start, end)
    notice(db, user, station, now, 1)
    db.commit()
    response = client.get(API + "/overview")  # Sole-station selection.
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["station"]["id"] == str(station.id)
    assert data["station"]["currency"] == "RON"
    assert data["station"]["timezone"] == station.timezone
    assert data["live"]["pv_power_kw"]["value"] == "1.20"
    assert data["live"]["pv_power_kw"]["unit"] == "kW"
    assert data["live"]["pv_power_kw"]["source"] == "device_rs485"
    assert {"simulated", "stale"} <= set(data["live"]["pv_power_kw"]["flags"])
    assert data["live"]["load_power_kw"]["value"] is None
    assert Decimal(data["live"]["battery_soc_percent"]["value"]) == 0
    kpi = data["energy"]["today"]["metrics"]["pv"]
    assert (
        Decimal(kpi["value"])
        == dashboard.get_energy_period_kpis(db, station, now=now, exact=True)["today"]["metrics"][
            "pv"
        ]["value"]
    )
    assert kpi["quality"] == "simulated" and kpi["coverage"] == "0.5"
    assert data["unread_notifications"] == 1
    assert data["costs"]["month"]["net_cost"]["value"] is None
    assert data["forecast"]["pv_energy"]["value"] is None
    assert response.headers["cache-control"].startswith("private")
    assert "Last-Modified" in response.headers


def test_etag_reauthorizes_and_invalidates_on_data_and_user_changes(db, client, context):
    user, org, station, device, now = context
    row = raw(db, station, device, now)
    db.commit()
    path = API + f"/overview?station_id={station.id}"
    first = client.get(path)
    etag = first.headers["etag"]
    assert client.get(path, headers={"If-None-Match": "W/" + etag}).status_code == 304
    row.pv_power_w = 2000
    db.commit()
    changed = client.get(path, headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
    membership.is_active = False
    db.commit()
    denied = client.get(path, headers={"If-None-Match": changed.headers["etag"]})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert denied.headers["cache-control"] == "no-store"


def test_unauthenticated_expired_cross_tenant_no_station_and_station_selection(db, client, context):
    user, org, station, device, now = context
    other = make_station(db, make_org(db, "Foreign"), user)
    db.commit()
    assert (
        client.get(API + f"/overview?station_id={other.id}").json()["error"]["code"] == "forbidden"
    )
    assert (
        client.get(API + f"/overview?station_id={uuid4()}").json()["error"]["code"] == "no_station"
    )
    make_station(db, org, user, name="Second")
    db.commit()
    assert client.get(API + "/overview").json()["error"]["code"] == "station_required"
    sess = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    sess.revoked_at = utcnow()
    db.commit()
    assert client.get(API + "/overview").json()["error"]["code"] == "reauth_required"
    client.cookies.clear()
    assert client.get(API + "/overview").json()["error"]["code"] == "unauthenticated"
    # Integration/device bearer credentials are never accepted as human sessions.
    assert (
        client.get(API + "/overview", headers={"Authorization": "Bearer device-secret"}).status_code
        == 401
    )


def test_typed_rate_limit_and_validation(db, client, context, monkeypatch):
    def limited(*args):
        raise RateLimitExceeded(19)

    monkeypatch.setattr("app.api.mobile_support.check_fixed_window", limited)
    response = client.get(API + "/overview")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "19"
    assert response.json()["error"]["retry_after_seconds"] == 19
    monkeypatch.setattr("app.api.mobile_support.check_fixed_window", lambda *args: 1)
    assert client.get(API + "/notifications?limit=999").json()["error"]["code"] == "invalid_request"
    assert client.get(API + "/charts/energy?range=1y&resolution=15m").status_code == 422
    assert client.get(API + "/charts/energy?end=2026-01-01T10:00:00").status_code == 422


def test_forecast_latest_batch_and_weather_simulation_are_preserved(db, client, context):
    user, org, station, device, now = context
    weather = WeatherForecast(
        station_id=station.id,
        issued_at=now,
        interval_start=now + timedelta(hours=1),
        interval_end=now + timedelta(hours=2),
        is_synthetic=True,
    )
    db.add(weather)
    db.flush()
    for issued, power in ((now - timedelta(hours=1), 99), (now, 2)):
        db.add(
            PvForecast(
                station_id=station.id,
                issued_at=issued,
                interval_start=now + timedelta(hours=1),
                interval_end=now + timedelta(hours=2),
                predicted_power_kw=power,
                based_on_weather_forecast_id=weather.id,
                scenario="expected",
                confidence="low",
                is_synthetic=False,
            )
        )
    db.commit()
    forecast = client.get(API + "/overview").json()["forecast"]
    assert Decimal(forecast["pv_energy"]["value"]) == 2
    assert forecast["pv_energy"]["quality"] == "simulated"
    assert Decimal(forecast["pv_energy"]["coverage"]) == pytest.approx(Decimal(1) / 24)


@pytest.mark.parametrize(
    ("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2024, 2, 29), 24)]
)
def test_chart_station_calendar_dst_leap_and_no_raw_fallback(db, client, context, day, hours):
    user, org, station, device, now = context
    from app.services.battery_service import midnight

    start, end = (
        midnight(day, station.timezone),
        midnight(day + timedelta(days=1), station.timezone),
    )
    aggregate(db, station, start, end)
    response = client.get(
        API + "/charts/energy", params={"range": "7d", "resolution": "1d", "end": end.isoformat()}
    )
    # Autumn date may be in the future relative to the test runner; the pure service still tests its calendar.
    if end > utcnow():
        assert response.status_code == 422
        chart = mobile.chart(db, station, "7d", "1d", end=end).model_dump(mode="json")
    else:
        assert response.status_code == 200
        chart = response.json()
    point = chart["points"][0]
    assert (
        datetime.fromisoformat(point["end"]) - datetime.fromisoformat(point["start"])
    ).total_seconds() == hours * 3600
    assert point["values"]["load"] is None and Decimal(point["values"]["soc"]) == 0
    assert chart["units"]["pv"] == "kWh" and chart["aggregation"]["soc"] == "mean"


def test_notifications_keyset_ties_insertions_and_scoped_cursors(db, client, context):
    user, org, station, device, now = context
    original = [notice(db, user, station, now - timedelta(days=1), i) for i in range(5)]
    db.commit()
    first = client.get(API + "/notifications?limit=2").json()
    cursor = first["next_cursor"]
    notice(db, user, station, utcnow(), 99)
    db.commit()
    seen = [item["id"] for item in first["items"]]
    while cursor:
        response = client.get(API + "/notifications", params={"limit": 2, "cursor": cursor})
        assert response.status_code == 200, response.text
        data = response.json()
        seen.extend(item["id"] for item in data["items"])
        assert "never-return" not in response.text
        cursor = data["next_cursor"]
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == {str(n.id) for n in original}
    assert (
        client.get(
            API + "/notifications", params={"cursor": first["next_cursor"], "unread": True}
        ).json()["error"]["code"]
        == "invalid_cursor"
    )
    assert (
        client.get(API + "/charging-sessions", params={"cursor": first["next_cursor"]}).status_code
        == 422
    )
    other = make_station(db, org, user, name="Other")
    db.commit()
    assert (
        client.get(
            API + "/notifications", params={"station_id": other.id, "cursor": first["next_cursor"]}
        ).status_code
        == 422
    )


def test_charging_session_pagination_preserves_unknown_energy(db, client, context):
    user, org, station, device, now = context
    evse = EVSE(station_id=station.id, name="Charger")
    db.add(evse)
    db.flush()
    connector = EVConnector(evse_id=evse.id, number=1)
    db.add(connector)
    db.flush()
    for i in range(4):
        db.add(
            ChargingSession(
                station_id=station.id,
                connector_id=connector.id,
                started_at=now - timedelta(days=i + 1),
                ended_at=now - timedelta(days=i + 1) + timedelta(hours=1),
                state="completed",
                source="device",
                quality="measured",
                energy_kwh=None if i == 0 else Decimal(0),
            )
        )
    db.commit()
    first = client.get(API + "/charging-sessions?limit=2").json()
    assert first["items"][0]["energy"]["value"] is None
    assert Decimal(first["items"][1]["energy"]["value"]) == 0
    second = client.get(
        API + "/charging-sessions", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert second["next_cursor"] is None and len(second["items"]) == 2


def test_home_assistant_allowlist_units_stale_simulated_disconnect_and_no_secrets(
    db, client, context, monkeypatch
):
    user, org, station, device, now = context
    monkeypatch.setattr(get_settings(), "home_assistant_bridge_enabled", True)
    bridge, token = configured(db, station, user)
    from app.services import home_assistant_bridge as service

    bridge.mappings = [{**m, "quality": "simulated"} for m in bridge.mappings]
    service.ingest(
        bridge,
        ingest_data(
            bridge, [sample(value="1200", quality="simulated", observed_at=now.isoformat())]
        ),
    )
    bridge.observations = {**bridge.observations, "sensor.private": {"value": "never-return"}}
    db.commit()
    response = client.get(API + "/home-assistant/context")
    assert response.status_code == 200, response.text
    assert token not in response.text and "never-return" not in response.text
    entity = response.json()["entities"][0]
    assert entity["mapped_unit"] == "W" and entity["metric"]["unit"] == "kW"
    assert Decimal(entity["metric"]["value"]) == Decimal("1.2") and entity["is_simulated"]
    monkeypatch.setattr(mobile, "evaluation_time", lambda: now + timedelta(minutes=10))
    stale = client.get(API + "/home-assistant/context").json()["entities"][0]
    assert stale["metric"]["value"] is None and stale["is_simulated"] and stale["is_stale"]
    service.revoke(bridge)
    db.commit()
    disconnected = client.get(API + "/home-assistant/context").json()
    assert disconnected["integration"]["status"] == "disconnected"
    assert disconnected["entities"] == []


def test_provider_reauth_summary_redacts_credentials_and_never_calls_provider(
    db, client, context, monkeypatch
):
    user, org, station, device, now = context
    db.add(
        DeyeCloudConnection(
            station_id=station.id,
            created_by_user_id=user.id,
            status="error",
            account_email="private@example.com",
            encrypted_account_password="secret",
            consent_accepted_at=now,
        )
    )
    db.commit()
    monkeypatch.setattr(
        "app.services.deye_cloud_service._post",
        lambda *args, **kwargs: pytest.fail("No provider request allowed"),
    )
    response = client.get(API + "/overview")
    assert response.status_code == 200
    deye = response.json()["integrations"][0]
    assert deye["issues"][0]["code"] == "reauth_required"
    assert "private@example.com" not in response.text and "secret" not in response.text


def test_first_screen_query_budget_payload_and_warm_load(db, client, context):
    from tests.unit.test_dashboard_service import _add_hour, _add_tariff_version

    user, org, station, device, now = context
    raw(db, station, device, now)
    _add_tariff_version(
        db, station, "import", valid_from=now - timedelta(days=60), fixed_price="0.80"
    )
    _add_tariff_version(
        db, station, "export", valid_from=now - timedelta(days=60), fixed_price="0.40"
    )
    _add_hour(db, station, now - timedelta(hours=1), load=2, pv=1, grid_import=1, grid_export=0)
    for index in range(24):
        db.add(
            PvForecast(
                station_id=station.id,
                issued_at=now,
                interval_start=now + timedelta(hours=index),
                interval_end=now + timedelta(hours=index + 1),
                predicted_power_kw=2,
                scenario="expected",
                confidence="medium",
            )
        )
    db.commit()
    queries = []

    def count(conn, cursor, statement, parameters, ctx, many):
        queries.append(statement)

    event.listen(db.bind, "before_cursor_execute", count)
    try:
        first = client.get(API + "/overview")
        assert first.status_code == 200, first.text
        baseline = len(queries)
        for i in range(35):
            extra = make_device(db, station, name=f"Source {i}")
            raw(db, station, extra, now)
            notice(db, user, station, now, i)
        for hour in range(2, 170):
            _add_hour(
                db, station, now - timedelta(hours=hour), load=2, pv=1, grid_import=1, grid_export=0
            )
        db.commit()
        queries.clear()
        durations = []
        for _ in range(8):
            before = perf_counter()
            response = client.get(API + "/overview")
            durations.append(perf_counter() - before)
            assert response.status_code == 200, response.text
            assert len(response.content) < 40000
        assert len(queries) <= 8 * (baseline + 2)
        assert len(queries) / 8 <= 45
        assert sorted(durations)[-1] < 2.0
        assert response.json()["sources"]["truncated"]
        print(
            json.dumps(
                {
                    "queries": len(queries) / 8,
                    "bytes": len(response.content),
                    "p95_ms": round(sorted(durations)[-1] * 1000, 1),
                }
            )
        )
    finally:
        event.remove(db.bind, "before_cursor_execute", count)


def test_forecast_includes_carry_in_and_ignores_future_issue_time(db, context):
    user, org, station, device, now = context
    for issued, power in ((now - timedelta(hours=2), 2), (now + timedelta(hours=1), 99)):
        db.add(
            PvForecast(
                station_id=station.id,
                issued_at=issued,
                interval_start=now - timedelta(minutes=30),
                interval_end=now + timedelta(minutes=30),
                predicted_power_kw=power,
                scenario="expected",
                confidence="low",
                is_synthetic=True,
            )
        )
    db.flush()
    forecast = mobile.forecast_summary(db, station, now)
    assert forecast.pv_energy.value == Decimal(1)
    assert forecast.pv_energy.quality == "simulated"
    assert forecast.pv_energy.coverage == Decimal(1) / 48


def test_cost_uses_decimal_rounding_and_weakest_input_coverage(db, context):
    from tests.unit.test_dashboard_service import _add_tariff_version

    user, org, station, device, now = context
    start = now - timedelta(hours=1)
    _add_tariff_version(
        db, station, "import", valid_from=start - timedelta(days=1), fixed_price="2.675"
    )
    aggregate(
        db,
        station,
        start,
        now,
        period="hour",
        load_energy_kwh=Decimal(2),
        pv_energy_kwh=Decimal(1),
        grid_import_energy_kwh=Decimal(1),
        grid_export_energy_kwh=Decimal(0),
        data_quality="stale",
        coverage={"pv": 0.25, "load": 0.5, "grid": 0.75},
    )
    result = mobile.cost_summary(db, station, start, now)
    assert result.net_cost.value == Decimal("2.68")
    assert result.net_cost.coverage == Decimal("0.25")
    assert result.net_cost.quality == "stale" and "partial" in result.net_cost.flags
    assert dashboard.get_estimated_savings(db, station, start, now)["actual_net_cost_lei"] == 2.68


def test_month_comparison_does_not_spill_into_current_month(db, context):
    user, org, station, device, _ = context
    now = datetime(2026, 3, 31, 12, tzinfo=UTC)
    start = dashboard._period_start_utc(station, "month", now)
    end = dashboard._period_end_utc(station, "month", now)
    aggregate(db, station, start, end, period="month", coverage={"pv": 1})
    previous = dashboard._previous_period_start_utc(station, "month", now)
    day = previous
    while day < now:
        aggregate(db, station, day, day + timedelta(days=1), coverage={"pv": 1})
        day += timedelta(days=1)
    assert (
        dashboard.get_energy_period_kpis(db, station, now=now, exact=True)["month"]["metrics"][
            "pv"
        ]["comparison"]
        is None
    )


def test_future_and_flagged_stale_sources_are_not_reported_fresh(db, client, context):
    user, org, station, device, now = context
    row = raw(db, station, device, now + timedelta(hours=1))
    db.commit()
    data = client.get(API + "/overview").json()
    assert data["live"]["pv_power_kw"]["quality"] == "stale"
    assert data["sources"]["devices"][0]["freshness"] == "stale"
    assert any(i["code"] == "stale" and i["resource"] == "telemetry" for i in data["issues"])
    row.measured_at, row.quality_flags = now, {"stale": True, "simulated": True}
    db.commit()
    data = client.get(API + "/overview").json()
    assert data["sources"]["devices"][0]["quality"] == "simulated"
    assert data["sources"]["devices"][0]["freshness"] == "stale"


def test_viewer_read_and_archived_organization_reauthorization(db, client, context):
    user, org, station, device, now = context
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
    membership.role = "viewer"
    db.commit()
    assert client.get(API + "/overview").status_code == 200
    org.status = "archived"
    db.commit()
    assert client.get(API + f"/overview?station_id={station.id}").status_code == 403
    assert client.get(API + "/overview").json()["error"]["code"] == "no_station"
