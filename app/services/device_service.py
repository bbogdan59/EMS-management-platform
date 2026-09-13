from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import (
    expires_in,
    generate_claim_code,
    generate_opaque_token,
    hash_password,
    hash_token,
    utcnow,
    verify_password,
)
from app.models.command import Command, CommandEvent
from app.models.device import ClaimCode, Device, DeviceCredential
from app.models.enums import ClaimCodeStatus, CommandStatus, DeviceStatus, PlanStatus
from app.models.optimization import Plan
from app.models.preference import PreferenceVersion
from app.models.station import Station, StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.models.user import User
from app.schemas.device_api import TelemetryItem

MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_TELEMETRY_AGE = timedelta(days=400)
LATE_TELEMETRY_THRESHOLD = timedelta(minutes=5)


class DeviceServiceError(Exception):
    pass


class CommandExpiredError(DeviceServiceError):
    """Semnaleaza ca tranzitia la expired trebuie pastrata de apelant."""


def create_claim_code(db: Session, station: Station, created_by: User) -> tuple[ClaimCode, str]:
    settings = get_settings()
    raw_code, prefix = generate_claim_code()
    claim = ClaimCode(
        station_id=station.id,
        code_hash=hash_token(raw_code),
        code_prefix=prefix,
        status=ClaimCodeStatus.pending.value,
        created_by_user_id=created_by.id,
        expires_at=expires_in(minutes=settings.device_claim_code_ttl_minutes),
    )
    db.add(claim)
    db.flush()
    return claim, raw_code


def revoke_device(db: Session, device: Device, reason: str) -> None:
    device.status = DeviceStatus.revoked.value
    device.revoked_at = utcnow()
    device.revoked_reason = reason
    db.add(device)
    db.execute(
        update(DeviceCredential)
        .where(DeviceCredential.device_id == device.id, DeviceCredential.is_active.is_(True))
        .values(is_active=False, revoked_at=utcnow())
    )
    db.flush()


def claim_device(db: Session, claim_code_raw: str, device_name: str, hardware_info: dict) -> tuple[Device, str]:
    code_hash = hash_token(claim_code_raw.strip().upper())
    # SELECT ... FOR UPDATE serializeaza doua cereri concurente pentru acelasi
    # cod: a doua asteapta pana cand prima isi comite (sau anuleaza) tranzactia,
    # apoi vede starea reala (claimed) si e respinsa corect -- fara acest lock,
    # ambele ar putea trece verificarea de status inainte ca vreuna sa scrie,
    # creand doua device-uri din acelasi cod de unica folosinta.
    claim = db.scalar(select(ClaimCode).where(ClaimCode.code_hash == code_hash).with_for_update())
    if claim is None or claim.status != ClaimCodeStatus.pending.value:
        raise DeviceServiceError("Cod de asociere invalid sau deja folosit.")
    if claim.expires_at < utcnow():
        claim.status = ClaimCodeStatus.expired.value
        db.add(claim)
        db.flush()
        raise DeviceServiceError("Cod de asociere expirat.")

    device = Device(
        station_id=claim.station_id,
        name=device_name,
        status=DeviceStatus.active.value,
        capabilities=hardware_info or {},
    )
    db.add(device)
    db.flush()

    raw_secret = generate_opaque_token(32)
    credential = DeviceCredential(device_id=device.id, secret_hash=hash_password(raw_secret))
    db.add(credential)

    claim.status = ClaimCodeStatus.claimed.value
    claim.claimed_at = utcnow()
    claim.claimed_device_id = device.id
    db.add(claim)
    db.flush()

    return device, raw_secret


def rotate_credential(db: Session, device: Device) -> str:
    db.execute(
        update(DeviceCredential)
        .where(DeviceCredential.device_id == device.id, DeviceCredential.is_active.is_(True))
        .values(is_active=False, revoked_at=utcnow())
    )
    raw_secret = generate_opaque_token(32)
    credential = DeviceCredential(device_id=device.id, secret_hash=hash_password(raw_secret))
    db.add(credential)
    db.flush()
    return raw_secret


def record_heartbeat(db: Session, device: Device, boot_id: str, firmware_version: str | None, capabilities: dict) -> Device:
    device.last_heartbeat_at = utcnow()
    device.last_boot_id = boot_id
    if firmware_version:
        device.firmware_version = firmware_version
    if capabilities:
        device.capabilities = {**(device.capabilities or {}), **capabilities}
    db.add(device)
    db.flush()
    return device


