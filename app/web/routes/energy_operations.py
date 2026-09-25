from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.database import get_db
from app.models.control import ControlPolicy, Recommendation
from app.models.device import Device
from app.models.ev import EVSE, ChargingSession, EVConnector, Vehicle
from app.models.optimization import Plan
from app.schemas.energy_operations import ControlPolicyIn, EVRequirementIn, EVSEIn, VehicleIn
from app.services import control_service as control
from app.services import ev_analytics_service as analytics
from app.services import ev_service as ev
from app.services import recommendation_service as recommendations
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
viewer = StationAccess("viewer")
operator = StationAccess("operator")
admin = StationAccess("organization_admin")


def fail(db, exc):
    db.rollback()
    raise HTTPException(400, str(exc)) from exc


def local_instant(value, station):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo:
        return parsed
    result = ev.resolve_local_deadline(parsed.date(), parsed.strftime("%H:%M"), station.timezone, 1)
    if result is None:
        raise ValueError("Ora locala nu exista la trecerea la ora de vara.")
    return result


def render(request, name, db, user, station, role, **values):
    return templates.TemplateResponse(request, "energy_operations/" + name + ".html", {
        **build_nav_context(db, user, station.id), "station": station, "role": role, **values,
    })


def scoped_plan(db, station, plan_id):
    plan = db.scalar(select(Plan).where(Plan.id == plan_id, Plan.station_id == station.id))
    if not plan:
        raise HTTPException(404, "Plan indisponibil.")
    return plan


