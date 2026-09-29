from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.station import StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.schemas.device_api import TelemetryItem
from app.services import battery_service as battery
from app.services.device_service import ingest_telemetry_batch
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.web_helpers import login


@pytest.fixture()
def setup(db):
    user = make_user(db)
    org = make_org(db)
    make_membership(db, user, org, "viewer")
    station = make_station(db, org, user)
    device = make_device(db, station)
    config = db.scalar(select(StationConfigVersion).where(StationConfigVersion.station_id == station.id))
    config.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    db.flush()
    return user, station, device, config


def sample(db, station, device, at, power=None, soc=None, **extra):
    row = TelemetryRaw(station_id=station.id, device_id=device.id, boot_id=str(uuid4()), sequence=1,
                       measured_at=at, received_at=at, battery_power_w=power, battery_soc_percent=soc, **extra)
    db.add(row)
    db.flush()
    return row


def test_dc_throughput_charge_discharge_and_soh_are_separate(db, setup):
    user, station, device, config = setup
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    for minute in range(0, 120, 5):
        sample(db, station, device, start + timedelta(minutes=minute), power=6000 if minute < 60 else -6000, soc=65,
               diagnostics={"battery": {"soh_percent": "92", "temperature_c": "24", "quality": "measured", "cycle_count": 50}})
    result = battery.detail(db, station, days=1, now=start + timedelta(hours=2))
    assert result.period.charge_kwh == result.period.discharge_kwh == Decimal(6)
    assert result.period.efc == Decimal("0.6")
    assert result.period.coverage == 1
    assert result.period.complete_days == 0
    assert result.current.power.value == -6
    assert result.current.state == "discharging"
    assert result.current.stored_energy.value == Decimal("6.5")
    assert result.current.stored_energy.quality == "estimated"
    assert result.current.soh.value == 92
    assert result.current.soh.status == "measured"
    assert result.current.soh.method == "source_reported"
    assert result.current.reported_cycles.value == 50
    assert len(result.hourly) == 24
    assert result.hourly[2].charge_kwh is None  # Future hours are unknown, not zero.


def test_explicit_zero_and_missing_values_break_the_hold(db, setup):
    user, station, device, config = setup
    config.battery_available_capacity_kwh = 0
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    sample(db, station, device, start, power=6000, soc=0)
    sample(db, station, device, start + timedelta(minutes=1), power=None, soc=0)
    sample(db, station, device, start + timedelta(minutes=5), power=0, soc=0)
    result = battery.detail(db, station, days=1, now=start + timedelta(minutes=10))
    assert result.period.charge_kwh == Decimal("0.1")
    assert result.period.discharge_kwh == 0
    assert result.period.coverage == Decimal("0.6")
    assert result.period.efc == Decimal("0.005")
    assert result.period.efc_reason == "partial_history"
    assert result.hourly[0].soc_percent == 0
    assert result.hourly[0].coverage["soc"] == 1
    assert result.current.usable_capacity.value == 0
    assert result.current.soh.status == "unavailable"


def test_capacity_change_splits_throughput_without_rewriting_history(db, setup):
    user, station, device, config = setup
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    new = StationConfigVersion(station_id=station.id, version=2, pv_installed_power_kw=5, inverter_power_kw=5,
                               battery_reference_capacity_kwh=20, created_at=start + timedelta(seconds=150))
    db.add(new)
    sample(db, station, device, start, power=24000)
    result = battery.detail(db, station, days=1, now=start + timedelta(minutes=5))
    assert result.period.charge_kwh == 2
    assert result.period.efc == Decimal("0.075")  # 1/(2*10) + 1/(2*20)
    assert len(result.period.capacity_basis) == 2


@pytest.mark.parametrize("capacity", [None, Decimal(0)])
def test_unknown_or_zero_capacity_cannot_create_efc(db, setup, capacity):
    user, station, device, config = setup
    config.battery_reference_capacity_kwh = capacity
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    sample(db, station, device, start, power=6000)
    result = battery.detail(db, station, days=1, now=start + timedelta(minutes=5))
    assert result.period.charge_kwh == Decimal("0.5")
    assert result.period.efc is None
    assert result.period.efc_reason == "capacity_missing"


