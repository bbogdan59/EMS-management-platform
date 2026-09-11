from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity


class AdminJob(Entity):
    """Urmarirea unui job asincron declansat manual din panoul admin (import
    OPCOM ad-hoc sau reoptimizare unei statii) -- nu inlocuieste `ImportRun`/
    `OptimizationRun` (acelea raman inregistrarea de business a rezultatului),
    ci ofera un ID/stare vizibil imediat in UI din momentul in care admin-ul
    apasa butonul, inainte ca worker-ul sa apuce sa creeze rezultatul.

    Job-urile PLANIFICATE (Celery beat, `app/workers/tasks.py`) nu trec prin
    acest model -- ele nu au un "declansator" uman de urmarit si isi
    gestioneaza deja idempotenta/lock-urile proprii."""

    __tablename__ = "admin_jobs"

    job_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)  # AdminJobType
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="queued")  # AdminJobStatus
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    target_label: Mapped[str] = mapped_column(String(200), nullable=False)
    station_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), nullable=True, index=True)

    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    result_resource_type: Mapped[str | None] = mapped_column(String(24), nullable=True)  # import_run|optimization_run
    result_resource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    station: Mapped[Station | None] = relationship()  # noqa: F821