def ingest_telemetry_batch(db: Session, device: Device, items: list[TelemetryItem]) -> tuple[int, int, int, list[str]]:
    now = utcnow()
    rows = []
    errors: list[str] = []
    rejected = 0

    for idx, item in enumerate(items):
        if item.measured_at > now + MAX_FUTURE_SKEW:
            rejected += 1
            errors.append(f"item {idx}: measured_at in viitor (peste toleranta de {MAX_FUTURE_SKEW}).")
            continue
        if item.measured_at < now - MAX_TELEMETRY_AGE:
            rejected += 1
            errors.append(f"item {idx}: measured_at prea vechi (peste {MAX_TELEMETRY_AGE.days} zile).")
            continue

        is_late = (now - item.measured_at) > LATE_TELEMETRY_THRESHOLD
        rows.append(
            {
                "id": uuid.uuid4(),
                "device_id": device.id,
                "station_id": device.station_id,
                "boot_id": item.boot_id,
                "sequence": item.sequence,
                "schema_version": item.schema_version,
                "measured_at": item.measured_at,
                "received_at": now,
                "pv_power_w": item.pv_power_w,
                "load_power_w": item.load_power_w,
                "battery_power_w": item.battery_power_w,
                "grid_power_w": item.grid_power_w,
                "battery_soc_percent": item.battery_soc_percent,
                "ev_connected": item.ev_connected,
                "ev_power_w": item.ev_power_w,
                "quality_flags": item.quality_flags,
                "raw_payload": item.raw_payload,
                "is_simulated": bool(item.raw_payload.get("simulated", False)),
                "is_late": is_late,
            }
        )

    accepted = 0
    duplicates = 0
    if rows:
        stmt = (
            pg_insert(TelemetryRaw)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["device_id", "boot_id", "sequence"])
            .returning(TelemetryRaw.id)
        )
        result = db.execute(stmt)
        inserted_ids = result.fetchall()
        accepted = len(inserted_ids)
        duplicates = len(rows) - accepted
        db.flush()

    return accepted, duplicates, rejected, errors


def get_active_station_config(db: Session, station_id: uuid.UUID) -> tuple[StationConfigVersion | None, PreferenceVersion | None]:
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.station_id == station_id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )
    return config, preference


def get_active_plan(db: Session, station_id: uuid.UUID) -> Plan | None:
    return db.scalar(
        select(Plan)
        .where(
            Plan.station_id == station_id,
            Plan.status.in_(
                [PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value]
            ),
        )
        .order_by(Plan.version.desc())
        .limit(1)
    )


def accept_plan(db: Session, device: Device, version: int) -> Plan:
    plan = get_active_plan(db, device.station_id)
    if plan is None or plan.version != version:
        raise DeviceServiceError("Nu exista un plan publicat cu aceasta versiune.")
    if plan.status == PlanStatus.published.value:
        plan.status = PlanStatus.accepted_by_device.value
        plan.accepted_at = utcnow()
        plan.accepted_by_device_id = device.id
        db.add(plan)
        db.flush()
    return plan


def list_pending_commands(db: Session, device: Device) -> list[Command]:
    from app.services.command_dispatch_service import command_allows_delivery

    now = utcnow()
    expired = db.scalars(
        select(Command).where(
            Command.device_id == device.id,
            Command.status.in_([CommandStatus.created.value, CommandStatus.delivered.value, CommandStatus.accepted.value]),
            Command.expires_at < now,
        )
    ).all()
    for cmd in expired:
        cmd.status = CommandStatus.expired.value
        db.add(cmd)
        db.add(CommandEvent(command_id=cmd.id, event_type="expired", source="system", message="Comanda a expirat inainte de confirmare."))
    if expired:
        db.flush()

    pending = db.scalars(
        select(Command)
        .where(
            Command.device_id == device.id,
            Command.status.in_([CommandStatus.created.value, CommandStatus.delivered.value]),
            Command.expires_at >= now,
            Command.valid_from <= now,
        )
        .order_by(Command.valid_from)
    ).all()

    deliverable = []
    for cmd in pending:
        if not command_allows_delivery(db, cmd, device, now):
            cmd.status = CommandStatus.superseded.value
            db.add(cmd)
            db.add(CommandEvent(command_id=cmd.id, event_type="superseded", source="system", message="Planul nu mai autorizeaza livrarea comenzii."))
            continue
        deliverable.append(cmd)
        if cmd.status == CommandStatus.created.value:
            cmd.status = CommandStatus.delivered.value
            cmd.delivered_at = now
            db.add(cmd)
            db.add(CommandEvent(command_id=cmd.id, event_type="delivered", source="system"))
    if pending:
        db.flush()

    return deliverable