@router.get("/stations/{station_id}/control")
def control_page(request: Request, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    state = control.control_state(db, station)
    config, preference = control.current_versions(db, station)
    plan = db.scalar(select(Plan).where(Plan.station_id == station.id).order_by(Plan.version.desc()).limit(1))
    devices = db.scalars(select(Device).where(Device.station_id == station.id, Device.status == "active")).all()
    return render(request, "control", db, user, station, role, state=state, config=config, preference=preference,
                  devices=devices, plan=plan, policy=db.get(ControlPolicy, state.policy_id) if state and state.policy_id else None)


@router.post("/stations/{station_id}/control/policy", dependencies=[Depends(verify_csrf)])
def policy_submit(
    device_id: uuid.UUID = Form(...), min_soc: Decimal = Form(...), max_soc: Decimal = Form(...),
    max_energy: Decimal = Form(...), charge: Decimal = Form(...), discharge: Decimal = Form(...),
    ramp: Decimal = Form(...), efc_day: Decimal = Form(...), efc_month: Decimal = Form(...),
    window_start: str = Form(...), window_end: str = Form(...), expires_at: str = Form(...),
    access=Depends(admin), user=Depends(get_current_user), db: Session = Depends(get_db),
):
    station, _ = access
    try:
        start_h, start_m = map(int, window_start.split(":"))
        end_h, end_m = map(int, window_end.split(":"))
        data = ControlPolicyIn(device_id=device_id, min_soc_percent=min_soc, max_soc_percent=max_soc,
                               max_managed_energy_kwh=max_energy, max_charge_kw=charge, max_discharge_kw=discharge,
                               max_ramp_kw_per_minute=ramp, max_efc_day=efc_day, max_efc_month=efc_month,
                               windows=[{"days": list(range(7)), "start_minute": start_h * 60 + start_m, "end_minute": end_h * 60 + end_m}],
                               expires_at=local_instant(expires_at, station))
        control.save_policy(db, station, user, data)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/control", 303)


@router.post("/stations/{station_id}/control/mode", dependencies=[Depends(verify_csrf)])
def mode_submit(mode: str = Form(...), reason: str = Form(...), revision: int = Form(...), confirmed: bool = Form(False),
                access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    try:
        control.change_mode(db, station, user, mode, reason, revision, confirmed)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/control", 303)


@router.post("/stations/{station_id}/control/stop", dependencies=[Depends(verify_csrf)])
def emergency_stop(access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    ev.lock_station(db, station.id)
    control.suspend(db, station, "user_emergency_stop", user)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/control", 303)


@router.get("/stations/{station_id}/control/plans/{plan_id}")
def plan_preview(plan_id: uuid.UUID, request: Request, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    plan = scoped_plan(db, station, plan_id)
    return render(request, "plan", db, user, station, role, plan=plan, preview=control.preview(db, station, plan))


@router.post("/stations/{station_id}/control/plans/{plan_id}/approve", dependencies=[Depends(verify_csrf)])
def approve_plan(plan_id: uuid.UUID, preview_hash: str = Form(...), confirmed: bool = Form(False),
                 access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    plan = scoped_plan(db, station, plan_id)
    if not confirmed:
        raise HTTPException(400, "Confirmati explicit planul prezentat.")
    try:
        control.approve_plan(db, station, plan, user, preview_hash)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/control/plans/{plan.id}", 303)


@router.get("/stations/{station_id}/ev")
def ev_page(request: Request, offset: int = Query(0, ge=0), access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    evses = db.scalars(select(EVSE).where(EVSE.station_id == station.id)).all()
    connectors = db.scalars(select(EVConnector).join(EVSE).where(EVSE.station_id == station.id)).all()
    sessions = db.scalars(select(ChargingSession).where(ChargingSession.station_id == station.id)
                          .order_by(ChargingSession.started_at.desc()).offset(offset).limit(30)).all()
    vehicles = db.scalars(select(Vehicle).where(Vehicle.organization_id == station.organization_id)).all()
    return render(request, "ev", db, user, station, role, evses=evses, connectors=connectors, vehicles=vehicles,
                  sessions=sessions, offset=offset, summaries={s.id: analytics.session_summary(db, s) for s in sessions},
                  devices=db.scalars(select(Device).where(Device.station_id == station.id, Device.status == "active")).all(),
                  requirements={c.id: ev.upcoming_requirements(db, station, c) for c in connectors})


@router.get("/api/stations/{station_id}/evses")
def ev_inventory(access=Depends(viewer), db: Session = Depends(get_db)):
    station, _ = access
    return [{"id": e.id, "name": e.name, "capabilities": e.capabilities, "capabilities_source": "declared",
             "max_power_kw": e.max_power_kw, "last_seen_at": e.last_seen_at,
             "connectors": [{"id": c.id, "number": c.number, "state": c.state}
                            for c in db.scalars(select(EVConnector).where(EVConnector.evse_id == e.id))]}
            for e in db.scalars(select(EVSE).where(EVSE.station_id == station.id))]


@router.post("/api/stations/{station_id}/evses", dependencies=[Depends(verify_csrf)])
def ev_create_api(payload: EVSEIn, access=Depends(admin), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    try:
        evse, connector = ev.create_evse(db, user, station, payload)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return {"evse_id": evse.id, "connector_id": connector.id, "physical_control": False}


@router.post("/stations/{station_id}/ev", dependencies=[Depends(verify_csrf)])
def ev_create(name: str = Form(...), device_id: str = Form(""), max_power_kw: str = Form(""), meter: bool = Form(False),
              vehicle_soc: bool = Form(False), consent: bool = Form(False),
              access=Depends(admin), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    try:
        data = EVSEIn(name=name, device_id=device_id or None, max_power_kw=max_power_kw or None,
                      capabilities={"meter": meter, "vehicle_soc": vehicle_soc}, vehicle_data_consent=consent)
        ev.create_evse(db, user, station, data)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/ev", 303)


@router.post("/stations/{station_id}/ev/vehicles", dependencies=[Depends(verify_csrf)])
def vehicle_create(evse_id: uuid.UUID = Form(...), alias: str = Form(...), capacity: str = Form(""), consumption: str = Form(""),
                   access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    evse = db.scalar(select(EVSE).where(EVSE.id == evse_id, EVSE.station_id == station.id))
    if not evse:
        raise HTTPException(404, "EVSE indisponibil.")
    try:
        ev.create_vehicle(db, user, station, evse, VehicleIn(alias=alias, battery_capacity_kwh=capacity or None, consumption_kwh_100km=consumption or None))
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/ev", 303)


@router.post("/stations/{station_id}/ev/requirements", dependencies=[Depends(verify_csrf)])
def requirement_create(
    connector_id: uuid.UUID = Form(...), energy: Decimal = Form(...), vehicle_id: str = Form(""), deadline: str = Form(""),
    weekdays: list[int] = Form([]), local_time: str = Form(""), max_cost: str = Form(""),
    access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db),
):
    station, _ = access
    try:
        data = EVRequirementIn(minimum_energy_kwh=energy, vehicle_id=vehicle_id or None,
                               deadline=local_instant(deadline, station) if deadline else None,
                               schedule={"weekdays": weekdays, "local_time": local_time, "fold": 1} if weekdays else None,
                               max_cost_lei=max_cost or None)
        ev.create_requirement(db, user, station, connector_id, data)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/ev", 303)


@router.post("/stations/{station_id}/ev/privacy", dependencies=[Depends(verify_csrf)])
def privacy_submit(evse_id: uuid.UUID = Form(...), consent: bool = Form(False), retention: int = Form(...), vacation_until: str = Form(""),
                   access=Depends(admin), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    evse = db.scalar(select(EVSE).where(EVSE.id == evse_id, EVSE.station_id == station.id))
    if not evse:
        raise HTTPException(404, "EVSE indisponibil.")
    try:
        ev.update_privacy(db, user, station, evse, consent, retention, local_instant(vacation_until, station) if vacation_until else None)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/ev", 303)


@router.get("/stations/{station_id}/ev/sessions/{session_id}")
def session_detail(session_id: uuid.UUID, request: Request, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    session = db.scalar(select(ChargingSession).where(ChargingSession.id == session_id, ChargingSession.station_id == station.id))
    if not session:
        raise HTTPException(404, "Sesiune indisponibila.")
    return render(request, "session", db, user, station, role, summary=analytics.session_summary(db, session))


@router.get("/stations/{station_id}/ev/export.csv")
def session_export(start: datetime, end: datetime, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end - start <= timedelta(days=366):
        raise HTTPException(400, "Intervalul necesita fus orar si maximum 366 zile.")
    sessions = db.scalars(select(ChargingSession).where(
        ChargingSession.station_id == station.id, ChargingSession.started_at >= start, ChargingSession.started_at < end,
    ).order_by(ChargingSession.started_at).limit(1001)).all()
    if len(sessions) > 1000:
        raise HTTPException(400, "Restrangeti intervalul la maximum 1000 sesiuni.")
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["session_id", "start_utc", "end_utc", "energy_kwh", "quality", "coverage", "estimated_grid_equivalent_cost_lei", "price_coverage"])
    for session in sessions:
        summary = analytics.session_summary(db, session)
        writer.writerow([session.id, session.started_at.isoformat(), session.ended_at.isoformat() if session.ended_at else "",
                         summary["total_energy_kwh"], summary["quality"], summary["energy_coverage"],
                         summary["estimated_cost_lei"], summary["price_coverage"]])
    ev.audit(db, station, user, "ev.export", station.id, {"sessions": len(sessions)})
    db.commit()
    return Response(stream.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="ev-sessions.csv"'})


@router.get("/stations/{station_id}/recommendations")
def recommendation_page(request: Request, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    return render(request, "recommendations", db, user, station, role, records=recommendations.ranked(db, station), presets=recommendations.PRESETS)


@router.post("/stations/{station_id}/recommendations/preset", dependencies=[Depends(verify_csrf)])
def preset_create(preset: str = Form(...), access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    try:
        record = recommendations.create_preset(db, station, user, preset)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/recommendations/{record.id}", 303)


def scoped_recommendation(db, station, recommendation_id):
    record = db.scalar(select(Recommendation).where(Recommendation.id == recommendation_id, Recommendation.station_id == station.id))
    if not record:
        raise HTTPException(404, "Recomandare indisponibila.")
    return record


@router.get("/stations/{station_id}/recommendations/{recommendation_id}")
def recommendation_preview(recommendation_id: uuid.UUID, request: Request, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    record = scoped_recommendation(db, station, recommendation_id)
    return render(request, "recommendation", db, user, station, role, record=record, preview=recommendations.preview(db, station, record))


@router.post("/stations/{station_id}/recommendations/{recommendation_id}", dependencies=[Depends(verify_csrf)])
def recommendation_act(recommendation_id: uuid.UUID, action: str = Form(...), reason: str = Form(...), preview_hash: str = Form(""), snoozed_until: str = Form(""),
                       access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, _ = access
    record = scoped_recommendation(db, station, recommendation_id)
    try:
        recommendations.act(db, station, user, record, action, reason, preview_hash,
                            local_instant(snoozed_until, station) if snoozed_until else None)
    except ValueError as exc:
        fail(db, exc)
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/recommendations/{record.id}", 303)


@router.post("/stations/{station_id}/control/plans/{plan_id}/scenarios", dependencies=[Depends(verify_csrf)])
def scenario_comparison(plan_id: uuid.UUID, request: Request, reserve: Decimal = Form(...), fixed_price: Decimal = Form(...), strategy: str = Form(...),
                        access=Depends(operator), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    plan = scoped_plan(db, station, plan_id)
    try:
        check_fixed_window(f"energy_scenarios:{user.id}", 5, 3600)
        result = recommendations.compare_scenarios(db, station, plan, reserve, fixed_price, strategy)
    except RateLimitExceeded as exc:
        raise HTTPException(429, "Maximum 5 comparatii pe ora.") from exc
    except ValueError as exc:
        fail(db, exc)
    return render(request, "scenarios", db, user, station, role, result=jsonable_encoder(result))
