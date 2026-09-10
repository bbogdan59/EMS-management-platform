from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.station import Station
from app.models.tariff import Tariff, TariffVersion


def get_or_create_tariff(db: Session, station: Station, direction: str, kind: str, name: str) -> Tariff:
    tariff = db.scalar(
        select(Tariff).where(Tariff.station_id == station.id, Tariff.direction == direction, Tariff.is_active.is_(True))
    )
    if tariff is not None:
        tariff.kind = kind
        tariff.name = name
        db.add(tariff)
        db.flush()
        return tariff

    tariff = Tariff(station_id=station.id, direction=direction, kind=kind, name=name)
    db.add(tariff)
    db.flush()
    return tariff


def add_tariff_version(
    db: Session,
    tariff: Tariff,
    *,
    valid_from: datetime,
    fixed_price_lei_per_kwh: Decimal | None,
    opcom_margin_lei_per_kwh: Decimal | None,
    fixed_monthly_fee_lei: Decimal,
    variable_component_lei_per_kwh: Decimal,
    settlement_method: str,
    settlement_interval_days: int,
    economic_calculation_disabled: bool = False,
    limitation_note: str | None = None,
) -> TariffVersion:
    open_version = db.scalar(
        select(TariffVersion)
        .where(TariffVersion.tariff_id == tariff.id, TariffVersion.valid_to.is_(None))
        .order_by(TariffVersion.valid_from.desc())
        .limit(1)
    )
    if open_version is not None and open_version.valid_from < valid_from:
        open_version.valid_to = valid_from
        db.add(open_version)

    version = TariffVersion(
        tariff_id=tariff.id,
        valid_from=valid_from,
        fixed_price_lei_per_kwh=fixed_price_lei_per_kwh,
        opcom_margin_lei_per_kwh=opcom_margin_lei_per_kwh,
        fixed_monthly_fee_lei=fixed_monthly_fee_lei,
        variable_component_lei_per_kwh=variable_component_lei_per_kwh,
        settlement_method=settlement_method,
        settlement_interval_days=settlement_interval_days,
        economic_calculation_disabled=economic_calculation_disabled,
        limitation_note=limitation_note,
    )
    db.add(version)
    db.flush()
    return version


def get_current_tariff_version(db: Session, station_id, direction: str, at: datetime) -> TariffVersion | None:
    return db.scalar(
        select(TariffVersion)
        .join(Tariff, Tariff.id == TariffVersion.tariff_id)
        .where(
            Tariff.station_id == station_id,
            Tariff.direction == direction,
            Tariff.is_active.is_(True),
            TariffVersion.valid_from <= at,
        )
        .where((TariffVersion.valid_to.is_(None)) | (TariffVersion.valid_to > at))
        .order_by(TariffVersion.valid_from.desc())
        .limit(1)
    )