def acknowledge_command(db: Session, device: Device, command_id: uuid.UUID, status_value: str, reason: str | None) -> Command:
    from app.services.command_dispatch_service import command_allows_delivery

    command = db.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise DeviceServiceError("Comanda nu exista pentru acest dispozitiv.")

    # Idempotenta la reincercare: un ACK deja aplicat cu ACELASI rezultat nu e o
    # eroare -- dispozitivul poate retrimite dupa ce raspunsul s-a pierdut in
    # retea. Un rezultat CONTRADICTORIU (ex. "rejected" dupa ce a fost deja
    # "accepted") e respins explicit, nu suprascris tacit.
    if command.status in (CommandStatus.accepted.value, CommandStatus.rejected.value):
        if command.status == status_value:
            last_event = db.scalar(
                select(CommandEvent)
                .where(CommandEvent.command_id == command.id, CommandEvent.event_type == status_value)
                .order_by(CommandEvent.created_at.desc())
                .limit(1)
            )
            if last_event is not None and last_event.message == reason:
                return command
        raise DeviceServiceError(
            f"Comanda este deja in starea '{command.status}'; reincercarea raporteaza un rezultat contradictoriu ('{status_value}')."
        )

    if command.status not in (CommandStatus.delivered.value, CommandStatus.created.value):
        raise DeviceServiceError(f"Comanda este in starea '{command.status}', nu poate fi confirmata/respinsa acum.")
    if command.valid_from > utcnow():
        raise DeviceServiceError("Comanda nu este inca valabila.")
    if status_value == "accepted" and not command_allows_delivery(db, command, device, utcnow()):
        raise DeviceServiceError("Planul nu mai autorizeaza acceptarea comenzii.")
    if command.expires_at < utcnow():
        command.status = CommandStatus.expired.value
        db.add(command)
        db.add(CommandEvent(command_id=command.id, event_type="expired", source="system"))
        db.flush()
        raise CommandExpiredError("Comanda a expirat.")

    if status_value == "accepted":
        command.status = CommandStatus.accepted.value
        command.accepted_at = utcnow()
    else:
        command.status = CommandStatus.rejected.value
    db.add(command)
    db.add(
        CommandEvent(
            command_id=command.id,
            event_type=status_value,
            source="device",
            message=reason,
        )
    )
    db.flush()
    return command


def report_command_result(
    db: Session, device: Device, command_id: uuid.UUID, status_value: str, details: dict, error_message: str | None
) -> Command:
    command = db.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise DeviceServiceError("Comanda nu exista pentru acest dispozitiv.")

    # Idempotenta la reincercare, simetric cu acknowledge_command: acelasi
    # rezultat terminal retrimis (payload si mesaj identice) e succes tacit, nu
    # eroare -- comparam impotriva ultimului CommandEvent de acest tip, nu doar
    # a statusului, ca sa detectam un rezultat CONTRADICTORIU (alt payload/eroare
    # pentru aceeasi comanda deja finalizata) si sa-l respingem explicit.
    if command.status in (CommandStatus.executed.value, CommandStatus.failed.value):
        if command.status == status_value:
            last_event = db.scalar(
                select(CommandEvent)
                .where(CommandEvent.command_id == command.id, CommandEvent.event_type == status_value)
                .order_by(CommandEvent.created_at.desc())
                .limit(1)
            )
            if last_event is not None and last_event.payload == (details or {}) and last_event.message == error_message:
                return command
        raise DeviceServiceError(
            f"Comanda este deja in starea '{command.status}'; reincercarea raporteaza un rezultat contradictoriu."
        )

    if command.status != CommandStatus.accepted.value:
        raise DeviceServiceError(
            f"Comanda este in starea '{command.status}'; rezultatul poate fi raportat doar dupa acceptare."
        )

    if status_value == "executed":
        from app.services.inverter_config_service import (
            COMMAND_TYPE,
            ConfigConflict,
            validate_execution_readback,
        )

        if command.type == COMMAND_TYPE:
            try:
                validate_execution_readback(db, command, device, details)
            except ConfigConflict as exc:
                raise DeviceServiceError(str(exc)) from exc
        command.status = CommandStatus.executed.value
        command.executed_at = utcnow()
    else:
        command.status = CommandStatus.failed.value
        command.last_error = (error_message or "")[:1000]
    db.add(command)
    db.add(
        CommandEvent(
            command_id=command.id,
            event_type=status_value,
            source="device",
            payload=details,
            message=error_message,
        )
    )
    db.flush()
    return command