def test_current_configuration_is_not_applied_before_its_creation(db, setup):
    user, station, device, config = setup
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    config.created_at = start + timedelta(minutes=10)
    sample(db, station, device, start, power=6000)
    result = battery.detail(db, station, days=1, now=start + timedelta(minutes=20))
    assert result.period.efc is None
    assert result.current.nominal_capacity.value == 10


def test_carry_in_keeps_all_provenance_and_never_fills_long_gaps(db, setup):
    user, station, device, config = setup
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    sample(db, station, device, start - timedelta(minutes=2), power=6000, is_simulated=True,
           quality_flags={"stale": True}, diagnostics={"battery": {"temperature_c": "33", "quality": "derived"}})
    result = battery.detail(db, station, days=1, now=start + timedelta(hours=1))
    assert result.period.charge_kwh == Decimal("0.3")
    assert result.period.coverage == Decimal("0.05")
    assert result.period.quality == "simulated"
    assert {"simulated", "stale"} <= set(result.period.flags)
    assert result.hourly[0].quality["temperature"] == "simulated"
    assert {"derived", "stale", "simulated"} <= set(result.hourly[0].flags["temperature"])


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2024, 2, 29), 24)])
def test_station_calendar_dst_and_leap_day(db, setup, day, hours):
    user, station, device, config = setup
    start, end = battery.midnight(day, station.timezone), battery.midnight(day + timedelta(days=1), station.timezone)
    for minute in range(0, hours * 60, 5):
        sample(db, station, device, start + timedelta(minutes=minute), power=1000)
    result = battery.detail(db, station, days=1, day=day, end=day, now=end + timedelta(hours=1))
    assert len(result.hourly) == hours
    assert result.period.charge_kwh == pytest.approx(Decimal(hours))
    assert result.period.coverage == 1
    assert result.period.efc == pytest.approx(Decimal(hours) / 20)
    assert result.period.complete_days == 1
    assert len({b.start for b in result.hourly}) == hours


def test_incompatible_packs_and_device_banks_are_never_merged(db, setup):
    user, station, device, config = setup
    start = battery.midnight(date(2026, 9, 1), station.timezone)
    other = make_device(db, station, name="Second inverter")
    for minute in range(0, 60, 5):
        sample(db, station, device, start + timedelta(minutes=minute), power=4000, soc=30, diagnostics={"battery_packs": [
            {"pack_id": "a", "power_w": "1000", "soc_percent": "0", "nominal_capacity_kwh": "10"},
            {"pack_id": "b", "power_w": "-2000", "soc_percent": "80", "nominal_capacity_kwh": "20"},
        ]})
        sample(db, station, other, start + timedelta(minutes=minute), power=8000, soc=50)
    now = start + timedelta(hours=1)
    a = battery.detail(db, station, battery_id=f"{device.id}:pack:a", days=1, now=now)
    b = battery.detail(db, station, battery_id=f"{device.id}:pack:b", days=1, now=now)
    assert a.period.charge_kwh == pytest.approx(Decimal(1))
    assert a.period.discharge_kwh == 0
    assert b.period.discharge_kwh == pytest.approx(Decimal(2))
    assert a.period.efc == b.period.efc == pytest.approx(Decimal("0.05"))
    assert a.current.soc.value == 0
    assert b.current.soc.value == 80
    summary = battery.summary(db, station, now=now)
    banks = [b for b in summary.batteries if b.target.scope == "reported_bank"]
    assert all(b.nominal_capacity.value is None for b in banks)


@pytest.mark.parametrize(("temperature", "flags", "minutes", "code"), [(55, {}, 0, "hot"), (-2, {}, 0, "cold"), (55, {"simulated": True}, 0, None), (55, {}, 20, "stale")])
def test_capability_temperature_notices_and_staleness(db, setup, temperature, flags, minutes, code):
    user, station, device, config = setup
    now = datetime.now(UTC)
    sample(db, station, device, now - timedelta(minutes=minutes), soc=50, quality_flags=flags,
           diagnostics={"battery": {"temperature_c": str(temperature)}})
    result = battery.detail(db, station, days=1, now=now)
    assert [n.code for n in result.notices] == ([code] if code else [])
    assert result.current.soh.value is None
    assert result.current.voltage.value is None


