"""Teste numerice pentru formula de cost efectiv (issue #46): separarea
cost marginal / cost fix, componente de retea/taxe distincte, TVA explicit
opt-in, blocarea calculului cand lipseste pretul OPCOM (fara fallback
tacut), pret negativ, si preview-ul de factura-exemplu.

Aceasta iteratie a issue-ului #46 adauga: validarea ca tipul de contract
(`Tariff.kind`) guverneaza efectiv campurile versiunii (nu doar o eticheta),
o verificare structurala explicita ca exportul are pret propriu -- nu
derivat prin scaderea componentelor de import -- si rezolvarea corecta a
versiunii valabile peste o tranzitie de ora de vara/iarna (DST)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models.tariff import TariffVersion
from app.services import tariff_service as svc
from tests.factories import make_org, make_station, make_user


def _version(**overrides) -> TariffVersion:
    defaults = {
        "tariff_id": uuid.uuid4(),
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "fixed_price_lei_per_kwh": None,
        "opcom_margin_lei_per_kwh": None,
        "fixed_monthly_fee_lei": Decimal("0"),
        "variable_component_lei_per_kwh": Decimal("0"),
        "distribution_lei_per_kwh": Decimal("0"),
        "transport_lei_per_kwh": Decimal("0"),
        "other_regulated_lei_per_kwh": Decimal("0"),
        "vat_rate_percent": None,
        "settlement_method": "net_metering_15min",
        "settlement_interval_days": 30,
        "economic_calculation_disabled": False,
    }
    defaults.update(overrides)
    return TariffVersion(**defaults)


def test_fixed_tariff_uses_constant_marginal_price_independent_of_market(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("0.85"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("999")) == Decimal("0.85")
    assert svc.compute_effective_price_lei_per_kwh(v, None) == Decimal("0.85")  # nu are nevoie de pret OPCOM


def test_fixed_tariff_separates_marginal_cost_from_fixed_monthly_fee(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("0.85"), fixed_monthly_fee_lei=Decimal("25"))
    preview = svc.build_invoice_preview(v, None, Decimal("300"))
    assert preview["available"] is True
    assert preview["effective_price_lei_per_kwh"] == Decimal("0.85")  # costul marginal, fara abonament amestecat
    assert preview["energy_cost_lei"] == Decimal("255")  # 300 * 0.85
    assert preview["fixed_monthly_fee_lei"] == Decimal("25")
    assert preview["total_lei"] == Decimal("280")  # 255 + 25


def test_indexed_opcom_adds_margin_to_market_price(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.12"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("0.50")) == Decimal("0.62")


def test_indexed_opcom_negative_margin_allowed(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("-0.05"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("0.50")) == Decimal("0.45")


def test_indexed_opcom_without_market_price_returns_none_no_silent_fallback(db):
    """Criteriu explicit din issue #46/#51: lipsa pretului OPCOM blocheaza
    calculul exact, nu produce un fallback tacut (ex. 0 sau ultimul pret)."""
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.12"))
    assert svc.compute_effective_price_lei_per_kwh(v, None) is None


def test_economic_calculation_disabled_always_returns_none(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("0.85"), economic_calculation_disabled=True)
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("0.50")) is None


def test_none_tariff_version_returns_none(db):
    assert svc.compute_effective_price_lei_per_kwh(None, Decimal("0.50")) is None


def test_negative_opcom_price_flows_through_correctly(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.10"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("-0.30")) == Decimal("-0.20")


def test_all_network_and_regulated_components_sum_correctly(db):
    v = _version(
        fixed_price_lei_per_kwh=Decimal("0.50"),
        variable_component_lei_per_kwh=Decimal("0.01"),
        distribution_lei_per_kwh=Decimal("0.15"),
        transport_lei_per_kwh=Decimal("0.05"),
        other_regulated_lei_per_kwh=Decimal("0.02"),
    )
    # 0.50 + 0.01 + 0.15 + 0.05 + 0.02 = 0.73
    assert svc.compute_effective_price_lei_per_kwh(v, None) == Decimal("0.73")


def test_vat_applied_when_rate_set(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("1.00"), vat_rate_percent=Decimal("19"))
    assert svc.compute_effective_price_lei_per_kwh(v, None) == Decimal("1.19")


def test_vat_none_means_not_included_not_zero_percent(db):
    """None (implicit) si 0% trebuie sa produca rezultate DIFERITE conceptual
    -- desi numeric ambele lasa subtotalul neschimbat, `vat_rate_percent`
    ramane vizibil None in rezultat (nu e transformat tacit in 0)."""
    v_none = _version(fixed_price_lei_per_kwh=Decimal("1.00"), vat_rate_percent=None)
    v_zero = _version(fixed_price_lei_per_kwh=Decimal("1.00"), vat_rate_percent=Decimal("0"))
    assert svc.compute_effective_price_lei_per_kwh(v_none, None) == Decimal("1.00")
    assert svc.compute_effective_price_lei_per_kwh(v_zero, None) == Decimal("1.00")

    preview_none = svc.build_invoice_preview(v_none, None, Decimal("100"))
    preview_zero = svc.build_invoice_preview(v_zero, None, Decimal("100"))
    assert preview_none["vat_rate_percent"] is None
    assert preview_zero["vat_rate_percent"] == Decimal("0")


def test_vat_applied_to_fixed_monthly_fee_separately_in_preview(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("1.00"), fixed_monthly_fee_lei=Decimal("20"), vat_rate_percent=Decimal("19"))
    preview = svc.build_invoice_preview(v, None, Decimal("100"))
    assert preview["fixed_monthly_fee_with_vat_lei"] == Decimal("23.80")  # 20 * 1.19
    assert preview["energy_cost_lei"] == Decimal("119.00")  # 100 * 1.19
    assert preview["total_lei"] == Decimal("142.80")  # 119.00 + 23.80


def test_invoice_preview_unavailable_when_price_missing_gives_reason(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.10"))
    preview = svc.build_invoice_preview(v, None, Decimal("300"))
    assert preview["available"] is False
    assert "OPCOM" in preview["reason"]


def test_invoice_preview_unavailable_when_calculation_disabled(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("1.00"), economic_calculation_disabled=True, limitation_note="test")
    preview = svc.build_invoice_preview(v, None, Decimal("300"))
    assert preview["available"] is False
    assert "dezactivat" in preview["reason"]


def test_add_tariff_version_persists_new_components(db):
    from tests.factories import make_org, make_station, make_user

    user = make_user(db, email="tariff-components@test.local")
    org = make_org(db, "Tariff Components Org")
    station = make_station(db, org, user, name="TC Station")
    db.commit()

    tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "Test Fixed")
    version = svc.add_tariff_version(
        db, tariff, valid_from=datetime.now(UTC), fixed_price_lei_per_kwh=Decimal("1.00"),
        opcom_margin_lei_per_kwh=None, fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
        distribution_lei_per_kwh=Decimal("0.12"), transport_lei_per_kwh=Decimal("0.03"),
        other_regulated_lei_per_kwh=Decimal("0.01"), vat_rate_percent=Decimal("19"),
    )
    db.commit()

    assert version.distribution_lei_per_kwh == Decimal("0.12")
    assert version.transport_lei_per_kwh == Decimal("0.03")
    assert version.other_regulated_lei_per_kwh == Decimal("0.01")
    assert version.vat_rate_percent == Decimal("19")


# --- Tip de contract explicit guverneaza formula (issue #46) --------------
#
# `Tariff.kind` (fixed | indexed_opcom) trebuie sa fie mai mult decat o
# eticheta de UI: `add_tariff_version` respinge acum orice versiune ale carei
# campuri de pret nu corespund EXACT tipului declarat -- niciun contract nu
# poate avea simultan/niciodata cele doua campuri de pret marginal.


def _station(db, suffix: str):
    user = make_user(db, email=f"tariff-kind-{suffix}@test.local")
    org = make_org(db, f"Tariff Kind Org {suffix}")
    return make_station(db, org, user, name=f"Tariff Kind Station {suffix}")


def test_get_or_create_tariff_rejects_unknown_kind(db):
    station = _station(db, "unknown")
    with pytest.raises(ValueError, match="Tip de contract necunoscut"):
        svc.get_or_create_tariff(db, station, "import", "some_random_string", "Bogus")


def test_fixed_contract_requires_fixed_price(db):
    station = _station(db, "fixed-missing-price")
    tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "Fix fara pret")
    with pytest.raises(ValueError, match="pretul fix"):
        svc.add_tariff_version(
            db, tariff, valid_from=datetime.now(UTC),
            fixed_price_lei_per_kwh=None, opcom_margin_lei_per_kwh=None,
            fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
            settlement_method="net_metering_15min", settlement_interval_days=30,
        )


def test_fixed_contract_rejects_stray_opcom_margin(db):
    """Un contract etichetat `fixed` caruia i s-ar completa din greseala si
    `opcom_margin_lei_per_kwh` nu trebuie acceptat tacit -- ar contrazice
    eticheta lui si ar face `compute_effective_price_lei_per_kwh` sa aleaga
    ramura gresita fata de ce a fost intentionat."""
    station = _station(db, "fixed-stray-margin")
    tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "Fix contaminat")
    with pytest.raises(ValueError, match="marja fata de OPCOM nu se aplica"):
        svc.add_tariff_version(
            db, tariff, valid_from=datetime.now(UTC),
            fixed_price_lei_per_kwh=Decimal("0.85"), opcom_margin_lei_per_kwh=Decimal("0.10"),
            fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
            settlement_method="net_metering_15min", settlement_interval_days=30,
        )


def test_dynamic_indexed_contract_requires_opcom_margin(db):
    station = _station(db, "dynamic-missing-margin")
    tariff = svc.get_or_create_tariff(db, station, "import", "indexed_opcom", "Dinamic fara marja")
    with pytest.raises(ValueError, match="marja fata de pretul OPCOM este obligatorie"):
        svc.add_tariff_version(
            db, tariff, valid_from=datetime.now(UTC),
            fixed_price_lei_per_kwh=None, opcom_margin_lei_per_kwh=None,
            fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
            settlement_method="net_metering_15min", settlement_interval_days=30,
        )


def test_dynamic_indexed_contract_rejects_stray_fixed_price(db):
    station = _station(db, "dynamic-stray-fixed")
    tariff = svc.get_or_create_tariff(db, station, "import", "indexed_opcom", "Dinamic contaminat")
    with pytest.raises(ValueError, match="pretul fix nu se aplica"):
        svc.add_tariff_version(
            db, tariff, valid_from=datetime.now(UTC),
            fixed_price_lei_per_kwh=Decimal("0.85"), opcom_margin_lei_per_kwh=Decimal("0.10"),
            fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
            settlement_method="net_metering_15min", settlement_interval_days=30,
        )


def test_valid_fixed_and_dynamic_versions_still_accepted(db):
    """Regresie: versiunile corect formate (un singur camp de pret completat,
    corespunzator tipului) tot trec, pentru ambele tipuri de contract."""
    station = _station(db, "valid-both")
    fixed_tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "Fix ok")
    fixed_version = svc.add_tariff_version(
        db, fixed_tariff, valid_from=datetime.now(UTC),
        fixed_price_lei_per_kwh=Decimal("0.85"), opcom_margin_lei_per_kwh=None,
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    assert fixed_version.fixed_price_lei_per_kwh == Decimal("0.85")

    dynamic_tariff = svc.get_or_create_tariff(db, station, "export", "indexed_opcom", "Dinamic ok")
    dynamic_version = svc.add_tariff_version(
        db, dynamic_tariff, valid_from=datetime.now(UTC),
        fixed_price_lei_per_kwh=None, opcom_margin_lei_per_kwh=Decimal("-0.05"),
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    assert dynamic_version.opcom_margin_lei_per_kwh == Decimal("-0.05")


# --- Export are formula proprie, nu derivata din import (issue #46) -------


def test_export_price_is_independent_of_import_components(db):
    """Criteriu explicit din issue #46: "componentele nerecuperabile nu sunt
    scazute fictiv din import" -- exportul e un `Tariff` separat (directie
    proprie), cu propriile componente; adaugarea unor taxe/distributie mari
    pe IMPORT nu trebuie sa modifice absolut deloc venitul de export calculat
    pentru aceeasi statie, si invers."""
    station = _station(db, "export-independent")

    import_tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "Import cu taxe")
    import_version = svc.add_tariff_version(
        db, import_tariff, valid_from=datetime.now(UTC),
        fixed_price_lei_per_kwh=Decimal("0.80"), opcom_margin_lei_per_kwh=None,
        fixed_monthly_fee_lei=Decimal("30"), variable_component_lei_per_kwh=Decimal("0"),
        distribution_lei_per_kwh=Decimal("0.20"), transport_lei_per_kwh=Decimal("0.05"),
        other_regulated_lei_per_kwh=Decimal("0.03"), vat_rate_percent=Decimal("19"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )

    export_tariff = svc.get_or_create_tariff(db, station, "export", "fixed", "Export simplu")
    export_version = svc.add_tariff_version(
        db, export_tariff, valid_from=datetime.now(UTC),
        fixed_price_lei_per_kwh=Decimal("0.40"), opcom_margin_lei_per_kwh=None,
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )

    export_price_before = svc.compute_effective_price_lei_per_kwh(export_version, None)
    assert export_price_before == Decimal("0.40")

    # Cresterea drastica a componentelor de import (distributie/transport/
    # taxe/TVA/abonament) nu are niciun efect asupra pretului de export deja
    # calculat -- structurile de date sunt complet separate.
    import_version.distribution_lei_per_kwh = Decimal("5.00")
    import_version.fixed_monthly_fee_lei = Decimal("500")
    export_price_after = svc.compute_effective_price_lei_per_kwh(export_version, None)
    assert export_price_after == Decimal("0.40")
    assert export_price_after == export_price_before

    # Import-ul, la randul lui, nu e afectat de venitul de export.
    # (0.80 + 5.00 + 0.05 + 0.03) subtotal, apoi *1.19 TVA (setat pe import).
    import_price = svc.compute_effective_price_lei_per_kwh(import_version, None)
    assert import_price == Decimal("6.9972")
    assert import_price != export_price_after
    assert import_version.tariff_id != export_version.tariff_id


# --- Versionare peste tranzitii DST (issue #46) ----------------------------
#
# `valid_from`/`valid_to` sunt DateTime(timezone=True) -- comparate ca
# instante absolute UTC, niciodata ca "ora locala", deci schimbarea orei de
# vara/iarna in Europe/Bucharest (ceas care sare sau se repeta local) nu
# trebuie sa produca o selectie gresita a versiunii valabile.


def test_tariff_version_resolves_correctly_across_spring_forward_dst(db):
    """Romania: ultima duminica din martie 2026 (29 martie), ora 03:00 EET
    devine 04:00 EEST -- ora locala 03:00-04:00 nu exista deloc. Versiunea
    trebuie totusi selectata corect de o parte si de alta a instantei UTC
    a tranzitiei (01:00 UTC)."""
    station = _station(db, "dst-spring")
    tz = ZoneInfo("Europe/Bucharest")
    transition_utc = datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 EET -> 04:00 EEST

    tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "DST spring")
    svc.add_tariff_version(
        db, tariff, valid_from=transition_utc - timedelta(days=30),
        fixed_price_lei_per_kwh=Decimal("1.00"), opcom_margin_lei_per_kwh=None,
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    svc.add_tariff_version(
        db, tariff, valid_from=transition_utc,
        fixed_price_lei_per_kwh=Decimal("2.00"), opcom_margin_lei_per_kwh=None,
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    db.commit()

    before = svc.get_current_tariff_version(db, station.id, "import", transition_utc - timedelta(seconds=1))
    at_transition = svc.get_current_tariff_version(db, station.id, "import", transition_utc)
    after = svc.get_current_tariff_version(db, station.id, "import", transition_utc + timedelta(hours=2))

    assert before.fixed_price_lei_per_kwh == Decimal("1.00")
    assert at_transition.fixed_price_lei_per_kwh == Decimal("2.00")
    assert after.fixed_price_lei_per_kwh == Decimal("2.00")
    # Sanity: la instanta UTC a tranzitiei, ceasul local a sarit deja la
    # 04:00 EEST (zoneinfo mapeaza instanta exacta a tranzitiei pe partea
    # de dupa salt) -- ora locala 03:00-04:00 nu a existat niciodata.
    assert transition_utc.astimezone(tz).isoformat() == "2026-03-29T04:00:00+03:00"


def test_tariff_version_resolves_correctly_across_fall_back_dst(db):
    """Romania: ultima duminica din octombrie 2026 (25 octombrie), ora 04:00
    EEST devine 03:00 EET -- ora locala 03:00-04:00 se repeta. O versiune cu
    `valid_from` chiar la instanta UTC a tranzitiei (01:00 UTC) trebuie
    selectata corect, fara ambiguitate, pentru ca rezolutia se face pe
    instanta absoluta, nu pe ora locala repetata."""
    station = _station(db, "dst-fall")
    tz = ZoneInfo("Europe/Bucharest")
    transition_utc = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 04:00 EEST -> 03:00 EET

    tariff = svc.get_or_create_tariff(db, station, "import", "indexed_opcom", "DST fall")
    svc.add_tariff_version(
        db, tariff, valid_from=transition_utc - timedelta(days=30),
        fixed_price_lei_per_kwh=None, opcom_margin_lei_per_kwh=Decimal("0.05"),
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    svc.add_tariff_version(
        db, tariff, valid_from=transition_utc,
        fixed_price_lei_per_kwh=None, opcom_margin_lei_per_kwh=Decimal("0.15"),
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    db.commit()

    just_before = svc.get_current_tariff_version(db, station.id, "import", transition_utc - timedelta(minutes=1))
    just_after = svc.get_current_tariff_version(db, station.id, "import", transition_utc + timedelta(minutes=1))

    assert just_before.opcom_margin_lei_per_kwh == Decimal("0.05")
    assert just_after.opcom_margin_lei_per_kwh == Decimal("0.15")
    # Efectul numeric complet peste tranzitie, la acelasi pret OPCOM de piata:
    market_price = Decimal("0.60")
    assert svc.compute_effective_price_lei_per_kwh(just_before, market_price) == Decimal("0.65")
    assert svc.compute_effective_price_lei_per_kwh(just_after, market_price) == Decimal("0.75")
    # Sanity: la instanta UTC a tranzitiei, ceasul local a cazut deja inapoi
    # la 03:00 EET -- ora locala 03:00-04:00 EEST/EET s-a repetat, dar
    # instanta absoluta UTC ramane fara ambiguitate.
    assert transition_utc.astimezone(tz).isoformat() == "2026-10-25T03:00:00+02:00"


# --- Rotunjire: Decimal pastreaza precizia, fara drift de float -----------


def test_decimal_precision_avoids_float_rounding_drift():
    """Issue #46 cere teste de rotunjire explicit -- suma a 3 componente cu
    5 zecimale (limita coloanei NUMERIC(10,5)) trebuie sa fie exacta in
    Decimal, nu aproximata cum ar produce binar-float (ex. 0.1 + 0.2 !=
    0.3 in float, dar exact in Decimal)."""
    v = TariffVersion(
        tariff_id=uuid.uuid4(), valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        fixed_price_lei_per_kwh=Decimal("0.10000"), opcom_margin_lei_per_kwh=None,
        fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0.20000"),
        distribution_lei_per_kwh=Decimal("0.00001"), transport_lei_per_kwh=Decimal("0"),
        other_regulated_lei_per_kwh=Decimal("0"), vat_rate_percent=None,
        settlement_method="net_metering_15min", settlement_interval_days=30,
    )
    result = svc.compute_effective_price_lei_per_kwh(v, None)
    assert result == Decimal("0.30001")
    assert str(result) == "0.30001"  # nicio zecimala parazita introdusa de float
