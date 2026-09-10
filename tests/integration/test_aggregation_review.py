from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.models.station import StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.services import aggregation_service as agg
from app.services import consumption_forecast_service as forecast
from app.services import dashboard_service as dashboard
from tests.factories import make_device, make_org, make_station, make_user


def test_nullable_flow_and_simulated_carry_in(db):
    user = make_user(db)
    station = make_station(db, make_org(db), user)
    device = make_device(db, station)
    start = datetime(2026, 6, 15, 10, tzinfo=UTC)
    for i, (seconds, simulated) in enumerate([(-30, True), (60, False), (360, False), (660, False)]):
        db.add(TelemetryRaw(device_id=device.id, station_id=station.id, boot_id='review', sequence=i,
                            measured_at=start+timedelta(seconds=seconds), received_at=start,
                            pv_power_w=Decimal(1000), load_power_w=Decimal(500), is_simulated=simulated))
    db.flush()
    value = agg.aggregate_interval_15m(db, station.id, start)
    assert value['data_quality'] == 'simulated'
    assert value['ev_energy_kwh'] is None
    rows = dashboard.get_energy_totals(db, station, 'interval_15m', 1)
    assert rows[0]['grid_import_kwh'] is None
    assert rows[0]['coverage']['load'] == 1
    # Explicit EV-disabled config makes the missing component structurally zero.
    config = db.scalar(select(StationConfigVersion))
    if config is None:
        with pytest.raises(forecast.ConsumptionForecastError):
            forecast.generate_consumption_forecast(db, station, start, start+timedelta(minutes=15))
    else:
        config.ev_enabled = False
        db.flush()
        result = forecast.generate_consumption_forecast(db, station, start, start+timedelta(minutes=15))
        assert result[0].ev_component_kw == 0


def test_migration_retains_values_and_separates_calendars(db, monkeypatch):
    spec = spec_from_file_location('coverage_migration', Path(__file__).parents[2] / 'alembic/versions/a3f7c9d1e6b2_telemetry_aggregate_coverage.py')
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    columns = migration._ENERGY_COLUMNS
    db.execute(text('CREATE TEMP TABLE telemetry_aggregates (period_type varchar(16), '+
                    ','.join(c+' numeric(12,4) NOT NULL' for c in columns)+') ON COMMIT DROP'))
    db.execute(text("INSERT INTO telemetry_aggregates VALUES ('day',5,2,1,1,2,3,0)"))
    monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(db.connection())))
    migration.upgrade()
    assert db.execute(text('SELECT period_type FROM telemetry_aggregates')).scalar_one() == 'legacy_day'
    db.execute(text("INSERT INTO telemetry_aggregates VALUES ('day',5,2,1,1,2,3,NULL,'{}')"))
    migration.downgrade()
    rows = db.execute(text('SELECT period_type,pv_energy_kwh,ev_energy_kwh FROM telemetry_aggregates ORDER BY period_type')).all()
    assert rows == [('day', Decimal(5), Decimal(0)), ('local_day', Decimal(5), Decimal(0))]