def test_deye_only_exposes_verified_fields_and_preserves_null(db, setup):
    user, station, device, config = setup
    now = datetime.now(UTC)
    sample(db, station, device, now, power=-600, soc=65, source="deye_cloud",
           raw_payload={"battery": {"temperature_c": 24, "soh_percent": 98}})
    live = battery.summary(db, station, now=now).batteries[0]
    assert live.power.value == Decimal("-0.6")
    assert live.temperature.supported is False
    assert live.temperature.value is None
    assert live.soh.status == "unavailable"
    assert live.nominal_capacity.quality == "declared"


def test_legacy_derived_soh_is_estimated_even_without_new_metadata(db, setup):
    user, station, device, config = setup
    now = datetime.now(UTC)
    sample(db, station, device, now, soc=65, quality_flags={"derived": True},
           diagnostics={"battery": {"soh_percent": "88"}})
    live = battery.summary(db, station, now=now).batteries[0]
    assert live.soh.status == "estimated"
    assert live.soh.quality == "estimated"
    assert live.soh.method is None  # Unknown legacy estimator is not invented.


def test_pack_ingestion_persists_validated_metadata_and_rejects_duplicates(db, setup):
    user, station, device, config = setup
    item = TelemetryItem(boot_id="pack-test", sequence=1, measured_at=datetime.now(UTC), battery_packs=[
        {"pack_id": "pack-1", "power_w": "1200", "soc_percent": "60", "nominal_capacity_kwh": "10.2",
         "soh_percent": "91", "soh_kind": "estimated", "soh_method": "bms_estimator", "soh_method_version": "1"}])
    result = ingest_telemetry_batch(db, device, [item])
    assert result[0] == 1
    row = db.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == device.id))
    assert row.diagnostics["battery_packs"][0]["power_w"] == "1200"
    live = battery.summary(db, station).batteries[0]
    assert live.soh.status == "estimated"
    assert live.soh.quality == "estimated"
    duplicate = item.model_copy(update={"sequence": 2, "battery_packs": item.battery_packs * 2})
    result = ingest_telemetry_batch(db, device, [duplicate])
    assert result[2] == 1
    assert result[-1][0].reason_code == "duplicate_metric"


def test_single_pack_does_not_inherit_station_bank_capacity(db, setup):
    user, station, device, config = setup
    now = datetime.now(UTC)
    sample(db, station, device, now - timedelta(minutes=5), diagnostics={
        "battery_packs": [{"pack_id": "single", "power_w": "1000", "soc_percent": "60"}],
    })
    result = battery.detail(db, station, days=1, now=now)
    assert len(result.targets) == 1
    assert result.current.target.pack_id == "single"
    assert result.current.nominal_capacity.value is None
    assert result.period.efc is None
    assert result.period.charge_kwh > 0


def test_human_api_authorization_openapi_and_cross_tenant(db, client, setup):
    user, station, device, config = setup
    path = f"/api/v1/stations/{station.id}/battery-health"
    assert client.get(path).status_code == 401
    reset_key("login_attempts:testclient")
    assert login(client, user.email, "TestPass1234").status_code == 303
    result = client.get(path)
    assert result.status_code == 200
    assert result.json()["today"]["charge_kwh"] is None
    assert client.get(path + "/summary").status_code == 200
    assert client.get(f"/stations/{station.id}/battery").status_code == 200
    other = make_station(db, make_org(db, "Other tenant"), user, name="Other station")
    foreign_device = make_device(db, other)
    assert client.get(f"/api/v1/stations/{other.id}/battery-health").status_code == 403
    assert client.get(f"/stations/{other.id}/battery").status_code == 403
    assert client.get(path, params={"battery_id": f"{foreign_device.id}:bank"}).status_code == 404
    assert client.get(path, params={"days": 32}).status_code == 422
    assert client.get(path, params={"end": "2099-01-01"}).status_code == 422
    assert client.get(path, params={"end": "0001-01-01"}).status_code == 422
    schemas = client.get("/api/openapi.json").json()["components"]["schemas"]
    assert "BatteryDetail" in schemas and "BatterySummary" in schemas
    assert "battery_packs" in schemas["TelemetryItem"]["properties"]
