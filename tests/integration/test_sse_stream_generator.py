"""Test real al generatorului `live_metric_events` (issue #50): consuma
efectiv fluxul async pentru cateva tururi, verificand secventierea reala
snapshot -> delta/heartbeat, sequence monoton, si oprirea imediata la
revocare mid-stream. Foloseste `engine` (date comise real), nu fixture-ul
`db` cu SAVEPOINT -- generatorul deschide propria sesiune prin
`app.database.SessionLocal()` (o conexiune noua, separata), la fel ca
taskurile Celery testate in `test_admin_job_tasks.py`."""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.device import Device
from app.models.organization import Membership, Organization
from app.models.station import PanelGroup, Station, StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.models.user import Session as UserSession
from app.models.user import User
from app.web.routes import sse
from tests.factories import make_device, make_membership, make_org, make_station, make_user


class _FakeRequest:
    """Simuleaza `Request.is_disconnected()` -- devine True dupa `max_ticks`
    iteratii, ca testul sa nu ruleze la infinit."""

    def __init__(self, max_ticks: int):
        self.max_ticks = max_ticks
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.max_ticks


@pytest.fixture()
def committed(engine):
    with Session(engine) as setup:
        suffix = uuid4().hex
        user = make_user(setup, email=f"{suffix}@sse-stream.test")
        org = make_org(setup, f"SSE Stream {suffix}")
        station = make_station(setup, org, user, name=f"SSE Stream Station {suffix}")
        make_membership(setup, user, org, role="viewer")
        device = make_device(setup, station)
        sess = UserSession(
            user_id=user.id, token_hash=f"sse-stream-{suffix}", csrf_secret="csrf",
            expires_at=utcnow() + timedelta(hours=1),
        )
        setup.add(sess)
        setup.commit()
        ids = {"user_id": user.id, "org_id": org.id, "station_id": station.id, "session_id": sess.id, "device_id": device.id}

    yield ids

    with Session(engine) as cleanup:
        cleanup.execute(delete(TelemetryRaw).where(TelemetryRaw.station_id == ids["station_id"]))
        cleanup.execute(delete(Device).where(Device.id == ids["device_id"]))
        cleanup.execute(delete(PanelGroup).where(PanelGroup.station_id == ids["station_id"]))
        cleanup.execute(delete(StationConfigVersion).where(StationConfigVersion.station_id == ids["station_id"]))
        cleanup.execute(delete(UserSession).where(UserSession.id == ids["session_id"]))
        cleanup.execute(delete(Membership).where(Membership.user_id == ids["user_id"]))
        cleanup.execute(delete(Station).where(Station.id == ids["station_id"]))
        cleanup.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        cleanup.execute(delete(User).where(User.id == ids["user_id"]))
        cleanup.commit()


async def _collect(request, ids, ticks, monkeypatch):
    monkeypatch.setattr(sse, "POLL_INTERVAL_SECONDS", 0)
    events = []
    async for ev in sse.live_metric_events(request, ids["station_id"], ids["user_id"], ids["session_id"]):
        events.append(ev)
        if len(events) >= ticks:
            break
    return events


async def test_first_event_is_always_a_snapshot(committed, monkeypatch):
    request = _FakeRequest(max_ticks=5)
    events = await _collect(request, committed, ticks=1, monkeypatch=monkeypatch)
    assert events[0]["event"] == "snapshot"
    payload = json.loads(events[0]["data"])
    assert payload["sequence"] == 1
    assert any(m["metric"] == "pv_power_kw" for m in payload["metrics"])


async def test_sequence_is_monotonic_across_ticks(committed, monkeypatch):
    request = _FakeRequest(max_ticks=10)
    events = await _collect(request, committed, ticks=3, monkeypatch=monkeypatch)
    sequences = [json.loads(ev["data"])["sequence"] for ev in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)  # niciodata repetata


async def test_unchanged_state_yields_heartbeat_after_snapshot(committed, monkeypatch):
    request = _FakeRequest(max_ticks=10)
    events = await _collect(request, committed, ticks=2, monkeypatch=monkeypatch)
    assert events[0]["event"] == "snapshot"
    assert events[1]["event"] == "heartbeat"  # nimic nu s-a schimbat intre cele doua tururi


