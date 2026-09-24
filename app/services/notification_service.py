from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.email import get_email_adapter
from app.core.security import constant_time_eq, hash_token, utcnow
from app.models.alert import Alert
from app.models.health import AlertEvent
from app.models.notification import Notification, NotificationDelivery, NotificationPreference
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import User
from app.services.health_service import RULES

CATEGORIES = ("incident", "warning", "opportunity", "info")
SEVERITIES = ("info", "warning", "error", "critical")
CHANNELS = ("email", "push")
MODES = ("off", "immediate", "daily")
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}


def member_can_receive(db, user_id, organization_id):
    user = db.get(User, user_id)
    org = db.get(Organization, organization_id)
    return bool(
        user
        and user.is_active
        and org
        and org.status != "archived"
        and db.scalar(
            select(Membership.id).where(
                Membership.user_id == user_id,
                Membership.organization_id == organization_id,
                Membership.is_active.is_(True),
            )
        )
    )


def preference(db, user, org, *, lock=False):
    db.execute(
        insert(NotificationPreference)
        .values(user_id=user.id, organization_id=org.id)
        .on_conflict_do_nothing(constraint="uq_notification_preference")
    )
    query = select(NotificationPreference).where(
        NotificationPreference.user_id == user.id, NotificationPreference.organization_id == org.id
    )
    return db.scalar(query.with_for_update() if lock else query)


def validate_matrix(matrix):
    allowed = {
        f"{category}:{severity}:{channel}"
        for category in CATEGORIES
        for severity in SEVERITIES
        for channel in CHANNELS
    }
    if not isinstance(matrix, dict) or any(
        k not in allowed or v not in MODES for k, v in matrix.items()
    ):
        raise ValueError("Preferinte de notificare invalide.")
    return matrix


def mode(pref, notice, channel):
    return pref.matrix.get(f"{notice.category}:{notice.severity}:{channel}", "off")


def quiet(pref, at):
    hour = at.astimezone(ZoneInfo(pref.timezone)).hour
    start, end = pref.quiet_start, pref.quiet_end
    return start != end and (start <= hour < end if start < end else hour >= start or hour < end)


def delivery_time(pref, at, delivery_mode, critical=False):
    candidate = at.astimezone(UTC).replace(second=0, microsecond=0)
    if critical and delivery_mode == "immediate":
        return at
    if delivery_mode == "immediate" and not quiet(pref, at):
        return at
    local_now = at.astimezone(ZoneInfo(pref.timezone))
    target_date = local_now.date()
    if delivery_mode == "daily" and local_now.hour >= 8:
        target_date += timedelta(days=1)
    elif delivery_mode == "weekly":
        days = (-local_now.weekday()) % 7
        if days == 0 and local_now.hour >= 8:
            days = 7
        target_date += timedelta(days=days)
    # Walk real UTC instants, including skipped and repeated local hours.
    for _ in range(8 * 24 * 60):
        local = candidate.astimezone(ZoneInfo(pref.timezone))
        scheduled = delivery_mode == "immediate" or (
            local.date() >= target_date
            and local.hour >= 8
            and (delivery_mode == "daily" or local.weekday() == 0)
        )
        if scheduled and not quiet(pref, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("Nu exista fereastra de livrare.")


def materialize(db, now=None):
    now = now or utcnow()
    events = db.execute(
        select(AlertEvent, Alert, Station)
        .join(Alert, Alert.id == AlertEvent.alert_id)
        .join(Station, Station.id == Alert.station_id)
        .where(
            AlertEvent.occurred_at >= now - timedelta(days=30),
            AlertEvent.status.in_(("active", "resolved")),
        )
        .order_by(AlertEvent.occurred_at)
    ).all()
    count = 0
    for event, alert, station in events:
        rule = RULES.get(alert.category)
        if rule is None:
            continue
        users = db.scalars(
            select(Membership.user_id)
            .join(User, User.id == Membership.user_id)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(
                Membership.organization_id == station.organization_id,
                Membership.is_active.is_(True),
                User.is_active.is_(True),
                Organization.status != "archived",
            )
        ).all()
        for user_id in users:
            inserted = db.execute(
                insert(Notification)
                .values(
                    user_id=user_id,
                    station_id=station.id,
                    event_id=event.id,
                    title=("Rezolvat: " if event.status == "resolved" else "") + rule.title,
                    severity=alert.severity,
                    category=rule.category,
                    link=f"/stations/{station.id}/health#alert-{alert.id}",
                )
                .on_conflict_do_nothing(constraint="uq_notification_event_user")
                .returning(Notification.id)
            ).scalar_one_or_none()
            count += inserted is not None
    return count


def _enqueue(db, pref, channel, due, key, ids, kind="alert", payload=None):
    db.execute(
        insert(NotificationDelivery)
        .values(
            user_id=pref.user_id,
            organization_id=pref.organization_id,
            channel=channel,
            due_at=due,
            dedupe_key=key,
            notification_ids=ids,
            kind=kind,
            encrypted_payload=payload,
        )
        .on_conflict_do_nothing(constraint="uq_notification_delivery_key")
    )
    delivery = db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.dedupe_key == key).with_for_update()
    )
    if delivery.status == "pending":
        delivery.notification_ids = sorted(set(delivery.notification_ids) | set(ids))
    return delivery


