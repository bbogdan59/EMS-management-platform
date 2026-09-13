from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.core.audit import record_audit
from app.core.rbac import can_export_data, can_manage_station_config
from app.core.security import utcnow
from app.database import get_db
from app.models.organization import Membership
from app.models.station import StationConfigVersion
from app.models.user import User
from app.services import dashboard_service, station_service
from app.web.context import build_nav_context
from app.web.templating import templates
from app.web.wizard import next_wizard_step, resume_url

router = APIRouter()


def _wizard_resume_url(db: Session, user: User, station) -> str | None:
    """URL de reluare a wizard-ului de configurare (issue #41) pentru un
    banner discret pe dashboard, NU un redirect fortat -- vizitarea normala a
    dashboard-ului nu trebuie niciodata blocata sau deturnata, doar insotita
    de o sugestie clara pentru cine chiar poate finaliza configurarea.
    platform_admin e exclus deliberat: viziteaza frecvent statii ale altor
    organizatii doar pentru supraveghere, nu pentru a le configura."""
    if user.is_platform_admin:
        return None
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == station.organization_id,
            Membership.is_active.is_(True),
        )
    )
    if membership is None or not can_manage_station_config(membership.role):
        return None
    progress = station_service.setup_progress(db, station)
    if next_wizard_step(progress) is None:
        return None
    return resume_url(db, station)


@router.get("/")
def home(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    station_id: uuid.UUID | None = Query(default=None),
):
    nav = build_nav_context(db, user, station_id)
    if station_id is None or not nav["nav_stations"]:
        return templates.TemplateResponse(request, "dashboard/no_station.html", {**nav})

    if nav["current_station"] is None:
        return templates.TemplateResponse(request, "dashboard/no_station.html", {**nav})

    from app.models.station import Station

    station = db.get(Station, station_id)
    summary = dashboard_service.get_summary(db, station)
    context = {
        "station": station,
        "summary": summary,
        "wizard_resume_url": _wizard_resume_url(db, user, station),
        **nav,
    }
    return templates.TemplateResponse(request, "dashboard/station.html", context)


def _parse_range(range_key: str) -> tuple[datetime, datetime]:
    now = utcnow()
    if range_key == "24h":
        return now - timedelta(hours=24), now
    if range_key == "7d":
        return now - timedelta(days=7), now
    if range_key == "30d":
        return now - timedelta(days=30), now
    return now - timedelta(hours=24), now


@router.get("/stations/{station_id}/data/timeseries")
def data_timeseries(
    station_id: uuid.UUID,
    range: str = Query(default="24h"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    start, end = _parse_range(range)
    return JSONResponse(dashboard_service.get_timeseries(db, station, start, end))


@router.get("/stations/{station_id}/data/prices")
def data_prices(
    station_id: uuid.UUID,
    day: str = Query(default="today"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    from app.web.context import build_nav_context  # noqa

    from app.services.opcom_service import BUCHAREST

    market_today = utcnow().astimezone(BUCHAREST).date()
    target_date = market_today if day == "today" else market_today + timedelta(days=1)
    data = dashboard_service.get_prices(db, station, target_date)
    return JSONResponse({"day": day, "date": target_date.isoformat(), "intervals": data, "published": len(data) > 0})


@router.get("/stations/{station_id}/data/plan")
def data_plan(
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    return JSONResponse(dashboard_service.get_plan_chart(db, station))


@router.get("/stations/{station_id}/data/energy-totals")
def data_energy_totals(
    station_id: uuid.UUID,
    granularity: str = Query(default="day"),
    periods: int = Query(default=30, le=366),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    return JSONResponse(dashboard_service.get_energy_totals(db, station, granularity, periods))


@router.get("/stations/{station_id}/data/heatmap")
def data_heatmap(
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    return JSONResponse(dashboard_service.get_heatmap(db, station))


@router.get("/stations/{station_id}/data/efc")
def data_efc(
    station_id: uuid.UUID,
    range: str = Query(default="30d"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    start, end = _parse_range(range)
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    efc = dashboard_service.get_efc_used(db, station, config, start, end)
    return JSONResponse({"efc_used": efc, "range": range})


@router.get("/stations/{station_id}/data/forecast-vs-actual")
def data_forecast_vs_actual(
    station_id: uuid.UUID,
    metric: str = Query(default="pv"),
    range: str = Query(default="24h"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    start, end = _parse_range(range)
    return JSONResponse(dashboard_service.get_forecast_vs_actual(db, station, metric, start, end))


@router.get("/stations/{station_id}/data/savings")
def data_savings(
    station_id: uuid.UUID,
    range: str = Query(default="30d"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    start, end = _parse_range(range)
    return JSONResponse(dashboard_service.get_estimated_savings(db, station, start, end))


@router.get("/stations/{station_id}/data/summary")
def data_summary(
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    station, _role = station_role
    return JSONResponse(dashboard_service.get_summary(db, station))


@router.get("/stations/{station_id}/export.csv")
def export_csv(
    station_id: uuid.UUID,
    range: str = Query(default="7d"),
    request: Request = None,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    station, role = station_role
    if not can_export_data(role):
        from fastapi import HTTPException, status

        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Nu ai voie sa exporti date.")

    start, end = _parse_range(range)
    rows = dashboard_service.get_timeseries(db, station, start, end)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp_utc", "pv_kw", "load_kw", "battery_kw", "grid_kw", "soc_pct", "simulat", "intarziat"])
    for r in rows:
        writer.writerow([r["t"], r["pv_kw"], r["load_kw"], r["battery_kw"], r["grid_kw"], r["soc_pct"], r["is_simulated"], r["is_late"]])
    buffer.seek(0)

    record_audit(
        db, action="data_exported", resource_type="telemetry", resource_id=str(station.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id, metadata={"range": range},
    )
    db.commit()

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=telemetrie_{station.id}_{range}.csv"},
    )
