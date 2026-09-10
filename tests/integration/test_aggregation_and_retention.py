from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.security import utcnow
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services import aggregation_service
from tests.factories import make_device, make_org, make_station, make_user

BUCHAREST = ZoneInfo("Europe/Bucharest")


def _raw(device, station, seq, measured_at, **kwargs):
    defaults = {"pv_power_w": None, "load_power_w": None, "battery_power_w": None, "grid_power_w": None, "battery_soc_percent": None, "ev_power_w": None}
    defaults.update(kwargs)
    return TelemetryRaw(
        device_id=device.id, station_id=station.id, boot_id="b", sequence=seq,
        measured_at=measured_at, received_at=utcnow(), **defaults,
    )


def test_aggregate_interval_15m_skips_when_no_samples(db):
    user = make_user(db, email="agg1@test.local")
    org = make_org(db, "Agg Org 1")
    station = make_station(db, org, user, name="Agg Station 1")
    db.commit()

    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    result = aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    assert result is None  # fara date -> fara rand, nu zero fabricat

    existing = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert existing is None


def test_aggregate_interval_15m_full_coverage_computes_energy(db):
    """Esantioane care acopera integral intervalul (fiecare in limita
    MAX_GAP_SECONDS de urmatorul/de limita intervalului) -> acoperire 1.0,
    energie egala cu integrala exacta a unei puteri constante."""
    user = make_user(db, email="agg2@test.local")
    org = make_org(db, "Agg Org 2")
    station = make_station(db, org, user, name="Agg Station 2")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    for minute_offset in (0, 5, 10):
        db.add(
            _raw(
                device, station, minute_offset, period_start + timedelta(minutes=minute_offset),
                pv_power_w=Decimal(2000), load_power_w=Decimal(500), battery_power_w=Decimal(0),
                grid_power_w=Decimal(-1500), battery_soc_percent=Decimal("50"),
            )
        )
    db.commit()

    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert row is not None
    assert row.sample_count == 3
    # 2000 W tinut constant pe toate cele 15 minute (acoperire completa) = 0.5 kWh.
    assert abs(float(row.pv_energy_kwh) - 0.5) < 1e-6
    assert row.coverage["pv"] == 1.0
    assert row.data_quality == "measured"
    assert float(row.avg_battery_soc_percent) == 50.0


def test_aggregate_interval_15m_irregular_sampling_and_gap_reduce_coverage(db):
    """Esantioane neregulate (nu la interval fix) + un gol final mai mare
    decat MAX_GAP_SECONDS -> acoperirea partiala trebuie reflectata explicit
    (energie mai mica decat media aritmetica naiva ar sugera, coverage < 1),
    NU extrapolata silentios pe tot intervalul."""
    user = make_user(db, email="agg3@test.local")
    org = make_org(db, "Agg Org 3")
    station = make_station(db, org, user, name="Agg Station 3")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    # Esantioane la 0 si 5 minute; ultimul e urmat de un gol de 10 minute
    # pana la finalul intervalului (peste MAX_GAP_SECONDS = 5 minute).
    db.add(_raw(device, station, 0, period_start, pv_power_w=Decimal(2000)))
    db.add(_raw(device, station, 1, period_start + timedelta(minutes=5), pv_power_w=Decimal(2000)))
    db.commit()

    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert row is not None
    # Acoperit: [0,5) prin esantionul de la minutul 0, [5,10) prin cel de la
    # minutul 5 (limitat la MAX_GAP_SECONDS) -- 10 din 15 minute, nu 15.
    assert abs(row.coverage["pv"] - (600 / 900)) < 2e-4
    assert abs(float(row.pv_energy_kwh) - (2000 * 600 / 3_600_000)) < 2e-4
    assert row.data_quality == "estimated"  # acoperire sub prag -> nu "measured"


def test_aggregate_interval_15m_full_gap_metric_is_null_not_zero(db):
    """O metrica complet neraportata (ex. EV) intr-un interval care totusi
    are alte esantioane trebuie sa ramana NULL, nu 0 -- necunoscut != zero."""
    user = make_user(db, email="agg4@test.local")
    org = make_org(db, "Agg Org 4")
    station = make_station(db, org, user, name="Agg Station 4")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    db.add(_raw(device, station, 0, period_start, pv_power_w=Decimal(1000)))  # ev_power_w ramane None
    db.commit()

    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert row is not None
    assert row.ev_energy_kwh is None
    assert row.coverage["ev"] == 0.0
    assert row.pv_energy_kwh is not None


def test_aggregate_interval_15m_soc_zero_is_not_collapsed_to_none(db):
    """SOC 0% (baterie complet descarcata) e o valoare reala si trebuie
    pastrata ca atare -- bug corectat: `if avg_soc:` trata Decimal(0) ca
    falsy si il transforma silentios in None."""
    user = make_user(db, email="agg5@test.local")
    org = make_org(db, "Agg Org 5")
    station = make_station(db, org, user, name="Agg Station 5")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    for minute_offset in (0, 5, 10):
        db.add(_raw(device, station, minute_offset, period_start + timedelta(minutes=minute_offset), battery_soc_percent=Decimal("0")))
    db.commit()

    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert row is not None
    assert row.avg_battery_soc_percent is not None
    assert float(row.avg_battery_soc_percent) == 0.0
    assert row.coverage["soc"] == 1.0


