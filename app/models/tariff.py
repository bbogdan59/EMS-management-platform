from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity

# Banii sunt stocati ca NUMERIC (Decimal), niciodata float: evitam erorile de
# rotunjire binar la insumarea a mii de intervale de 15 minute pe luni de zile,
# care ar produce diferente vizibile in facturi/rapoarte. Energia (kWh) e tot
# NUMERIC pentru acelasi motiv -- se aduna in agregate si trebuie sa fie
# reproductibila bit-cu-bit indiferent de ordinea insumarii.


# Tip de contract (issue #46): valorile canonice stocate in `Tariff.kind`.
# Numele pastreaza compatibilitatea cu datele/migratiile existente
# ("indexed_opcom", nu "dynamic_indexed" ca in textul issue-ului) -- doar doua
# tipuri sunt implementate concret (fix / dinamic indexat pe OPCOM); "provider"
# sau "custom" din issue raman doar etichete libere in `Tariff.name`, nu tipuri
# de calcul distincte, pentru ca niciuna nu are o formula de calcul proprie
# implementata inca (ar necesita sursa de date/formula reala, in afara
# scopului acestui PR -- vezi LIMITATIONS.md). `tariff_service` valideaza la
# scriere ca `kind` e una dintre aceste valori SI ca versiunea nou creata are
# EXACT campurile care corespund acelui tip (nu ambele, nu niciunul) -- esec
# explicit (ValueError), nu o presupunere tacita despre ce a vrut operatorul.
TARIFF_KIND_FIXED = "fixed"
TARIFF_KIND_DYNAMIC_INDEXED = "indexed_opcom"
TARIFF_KINDS = frozenset({TARIFF_KIND_FIXED, TARIFF_KIND_DYNAMIC_INDEXED})


class Tariff(Entity):
    """Contract tarifar pentru o statie, pe o directie (import/export)."""

    __tablename__ = "tariffs"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # import|export
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # vezi TARIFF_KINDS
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    station: Mapped[Station] = relationship()  # noqa: F821
    versions: Mapped[list[TariffVersion]] = relationship(
        back_populates="tariff", cascade="all, delete-orphan", order_by="TariffVersion.valid_from"
    )


class TariffVersion(Entity):
    """O versiune cu valabilitate determinata a unui tarif.

    Pentru `kind=fixed`: se foloseste `fixed_price_lei_per_kwh` (costul MARGINAL
    de energie, constant -- separat de `fixed_monthly_fee_lei`, care e un cost
    FIX, independent de consum). Pentru `kind=indexed_opcom`: pretul de energie
    efectiv = pret_OPCOM + `opcom_margin_lei_per_kwh` (marja poate fi negativa).
    Daca formula contractuala reala nu e implementata (ex. formule cu
    componente reglementate variabile complexe), seteaza
    `economic_calculation_disabled=True` si documenteaza in `limitation_note`.

    Componentele de retea/taxe (issue #51/#46) sunt distincte de pretul de
    energie -- `distribution_lei_per_kwh`/`transport_lei_per_kwh`/
    `other_regulated_lei_per_kwh` -- fiecare 0 implicit (backward-compatibil
    cu versiunile existente, care foloseau doar `variable_component_lei_per_kwh`
    ca "gaura neagra" pentru orice cost variabil suplimentar; acel camp ramane
    disponibil pentru compatibilitate/simplitate, dar versiunile noi ar trebui
    sa foloseasca componentele explicite de mai jos cand contractul le separa).
    `vat_rate_percent=None` inseamna explicit "TVA nu e inclus in aceasta
    formula" (nu 0% -- diferenta conteaza pentru un audit financiar), nu o
    presupunere implicita despre legislatia romaneasca -- operatorul introduce
    cota reala din contractul/factura lui. Vezi
    `tariff_service.compute_effective_price_lei_per_kwh` pentru formula exacta,
    documentata acolo cu sursa/data introducerii (acest PR)."""

    __tablename__ = "tariff_versions"

    tariff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tariffs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fixed_price_lei_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 5), nullable=True)
    opcom_margin_lei_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 5), nullable=True)

    fixed_monthly_fee_lei: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    variable_component_lei_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0, nullable=False)

    distribution_lei_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0, nullable=False)
    transport_lei_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0, nullable=False)
    other_regulated_lei_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0, nullable=False)
    vat_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    settlement_method: Mapped[str] = mapped_column(String(64), default="net_metering_15min", nullable=False)
    settlement_interval_days: Mapped[int] = mapped_column(default=30, nullable=False)

    economic_calculation_disabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    limitation_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    tariff: Mapped[Tariff] = relationship(back_populates="versions")
