from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.api.deps import StationAccess
from app.core.audit import record_audit
from app.core.security import utcnow
from app.models.device import Device
from app.models.ev import EVSE, ChargingSession, EVConnector, EVObservation, EVRequirement, Vehicle
from app.models.organization import Organization
from app.models.station import Station
from app.schemas.energy_operations import EVObservationIn, EVRequirementIn, EVSEIn, VehicleIn

STATES = {
    "unknown": {"disconnected", "connected", "available", "charging", "paused", "completed", "faulted"},
    "disconnected": {"disconnected", "connected", "available", "charging", "faulted"},
    "connected": {"connected", "available", "charging", "paused", "completed", "disconnected", "faulted"},
    "available": {"available", "connected", "charging", "disconnected", "faulted"},
    "charging": {"charging", "paused", "completed", "connected", "disconnected", "faulted"},
    "paused": {"paused", "charging", "connected", "completed", "disconnected", "faulted"},
    "completed": {"completed", "available", "connected", "charging", "disconnected", "faulted"},
    "faulted": {"faulted", "available", "connected", "charging", "paused", "disconnected", "completed"},
}


def lock_station(db, station_id):
    return db.scalar(select(Station).where(Station.id == station_id).with_for_update(key_share=True))


def audit(db, station, actor, action, resource_id, metadata=None):
    record_audit(
        db, action=action, resource_type="energy_operations", resource_id=str(resource_id),
        station_id=station.id, organization_id=station.organization_id,
        actor_user_id=actor.id if actor else None, metadata=metadata or {},
    )


def create_evse(db, user, station, data: EVSEIn):
    StationAccess("organization_admin")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    if data.device_id:
        device = db.get(Device, data.device_id)
        if not device or device.station_id != station.id or device.status != "active":
            raise ValueError("Dispozitivul nu apartine statiei active.")
    evse = EVSE(station_id=station.id, **data.model_dump(exclude={"capabilities"}), capabilities=data.capabilities.model_dump())
    db.add(evse)
    db.flush()
    connector = EVConnector(evse_id=evse.id, number=1)
    db.add(connector)
    audit(db, station, user, "ev.onboard", evse.id, {"capabilities_source": "declared", "physical_control": False})
    db.flush()
    return evse, connector


def create_vehicle(db, user, station, evse, data: VehicleIn):
    StationAccess("operator")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    db.refresh(evse)
    if evse.station_id != station.id or not evse.vehicle_data_consent:
        raise ValueError("Consimtamantul pentru datele vehiculului este necesar.")
    vehicle = Vehicle(organization_id=station.organization_id, **data.model_dump())
    db.add(vehicle)
    db.flush()
    audit(db, station, user, "ev.vehicle_created", vehicle.id)
    return vehicle


def connector_scope(db, station, connector_id):
    connector = db.get(EVConnector, connector_id)
    evse = db.get(EVSE, connector.evse_id) if connector else None
    if not evse or evse.station_id != station.id or not evse.is_active:
        raise ValueError("Conector indisponibil pentru aceasta statie.")
    return connector, evse


def vehicle_scope(db, station, evse, vehicle_id):
    if vehicle_id is None:
        return None
    vehicle = db.get(Vehicle, vehicle_id)
    if not evse.vehicle_data_consent or not vehicle or vehicle.organization_id != station.organization_id:
        raise ValueError("Vehicul indisponibil pentru aceasta statie sau consimtamant absent.")
    return vehicle


