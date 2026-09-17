"""Adaptor Deye Cloud Open API (https://developer.deyecloud.com), read-only
(issue #43) -- conector separat pentru clienti FARA hardware EMS local, care
vor doar sa vada statia/telemetria importata din contul lor Deye Cloud.

NU implementeaza NICIUN endpoint de scriere/comanda catre Deye (`device/register`,
`order*`, `strategy*` din API-ul oficial raman neatinse deliberat) -- controlul
prin cloud necesita un review separat de siguranta, explicit in afara scopului
acestui PR.

Flow de autentificare, verificat impotriva specificatiei OpenAPI oficiale
bundle-uita in portalul de dezvoltatori (vezi docs/LIMITATIONS.md pentru ce nu
a putut fi verificat live, fetch-ul HTTP direct catre developer.deyecloud.com
fiind blocat in acest mediu de dezvoltare -- acelasi tip de limitare de retea
ca la OPCOM/Open-Meteo):

  POST {base}/v1.0/account/token?appId={app_id}
  body: {"appSecret": "...", "email": "...", "password": sha256(parola_in_clar)}
  -> {"access_token": "...", "expires_in": <secunde>, ...}

`appId`/`appSecret` sunt credentialele aplicatiei create in portalul Deye.
Pentru EMS le stocam pe conexiunea statiei, nu ca set global unic, deoarece
operatorii pot folosi aplicatii Deye diferite pe statii/organizatii diferite.
API-ul token observat/documentat cere in continuare si email/parola de cont
Deye; parola se trimite hash-uita SHA-256 (cerut explicit de schema
documentata a `tokenRequest`), NICIODATA in clar peste retea.

Documentatia publica curenta mentioneaza `refreshToken` in raspunsul de
token, dar aceasta versiune a conectorului nu implementeaza inca rotirea pe
refresh token; dupa expirarea `access_token` reautentifica folosind
email/parola, criptate la repaus (`app.core.crypto`). Nu stergem parola pana
nu verificam live grant-ul de refresh si semantica expirarii.

Regiune: doar centrul de date UE (`eu1-developer.deyecloud.com`) e suportat in
aceasta versiune (`Settings.deye_cloud_region`) -- vezi docs/LIMITATIONS.md.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from app.core.security import utcnow
from app.models.device import Device
from app.models.deye_integration import DeyeCloudConnection, DeyeCloudDeviceLink
from app.models.enums import DeviceStatus, DeyeCloudConnectionStatus, TelemetrySource
from app.models.station import Station, StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.models.user import User

logger = structlog.get_logger(__name__)
settings = get_settings()

# Dedicat, distinct de `boot_id`-urile reale ale dispozitivelor RS485 (mereu
# un UUID/token generat de firmware) -- foloseste constrangerea unica
# existenta (device_id, boot_id, sequence) de pe `TelemetryRaw` pentru
# deduplicare, fara sa introduca un mecanism paralel.
CLOUD_BOOT_ID = "deye_cloud"

# Prag de "dispozitiv local activ" -- cat timp exista telemetrie RS485 mai
# recenta decat acest interval pentru statie, Deye Cloud NU mai scrie deloc
# (regula de prioritate ceruta de issue #43, vezi docstring `poll_connection`).
LOCAL_DEVICE_ACTIVE_WINDOW = timedelta(minutes=settings.deye_cloud_local_device_active_minutes)

# Issue #118: conventia reala de unitate a campurilor de putere raportate de
# Deye/Solarman (`generationPower`/`chargePower`/etc.) nu a putut fi
# verificata impotriva unui cont/statie reale (fetch HTTP live blocat in
# acest mediu de dezvoltare) -- `map_station_latest_to_telemetry` PRESUPUNE
# ca API-ul raporteaza kW, nu W. Daca presupunerea e gresita, fiecare citire
# ar fi 1000x prea mare. In lipsa unei verificari live, nu inlocuim tacit
# presupunerea cu alta la fel de neverificata -- in schimb, un plafon de
# plauzibilitate (capacitatea invertorului statiei, cu marja generoasa
# pentru varfuri tranzitorii; un plafon fix generos daca statia nu are inca
# o configuratie) transforma o presupunere gresita intr-un avertisment
# vizibil (`last_sync_status="warning"`, log structurat), in
# loc sa umfle tacit 1000x consumul/economiile afisate.
_PLAUSIBILITY_MARGIN = Decimal(3)
_FALLBACK_POWER_CEILING_W = Decimal(50_000)  # 50 kW: generos pentru rezidential/comercial mic


def _power_plausibility_ceiling_w(db: Session, station_id) -> Decimal:
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    if config is None:
        return _FALLBACK_POWER_CEILING_W
    rated_kw = max(config.pv_installed_power_kw, config.inverter_power_kw)
    return rated_kw * 1000 * _PLAUSIBILITY_MARGIN


def _implausible_power_fields(mapped: dict, ceiling_w: Decimal) -> list[str]:
    offenders = []
    for field in ("pv_power_w", "load_power_w", "battery_power_w", "grid_power_w"):
        value = mapped.get(field)
        if value is not None and abs(value) > ceiling_w:
            offenders.append(field)
    return offenders


_BACKOFF_BASE_SECONDS = 60
_BACKOFF_MAX_SECONDS = 3600


class DeyeCloudError(Exception):
    pass


class DeyeCloudConfigError(DeyeCloudError):
    """Conexiunea nu are credentiale Deye appId/appSecret configurate."""


class DeyeCloudAuthError(DeyeCloudError):
    """Autentificare esuata -- email/parola gresite sau cont revocat la Deye."""


class DeyeCloudApiError(DeyeCloudError):
    """Raspuns `success: false` de la Deye Cloud, cu cod/mesaj de eroare."""

    def __init__(self, code: str | None, msg: str):
        self.code = code
        super().__init__(f"Deye Cloud a raspuns cu eroare (cod {code}): {msg}")


class DeyeCloudUnavailableError(DeyeCloudError):
    """Eroare de retea/timeout dupa toate reincercarile."""


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _require_app_credentials(app_id: str | None, app_secret: str | None) -> tuple[str, str]:
    clean_app_id = (app_id or "").strip()
    if not clean_app_id or not app_secret:
        raise DeyeCloudConfigError("Integrarea Deye Cloud necesita appId si appSecret configurate pentru aceasta statie.")
    return clean_app_id, app_secret


def _post(path: str, *, params: dict | None = None, body: dict | None = None, access_token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    url = f"{settings.deye_cloud_base_url}{path}"
    try:
        for attempt in Retrying(
            stop=stop_after_attempt(settings.deye_cloud_max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=15),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        ):
            with attempt, httpx.Client(timeout=settings.deye_cloud_request_timeout_seconds) as client:
                resp = client.post(url, params=params, json=body or {}, headers=headers)
                # 5xx e retriabil (server temporar indisponibil); 4xx NU e
                # (credentiale/parametri gresiti nu se rezolva prin retry).
                if resp.status_code >= 500:
                    resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise DeyeCloudApiError(None, "raspuns JSON invalid") from exc
                if resp.status_code >= 400 and not (
                    isinstance(data, dict) and data.get("success") is False
                ):
                    raise DeyeCloudApiError(str(resp.status_code), "raspuns HTTP invalid")
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        logger.warning("deye_cloud.request_failed", path=path, error=str(exc))
        raise DeyeCloudUnavailableError(f"Deye Cloud indisponibil ({path}): {exc}") from exc

    if isinstance(data, dict) and data.get("success") is False:
        code = data.get("code")
        msg = data.get("msg", "eroare necunoscuta")
        if str(code) in {"2101021", "2101022", "2101023", "2101024"}:
            # Coduri observate direct impotriva serverului real (auth invalid
            # appId/appSecret/cont) -- vezi docs/LIMITATIONS.md. Orice alt cod
            # e tratat generic (nu presupunem un catalog complet neverificat).
            raise DeyeCloudAuthError(msg)
        raise DeyeCloudApiError(code, msg)
    return data


def authenticate(app_id: str, app_secret: str, email: str, password: str) -> dict:
    """Obtine un access_token nou (POST /v1.0/account/token). Parola e
    hash-uita SHA-256 inainte de a fi trimisa, niciodata in clar."""
    app_id, app_secret = _require_app_credentials(app_id, app_secret)
    data = _post(
        "/v1.0/account/token",
        params={"appId": app_id},
        body={"appSecret": app_secret, "email": email, "password": _hash_password(password)},
    )
    if "access_token" not in data and "accessToken" in data:
        data["access_token"] = data["accessToken"]
    if "expires_in" not in data and "expiresIn" in data:
        data["expires_in"] = data["expiresIn"]
    if "access_token" not in data:
        raise DeyeCloudAuthError("Raspuns de autentificare fara access_token.")
    return data


def list_remote_stations(access_token: str) -> list[dict]:
    data = _post("/v1.0/station/list", body={"page": 1, "size": 200}, access_token=access_token)
    return data.get("stationList") or data.get("stations") or []


def list_remote_station_devices(access_token: str, remote_station_id: int) -> list[dict]:
    data = _post(
        "/v1.0/station/device",
        body={"stationIds": [remote_station_id], "page": 1, "size": 200},
        access_token=access_token,
    )
    return data.get("deviceListItems") or data.get("deviceList") or data.get("list") or []


def fetch_station_latest(access_token: str, remote_station_id: int) -> dict:
    return _post("/v1.0/station/latest", body={"stationId": remote_station_id}, access_token=access_token)


# --- Mapare Deye -> model canonic -------------------------------------------
#
# Cheile de mai jos (`generationPower`, `consumptionPower`, `purchasePower`,
# `wirePower`, `chargePower`, `dischargePower`, `batterySOC`) reflecta
# conventia publica documentata a familiei de API-uri Deye/Solarman pentru
# datele in timp real ale statiei -- NU au putut fi verificate impotriva unui
# raspuns real (cont Deye Cloud platitor, cu statie reala), pentru ca fetch-ul
# HTTP live e blocat in acest mediu de dezvoltare (vezi docs/LIMITATIONS.md).
# Maparea e deliberat DEFENSIVA: o cheie lipsa/necunoscuta produce `None`
# (necunoscut), niciodata 0 -- consecvent cu restul platformei (vezi
# docs/CODE_STANDARDS.md, regula 2).
def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def map_station_latest_to_telemetry(raw: dict) -> dict:
    generation_kw = _dec(raw.get("generationPower"))
    consumption_kw = _dec(raw.get("consumptionPower"))
    charge_kw = _dec(raw.get("chargePower"))
    discharge_kw = _dec(raw.get("dischargePower"))
    purchase_kw = _dec(raw.get("purchasePower"))
    export_kw = _dec(raw.get("wirePower"))
    soc = _dec(raw.get("batterySOC"))

    # Conventia platformei (vezi app/models/telemetry.py): baterie
    # pozitiv=incarcare/negativ=descarcare; retea pozitiv=import/negativ=export.
    # Deye Cloud raporteaza incarcare/descarcare si import/export ca doua
    # valori separate, ambele >= 0 -- combinate aici in convenția cu semn.
    battery_kw = None
    if charge_kw is not None or discharge_kw is not None:
        battery_kw = (charge_kw or Decimal(0)) - (discharge_kw or Decimal(0))
    grid_kw = None
    if purchase_kw is not None or export_kw is not None:
        grid_kw = (purchase_kw or Decimal(0)) - (export_kw or Decimal(0))

    measured_at = None
    last_update_time = raw.get("lastUpdateTime")
    if isinstance(last_update_time, int | float):
        try:
            measured_at = datetime.fromtimestamp(last_update_time, tz=UTC)
        except (OverflowError, OSError, ValueError):
            measured_at = None

    def _w(kw: Decimal | None) -> Decimal | None:
        return kw * 1000 if kw is not None else None

    return {
        "pv_power_w": _w(generation_kw),
        "load_power_w": _w(consumption_kw),
        "battery_power_w": _w(battery_kw),
        "grid_power_w": _w(grid_kw),
        "battery_soc_percent": soc,
        "measured_at": measured_at,
    }


# --- Ciclul de viata al conexiunii -------------------------------------------


def start_connection(
    db: Session, station: Station, user: User, app_id: str, app_secret: str, email: str, password: str
) -> tuple[DeyeCloudConnection, list[dict]]:
    """Autentifica si creeaza/actualizeaza conexiunea in starea
    `pending_selection`, fara sa lege inca o statie Deye Cloud anume --
    utilizatorul alege explicit in pasul urmator (`select_remote_station`),
    consimtamant explicit inainte de orice import."""
    app_id, app_secret = _require_app_credentials(app_id, app_secret)
    auth = authenticate(app_id, app_secret, email, password)
    stations = list_remote_stations(auth["access_token"])

    existing = db.scalar(select(DeyeCloudConnection).where(DeyeCloudConnection.station_id == station.id))
    now = utcnow()
    expires_in = auth.get("expires_in")
    expires_at = now + timedelta(seconds=int(expires_in)) if isinstance(expires_in, int | float) else None

    if existing is not None:
        connection = existing
        connection.status = DeyeCloudConnectionStatus.pending_selection.value
        connection.remote_station_id = None
        connection.remote_station_name = None
        connection.disconnected_at = None
        connection.disconnected_by_user_id = None
        connection.last_sync_status = None
        connection.last_sync_message = None
        connection.consecutive_failure_count = 0
        if connection.device_id is not None:
            device = db.get(Device, connection.device_id)
            if device is not None:
                device.status = DeviceStatus.revoked.value
                device.revoked_at = now
                device.revoked_reason = "Reconectare Deye Cloud in asteptarea selectiei statiei."
                db.add(device)
    else:
        connection = DeyeCloudConnection(
            station_id=station.id,
            created_by_user_id=user.id,
            consent_accepted_at=now,
        )
    connection.app_id = app_id
    connection.encrypted_app_secret = encrypt_secret(app_secret)
    connection.account_email = email
    connection.encrypted_account_password = encrypt_secret(password)
    connection.encrypted_access_token = encrypt_secret(auth["access_token"])
    connection.access_token_expires_at = expires_at
    connection.region = settings.deye_cloud_region
    connection.pending_remote_stations = _normalize_station_choices(stations)
    db.add(connection)
    db.flush()
    return connection, stations


def get_connection_for_station(db: Session, station_id: uuid.UUID) -> DeyeCloudConnection | None:
    return db.scalar(select(DeyeCloudConnection).where(DeyeCloudConnection.station_id == station_id))


def list_pending_remote_stations(connection: DeyeCloudConnection) -> list[dict]:
    """Lista minimala persistata la autentificare; GET-ul UI nu apeleaza
    providerul extern si nu poate bloca randarea paginii."""
    return list(connection.pending_remote_stations or [])


def _normalize_station_choices(stations: list[dict]) -> list[dict]:
    choices: list[dict] = []
    seen: set[int] = set()
    for item in stations:
        raw_id = item.get("id") or item.get("stationId") or item.get("station_id")
        if isinstance(raw_id, bool):
            continue
        try:
            station_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if station_id in seen:
            continue
        raw_name = item.get("name") or item.get("stationName") or f"Statia {station_id}"
        choices.append({"id": station_id, "name": str(raw_name)[:200]})
        seen.add(station_id)
    return choices


def select_remote_station(db: Session, connection: DeyeCloudConnection, remote_station_id: int) -> None:
    """Al doilea pas, explicit, al consimtamantului: clientul a ales CE
    statie din contul lui Deye Cloud sa fie legata de statia platformei.
    Importa inventarul de dispozitive (doar metadata, vezi
    `DeyeCloudDeviceLink`) si creeaza device-ul sintetic care va detine
    telemetria ingerata la polling."""
    choice = next(
        (item for item in (connection.pending_remote_stations or []) if item.get("id") == remote_station_id),
        None,
    )
    if choice is None:
        raise DeyeCloudApiError(None, "Statia selectata nu apartine listei autorizate pentru acest cont.")
    remote_station_name = str(choice.get("name") or f"Statia {remote_station_id}")[:200]
    access_token = _valid_access_token(connection)

    if connection.device_id is None:
        device = Device(
            station_id=connection.station_id,
            name=f"Deye Cloud ({remote_station_name})"[:200],
            status=DeviceStatus.active.value,
            capabilities={"deye_cloud": True, "read_only": True},
        )
        db.add(device)
        db.flush()
        connection.device_id = device.id
    else:
        device = db.get(Device, connection.device_id)
        if device is not None:
            device.status = DeviceStatus.active.value
            device.revoked_at = None
            device.revoked_reason = None
            db.add(device)

    try:
        remote_devices = list_remote_station_devices(access_token, remote_station_id)
    except DeyeCloudError:
        remote_devices = []  # Best-effort: importul de statie nu esueaza doar pt inventar.
        logger.warning("deye_cloud.device_inventory_failed", connection_id=str(connection.id))

    for link in list(connection.device_links):
        connection.device_links.remove(link)
        db.delete(link)
    db.flush()
    for item in remote_devices:
        sn = item.get("deviceSn") or item.get("sn")
        if not sn:
            continue
        # Adaugat prin colectia relatiei (nu doar `connection_id` bruta) ca sa
        # ramana vizibil imediat pe `connection.device_links` in aceeasi
        # sesiune, fara sa necesite un refresh explicit al obiectului parinte.
        connection.device_links.append(
            DeyeCloudDeviceLink(
                remote_device_sn=str(sn),
                remote_device_type=item.get("deviceType"),
                raw_snapshot=item,
            )
        )

    connection.remote_station_id = remote_station_id
    connection.remote_station_name = remote_station_name
    connection.pending_remote_stations = []
    connection.status = DeyeCloudConnectionStatus.connected.value
    connection.consecutive_failure_count = 0
    db.add(connection)
    db.flush()


def disconnect(db: Session, connection: DeyeCloudConnection, user: User) -> None:
    """Deconectare din UI (issue #43): opreste polling-ul si sterge
    credentialele stocate (parola contului + token cache-uit) -- NEDISTRUCTIV
    fata de telemetria deja importata (ramane in `TelemetryRaw`, ca istoric,
    la fel ca arhivarea unei statii). Revocarea token-ului direct la Deye NU
    e posibila -- specificatia bundle-uita nu expune un endpoint de revocare;
    stergerea locala a credentialelor e singura actiune disponibila."""
    connection.status = DeyeCloudConnectionStatus.disconnected.value
    connection.app_id = None
    connection.encrypted_app_secret = None
    connection.encrypted_account_password = ""
    connection.encrypted_access_token = None
    connection.access_token_expires_at = None
    connection.pending_remote_stations = []
    connection.disconnected_at = utcnow()
    connection.disconnected_by_user_id = user.id
    db.add(connection)
    if connection.device_id is not None:
        device = db.get(Device, connection.device_id)
        if device is not None:
            device.status = DeviceStatus.revoked.value
            device.revoked_at = utcnow()
            device.revoked_reason = "Conexiune Deye Cloud deconectata de utilizator."
            db.add(device)


def _valid_access_token(connection: DeyeCloudConnection) -> str:
    now = utcnow()
    if connection.encrypted_access_token and connection.access_token_expires_at and connection.access_token_expires_at > now + timedelta(minutes=1):
        try:
            return decrypt_secret(connection.encrypted_access_token)
        except DecryptionError:
            pass  # SECRET_KEY schimbat de la criptare -- reautentifica mai jos.

    if not connection.app_id or not connection.encrypted_app_secret:
        raise DeyeCloudConfigError("Conexiunea Deye Cloud nu are appId/appSecret configurate; reconecteaza statia.")

    try:
        password = decrypt_secret(connection.encrypted_account_password)
    except DecryptionError as exc:
        raise DeyeCloudAuthError("Parola stocata nu a putut fi decriptata (SECRET_KEY schimbat?).") from exc
    try:
        app_secret = decrypt_secret(connection.encrypted_app_secret or "")
    except DecryptionError as exc:
        raise DeyeCloudAuthError("AppSecret-ul stocat nu a putut fi decriptat (SECRET_KEY schimbat?).") from exc
    auth = authenticate(connection.app_id or "", app_secret, connection.account_email, password)
    connection.encrypted_access_token = encrypt_secret(auth["access_token"])
    expires_in = auth.get("expires_in")
    connection.access_token_expires_at = (
        now + timedelta(seconds=int(expires_in)) if isinstance(expires_in, int | float) else None
    )
    return auth["access_token"]


def local_device_active(db: Session, station_id: uuid.UUID, exclude_device_id: uuid.UUID | None) -> bool:
    """Regula de prioritate ceruta de issue #43: exista telemetrie RS485
    recenta (dintr-un dispozitiv EMS local real, deci diferit de device-ul
    sintetic Deye Cloud) pentru aceasta statie? Daca da, Deye Cloud NU scrie
    deloc in acest ciclu de polling -- dispozitivul local e mereu sursa de
    adevar cand e activ. Verificat pe `TelemetryRaw` (nu pe `Device.last_heartbeat_at`,
    care e specific protocolului web-device si nu ar acoperi un viitor
    dispozitiv local cu alt mecanism de raportare)."""
    cutoff = utcnow() - LOCAL_DEVICE_ACTIVE_WINDOW
    query = select(TelemetryRaw.id).where(
        TelemetryRaw.station_id == station_id,
        TelemetryRaw.source == TelemetrySource.device_rs485.value,
        TelemetryRaw.measured_at > cutoff,
    )
    if exclude_device_id is not None:
        query = query.where(TelemetryRaw.device_id != exclude_device_id)
    return db.scalar(query.limit(1)) is not None


def _backoff_seconds(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 0
    return min(_BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1)), _BACKOFF_MAX_SECONDS)


def poll_connection(db: Session, connection: DeyeCloudConnection) -> dict:
    """Un ciclu de polling (apelat din `app.workers.tasks.deye_cloud_poll_task`,
    NICIODATA dintr-un handler de cerere web -- issue #43 cere explicit ca
    pagina sa nu astepte API-ul Deye). Idempotent: fiecare citire e deduplicata
    prin constrangerea unica existenta (device_id, boot_id, sequence) de pe
    `TelemetryRaw`, cheind pe timestamp-ul UNIX raportat de Deye."""
    if connection.status != DeyeCloudConnectionStatus.connected.value:
        return {"status": "skipped", "reason": "not_connected"}
    if connection.remote_station_id is None or connection.device_id is None:
        return {"status": "skipped", "reason": "no_remote_station_selected"}

    now = utcnow()
    if connection.consecutive_failure_count > 0 and connection.last_sync_at is not None:
        wait = _backoff_seconds(connection.consecutive_failure_count)
        if (now - connection.last_sync_at).total_seconds() < wait:
            return {"status": "skipped", "reason": "backoff"}

    if local_device_active(db, connection.station_id, connection.device_id):
        connection.last_sync_at = now
        connection.last_sync_status = "skipped"
        connection.last_sync_message = "Dispozitiv EMS local activ -- Deye Cloud nu suprascrie telemetria."
        db.add(connection)
        return {"status": "skipped", "reason": "local_device_active"}

    try:
        access_token = _valid_access_token(connection)
        raw = fetch_station_latest(access_token, connection.remote_station_id)
    except DeyeCloudConfigError as exc:
        connection.status = DeyeCloudConnectionStatus.error.value
        connection.last_sync_at = now
        connection.last_sync_status = "failed"
        connection.last_sync_message = "AppId/appSecret lipsesc -- reconectati Deye Cloud pentru aceasta statie."
        connection.consecutive_failure_count += 1
        db.add(connection)
        logger.warning("deye_cloud.poll_config_failed", connection_id=str(connection.id), error=str(exc))
        return {"status": "failed", "reason": "config_error"}
    except DeyeCloudAuthError as exc:
        connection.status = DeyeCloudConnectionStatus.error.value
        connection.last_sync_at = now
        connection.last_sync_status = "failed"
        connection.last_sync_message = "Autentificare esuata -- reconectati contul Deye Cloud."
        connection.consecutive_failure_count += 1
        db.add(connection)
        logger.warning("deye_cloud.poll_auth_failed", connection_id=str(connection.id), error=str(exc))
        return {"status": "failed", "reason": "auth_error"}
    except DeyeCloudError as exc:
        connection.last_sync_at = now
        connection.last_sync_status = "failed"
        connection.last_sync_message = "Deye Cloud indisponibil sau eroare API -- reincercare programata."
        connection.consecutive_failure_count += 1
        db.add(connection)
        logger.warning("deye_cloud.poll_failed", connection_id=str(connection.id), error=str(exc))
        return {"status": "failed", "reason": "api_error"}

    mapped = map_station_latest_to_telemetry(raw)
    measured_at = mapped.pop("measured_at") or now
    received_at = now
    sequence = int(measured_at.timestamp())

    existing_row = db.scalar(
        select(TelemetryRaw.id).where(
            TelemetryRaw.device_id == connection.device_id,
            TelemetryRaw.boot_id == CLOUD_BOOT_ID,
            TelemetryRaw.sequence == sequence,
        )
    )
    created = False
    if existing_row is None:
        db.add(
            TelemetryRaw(
                device_id=connection.device_id,
                station_id=connection.station_id,
                boot_id=CLOUD_BOOT_ID,
                sequence=sequence,
                measured_at=measured_at,
                received_at=received_at,
                source=TelemetrySource.deye_cloud.value,
                raw_payload=raw,
                **mapped,
            )
        )
        created = True
        # Flush explicit: sesiunea (atat `SessionLocal`, cat si fixture-ul de
        # test) foloseste `autoflush=False` -- fara acest flush, o a doua
        # rulare in ACEEASI sesiune/tranzactie (ex. reintent) nu ar vedea
        # inca randul de mai sus la verificarea de deduplicare de mai jos.
        db.flush()

    ceiling_w = _power_plausibility_ceiling_w(db, connection.station_id)
    offenders = _implausible_power_fields(mapped, ceiling_w)

    connection.last_sync_at = now
    connection.consecutive_failure_count = 0
    if offenders:
        connection.last_sync_status = "warning"
        connection.last_sync_message = (
            f"Putere neplauzibil de mare ({', '.join(offenders)}) -- posibil ca API-ul Deye sa raporteze W, "
            "nu kW (issue #118, neverificat live). Verificati valorile fata de puterea reala a statiei."
        )
        logger.warning(
            "deye_cloud.poll_implausible_power",
            connection_id=str(connection.id),
            station_id=str(connection.station_id),
            offenders=offenders,
            ceiling_w=str(ceiling_w),
            mapped={k: (str(v) if v is not None else None) for k, v in mapped.items()},
        )
    else:
        connection.last_sync_status = "succeeded"
        connection.last_sync_message = None
    db.add(connection)
    return {
        "status": "succeeded",
        "telemetry_created": created,
        "measured_at": measured_at.isoformat(),
        "implausible_power_fields": offenders,
    }
