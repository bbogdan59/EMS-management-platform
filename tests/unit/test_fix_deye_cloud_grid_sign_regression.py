"""Regresie comportamentala pentru scripts/fix_deye_cloud_grid_sign_regression.py.

Simptom real raportat: import/export afisate inversat pe dashboard in
"Astazi"/"Luna curenta". Cauza radacina, confirmata din istoricul git:
PR #173 (2026-09-18) a scris `grid_power_w = -wirePower` pentru randurile
Deye Cloud pana la reparare in PR #178 (2026-09-23); semnul e persistat
definitiv la ingestie, deci randurile din acea fereastra raman gresite
pentru totdeauna daca nu sunt corectate explicit -- fixul din PR #178 nu
recalculeaza retroactiv nimic."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.enums import TelemetrySource
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from scripts.fix_deye_cloud_grid_sign_regression import correct_deye_cloud_grid_sign
from tests.factories import make_device, make_org, make_station, make_user


def _setup_station(db, suffix: str):
    org = make_org(db, f"Org SignFix {suffix}")
    user = make_user(db, email=f"signfix{suffix}@test.local")
    station = make_station(db, org, user, name=f"Statie SignFix {suffix}")
    cloud_device = make_device(db, station, name="Deye Cloud (Casa Test)")
    cloud_device.capabilities = {"deye_cloud": True, "read_only": True}
    db.add(cloud_device)
    db.flush()
    return station, cloud_device


def _cloud_row(device, station, *, measured_at, wire_power, stored_grid_w, sequence, battery_power_w=None):
    return TelemetryRaw(
        device_id=device.id, station_id=station.id, boot_id="deye_cloud", sequence=sequence,
        measured_at=measured_at, received_at=measured_at, source=TelemetrySource.deye_cloud.value,
        raw_payload={"wirePower": wire_power, "generationPower": 500, "consumptionPower": 300},
        grid_power_w=Decimal(str(stored_grid_w)),
        battery_power_w=Decimal(str(battery_power_w)) if battery_power_w is not None else None,
    )


def test_corrects_rows_from_regression_window_only(db):
    """Un rand cu semnul inca inversat (regresia PR #173) e corectat la
    valoarea `wirePower` bruta; un rand deja corect (in afara ferestrei, sau
    scris dupa fixul PR #178) ramane neschimbat."""
    station, device = _setup_station(db, "window")
    t0 = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)  # in fereastra regresiei
    t1 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)  # dupa fix (#178)

    wrong = _cloud_row(device, station, measured_at=t0, wire_power=-500, stored_grid_w=500, sequence=1)
    correct = _cloud_row(device, station, measured_at=t1, wire_power=-200, stored_grid_w=-200, sequence=2)
    db.add_all([wrong, correct])
    db.flush()

    result = correct_deye_cloud_grid_sign(db)
    db.commit()

    assert result["corrected"] == 1
    assert result["unchanged"] == 1

    db.refresh(wrong)
    db.refresh(correct)
    assert wrong.grid_power_w == Decimal("-500")
    assert correct.grid_power_w == Decimal("-200")


def test_leaves_battery_and_local_device_rows_untouched(db):
    """Regresia a afectat STRICT `grid_power_w` pe randurile `deye_cloud` --
    `battery_power_w` si telemetria de la dispozitivul local (`device_rs485`)
    nu au fost niciodata implicate si nu trebuie atinse."""
    station, device = _setup_station(db, "scope")
    t0 = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)

    cloud_row = _cloud_row(device, station, measured_at=t0, wire_power=-500, stored_grid_w=500, sequence=1, battery_power_w="300")
    local_device = make_device(db, station, name="EMS local")
    local_row = TelemetryRaw(
        device_id=local_device.id, station_id=station.id, boot_id="boot-1", sequence=1,
        measured_at=t0, received_at=t0, source=TelemetrySource.device_rs485.value,
        grid_power_w=Decimal("500"), raw_payload={},
    )
    db.add_all([cloud_row, local_row])
    db.flush()

    result = correct_deye_cloud_grid_sign(db)
    db.commit()

    assert result["corrected"] == 1
    db.refresh(cloud_row)
    db.refresh(local_row)
    assert cloud_row.grid_power_w == Decimal("-500")
    assert cloud_row.battery_power_w == Decimal("300")  # neatins
    assert local_row.grid_power_w == Decimal("500")  # neatins -- sursa locala, niciodata afectata


def test_is_idempotent_second_run_changes_nothing(db):
    station, device = _setup_station(db, "idempotent")
    t0 = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    row = _cloud_row(device, station, measured_at=t0, wire_power=-500, stored_grid_w=500, sequence=1)
    db.add(row)
    db.flush()

    first = correct_deye_cloud_grid_sign(db)
    db.commit()
    second = correct_deye_cloud_grid_sign(db)
    db.commit()

    assert first["corrected"] == 1
    assert second["corrected"] == 0
    assert second["unchanged"] == 1


def test_reaggregates_touched_stations_so_dashboard_kpis_recover(db):
    """Dupa corectie, agregatele zilei sunt refacute din datele corecte --
    cardul 'Astazi' trebuie sa arate import/export corect, nu doar
    `TelemetryRaw` corectat fara efect vizibil pe dashboard."""
    station, device = _setup_station(db, "reagg")
    day_start = datetime(2026, 9, 19, 0, 0, tzinfo=UTC)
    t0 = day_start + timedelta(hours=10)
    t1 = t0 + timedelta(minutes=15)

    # Import de 500W (gresit stocat ca -500 in timpul regresiei); a doua
    # proba mentine acelasi semn pentru 15 minute (integrare ZOH).
    row1 = _cloud_row(device, station, measured_at=t0, wire_power=500, stored_grid_w=-500, sequence=1)
    row2 = _cloud_row(device, station, measured_at=t1, wire_power=500, stored_grid_w=-500, sequence=2)
    db.add_all([row1, row2])
    db.flush()

    result = correct_deye_cloud_grid_sign(db)
    db.commit()
    assert result["corrected"] == 2

    agg = db.query(TelemetryAggregate).filter(
        TelemetryAggregate.station_id == station.id,
        TelemetryAggregate.period_type == "interval_15m",
        TelemetryAggregate.period_start == t0,
    ).one()
    assert agg.grid_import_energy_kwh is not None
    assert agg.grid_import_energy_kwh > 0
    assert (agg.grid_export_energy_kwh or Decimal(0)) == Decimal(0)