def ingest_observation(db, device, connector_id, data: EVObservationIn, now=None):
    now = now or utcnow()
    station = lock_station(db, device.station_id)
    org = db.get(Organization, station.organization_id) if station else None
    if not station or not station.is_active or not org or org.status != "active" or device.status != "active":
        raise ValueError("Dispozitiv indisponibil.")
    connector, evse = connector_scope(db, station, connector_id)
    if evse.device_id != device.id:
        raise ValueError("Dispozitivul nu este asociat acestui EVSE.")
    vehicle = vehicle_scope(db, station, evse, data.vehicle_id)
    if data.vehicle_soc_percent is not None and (
        not evse.vehicle_data_consent or not evse.capabilities.get("vehicle_soc")
    ):
        raise ValueError("SOC vehicul neautorizat sau capabilitate nedeclarata.")
    if data.meter_kwh is not None and not evse.capabilities.get("meter"):
        raise ValueError("Contorul nu este declarat in capabilitati.")
    if not now - timedelta(days=evse.retention_days) <= data.observed_at <= now + timedelta(seconds=30):
        raise ValueError("Momentul observatiei depaseste fereastra acceptata.")
    payload_hash = hashlib.sha256(json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(EVObservation).where(
        EVObservation.connector_id == connector.id, EVObservation.event_id == data.event_id,
    ))
    if existing:
        if existing.payload_hash != payload_hash:
            raise ValueError("Identitatea evenimentului a fost reutilizata cu alte valori.")
        return existing, "duplicate"
    late = connector.last_observed_at is not None and data.observed_at <= connector.last_observed_at
    if not late and data.state not in STATES[connector.state]:
        raise ValueError("Tranzitie EV invalida.")
    session = db.scalar(select(ChargingSession).where(
        ChargingSession.connector_id == connector.id, ChargingSession.ended_at.is_(None),
    ))
    observation = EVObservation(
        connector_id=connector.id,
        **data.model_dump(exclude={"schema_version", "vehicle_id"}),
        payload_hash=payload_hash, applied=not late,
    )
    db.add(observation)
    if late:
        db.flush()
        return observation, "retained_out_of_order"
    if session is None and data.state in ("connected", "charging", "paused"):
        session = ChargingSession(
            station_id=station.id, connector_id=connector.id,
            vehicle_id=vehicle.id if vehicle else None, started_at=data.observed_at,
            state=data.state, meter_start_kwh=data.meter_kwh, source="device",
        )
        db.add(session)
        db.flush()
        audit(db, station, None, "ev.session_started", session.id)
    if session:
        if vehicle and session.vehicle_id not in (None, vehicle.id):
            raise ValueError("Vehiculul unei sesiuni active nu poate fi inlocuit.")
        if vehicle:
            session.vehicle_id = vehicle.id
        observation.session_id = session.id
        session.state = data.state
        if data.state in ("disconnected", "completed"):
            session.ended_at = data.observed_at
            session.meter_end_kwh = data.meter_kwh
            if data.state == "disconnected":
                session.reason_codes = [*session.reason_codes, "unplugged"]
        db.flush()
        from app.services.ev_analytics_service import energy_segments

        segments = energy_segments(db, session)
        session.energy_kwh = segments["total_energy_kwh"]
        session.quality = segments["quality"]
        session.reason_codes = sorted(set(session.reason_codes + segments["reason_codes"]))
    connector.state, connector.last_observed_at = data.state, data.observed_at
    evse.last_seen_at = now
    db.flush()
    return observation, "accepted"


def create_requirement(db, user, station, connector_id, data: EVRequirementIn):
    StationAccess("operator")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    connector, evse = connector_scope(db, station, connector_id)
    vehicle = vehicle_scope(db, station, evse, data.vehicle_id)
    if data.deadline and not utcnow() < data.deadline <= utcnow() + timedelta(days=366):
        raise ValueError("Plecare invalida; alegeti urmatoarele 366 zile.")
    if data.target_soc_percent is not None and (not evse.vehicle_data_consent or not evse.capabilities.get("vehicle_soc")):
        raise ValueError("SOC vehicul indisponibil; folositi tinta in kWh.")
    if data.target_range_km is not None and (not vehicle or vehicle.consumption_kwh_100km is None):
        raise ValueError("Autonomia necesita consumul configurat al vehiculului.")
    requirement = EVRequirement(
        connector_id=connector.id, created_by=user.id,
        **data.model_dump(exclude={"schedule"}),
        schedule=data.schedule.model_dump(mode="json") if data.schedule else {},
    )
    db.add(requirement)
    db.flush()
    audit(db, station, user, "ev.requirement_created", requirement.id)
    return requirement