# --- Enrollment automat (issue #16) -------------------------------------
#
# Flux distinct de ClaimCode (mai sus): dispozitivul se prezinta singur, cu
# o identitate PROPRIE (installation_uuid + provisioning_secret, generate si
# pastrate local de el, nu de server), fara sa aleaga nicio statie. Serverul
# creeaza un Device `pending_claim` FARA statie; doar un admin poate aloca
# explicit statia (vezi `allocate_device`). Fiindca dovada de posesie e
# secretul propriu al dispozitivului (nu un cod emis o singura data de
# server), `enroll_device` e sigur de reincercat oricand cu aceeasi
# identitate -- rezolva exact problema semnalata in issue: "timeout dupa
# claim nu lasa device-ul fara metoda sigura de recuperare".


def _enrollment_status_payload(device: Device) -> dict:
    if device.status == DeviceStatus.revoked.value:
        return {"status": "revoked", "device_id": None, "station_id": None, "credential_secret": None, "enrollment_expires_at": None}
    if device.status == DeviceStatus.active.value and device.station_id is not None:
        return {
            "status": "assigned",
            "device_id": device.id,
            "station_id": device.station_id,
            "credential_secret": device.pending_credential_secret,
            "enrollment_expires_at": None,
        }
    return {
        "status": "pending",
        "device_id": None,
        "station_id": None,
        "credential_secret": None,
        "enrollment_expires_at": device.enrollment_expires_at,
    }


def enroll_device(
    db: Session,
    installation_uuid: str,
    provisioning_secret: str,
    hardware_info: dict,
    *,
    serial_number: str | None = None,
    activation_code: str | None = None,
) -> dict:
    """Idempotenta: un `installation_uuid` necunoscut creeaza un device nou
    `pending_claim` fara statie; unul cunoscut verifica secretul de
    provisioning si intoarce starea CURENTA (pending/assigned/revoked), fara
    sa creeze un al doilea device sau sa retrimita o identitate noua.

    Anti-insusire: daca `installation_uuid` exista deja dar secretul nu se
    potriveste, cererea e respinsa explicit -- o serie/UUID declarat de
    altcineva nu poate "prelua" un enrollment existent."""
    if (serial_number is None) != (activation_code is None):
        raise DeviceServiceError("Serialul si codul de activare trebuie trimise impreuna.")
    if serial_number is not None:
        serial_number = serial_number.strip().upper()

    existing = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    if existing is not None:
        if not existing.provisioning_secret_hash or not verify_password(provisioning_secret, existing.provisioning_secret_hash):
            raise DeviceServiceError("Identitate de enrollment invalida pentru acest installation_uuid.")
        if serial_number is not None:
            assert activation_code is not None  # pair validated above
            expected_activation_hash = hash_token(activation_code.strip().upper())
            if existing.serial_number not in (None, serial_number):
                raise DeviceServiceError("Serialul nu corespunde identitatii provisionate.")
            if existing.activation_code_hash not in (None, expected_activation_hash):
                raise DeviceServiceError("Codul de activare nu corespunde identitatii provisionate.")
            if existing.station_id is None and existing.activation_claimed_at is None:
                existing.serial_number = serial_number
                existing.activation_code_hash = expected_activation_hash
                db.add(existing)
        if (
            existing.status == DeviceStatus.pending_claim.value
            and existing.station_id is None
            and existing.enrollment_expires_at is not None
            and existing.enrollment_expires_at < utcnow()
        ):
            # A unit may sit powered off in inventory for weeks. A valid
            # proof from the physical device renews pending enrollment.
            existing.enrollment_expires_at = expires_in(hours=get_settings().device_enrollment_ttl_hours)
            db.add(existing)
        db.flush()
        return _enrollment_status_payload(existing)

    settings = get_settings()
    device = Device(
        station_id=None,
        name=f"Device neasociat {installation_uuid[:8]}",
        status=DeviceStatus.pending_claim.value,
        capabilities=hardware_info or {},
        installation_uuid=installation_uuid,
        serial_number=serial_number,
        activation_code_hash=(hash_token(activation_code.strip().upper()) if activation_code else None),
        provisioning_secret_hash=hash_password(provisioning_secret),
        enrolled_at=utcnow(),
        enrollment_expires_at=expires_in(hours=settings.device_enrollment_ttl_hours),
    )
    db.add(device)
    try:
        db.flush()
    except IntegrityError:
        # Cursa concurenta reala: alta cerere cu acelasi installation_uuid a
        # castigat intre SELECT-ul de mai sus si acest INSERT (constrangerea
        # unica de pe coloana o garanteaza). Nu e o eroare pentru apelant --
        # verificam identitatea impotriva castigatorului si raspundem la fel
        # ca la o reincercare normala.
        db.rollback()
        winner = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
        if winner is None or not winner.provisioning_secret_hash or not verify_password(provisioning_secret, winner.provisioning_secret_hash):
            raise DeviceServiceError("Identitate de enrollment invalida pentru acest installation_uuid.") from None
        return _enrollment_status_payload(winner)

    return _enrollment_status_payload(device)


