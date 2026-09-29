import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from app.core.security import utcnow
from app.models.grid_voltage import GridVoltageSample
from app.models.telemetry import TelemetryRaw
from app.schemas.grid_voltage import GridVoltageHistory
from app.services import grid_voltage_service as service
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.integration.test_health_diagnostics import sample
from tests.web_helpers import login


@pytest.fixture()
def context(db):
    user = make_user(db, email=f"voltage-{uuid.uuid4()}@test.local")
    org = make_org(db)
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user)
    device = make_device(db, station)
    return user, station, device


def reading(db, station, device, at, value, **kwargs):
    return sample(
        db,
        station,
        device,
        at,
        diagnostics={
            "phases": [
                {"phase": "L1", "circuit": "grid", "voltage_v": value, "quality": "measured"},
                {"phase": "L2", "circuit": "load", "voltage_v": "222"},
            ]
        },
        **kwargs,
    )


def test_minute_extrema_gaps_zero_and_latest_missing(db, context):
    _, station, device = context
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    at = now - timedelta(minutes=10)
    reading(db, station, device, at, "230")
    reading(db, station, device, at + timedelta(seconds=10), "265")
    reading(db, station, device, at + timedelta(seconds=30), "225")
    reading(db, station, device, at + timedelta(minutes=3), "0")
    last = reading(db, station, device, now - timedelta(minutes=1), None)
    last.received_at = now
    db.flush()
    result = GridVoltageHistory.model_validate(service.history(db, station, now=now))
    phase = result.phases[0]
    points = [p for p in phase.points if p.samples]
    assert len(points) == 2
    assert points[0].mean_v == "240.000" and points[0].max_v == "265.000"
    assert points[0].min_v == "225.000" and points[0].samples == 3
    assert points[1].mean_v == "0.000"
    assert phase.min_v == "0.000" and phase.max_v == "265.000"
    assert phase.samples == 4 and phase.observed_minutes == 2
    assert phase.latest.value_v is None and phase.latest.quality == "missing"
    assert phase.latest.received_at == now
    assert phase.latest.measured_at == now - timedelta(minutes=1)
    assert all(p.mean_v is None and p.quality == "missing" for p in result.phases[1].points)
    assert result.phases[2].min_v is None
    assert result.start == datetime(2026, 9, 28, 21, tzinfo=UTC)
    assert len(phase.points) == 1440


def test_per_phase_provenance_and_no_carry_or_future_data(db, context):
    _, station, device = context
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    midnight = datetime(2026, 9, 28, 21, tzinfo=UTC)
    reading(db, station, device, midnight - timedelta(seconds=1), "999", is_simulated=True)
    reading(db, station, device, midnight + timedelta(minutes=2), "228", is_late=True)
    sample(
        db,
        station,
        device,
        midnight + timedelta(minutes=3),
        diagnostics={
            "phases": [
                {"phase": "L1", "voltage_v": "250", "quality": "simulated"},
                {"phase": "L2", "voltage_v": "231", "quality": "derived"},
                {"phase": "L3", "voltage_v": "229", "quality": "stale"},
            ]
        },
    )
    reading(db, station, device, now + timedelta(minutes=1), "999")
    result = service.history(db, station, now=now)
    l1, l2, l3 = result["phases"]
    assert l1["points"][0]["mean_v"] is None
    assert l1["max_v"] == "250.000" and l1["quality"] == "simulated"
    assert l1["flags"] == ["late", "simulated"]
    assert l2["quality"] == "derived" and l3["quality"] == "stale"
    assert l2["latest"]["quality"] == "stale"
    assert l1["latest"]["flags"] == ["simulated", "stale"]
    assert l1["points"][3]["flags"] == ["simulated"]


@pytest.mark.parametrize(
    "day,minutes",
    [(date(2026, 3, 29), 1380), (date(2026, 10, 25), 1500), (date(2024, 2, 29), 1440)],
)
def test_station_calendar_dst_and_leap_day(db, context, day, minutes):
    _, station, _ = context
    now = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
    result = service.history(db, station, day=day, now=now)
    assert len(result["phases"][0]["points"]) == minutes
    assert result["elapsed_minutes"] == minutes
    assert (result["end"] - result["start"]).total_seconds() == minutes * 60