def resolve_local_deadline(day, local_time, timezone, fold=1):
    zone = ZoneInfo(timezone)
    naive = datetime.combine(day, datetime.strptime(local_time, "%H:%M").time())
    candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
    if candidate.astimezone(zone).replace(tzinfo=None) != naive:
        return None  # A nonexistent spring-forward time is skipped, never silently shifted.
    return candidate


def upcoming_requirements(db, station, connector, now=None):
    now = now or utcnow()
    evse = db.get(EVSE, connector.evse_id)
    if evse.vacation_until and evse.vacation_until > now:
        return []
    requirements = db.scalars(select(EVRequirement).where(
        EVRequirement.connector_id == connector.id, EVRequirement.status == "active",
    )).all()
    unique = [(r, r.deadline) for r in requirements if r.deadline and r.deadline > now]
    one_off_days = {deadline.astimezone(ZoneInfo(station.timezone)).date() for _, deadline in unique}
    local_day = now.astimezone(ZoneInfo(station.timezone)).date()
    for requirement in requirements:
        if not requirement.schedule:
            continue
        for offset in range(15):
            day = local_day + timedelta(days=offset)
            if day in one_off_days or day.weekday() not in requirement.schedule["weekdays"]:
                continue
            deadline = resolve_local_deadline(
                day, requirement.schedule["local_time"], station.timezone, requirement.schedule.get("fold", 1),
            )
            if deadline and deadline > now:
                unique.append((requirement, deadline))
                break
    return sorted(unique, key=lambda pair: pair[1])


def update_privacy(db, user, station, evse, consent, retention_days, vacation_until=None):
    StationAccess("organization_admin")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    if evse.station_id != station.id or not 7 <= retention_days <= 3650:
        raise ValueError("Preferinte EV invalide.")
    if vacation_until and (vacation_until.tzinfo is None or vacation_until <= utcnow()):
        raise ValueError("Data de revenire trebuie sa fie in viitor, cu fus orar.")
    evse.vehicle_data_consent, evse.retention_days = consent, retention_days
    evse.vacation_until = vacation_until
    if not consent:
        connectors = select(EVConnector.id).where(EVConnector.evse_id == evse.id)
        for session in db.scalars(select(ChargingSession).where(ChargingSession.connector_id.in_(connectors))):
            session.vehicle_id = None
        for observation in db.scalars(select(EVObservation).where(EVObservation.connector_id.in_(connectors))):
            observation.vehicle_soc_percent = None
        for requirement in db.scalars(select(EVRequirement).where(EVRequirement.connector_id.in_(connectors))):
            requirement.vehicle_id = None
            requirement.target_soc_percent = requirement.target_range_km = None
    audit(db, station, user, "ev.privacy_updated", evse.id, {"consent": consent, "retention_days": retention_days})
    db.flush()


def retention(db, now=None):
    now = now or utcnow()
    deleted = 0
    for evse in db.scalars(select(EVSE).order_by(EVSE.station_id)):
        lock_station(db, evse.station_id)
        cutoff = now - timedelta(days=evse.retention_days)
        connectors = select(EVConnector.id).where(EVConnector.evse_id == evse.id)
        deleted += db.execute(delete(ChargingSession).where(
            ChargingSession.connector_id.in_(connectors), ChargingSession.ended_at < cutoff,
        )).rowcount
        db.execute(delete(EVObservation).where(
            EVObservation.connector_id.in_(connectors), EVObservation.session_id.is_(None),
            EVObservation.observed_at < cutoff,
        ))
    return deleted
