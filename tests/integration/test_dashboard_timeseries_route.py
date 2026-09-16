"""Teste HTTP pentru /stations/{id}/data/timeseries (issue #33): rezolutia
aleasa server-side, metadatele explicite din raspuns si empty-state-ul care
nu devine niciodata o serie umpluta cu zero."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _setup(db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Timeseries Route Org")
    user = make_user(db, email="tsroute@test.local", password="Password1234")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="Timeseries Route Station")
    device = make_device(db, station)
    db.commit()
    return user, station, device


def _add_raw(db, station, device, measured_at, *, pv=1.0, soc=50.0, sequence=1):
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="boot-1", sequence=sequence,
            measured_at=measured_at, received_at=measured_at,
            pv_power_w=Decimal(str(pv * 1000)), battery_soc_percent=Decimal(str(soc)),
        )
    )
    db.flush()


def _period_start(station, period):
    local_now = utcnow().astimezone(ZoneInfo(station.timezone))
    if period == "day":
        local_start = datetime(local_now.year, local_now.month, local_now.day, tzinfo=ZoneInfo(station.timezone))
    else:
        local_start = datetime(local_now.year, local_now.month, 1, tzinfo=ZoneInfo(station.timezone))
    return local_start.astimezone(UTC)


def test_timeseries_route_declares_resolution_aggregation_timezone_coverage(client, db):
    user, station, device = _setup(db)
    _add_raw(db, station, device, utcnow() - timedelta(minutes=30), sequence=1)
    _add_raw(db, station, device, utcnow() - timedelta(minutes=10), sequence=2)
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/data/timeseries?range=24h")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resolution"] == "15m"
    assert body["aggregation"]["soc_pct"] == "mean"
    assert body["timezone"] == station.timezone
    assert isinstance(body["coverage"], float)
    assert isinstance(body["points"], list)
    assert len(body["points"]) >= 1


def test_timeseries_route_uses_daily_resolution_for_one_year_range(client, db):
    user, station, device = _setup(db)
    _add_raw(db, station, device, utcnow() - timedelta(days=200), sequence=1)
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/data/timeseries?range=1y")

    assert resp.status_code == 200
    assert resp.json()["resolution"] == "1d"


def test_timeseries_route_returns_empty_points_not_zero_series_when_no_data(client, db):
    user, station, _device = _setup(db)
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/data/timeseries?range=24h")

    assert resp.status_code == 200
    body = resp.json()
    assert body["points"] == []
    assert body["coverage"] == 0.0


def test_timeseries_route_forbidden_for_other_organizations_station(client, db):
    user, _station, _device = _setup(db)
    other_org = make_org(db, "Timeseries Route Org 2")
    other_owner = make_user(db, email="tsroute-other@test.local", password="Password1234")
    other_station = make_station(db, other_org, other_owner, name="Other Station")
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{other_station.id}/data/timeseries?range=24h")

    assert resp.status_code == 403


def test_energy_kpi_route_keeps_missing_separate_from_zero(client, db):
    user, station, _device = _setup(db)
    day_start = _period_start(station, "day")
    db.add(
        TelemetryAggregate(
            station_id=station.id, period_type="day",
            period_start=day_start, period_end=day_start + timedelta(days=1),
            pv_energy_kwh=Decimal("0.0000"), load_energy_kwh=None,
            grid_import_energy_kwh=Decimal("2.5000"), grid_export_energy_kwh=None,
            battery_charge_energy_kwh=None, battery_discharge_energy_kwh=None,
            sample_count=1, data_quality="measured", coverage={"pv": 1.0, "grid": 1.0},
        )
    )
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/data/energy-kpis")

    assert resp.status_code == 200
    today = resp.json()["today"]["metrics"]
    assert today["pv"] == {"value": 0.0, "coverage": 1.0, "quality": "measured", "comparison": None}
    assert today["load"]["value"] is None
    assert today["load"]["quality"] == "missing"
    assert today["grid_import"]["value"] == 2.5
    assert resp.json()["month"]["metrics"]["pv"]["value"] is None


def test_energy_kpi_route_marks_partial_coverage(client, db):
    user, station, _device = _setup(db)
    month_start = _period_start(station, "month")
    db.add(
        TelemetryAggregate(
            station_id=station.id, period_type="month",
            period_start=month_start, period_end=month_start + timedelta(days=31),
            pv_energy_kwh=Decimal("12.5000"), load_energy_kwh=Decimal("20.0000"),
            grid_import_energy_kwh=None, grid_export_energy_kwh=None,
            battery_charge_energy_kwh=None, battery_discharge_energy_kwh=None,
            sample_count=1, data_quality="measured", coverage={"pv": 0.5, "load": 1.0},
        )
    )
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/data/energy-kpis")

    assert resp.status_code == 200
    month = resp.json()["month"]["metrics"]
    assert month["pv"] == {"value": 12.5, "coverage": 0.5, "quality": "partial", "comparison": None}
    assert month["load"] == {"value": 20.0, "coverage": 1.0, "quality": "measured", "comparison": None}


def test_dashboard_chart_widgets_have_isolated_retry_errors(client, db):
    user, station, _device = _setup(db)
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/?station_id={station.id}")

    assert resp.status_code == 200
    for chart_id in [
        "chart-power",
        "chart-soc",
        "chart-prices",
        "chart-plan",
        "chart-forecast-pv",
        "chart-forecast-load",
        "chart-heatmap",
        "chart-energy-daily",
        "chart-energy-monthly",
    ]:
        idx = resp.text.find(f'id="{chart_id}"')
        assert idx != -1
        card_start = resp.text.rfind('<div class="card', 0, idx)
        assert card_start != -1
        card_html = resp.text[card_start:idx]
        assert "empty-state" in card_html
        assert "error-state" in card_html
        assert "retry-btn" in card_html
