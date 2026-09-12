from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity


class EquipmentManufacturer(Entity):
    """Producator din catalogul administrabil (issue #42) -- Deye, etc."""

    __tablename__ = "equipment_manufacturers"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    models: Mapped[list[EquipmentModel]] = relationship(
        back_populates="manufacturer", cascade="all, delete-orphan"
    )


class EquipmentModel(Entity):
    """Model de echipament (invertor/baterie/panou) din catalogul administrabil.

    `spec_revision` creste la fiecare editare a `specs` -- consumatorii
    (`StationConfigVersion`, `PanelGroup`) retin propriul snapshot capturat
    la momentul selectiei, deci o editare ulterioara a catalogului NU
    modifica retroactiv configuratiile deja publicate (acelasi principiu
    de imutabilitate ca `StationConfigVersion` insusi).

    `source_note` e obligatoriu la crearea prin formularul admin (validat in
    `app/schemas`, nu aici) -- catalogul nu trebuie sa contina specificatii
    inventate; fiecare model trebuie sa citeze fisa oficiala sau sursa
    folosita. Dezactivarea (`is_active=False`) e nedistructiva: modelul
    ramane vizibil in istoricul configuratiilor care il refera, dar dispare
    din search-ul folosit la configurarea de statii noi.
    """

    __tablename__ = "equipment_models"
    __table_args__ = (
        UniqueConstraint("manufacturer_id", "equipment_type", "model_name", name="uq_equipment_model_identity"),
    )

    manufacturer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("equipment_manufacturers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    spec_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    specs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    manufacturer: Mapped[EquipmentManufacturer] = relationship(back_populates="models")
