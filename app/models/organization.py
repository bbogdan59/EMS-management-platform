from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity

# Stari posibile pentru `Organization.status` -- tranzitii aplicate/validate
# doar prin `organization_service` (nu direct pe model), ca sa ramana mereu
# insotite de actor/motiv/timestamp si de intrarea de audit corespunzatoare.
#   active <-> suspended -> archived -> active (restaurare)
# Hard-delete e deliberat INDISPONIBIL in aceasta prima versiune (vezi
# docs/LIMITATIONS.md) -- arhivarea e starea cea mai "finala" disponibila,
# si ramane recuperabila.
ORGANIZATION_STATUSES = ("active", "suspended", "archived")


class Organization(Entity):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # Metadate ale ULTIMEI tranzitii de suspendare/arhivare -- pastrate ca istoric
    # (nu sterse la reactivare/restaurare), sursa de adevar pentru starea CURENTA
    # ramanand exclusiv `status`.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    suspended_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    billing_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    stations: Mapped[list[Station]] = relationship(back_populates="organization")  # noqa: F821


class Membership(Entity):
    """Asociaza un utilizator cu o organizatie si un rol in acea organizatie."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # Dezactivare NEDISTRUCTIVA (issue #23) -- pastreaza randul (istoric de
    # rol/audit ramane atasabil), dar echivaleaza cu lipsa accesului peste
    # tot unde e verificata apartenenta (`OrganizationAccess`/`StationAccess`,
    # SSE). Eliminarea completa (hard delete, vezi `membership_service.remove_member`)
    # ramane disponibila separat, pentru corectarea unei invitatii/membership
    # gresite, nu ca mecanism normal de offboarding.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="memberships")  # noqa: F821
    organization: Mapped[Organization] = relationship(back_populates="memberships")
