"""Invoice-based entry of a fixed import/export pair and a monthly scenario."""

import hashlib
from decimal import Decimal

from sqlalchemy import select

from app.models.station import Station
from app.models.tariff import Tariff, TariffVersion
from app.schemas.romanian_tariff import COMPONENTS, RomanianTariffInput
from app.services import tariff_service

SETTLEMENT = "ro_prosumer_monthly"


def prices(config: RomanianTariffInput):
    charges = sum(
        (getattr(config, key) for key, _ in COMPONENTS if key != "active_energy"), Decimal(0)
    )
    if config.tg_in_active:
        charges -= config.tg
    import_net = config.active_energy + charges
    export_net = config.active_energy - config.tg
    return {
        "import_net": import_net,
        "import_gross": import_net * (1 + config.import_vat / 100),
        "export_net": export_net,
        "export_gross": export_net * (1 + config.export_vat / 100),
        "import_charges": charges,
    }


def monthly_preview(
    config: RomanianTariffInput, import_kwh: Decimal | None, export_kwh: Decimal | None
):
    rates = prices(config)
    result = {"rates": rates, "available": import_kwh is not None and export_kwh is not None}
    if not result["available"]:
        return result
    matched = min(import_kwh, export_kwh)
    surplus = max(export_kwh - import_kwh, Decimal(0))
    rows = []
    for key, label in COMPONENTS:
        rate = getattr(config, key)
        included = key == "tg" and config.tg_in_active
        rows.append(
            {
                "label": label,
                "quantity": import_kwh,
                "rate": rate,
                "value": Decimal(0) if included else import_kwh * rate,
                "included": included,
            }
        )
    import_net = import_kwh * rates["import_net"] + config.monthly_fee
    import_vat = import_net * config.import_vat / 100
    active_credit = matched * config.active_energy
    tg_retained = matched * config.tg
    credit_net = active_credit - tg_retained
    credit_vat = credit_net * config.export_vat / 100
    # The exported surplus is a separate credit, not an immediate reduction
    # of distribution/taxes or money paid out this month.
    result.update(
        {
            "import_kwh": import_kwh,
            "export_kwh": export_kwh,
            "matched_kwh": matched,
            "remaining_active_kwh": import_kwh - matched,
            "surplus_kwh": surplus,
            "rows": rows,
            "import_net": import_net,
            "import_vat": import_vat,
            "import_total": import_net + import_vat,
            "active_credit": active_credit,
            "tg_retained": tg_retained,
            "credit_net": credit_net,
            "credit_vat": credit_vat,
            "credit_total": credit_net + credit_vat,
            "payable": import_net + import_vat - credit_net - credit_vat,
            "surplus_credit_net": surplus * rates["export_net"],
            "monthly_fee": config.monthly_fee,
        }
    )
    return result


def latest_versions(db, station):
    versions = db.scalars(
        select(TariffVersion)
        .join(Tariff)
        .where(
            Tariff.station_id == station.id,
            Tariff.is_active.is_(True),
            TariffVersion.valid_to.is_(None),
        )
        .order_by(TariffVersion.valid_from.desc())
    ).all()
    return {
        direction: next((v for v in versions if v.tariff.direction == direction), None)
        for direction in ("import", "export")
    }


def revision(versions):
    return hashlib.sha256(
        "|".join(
            f"{key}:{value.id if value else '-'}" for key, value in sorted(versions.items())
        ).encode()
    ).hexdigest()


def save_pair(db, station, config: RomanianTariffInput, valid_from, expected_revision):
    db.execute(
        select(Station.id).where(Station.id == station.id).with_for_update(key_share=True)
    ).scalar_one()
    if revision(latest_versions(db, station)) != expected_revision:
        raise ValueError(
            "Tarifele au fost modificate intre timp. Reincarca pagina si verifica noile valori."
        )
    active_contracts = db.scalars(
        select(Tariff).where(Tariff.station_id == station.id, Tariff.is_active.is_(True))
    ).all()
    if any(t.kind != "fixed" for t in active_contracts):
        raise ValueError(
            "Exista un contract indexat OPCOM. Foloseste configurarea avansata pentru acesta; formularul de prosumator necesita preturi fixe."
        )
    snapshot = {"schema_version": 1, **config.model_dump(mode="json")}
    rates = prices(config)
    versions = []
    for direction in ("import", "export"):
        importing = direction == "import"
        tariff = tariff_service.get_or_create_tariff(db, station, direction, "fixed", config.name)
        version = tariff_service.add_tariff_version(
            db,
            tariff,
            valid_from=valid_from,
            fixed_price_lei_per_kwh=config.active_energy if importing else rates["export_net"],
            opcom_margin_lei_per_kwh=None,
            fixed_monthly_fee_lei=config.monthly_fee if importing else Decimal(0),
            variable_component_lei_per_kwh=Decimal(0),
            distribution_lei_per_kwh=config.distribution if importing else Decimal(0),
            transport_lei_per_kwh=(config.tl + (Decimal(0) if config.tg_in_active else config.tg))
            if importing
            else Decimal(0),
            other_regulated_lei_per_kwh=sum(
                (
                    getattr(config, k)
                    for k in (
                        "system_services",
                        "cogeneration",
                        "cfd",
                        "green_certificates",
                        "excise",
                    )
                ),
                Decimal(0),
            )
            if importing
            else Decimal(0),
            vat_rate_percent=config.import_vat if importing else config.export_vat,
            settlement_method=SETTLEMENT,
            settlement_interval_days=30,
            invoice_breakdown=snapshot,
        )
        versions.append(version)
    return versions
