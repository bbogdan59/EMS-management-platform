from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.station import Station
from app.models.tariff import (
    TARIFF_DIRECTIONS,
    TARIFF_KIND_DYNAMIC_INDEXED,
    TARIFF_KIND_FIXED,
    TARIFF_KINDS,
    Tariff,
    TariffVersion,
)

DEFAULT_SETTLEMENT_METHOD = "net_metering_15min"
SUPPORTED_SETTLEMENT_METHODS = frozenset({DEFAULT_SETTLEMENT_METHOD})


def validate_tariff_kind(kind: str) -> None:
    """Blocheaza explicit un `kind` necunoscut (issue #46: tip de contract
    trebuie sa fie unul dintre cele implementate, nu orice sir liber) --
    esec clar la scriere, nu o eticheta ignorata tacit de restul calculului."""
    if kind not in TARIFF_KINDS:
        raise ValueError(
            f"kind: tip de contract necunoscut ({kind!r}). Valorile permise sunt: {sorted(TARIFF_KINDS)}."
        )


def validate_tariff_direction(direction: str) -> None:
    """Tarifele sunt separate strict pe import/export. O directie libera ar
    crea un contract care nu intra corect nici in costul de import, nici in
    venitul de export."""
    if direction not in TARIFF_DIRECTIONS:
        raise ValueError(
            f"direction: directie de tarif necunoscuta ({direction!r}). Valorile permise sunt: {sorted(TARIFF_DIRECTIONS)}."
        )


def get_or_create_tariff(db: Session, station: Station, direction: str, kind: str, name: str) -> Tariff:
    validate_tariff_direction(direction)
    validate_tariff_kind(kind)
    tariff = db.scalar(
        select(Tariff).where(Tariff.station_id == station.id, Tariff.direction == direction, Tariff.is_active.is_(True))
    )
    if tariff is not None:
        # `kind` apartine contractului parinte, nu versiunii. Mutarea lui pe
        # un contract existent ar reclasifica retroactiv toate versiunile
        # istorice. Pana cand tranzitia este modelata ca un contract nou cu
        # inchiderea celui vechi, o refuzam explicit.
        if tariff.kind != kind:
            raise ValueError(
                "kind: tipul unui contract existent nu poate fi schimbat; "
                "tranzitia trebuie modelata ca un contract nou pentru a pastra istoricul."
            )
        tariff.name = name
        db.add(tariff)
        db.flush()
        return tariff

    tariff = Tariff(station_id=station.id, direction=direction, kind=kind, name=name)
    db.add(tariff)
    db.flush()
    return tariff


def _validate_version_matches_contract_kind(
    kind: str,
    *,
    fixed_price_lei_per_kwh: Decimal | None,
    opcom_margin_lei_per_kwh: Decimal | None,
    economic_calculation_disabled: bool,
) -> None:
    """Impune ca versiunea noua sa aiba EXACT campurile care corespund
    tipului de contract al tarifului parinte (issue #46: "Contract versionat
    ... cu tip: fixed, dynamic-indexed" trebuie sa guverneze efectiv formula,
    nu doar sa fie o eticheta afisata) -- esec explicit (ValueError), nu o
    alegere tacita intre cele doua campuri bazata pe care e completat.

    Fara aceasta validare, `compute_effective_price_lei_per_kwh` alege
    ramura fix/indexat dupa care camp e nenul, IGNORAND `kind` -- un tarif
    etichetat "indexed_opcom" caruia i s-a completat din greseala si
    `fixed_price_lei_per_kwh` s-ar comporta ca fix, contrazicand eticheta lui
    si contractul real (indexarea pe OPCOM nu s-ar mai aplica niciodata)."""
    validate_tariff_kind(kind)
    if kind == TARIFF_KIND_FIXED:
        if fixed_price_lei_per_kwh is None and not economic_calculation_disabled:
            raise ValueError(
                "fixed_price_lei_per_kwh: obligatoriu pentru un contract fix (pretul fix de energie, lei/kWh)."
            )
        if opcom_margin_lei_per_kwh is not None:
            raise ValueError(
                "opcom_margin_lei_per_kwh: marja fata de OPCOM nu se aplica unui contract fix -- lasa acest camp gol."
            )
    elif kind == TARIFF_KIND_DYNAMIC_INDEXED:
        if opcom_margin_lei_per_kwh is None and not economic_calculation_disabled:
            raise ValueError(
                "opcom_margin_lei_per_kwh: obligatoriu pentru un contract dinamic-indexat -- formula de "
                "mapare (pret OPCOM + marja) trebuie sa fie explicita, nu implicita."
            )
        if fixed_price_lei_per_kwh is not None:
            raise ValueError(
                "fixed_price_lei_per_kwh: nu se aplica unui contract dinamic-indexat -- foloseste doar marja fata de OPCOM."
            )


def _validate_version_time(valid_from: datetime) -> None:
    if valid_from.tzinfo is None or valid_from.utcoffset() is None:
        raise ValueError("valid_from: trebuie sa fie un instant timezone-aware (UTC recomandat).")


