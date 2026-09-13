"""Test real al generatorului `live_metric_events` (issue #50): consuma
efectiv fluxul async pentru cateva tururi, verificand secventierea reala
snapshot -> delta/heartbeat, sequence monoton, si oprirea imediata la
revocare mid-stream. Foloseste `engine` (date comise real), nu fixture-ul
`db` cu SAVEPOINT -- generatorul deschide propria sesiune prin
`app.database.SessionLocal()` (o conexiune noua, separata), la fel ca
taskurile Celery testate in `test_admin_job_tasks.py`."""
from __future__ import annotations

import asyncio
import json
import threading
import time
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


async def test_concurrent_connections_across_tenants_never_cross_contaminate(engine, monkeypatch):
    """Doua conexiuni SSE (statii/organizatii DIFERITE) avansate CONCURENT
    (`asyncio.gather`, nu una dupa alta) -- simuleaza doi vizitatori reali cu
    dashboard-uri diferite deschise in acelasi moment. Testele anterioare
    (`test_cross_tenant_station_never_yields_any_event`) verifica izolarea
    consumand un singur flux o data; acesta verifica in plus ca izolarea
    tine si cand cele doua bucle de polling ruleaza efectiv in paralel, nu
    doar secvential."""
    monkeypatch.setattr(sse, "POLL_INTERVAL_SECONDS", 0)
    suffix = uuid4().hex
    tenants = []
    with Session(engine) as setup:
        for label in ("a", "b"):
            user = make_user(setup, email=f"{suffix}-{label}@sse-concurrent.test")
            org = make_org(setup, f"SSE Concurrent {label} {suffix}")
            station = make_station(setup, org, user, name=f"SSE Concurrent Station {label} {suffix}")
            make_membership(setup, user, org, role="viewer")
            device = make_device(setup, station)
            sess = UserSession(
                user_id=user.id, token_hash=f"sse-concurrent-{label}-{suffix}", csrf_secret="csrf",
                expires_at=utcnow() + timedelta(hours=1),
            )
            setup.add(sess)
            setup.flush()
            tenants.append({"label": label, "user_id": user.id, "org_id": org.id, "station_id": station.id, "session_id": sess.id, "device_id": device.id})
        setup.commit()

    try:
        request_a = _FakeRequest(max_ticks=10)
        request_b = _FakeRequest(max_ticks=10)
        gen_a = sse.live_metric_events(request_a, tenants[0]["station_id"], tenants[0]["user_id"], tenants[0]["session_id"])
        gen_b = sse.live_metric_events(request_b, tenants[1]["station_id"], tenants[1]["user_id"], tenants[1]["session_id"])

        snap_a, snap_b = await asyncio.gather(gen_a.__anext__(), gen_b.__anext__())
        assert snap_a["event"] == "snapshot"
        assert snap_b["event"] == "snapshot"

        with Session(engine) as write:
            write.add(TelemetryRaw(
                device_id=tenants[0]["device_id"], station_id=tenants[0]["station_id"], boot_id="concurrent-a", sequence=1,
                measured_at=utcnow(), received_at=utcnow(), pv_power_w=4200, load_power_w=1000, is_simulated=False,
            ))
            write.commit()

        # Numai organizatia A a primit telemetrie noua -- avansate CONCURENT,
        # A trebuie sa produca `delta` cu valoarea ei, B doar `heartbeat`,
        # niciodata amestecate intre fluxuri.
        ev_a, ev_b = await asyncio.gather(gen_a.__anext__(), gen_b.__anext__())
        assert ev_a["event"] == "delta"
        payload_a = json.loads(ev_a["data"])
        assert any(m["metric"] == "pv_power_kw" and m["value"] == 4.2 for m in payload_a["metrics"])
        assert ev_b["event"] == "heartbeat"

        await gen_a.aclose()
        await gen_b.aclose()
    finally:
        with Session(engine) as cleanup:
            for t in tenants:
                cleanup.execute(delete(TelemetryRaw).where(TelemetryRaw.station_id == t["station_id"]))
                cleanup.execute(delete(Device).where(Device.id == t["device_id"]))
                cleanup.execute(delete(PanelGroup).where(PanelGroup.station_id == t["station_id"]))
                cleanup.execute(delete(StationConfigVersion).where(StationConfigVersion.station_id == t["station_id"]))
                cleanup.execute(delete(UserSession).where(UserSession.id == t["session_id"]))
                cleanup.execute(delete(Membership).where(Membership.user_id == t["user_id"]))
                cleanup.execute(delete(Station).where(Station.id == t["station_id"]))
                cleanup.execute(delete(Organization).where(Organization.id == t["org_id"]))
                cleanup.execute(delete(User).where(User.id == t["user_id"]))
            cleanup.commit()


async def test_many_concurrent_connections_execute_loads_in_parallel(committed, monkeypatch):
    """Sanity check usor de sarcina (nu un load-test complet, cerinta
    explicita a issue #50): N conexiuni concurente NU se serializeaza
    reciproc. Fiecare tur de polling isi deschide propria sesiune DB scurta
    prin `SessionLocal()` (`_load_authorized_live_metrics`), deci timpul
    total pentru N conexiuni concurente ar trebui sa ramana apropiat de
    timpul unei singure conexiuni, nu N x cost-per-conexiune (ceea ce ar
    insemna contentie/serializare -- exact tipul de regresie prins de fix-ul
    critic anterior, vezi addendum-ul din LIMITATIONS.md)."""
    monkeypatch.setattr(sse, "POLL_INTERVAL_SECONDS", 0)
    concurrency = 15
    ticks_per_connection = 3
    original_loader = sse._load_authorized_live_metrics
    lock = threading.Lock()
    active_loaders = 0
    max_active_loaders = 0

    def tracked_loader(*args):
        nonlocal active_loaders, max_active_loaders
        with lock:
            active_loaders += 1
            max_active_loaders = max(max_active_loaders, active_loaders)
        try:
            # Largeste controlat fereastra de suprapunere. Assertia de mai
            # jos masoara concurenta direct, fara un prag de timp dependent
            # de viteza runner-ului CI.
            time.sleep(0.03)
            return original_loader(*args)
        finally:
            with lock:
                active_loaders -= 1

    monkeypatch.setattr(sse, "_load_authorized_live_metrics", tracked_loader)

    async def _consume():
        request = _FakeRequest(max_ticks=ticks_per_connection + 2)
        gen = sse.live_metric_events(request, committed["station_id"], committed["user_id"], committed["session_id"])
        events = []
        async for ev in gen:
            events.append(ev)
            if len(events) >= ticks_per_connection:
                break
        await gen.aclose()
        return events

    results = await asyncio.gather(*(_consume() for _ in range(concurrency)))

    assert len(results) == concurrency
    assert all(r[0]["event"] == "snapshot" for r in results)
    assert max_active_loaders > 1  # detecteaza direct o regresie la executie seriala