def route_pending(db, now=None):
    now = now or utcnow()
    notices = db.scalars(
        select(Notification)
        .where(Notification.routed_at.is_(None))
        .order_by(Notification.created_at)
        .limit(200)
        .with_for_update(skip_locked=True)
    ).all()
    for notice in notices:
        station = db.get(Station, notice.station_id)
        pref = db.scalar(
            select(NotificationPreference)
            .where(
                NotificationPreference.user_id == notice.user_id,
                NotificationPreference.organization_id == station.organization_id,
            )
            .with_for_update()
        )
        if pref:
            event = db.get(AlertEvent, notice.event_id)
            for channel in CHANNELS:
                chosen = mode(pref, notice, channel)
                due = (
                    now
                    if chosen == "off"
                    else delivery_time(pref, now, chosen, notice.severity == "critical")
                )
                bucket = str(notice.id) if chosen != "daily" else due.isoformat()
                key = f"{pref.id}:{channel}:{chosen}:{bucket}"
                delivery = _enqueue(db, pref, channel, due, key, [str(notice.id)])
                if chosen == "off":
                    delivery.status, delivery.failure_code = "suppressed", "opted_out"
                elif (
                    notice.severity == "critical"
                    and event.status == "active"
                    and pref.escalation_minutes
                ):
                    due = now + timedelta(minutes=pref.escalation_minutes)
                    _enqueue(
                        db,
                        pref,
                        channel,
                        due,
                        f"{pref.id}:{channel}:escalation:{notice.id}",
                        [str(notice.id)],
                        "escalation",
                    )
        notice.routed_at = now
    db.flush()
    return len(notices)


def weekly_reports(db, now=None):
    now = now or utcnow()
    for pref in db.scalars(
        select(NotificationPreference)
        .where(NotificationPreference.weekly_report.is_(True))
        .with_for_update(skip_locked=True)
    ):
        local = now.astimezone(ZoneInfo(pref.timezone))
        if local.weekday() != 0 or local.hour < 8 or quiet(pref, now):
            continue
        ids = [
            str(v)
            for v in db.scalars(
                select(Notification.id)
                .join(Station, Station.id == Notification.station_id)
                .where(
                    Notification.user_id == pref.user_id,
                    Station.organization_id == pref.organization_id,
                    Notification.created_at >= now - timedelta(days=7),
                )
            )
        ]
        for channel in CHANNELS:
            if any(k.endswith(f":{channel}") and v != "off" for k, v in pref.matrix.items()):
                _enqueue(
                    db,
                    pref,
                    channel,
                    now,
                    f"{pref.id}:{channel}:weekly:{local.date()}",
                    ids,
                    "weekly",
                )


def validate_subscription(value):
    if not isinstance(value, dict) or set(value) - {"endpoint", "keys", "expirationTime"}:
        raise ValueError("Subscriptie push invalida.")
    endpoint = value.get("endpoint", "")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PUSH_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(endpoint) > 2048
    ):
        raise ValueError("Serviciu push nesuportat.")
    keys = value.get("keys", {})
    for name, length in (("auth", 16), ("p256dh", 65)):
        try:
            encoded = keys[name]
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Cheie push invalida.") from exc
        if len(decoded) != length:
            raise ValueError("Lungime cheie push invalida.")
    return {"endpoint": endpoint, "keys": {k: keys[k] for k in ("auth", "p256dh")}}


class PushAdapter:
    def send(self, subscription, payload, key):
        import requests
        from pywebpush import webpush

        class NoRedirectSession(requests.Session):
            def post(self, *args, **kwargs):
                kwargs["allow_redirects"] = False
                return super().post(*args, **kwargs)

        settings = get_settings()
        with NoRedirectSession() as session:
            response = webpush(
                validate_subscription(subscription),
                json.dumps(payload),
                vapid_private_key=settings.notifications_vapid_private_key,
                vapid_claims={"sub": settings.notifications_vapid_subject},
                timeout=10,
                headers={"Topic": hash_token(key)[:32]},
                requests_session=session,
            )
        if not 200 <= response.status_code < 300:
            raise RuntimeError("push_delivery_failed")