def _validate_non_negative_decimal(field: str, value: Decimal | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{field}: trebuie sa fie >= 0; nu introduce costuri/taxe negative neverificate.")


def _validate_version_components(
    *,
    fixed_price_lei_per_kwh: Decimal | None,
    fixed_monthly_fee_lei: Decimal,
    variable_component_lei_per_kwh: Decimal,
    distribution_lei_per_kwh: Decimal,
    transport_lei_per_kwh: Decimal,
    other_regulated_lei_per_kwh: Decimal,
    vat_rate_percent: Decimal | None,
    settlement_interval_days: int,
) -> None:
    _validate_non_negative_decimal("fixed_price_lei_per_kwh", fixed_price_lei_per_kwh)
    _validate_non_negative_decimal("fixed_monthly_fee_lei", fixed_monthly_fee_lei)
    _validate_non_negative_decimal("variable_component_lei_per_kwh", variable_component_lei_per_kwh)
    _validate_non_negative_decimal("distribution_lei_per_kwh", distribution_lei_per_kwh)
    _validate_non_negative_decimal("transport_lei_per_kwh", transport_lei_per_kwh)
    _validate_non_negative_decimal("other_regulated_lei_per_kwh", other_regulated_lei_per_kwh)
    _validate_non_negative_decimal("vat_rate_percent", vat_rate_percent)
    if vat_rate_percent is not None and vat_rate_percent > 100:
        raise ValueError("vat_rate_percent: trebuie sa fie intre 0 si 100.")
    if settlement_interval_days <= 0:
        raise ValueError("settlement_interval_days: trebuie sa fie un numar pozitiv de zile.")


def _validate_settlement_method(
    settlement_method: str,
    *,
    economic_calculation_disabled: bool,
    limitation_note: str | None,
) -> None:
    if economic_calculation_disabled and not (limitation_note or "").strip():
        raise ValueError(
            "limitation_note: obligatorie cand calculul economic este dezactivat; "
            "documenteaza formula contractuala nesuportata sau limita cunoscuta."
        )
    if settlement_method not in SUPPORTED_SETTLEMENT_METHODS and not economic_calculation_disabled:
        raise ValueError(
            "settlement_method: metoda de decontare nu are formula economica implementata; "
            "dezactiveaza calculul economic si adauga o nota de limitare in loc de un calcul tacit."
        )


def _settlement_method_or_default(tariff_version: TariffVersion) -> str:
    return tariff_version.settlement_method or DEFAULT_SETTLEMENT_METHOD


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
    distribution_lei_per_kwh: Decimal = Decimal(0),
    transport_lei_per_kwh: Decimal = Decimal(0),
    other_regulated_lei_per_kwh: Decimal = Decimal(0),
    vat_rate_percent: Decimal | None = None,
    economic_calculation_disabled: bool = False,
    limitation_note: str | None = None,
) -> TariffVersion:
    _validate_version_time(valid_from)
    _validate_version_matches_contract_kind(
        tariff.kind,
        fixed_price_lei_per_kwh=fixed_price_lei_per_kwh,
        opcom_margin_lei_per_kwh=opcom_margin_lei_per_kwh,
        economic_calculation_disabled=economic_calculation_disabled,
    )
    _validate_version_components(
        fixed_price_lei_per_kwh=fixed_price_lei_per_kwh,
        fixed_monthly_fee_lei=fixed_monthly_fee_lei,
        variable_component_lei_per_kwh=variable_component_lei_per_kwh,
        distribution_lei_per_kwh=distribution_lei_per_kwh,
        transport_lei_per_kwh=transport_lei_per_kwh,
        other_regulated_lei_per_kwh=other_regulated_lei_per_kwh,
        vat_rate_percent=vat_rate_percent,
        settlement_interval_days=settlement_interval_days,
    )
    _validate_settlement_method(
        settlement_method,
        economic_calculation_disabled=economic_calculation_disabled,
        limitation_note=limitation_note,
    )

    open_version = db.scalar(
        select(TariffVersion)
        .where(TariffVersion.tariff_id == tariff.id, TariffVersion.valid_to.is_(None))
        .order_by(TariffVersion.valid_from.desc())
        .limit(1)
    )
    if open_version is not None and open_version.valid_from >= valid_from:
        raise ValueError(
            "valid_from: o versiune noua trebuie sa inceapa dupa versiunea deschisa curenta; "
            "nu se insereaza retroactiv/duplicat peste istoricul tarifar."
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
        distribution_lei_per_kwh=distribution_lei_per_kwh,
        transport_lei_per_kwh=transport_lei_per_kwh,
        other_regulated_lei_per_kwh=other_regulated_lei_per_kwh,
        vat_rate_percent=vat_rate_percent,
        settlement_method=settlement_method,
        settlement_interval_days=settlement_interval_days,
        economic_calculation_disabled=economic_calculation_disabled,
        limitation_note=limitation_note,
    )
    db.add(version)
    db.flush()
    return version