def test_aggregate_interval_15m_battery_charge_discharge_split_from_signed_series(db):
    user = make_user(db, email="agg6@test.local")
    org = make_org(db, "Agg Org 6")
    station = make_station(db, org, user, name="Agg Station 6")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    # Incarcare (pozitiv) primele 10 minute (2 esantioane la 5 min distanta),
    # descarcare (negativ) ultimele 5 -- acoperire completa a intervalului.
    db.add(_raw(device, station, 0, period_start, battery_power_w=Decimal(1000)))
    db.add(_raw(device, station, 1, period_start + timedelta(minutes=5), battery_power_w=Decimal(1000)))
    db.add(_raw(device, station, 2, period_start + timedelta(minutes=10), battery_power_w=Decimal(-2000)))
    db.commit()

    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert row is not None
    # Incarcare: 1000W tinut constant 10 minute = 600 Ws-min -> 0.1667 kWh.
    assert abs(float(row.battery_charge_energy_kwh) - (1000 * 600 / 3_600_000)) < 2e-4
    # Descarcare: 2000W tinut constant 5 minute = 0.1667 kWh.
    assert abs(float(row.battery_discharge_energy_kwh) - (2000 * 300 / 3_600_000)) < 2e-4
    assert row.coverage["battery"] == 1.0


def test_reaggregate_range_is_idempotent_replay(db):
    """Rularea de doua ori pe acelasi interval nu produce randuri duplicate
    si da acelasi rezultat (upsert pe (station_id, period_type, period_start))."""
    user = make_user(db, email="agg7@test.local")
    org = make_org(db, "Agg Org 7")
    station = make_station(db, org, user, name="Agg Station 7")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    for minute_offset in (0, 5, 10):
        db.add(_raw(device, station, minute_offset, period_start + timedelta(minutes=minute_offset), pv_power_w=Decimal(1500)))
    db.commit()

    aggregation_service.reaggregate_range(db, station, period_start, period_start + timedelta(hours=1))
    db.commit()
    aggregation_service.reaggregate_range(db, station, period_start, period_start + timedelta(hours=1))
    db.commit()

    rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_type == "interval_15m"
        )
    ).all()
    matching = [r for r in rows if r.period_start == period_start]
    assert len(matching) == 1
    assert abs(float(matching[0].pv_energy_kwh) - 0.375) < 1e-6  # 1500W * 15min


def test_reaggregate_range_backfills_late_telemetry_and_cascades_rollups(db):
    """Telemetrie care soseste dupa ce agregatele au fost deja calculate
    (ex. un dispozitiv reconectat) trebuie sa poata rescrie, idempotent,
    15m/ora/zi/luna -- nu ramane blocata la valoarea veche."""
    user = make_user(db, email="agg8@test.local")
    org = make_org(db, "Agg Org 8")
    station = make_station(db, org, user, name="Agg Station 8")
    db.commit()

    device = make_device(db, station)
    period_start = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)

    db.add(_raw(device, station, 0, period_start, pv_power_w=Decimal(1000)))
    db.commit()
    aggregation_service.reaggregate_range(db, station, period_start, period_start + timedelta(minutes=15))
    db.commit()

    first = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start == period_start,
        )
    )
    assert first is not None
    first_energy = float(first.pv_energy_kwh)

    # Telemetrie intarziata pentru ACELASI interval, cu o valoare diferita.
    db.add(_raw(device, station, 1, period_start + timedelta(minutes=2), pv_power_w=Decimal(3000)))
    db.commit()

    aggregation_service.reaggregate_range(db, station, period_start, period_start + timedelta(minutes=15))
    db.commit()

    updated = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start == period_start,
        )
    )
    assert updated is not None
    assert float(updated.pv_energy_kwh) != first_energy

    hour_row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "hour",
            TelemetryAggregate.period_start == period_start.replace(minute=0, second=0, microsecond=0),
        )
    )
    assert hour_row is not None
    assert abs(float(hour_row.pv_energy_kwh) - float(updated.pv_energy_kwh)) < 1e-6