def activate_device_for_station(
    db: Session, activation_code: str, station: Station, actor: User
) -> Device:
    """Atomically consume a sealed Device Code and assign its pending device.

    The lookup is deliberately generic on failure so the form cannot be used
    to enumerate inventory. The device's independent provisioning proof was
    already verified by ``enroll_device``.
    """
    normalized = activation_code.strip().upper()
    code_hash = hash_token(normalized)
    device = db.scalar(
        select(Device).where(Device.activation_code_hash == code_hash).with_for_update()
    )
    if device is None:
        raise DeviceServiceError("Cod invalid, expirat sau deja folosit.")
    invalid = (
        device.status != DeviceStatus.pending_claim.value
        or device.station_id is not None
        or device.enrollment_expires_at is None
        or device.enrollment_expires_at < utcnow()
    )
    if invalid:
        raise DeviceServiceError("Cod invalid, expirat sau deja folosit.")

    allocate_device(db, device, station, actor)
    device.activation_code_hash = None
    device.activation_claimed_at = utcnow()
    db.add(device)
    db.flush()
    return device


def list_pending_devices(db: Session) -> list[Device]:
    """Inventar pentru admin: enrollment-uri fara statie inca, indiferent
    daca au expirat deja (UI-ul marcheaza starea, alocarea unuia expirat e
    respinsa explicit de `allocate_device`)."""
    return db.scalars(
        select(Device)
        .where(Device.station_id.is_(None), Device.status == DeviceStatus.pending_claim.value)
        .order_by(Device.enrolled_at.desc())
    ).all()


def allocate_device(db: Session, device: Device, station: Station, admin_user: User) -> str:
    """Aloca un device enrollat-dar-neasociat unei statii. Doar apelabil de
    un administrator autorizat (verificat la nivel de ruta) -- niciodata
    derivat din identitatea declarata de dispozitiv insusi."""
    if device.station_id is not None:
        raise DeviceServiceError("Device-ul este deja alocat unei statii.")
    if device.status == DeviceStatus.revoked.value:
        raise DeviceServiceError("Device-ul a fost revocat si nu mai poate fi alocat.")
    if device.enrollment_expires_at is not None and device.enrollment_expires_at < utcnow():
        raise DeviceServiceError("Enrollment-ul a expirat; dispozitivul trebuie sa refaca /devices/enroll inainte de alocare.")

    raw_secret = generate_opaque_token(32)
    device.station_id = station.id
    device.status = DeviceStatus.active.value
    device.allocated_at = utcnow()
    device.allocated_by_user_id = admin_user.id
    device.pending_credential_secret = raw_secret
    db.add(device)

    credential = DeviceCredential(device_id=device.id, secret_hash=hash_password(raw_secret))
    db.add(credential)
    db.flush()
    return raw_secret