def compute_effective_price_lei_per_kwh(
    tariff_version: TariffVersion | None, market_price_lei_per_kwh: Decimal | None
) -> Decimal | None:
    """Pretul efectiv de energie (lei/kWh) pentru o versiune de tarif deja
    rezolvata (si, pentru tarife indexate, intervalul OPCOM corespunzator
    aceleiasi ore) -- SINGURUL loc care implementeaza formula, folosit atat
    de calculul "acum" cat si de cel istoric (`dashboard_service`).

    Formula (issue #46, documentata AICI -- sursa: acest PR, 2026-09-12; NU o
    regula legala verificata, doar aritmetica generica de facturare pe care
    operatorul trebuie sa o confirme fata de contractul/factura lui reala):

        cost_marginal_energie
          + variable_component_lei_per_kwh   (compat cu versiuni vechi)
          + distribution_lei_per_kwh
          + transport_lei_per_kwh
          + other_regulated_lei_per_kwh
        = subtotal PRE-TVA

        daca `vat_rate_percent` e setat: subtotal * (1 + vat_rate_percent/100)
        altfel: subtotal neschimbat -- `None` inseamna explicit "TVA neinclus
        in aceasta cifra", nu "0% TVA" (diferenta conteaza pentru un audit).

    `cost_marginal_energie`:
      - `kind=fixed` (`fixed_price_lei_per_kwh` setat): valoare CONSTANTA,
        aceeasi la orice ora -- un pret constant nu are niciun gradient de
        arbitraj intraday de extras (optimizerul vede acelasi pret peste tot,
        deci nu "inventeaza" o oportunitate de arbitraj care nu exista).
      - `kind=indexed_opcom` (`opcom_margin_lei_per_kwh` setat): pretul OPCOM
        AL OREI + marja. Daca pretul OPCOM al acelei ore lipseste, functia
        returneaza `None` explicit -- apelantul trebuie sa EXCLUDA acea ora
        din orice suma, niciodata sa o trateze ca gratuita sau sa foloseasca
        tacit un alt pret.
      - `economic_calculation_disabled=True`: `None` neconditionat (formula
        contractuala reala nu e implementata -- vezi `limitation_note`)."""
    if (
        tariff_version is None
        or tariff_version.economic_calculation_disabled
        or _settlement_method_or_default(tariff_version) not in SUPPORTED_SETTLEMENT_METHODS
    ):
        return None
    if tariff_version.fixed_price_lei_per_kwh is not None:
        energy = tariff_version.fixed_price_lei_per_kwh
    elif tariff_version.opcom_margin_lei_per_kwh is not None and market_price_lei_per_kwh is not None:
        energy = market_price_lei_per_kwh + tariff_version.opcom_margin_lei_per_kwh
    else:
        return None

    subtotal = (
        energy
        + tariff_version.variable_component_lei_per_kwh
        + tariff_version.distribution_lei_per_kwh
        + tariff_version.transport_lei_per_kwh
        + tariff_version.other_regulated_lei_per_kwh
    )
    if tariff_version.vat_rate_percent is not None:
        subtotal = subtotal * (Decimal(1) + tariff_version.vat_rate_percent / Decimal(100))
    return subtotal


def build_invoice_preview(
    tariff_version: TariffVersion, market_price_lei_per_kwh: Decimal | None, consumption_kwh: Decimal
) -> dict:
    """Descompunere numerica a unei facturi-EXEMPLU pentru `consumption_kwh`
    kWh, la un singur pret OPCOM de referinta (pentru tarife indexate) --
    pentru afisarea "preview" ceruta de issue #46. NU e o factura reala (nu
    face decontare neta ora-cu-ora, doar o aproximare la pret mediu unic) --
    UI-ul care afiseaza acest rezultat trebuie sa il eticheteze explicit ca
    exemplu, nu ca factura efectiva."""
    effective_price = compute_effective_price_lei_per_kwh(tariff_version, market_price_lei_per_kwh)
    if effective_price is None:
        reason = (
            "Calcul economic dezactivat pentru aceasta versiune."
            if tariff_version.economic_calculation_disabled
            else "Metoda de decontare nu are formula economica implementata."
            if _settlement_method_or_default(tariff_version) not in SUPPORTED_SETTLEMENT_METHODS
            else "Necesita un pret OPCOM de referinta (tarif indexat, dar niciun pret disponibil)."
        )
        return {"available": False, "reason": reason}

    energy_cost = consumption_kwh * effective_price
    fixed_fee = tariff_version.fixed_monthly_fee_lei
    fixed_fee_with_vat = fixed_fee
    if tariff_version.vat_rate_percent is not None:
        fixed_fee_with_vat = fixed_fee * (Decimal(1) + tariff_version.vat_rate_percent / Decimal(100))

    return {
        "available": True,
        "consumption_kwh": consumption_kwh,
        "effective_price_lei_per_kwh": effective_price,
        "energy_cost_lei": energy_cost,
        "fixed_monthly_fee_lei": fixed_fee,
        "fixed_monthly_fee_with_vat_lei": fixed_fee_with_vat,
        "vat_rate_percent": tariff_version.vat_rate_percent,
        "total_lei": energy_cost + fixed_fee_with_vat,
    }


def get_current_tariff_version(db: Session, station_id, direction: str, at: datetime) -> TariffVersion | None:
    validate_tariff_direction(direction)
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