def test_observations_stay_with_station_and_source(db, context):
    user, station, device = context
    other = make_station(db, make_org(db, "Other voltage org"), user)
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    reading(db, station, device, now - timedelta(minutes=1), "230")
    source_id = uuid.uuid4()
    service.ingest_deye(db, station.id, source_id, "Deye", now, now, {"L1": Decimal("240")})
    service.ingest_deye(db, other.id, source_id, "Other", now, now, {"L1": Decimal("999")})
    reading(db, other, device, now, "999")
    result = service.history(db, station, source_id=f"deye:{source_id}", now=now)
    assert result["phases"][0]["max_v"] == "240.000"
    native_id = next(s["id"] for s in result["sources"] if s["provider"] != "deye_cloud")
    assert (
        service.history(db, station, source_id=native_id, now=now)["phases"][0]["max_v"]
        == "230.000"
    )
    with pytest.raises(LookupError):
        service.history(db, other, source_id=f"deye:{uuid.uuid4()}", now=now)


def test_provider_retry_idempotency_does_not_write_power_rows(db, context):
    _, station, _ = context
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    source_id = uuid.uuid4()
    for _ in range(2):
        service.ingest_deye(
            db, station.id, source_id, "Deye", now, now, {"L1": Decimal(0), "L2": None}
        )
    assert (
        db.scalar(
            select(func.count())
            .select_from(GridVoltageSample)
            .where(GridVoltageSample.station_id == station.id)
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(TelemetryRaw)
            .where(TelemetryRaw.station_id == station.id)
        )
        == 0
    )
    result = service.history(db, station, now=now)
    assert result["phases"][0]["latest"]["value_v"] == "0.000"
    assert result["phases"][1]["latest"]["value_v"] is None


def test_route_viewer_scope_validation_and_no_store(db, client, context):
    user, station, device = context
    reading(db, station, device, utcnow(), "230")
    db.commit()
    login(client, user.email, "TestPass1234")
    url = f"/api/v1/stations/{station.id}/grid-voltage"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["phases"][0]["latest"]["value_v"] == "230.000"
    assert client.get(url, params={"source_id": "device:invalid"}).status_code == 404
    assert client.get(url, params={"day": "2099-01-01"}).status_code == 422
    assert client.get(url, params={"day": "2020-01-01"}).status_code == 422
    assert client.get(url, params={"day": "bad-date"}).status_code == 422
    other = make_station(db, make_org(db, "Other voltage org"), user)
    db.commit()
    assert client.get(f"/api/v1/stations/{other.id}/grid-voltage").status_code == 403


def test_populated_downgrade_and_reupgrade_preserves_null_and_zero(db, context):
    _, station, _ = context
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    source_id = uuid.uuid4()
    for offset, phases in enumerate(
        (
            {"L1": Decimal(0), "L2": None},
            {"L1": None, "L2": Decimal("241.25"), "L3": Decimal("230.1")},
        )
    ):
        service.ingest_deye(
            db, station.id, source_id, "Deye", now + timedelta(minutes=offset), now, phases
        )
    path = Path(__file__).parents[2] / "alembic/versions/b322c222d903_grid_voltage.py"
    spec = spec_from_file_location("voltage_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.op = Operations(MigrationContext.configure(db.connection()))
    expected = [(Decimal(0), None, None), (None, Decimal("241.25"), Decimal("230.1"))]
    migration.downgrade()
    retained = db.execute(
        text(
            "SELECT l1_v,l2_v,l3_v FROM legacy_grid_voltage_samples WHERE station_id=:id ORDER BY measured_at"
        ),
        {"id": station.id},
    ).all()
    assert retained == expected
    migration.upgrade()
    restored = db.execute(
        select(GridVoltageSample.l1_v, GridVoltageSample.l2_v, GridVoltageSample.l3_v)
        .where(GridVoltageSample.station_id == station.id)
        .order_by(GridVoltageSample.measured_at)
    ).all()
    assert restored == expected
