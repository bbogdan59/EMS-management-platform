from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import User
from app.services.task_execution_service import failed_count


def build_nav_context(db: Session, user: User, current_station_id: uuid.UUID | None = None) -> dict:
    if user.is_platform_admin:
        stations = db.scalars(select(Station).order_by(Station.name)).all()
    else:
        org_ids = [
            row[0]
            for row in db.execute(select(Membership.organization_id).where(Membership.user_id == user.id)).all()
        ]
        stations = (
            db.scalars(select(Station).where(Station.organization_id.in_(org_ids)).order_by(Station.name)).all()
            if org_ids
            else []
        )

    org_map = {o.id: o for o in db.scalars(select(Organization)).all()} if stations else {}

    nav_stations = [
        {
            "id": s.id,
            "name": s.name,
            "organization_name": org_map.get(s.organization_id).name if org_map.get(s.organization_id) else "",
            "is_demo": s.is_demo,
        }
        for s in stations
    ]

    current_station = None
    if current_station_id:
        current_station = next((s for s in nav_stations if s["id"] == current_station_id), None)

    return {
        "current_user": user,
        "nav_stations": nav_stations,
        "current_station": current_station,
        "worker_failure_count": failed_count(db) if user.is_platform_admin else 0,
    }