def test_aggregate_day_respects_station_timezone_and_dst_spring_23h(db):
    """Ziua schimbarii de ora primavara (ultima duminica din martie) are
    doar 23 de ore in Europe/Bucharest -- limitele UTC trebuie calculate din
    delta reala intre miezurile de noapte locale, nu +24h fix."""
    user = make_user(db, email="agg9@test.local")
    org = make_org(db, "Agg Org 9")
    station = make_station(db, org, user, name="Agg Station 9", timezone="Europe/Bucharest")
    db.commit()

    device = make_device(db, station)
    dst_day = date(2026, 3, 29)  # ultima duminica din martie 2026
    local_midnight = datetime(2026, 3, 29, 0, 0, tzinfo=BUCHAREST)
    next_local_midnight = datetime(2026, 3, 30, 0, 0, tzinfo=BUCHAREST)
    utc = ZoneInfo("UTC")
    day_start_utc = local_midnight.astimezone(utc)
    day_end_utc = next_local_midnight.astimezone(utc)
    assert (day_end_utc - day_start_utc) == timedelta(hours=23)  # confirma ipoteza zilei de 23h

    hour_start = day_start_utc
    while hour_start < day_end_utc:
        db.add(_raw(device, station, int(hour_start.timestamp()), hour_start, pv_power_w=Decimal(1000)))
        db.flush()  # autoflush=False in fixture-ul de test -- fara asta, query-ul din aggregate_interval_15m nu ar vedea randul abia adaugat.
        aggregation_service.aggregate_interval_15m(db, station.id, hour_start)
        aggregation_service.aggregate_hour(db, station.id, hour_start)
        hour_start += timedelta(hours=1)
    db.commit()

    aggregation_service.aggregate_day(db, station, dst_day)
    db.commit()

    day_row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "day",
            TelemetryAggregate.period_start == day_start_utc,
        )
    )
    assert day_row is not None
    assert day_row.period_end == day_end_utc
    assert day_row.sample_count == 23  # nu 24 -- ziua are efectiv 23 de ore


def test_aggregate_day_respects_station_timezone_and_dst_autumn_25h(db):
    """Ziua schimbarii de ora toamna (ultima duminica din octombrie) are 25
    de ore in Europe/Bucharest."""
    user = make_user(db, email="agg10@test.local")
    org = make_org(db, "Agg Org 10")
    station = make_station(db, org, user, name="Agg Station 10", timezone="Europe/Bucharest")
    db.commit()

    device = make_device(db, station)
    dst_day = date(2026, 10, 25)
    local_midnight = datetime(2026, 10, 25, 0, 0, tzinfo=BUCHAREST)
    next_local_midnight = datetime(2026, 10, 26, 0, 0, tzinfo=BUCHAREST)
    utc = ZoneInfo("UTC")
    day_start_utc = local_midnight.astimezone(utc)
    day_end_utc = next_local_midnight.astimezone(utc)
    assert (day_end_utc - day_start_utc) == timedelta(hours=25)

    hour_start = day_start_utc
    while hour_start < day_end_utc:
        db.add(_raw(device, station, int(hour_start.timestamp()), hour_start, pv_power_w=Decimal(1000)))
        db.flush()  # autoflush=False in fixture-ul de test -- fara asta, query-ul din aggregate_interval_15m nu ar vedea randul abia adaugat.
        aggregation_service.aggregate_interval_15m(db, station.id, hour_start)
        aggregation_service.aggregate_hour(db, station.id, hour_start)
        hour_start += timedelta(hours=1)
    db.commit()

    aggregation_service.aggregate_day(db, station, dst_day)
    db.commit()

    day_row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "day",
            TelemetryAggregate.period_start == day_start_utc,
        )
    )
    assert day_row is not None
    assert day_row.period_end == day_end_utc
    assert day_row.sample_count == 25


def test_get_energy_kwh_for_interval_reports_partial_coverage(db):
    user = make_user(db, email="agg11@test.local")
    org = make_org(db, "Agg Org 11")
    station = make_station(db, org, user, name="Agg Station 11")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    for minute_offset in (0, 5, 10):
        db.add(_raw(device, station, minute_offset, period_start + timedelta(minutes=minute_offset), battery_power_w=Decimal(-2000)))
    db.commit()
    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    result = aggregation_service.get_energy_kwh_for_interval(
        db, station.id, "battery_discharge", period_start, period_start + timedelta(minutes=15)
    )
    assert result["energy_kwh"] is not None
    assert abs(float(result["energy_kwh"]) - 0.5) < 1e-6
    assert result["coverage"] == 1.0
    assert result["buckets_found"] == 1
    assert result["buckets_expected"] == 1


def test_retention_deletes_old_telemetry(db):
    user = make_user(db, email="ret1@test.local")
    org = make_org(db, "Ret Org 1")
    station = make_station(db, org, user, name="Ret Station 1")
    db.commit()

    device = make_device(db, station)
    old_time = utcnow() - timedelta(days=400)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="b", sequence=1,
            measured_at=old_time, received_at=old_time, pv_power_w=Decimal(100),
        )
    )
    recent_time = utcnow() - timedelta(hours=1)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="b", sequence=2,
            measured_at=recent_time, received_at=recent_time, pv_power_w=Decimal(200),
        )
    )
    db.commit()

    from app.config import get_settings

    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.telemetry_raw_retention_days)
    deleted = db.query(TelemetryRaw).filter(TelemetryRaw.measured_at < cutoff).delete(synchronize_session=False)
    db.commit()

    assert deleted == 1
    remaining = db.scalars(select(TelemetryRaw).where(TelemetryRaw.station_id == station.id)).all()
    assert len(remaining) == 1
    assert remaining[0].sequence == 2