def transfer_device(db: Session, device: Device, target_station: Station, actor: User) -> tuple[uuid.UUID, str]:
    """Transfera un device deja ACTIV catre o alta statie (issue #44).

    Platform_admin only (verificat la nivel de ruta, ca `allocate_device`) --
    e o operatie administrativa deliberat cross-tenant-capabila (hardware
    revandut/reinstalat la alt client), nu una expusa unui organization_admin
    peste propriile statii. Credentiala CURENTA e revocata imediat -- device-ul
    nu mai poate autentifica nicio cerere (heartbeat/telemetrie/comenzi) pana
    nu recupereaza noua credentiala prin exact acelasi canal idempotent deja
    folosit la alocarea initiala (`POST /api/v1/devices/enroll`, raspuns
    `assigned` cu `credential_secret`) -- niciun cod nou de editat manual pe
    device. Devices asigura ca dispozitivul nu primeste config/secrete ale
    NOII statii inainte ca acest transfer sa fie explicit autorizat aici.

    Returneaza (statia_veche_id, secret_nou_in_clar) -- apelantul afiseaza
    secretul o singura data, ca la alocare, pentru recuperare manuala daca
    device-ul nu reia singur polling-ul de enrollment."""
    # Serializam transfer/reset pe randul device-ului. Fara lock, doua POST-uri
    # concurente puteau emite doua credentiale active sau muta hardware-ul in
    # doua statii succesiv pe baza aceleiasi stari citite anterior.
    device = db.scalar(
        select(Device)
        .where(Device.id == device.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if device is None or device.status != DeviceStatus.active.value or device.station_id is None:
        raise DeviceServiceError("Doar un device activ, deja asociat unei statii, poate fi transferat.")
    if device.station_id == target_station.id:
        raise DeviceServiceError("Device-ul este deja asociat acestei statii.")

    old_station_id = device.station_id
    db.execute(
        update(DeviceCredential)
        .where(DeviceCredential.device_id == device.id, DeviceCredential.is_active.is_(True))
        .values(is_active=False, revoked_at=utcnow())
    )
    raw_secret = generate_opaque_token(32)
    credential = DeviceCredential(device_id=device.id, secret_hash=hash_password(raw_secret))
    db.add(credential)

    device.station_id = target_station.id
    device.pending_credential_secret = raw_secret
    device.allocated_at = utcnow()
    device.allocated_by_user_id = actor.id
    db.add(device)
    db.flush()
    return old_station_id, raw_secret


def factory_reset_device(db: Session, device: Device) -> None:
    """Reseteaza un device ACTIV la starea `pending_claim`, fara statie
    (issue #44): "factory reset / reprovisioning" administrativ. Revoca
    imediat orice credentiala curenta (device-ul nu mai poate autentifica
    nicio cerere) si sterge orice urma a alocarii vechi, ca sa poata fi
    revendicat din nou -- fie de un admin prin `/admin/devices/pending`
    (daca reincepe enrollment automat), fie prin autoservire daca
    prezinta un Device Code nou, sigilat, la un enroll ulterior.

    Identitatea proprie a dispozitivului (`installation_uuid`/
    `provisioning_secret_hash`/`serial_number`) NU e atinsa aici -- un
    factory reset FIZIC real, pe hardware, ar regenera-o de partea
    device-ului insusi la urmatorul enroll; acest capat administrativ doar
    detaseaza si revoca partea controlata de server."""
    device = db.scalar(
        select(Device)
        .where(Device.id == device.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if device is None or device.status != DeviceStatus.active.value or device.station_id is None:
        raise DeviceServiceError("Doar un device activ, deja asociat unei statii, poate fi resetat din fabrica.")

    db.execute(
        update(DeviceCredential)
        .where(DeviceCredential.device_id == device.id, DeviceCredential.is_active.is_(True))
        .values(is_active=False, revoked_at=utcnow())
    )
    device.station_id = None
    device.status = DeviceStatus.pending_claim.value
    device.pending_credential_secret = None
    device.allocated_at = None
    device.allocated_by_user_id = None
    device.activation_claimed_at = None
    device.enrollment_expires_at = expires_in(hours=get_settings().device_enrollment_ttl_hours)
    db.add(device)
    db.flush()


def mark_bootstrap_credential_delivered(db: Session, device: Device) -> None:
    """Sterge secretul de credentiala pastrat temporar in clar, odata ce
    dispozitivul a demonstrat ca l-a primit (prima cerere autentificata
    reusita cu noua credentiala). Fereastra de expunere ramane deliberat
    scurta -- vezi comentariul de pe `Device.pending_credential_secret`."""
    if device.pending_credential_secret is not None:
        device.pending_credential_secret = None
        db.add(device)
        db.flush()