async def test_new_telemetry_between_ticks_produces_delta_with_only_changed_metric(committed, monkeypatch, engine):
    request = _FakeRequest(max_ticks=10)
    monkeypatch.setattr(sse, "POLL_INTERVAL_SECONDS", 0)

    gen = sse.live_metric_events(request, committed["station_id"], committed["user_id"], committed["session_id"])
    first = await gen.__anext__()
    assert first["event"] == "snapshot"

    with Session(engine) as write:
        write.add(TelemetryRaw(
            device_id=committed["device_id"], station_id=committed["station_id"], boot_id="stream-test", sequence=1,
            measured_at=utcnow(), received_at=utcnow(), pv_power_w=2500, load_power_w=900, is_simulated=False,
        ))
        write.commit()

    second = await gen.__anext__()
    assert second["event"] == "delta"
    payload = json.loads(second["data"])
    metric_names = {m["metric"] for m in payload["metrics"]}
    assert "pv_power_kw" in metric_names
    pv_entry = next(m for m in payload["metrics"] if m["metric"] == "pv_power_kw")
    assert pv_entry["value"] == 2.5
    await gen.aclose()


async def test_stream_stops_immediately_when_membership_revoked_mid_stream(committed, monkeypatch, engine):
    """Fluxul se opreste in cel mult un tur de polling dupa revocare -- nu
    doar la o noua conexiune (aceeasi cerinta ca in test_sse_authorization.py,
    verificata aici pe generatorul real, nu doar pe functia de autorizare)."""
    request = _FakeRequest(max_ticks=10)
    monkeypatch.setattr(sse, "POLL_INTERVAL_SECONDS", 0)

    gen = sse.live_metric_events(request, committed["station_id"], committed["user_id"], committed["session_id"])
    first = await gen.__anext__()
    assert first["event"] == "snapshot"

    with Session(engine) as write:
        membership = write.scalar(
            select(Membership).where(Membership.user_id == committed["user_id"], Membership.organization_id == committed["org_id"])
        )
        membership.is_active = False
        write.add(membership)
        write.commit()

    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


async def test_cross_tenant_station_never_yields_any_event(engine):
    """Un utilizator fara nicio membership in organizatia statiei nu trebuie
    sa primeasca niciun eveniment -- nici macar un snapshot gol."""
    suffix = uuid4().hex
    with Session(engine) as setup:
        outsider = make_user(setup, email=f"{suffix}-outsider@sse-stream.test")
        org = make_org(setup, f"SSE Stream Other {suffix}")
        owner = make_user(setup, email=f"{suffix}-owner@sse-stream.test")
        make_membership(setup, owner, org, role="viewer")
        station = make_station(setup, org, owner, name=f"SSE Stream Other Station {suffix}")
        sess = UserSession(
            user_id=outsider.id, token_hash=f"sse-stream-outsider-{suffix}", csrf_secret="csrf",
            expires_at=utcnow() + timedelta(hours=1),
        )
        setup.add(sess)
        setup.commit()
        ids = {"user_id": outsider.id, "owner_id": owner.id, "org_id": org.id, "station_id": station.id, "session_id": sess.id}

    try:
        request = _FakeRequest(max_ticks=3)
        events = [ev async for ev in sse.live_metric_events(request, ids["station_id"], ids["user_id"], ids["session_id"])]
        assert events == []
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(StationConfigVersion).where(StationConfigVersion.station_id == ids["station_id"]))
            cleanup.execute(delete(PanelGroup).where(PanelGroup.station_id == ids["station_id"]))
            cleanup.execute(delete(Station).where(Station.id == ids["station_id"]))
            cleanup.execute(delete(Membership).where(Membership.organization_id == ids["org_id"]))
            cleanup.execute(delete(Organization).where(Organization.id == ids["org_id"]))
            cleanup.execute(delete(UserSession).where(UserSession.id == ids["session_id"]))
            cleanup.execute(delete(User).where(User.id.in_([ids["user_id"], ids["owner_id"]])))
            cleanup.commit()