def request_verification(db, pref, user, now=None):
    now = now or utcnow()
    if not get_settings().notifications_email_enabled:
        raise ValueError("Emailul nu este configurat.")
    code = secrets.token_hex(4).upper()
    pref.verification_hash = hash_token(code)
    pref.verification_expires_at = now + timedelta(minutes=15)
    _enqueue(
        db,
        pref,
        "email",
        now,
        f"verify:{pref.id}:{pref.verification_hash[:20]}",
        [],
        "verification",
        encrypt_secret(json.dumps({"email": user.email, "code": code})),
    )


def verify_email(pref, user, code, now=None):
    now = now or utcnow()
    if (
        not pref.verification_hash
        or not pref.verification_expires_at
        or now >= pref.verification_expires_at
        or not constant_time_eq(hash_token(code.strip().upper()), pref.verification_hash)
    ):
        raise ValueError("Cod invalid sau expirat.")
    pref.verified_email = user.email
    pref.verification_hash = None
    pref.verification_expires_at = None


def deliver_one(db, now=None, *, email_adapter=None, push_adapter=None):
    now = now or utcnow()
    delivery = db.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.status == "pending", NotificationDelivery.due_at <= now)
        .order_by(NotificationDelivery.due_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if delivery is None:
        return False
    pref = db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == delivery.user_id,
            NotificationPreference.organization_id == delivery.organization_id,
        )
    )
    user = db.get(User, delivery.user_id)
    settings = get_settings()
    reason = None
    if not pref or not member_can_receive(db, delivery.user_id, delivery.organization_id):
        reason = "access_revoked"
    elif delivery.kind == "verification":
        if not pref.verification_expires_at or now >= pref.verification_expires_at:
            reason = "verification_expired"
    elif delivery.channel == "email" and pref.verified_email != user.email:
        reason = "destination_unverified"
    elif delivery.channel == "push" and not pref.encrypted_push_subscription:
        reason = "destination_missing"
    notices = []
    if not reason and delivery.kind != "verification":
        notices = list(
            db.scalars(
                select(Notification)
                .join(Station, Station.id == Notification.station_id)
                .where(
                    Notification.id.in_(delivery.notification_ids),
                    Notification.user_id == delivery.user_id,
                    Station.organization_id == delivery.organization_id,
                )
                .order_by(Notification.created_at, Notification.id)
            )
        )
        notices = [n for n in notices if mode(pref, n, delivery.channel) != "off"]
        if delivery.kind == "escalation":
            notices = [
                n
                for n in notices
                if db.get(Alert, db.get(AlertEvent, n.event_id).alert_id).status
                in ("active", "detected", "open")
            ]
        if not notices and delivery.kind != "weekly":
            reason = "resolved_or_opted_out"
        if delivery.kind == "weekly" and not pref.weekly_report:
            reason = "opted_out"
        if (
            not reason
            and quiet(pref, now)
            and not any(
                n.severity == "critical" and mode(pref, n, delivery.channel) == "immediate"
                for n in notices
            )
        ):
            delivery.due_at = delivery_time(pref, now, "immediate")
            return True
    if delivery.channel == "email" and not settings.notifications_email_enabled:
        reason = "channel_unconfigured"
    if delivery.channel == "push" and not settings.notifications_push_enabled:
        reason = "channel_unconfigured"
    if reason:
        delivery.status, delivery.failure_code = "suppressed", reason
        return True
    title = "Rezumat saptamanal EMS" if delivery.kind == "weekly" else "Notificari EMS"
    link = "/notifications"
    body = (
        "\n".join(n.title for n in notices)
        + "\nDeschide Notificari in aplicatia EMS. Preferintele si dezabonarea sunt disponibile in aceeasi pagina."
    )
    destination = user.email
    if delivery.kind == "verification":
        payload = json.loads(decrypt_secret(delivery.encrypted_payload))
        if payload["email"] != user.email or hash_token(payload["code"]) != pref.verification_hash:
            delivery.status, delivery.failure_code = "suppressed", "verification_superseded"
            return True
        body = f"Codul tau de verificare EMS: {payload['code']}. Valabil 15 minute."
        title = "Verificare notificari EMS"
    delivery.attempts += 1
    try:
        if delivery.channel == "email":
            (email_adapter or get_email_adapter()).send(destination, title, body)
        else:
            (push_adapter or PushAdapter()).send(
                json.loads(decrypt_secret(pref.encrypted_push_subscription)),
                {"title": title, "body": "Ai notificari noi in EMS.", "url": link},
                delivery.dedupe_key,
            )
    except Exception:
        # Provider responses may contain subscription credentials or addresses.
        delivery.failure_code = "adapter_failure"
        delivery.due_at = now + timedelta(minutes=min(2**delivery.attempts, 60))
        if delivery.attempts >= 5:
            delivery.status = "failed"
    else:
        delivery.status, delivery.delivered_at, delivery.failure_code = "delivered", now, None
        delivery.encrypted_payload = None
    db.flush()
    return True
