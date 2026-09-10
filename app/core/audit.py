from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.audit import AuditLog


def record_audit(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_label: str = "system",
    organization_id: uuid.UUID | None = None,
    station_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    metadata: dict | None = None,
    outcome: str = "success",
) -> AuditLog:
    """Scrie o intrare in jurnalul de audit. NU trece niciodata parole/token-uri
    in `metadata` -- doar identificatori si valori nesensibile."""
    entry = AuditLog(
        occurred_at=utcnow(),
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        organization_id=organization_id,
        station_id=station_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        metadata_json=metadata or {},
        outcome=outcome,
    )
    db.add(entry)
    db.flush()
    return entry
