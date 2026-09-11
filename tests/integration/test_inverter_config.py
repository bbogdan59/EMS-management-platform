import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.models.command import Command
from app.models.inverter_config import InverterDesired, InverterReport
from app.schemas.inverter_config import DesiredRequest, Profile, ReportRequest
from app.services import device_service
from app.services import inverter_config_service as service
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.web_helpers import login


@pytest.fixture()
def context(db):
    user = make_user(db, is_platform_admin=True)
    station = make_station(db, make_org(db), user)
    device = make_device(db, station)
    # Test-only map, never shipped as an approved DEYE model.
    profile = service.approved_profile(db, Profile.model_validate({
        'model': 'TEST ONLY', 'firmware': ['test-1'], 'source': 'unit fixture', 'protocol_revision': 'test',
        'registers': {'reserve_soc': {'address': 100, 'encoding': 'u16', 'function': 3, 'scale': '1',
                                    'unit': '%', 'writable': True, 'minimum': '20', 'maximum': '100'}},
    }), user)
    return user, station, device, profile


def desired(profile, version=0, **changes):
    data = {'request_id': str(uuid.uuid4()), 'expected_version': version, 'profile_id': str(profile.id),
            'connection': {'port': '/dev/serial/by-id/test', 'device_id': 1, 'baudrate': 9600,
                           'parity': 'N', 'stopbits': 1, 'sample_seconds': 10}, 'reason': 'test', 'settings': {}}
    data.update(changes)
    return DesiredRequest.model_validate(data)


def observation(profile, version=1, base=0, **changes):
    data = {'request_id': str(uuid.uuid4()), 'desired_version': version, 'base_version': base,
            'profile_hash': profile.digest, 'model': 'TEST ONLY', 'firmware': 'test-1',
            'measured_at': utcnow(), 'settings': {'reserve_soc': '30'}}
    data.update(changes)
    return ReportRequest.model_validate(data)


def setup_config(db, context):
    user, station, device, profile = context
    service.save_desired(db, station, device.id, desired(profile), user)
    return service.report_snapshot(db, device.id, observation(profile))


def test_snapshot_delta_retry_and_reconciliation(db, context):
    user, station, device, profile = context
    request = desired(profile)
    first = service.save_desired(db, station, device.id, request, user)
    assert service.save_desired(db, station, device.id, request, user).id == first.id
    initial = observation(profile)
    report = service.report_snapshot(db, device.id, initial)
    assert service.report_snapshot(db, device.id, initial).id == report.id
    row = service.save_desired(db, station, device.id, desired(profile, 1, settings={'reserve_soc': '50'}), user)
    assert row.command_id is None  # read-only/shadow does not receive physical commands
    service.report_snapshot(db, device.id, observation(profile, 2, 1, kind='delta', settings={'reserve_soc': '50'}))
    assert service.reconciliation(db, device.id)[2] == 'applied'
    service.report_snapshot(db, device.id, observation(profile, 2, 2, kind='delta', settings={'reserve_soc': None}))
    assert service.reconciliation(db, device.id)[2] == 'drift'
    assert service.latest(db, InverterReport, device.id).snapshot['reserve_soc'] is None


def test_stale_versions_and_idempotency_conflicts(db, context):
    user, station, device, profile = context
    setup_config(db, context)
    with pytest.raises(service.ConfigConflict):
        service.save_desired(db, station, device.id, desired(profile, 0), user)
    with pytest.raises(service.ConfigConflict):
        service.report_snapshot(db, device.id, observation(profile, base=0))
    request = observation(profile, base=1)
    service.report_snapshot(db, device.id, request)
    with pytest.raises(service.ConfigConflict):
        service.report_snapshot(db, device.id, request.model_copy(update={'settings': {'reserve_soc': Decimal(60)}}))


@pytest.mark.parametrize('changes', [
    {'firmware': 'other'}, {'model': 'other'}, {'profile_hash': '0'*64},
    {'measured_at': utcnow()+timedelta(days=1)}, {'settings': {'unknown': '50'}},
])
def test_bad_report_rejected(db, context, changes):
    user, station, device, profile = context
    service.save_desired(db, station, device.id, desired(profile), user)
    with pytest.raises(service.ConfigConflict):
        service.report_snapshot(db, device.id, observation(profile, **changes))


def test_limits_and_stale_snapshot(db, context):
    user, station, device, profile = context
    setup_config(db, context)
    with pytest.raises(service.ConfigConflict):
        service.save_desired(db, station, device.id, desired(profile, 1, settings={'reserve_soc': '19'}), user)
    report = service.latest(db, InverterReport, device.id)
    report.measured_at = utcnow()-timedelta(hours=1)
    db.flush()
    with pytest.raises(service.ConfigConflict):
        service.save_desired(db, station, device.id, desired(profile, 1, settings={'reserve_soc': '50'}), user)


def test_command_requires_readback_and_expires(db, context):
    user, station, device, profile = context
    setup_config(db, context)
    station.execution_mode = 'live'
    device.capabilities = {'inverter_write': True, 'inverter_settings_v1': True}
    device.last_heartbeat_at = utcnow()
    db.flush()
    row = service.save_desired(db, station, device.id, desired(profile, 1, settings={'reserve_soc': '50'}), user)
    command = db.get(Command, row.command_id)
    assert service.delivery_allowed(db, command, device, utcnow())
    assert not service.delivery_allowed(db, command, device, command.expires_at+timedelta(seconds=1))
    device_service.acknowledge_command(db, device, command.id, 'accepted', None)
    with pytest.raises(device_service.DeviceServiceError):
        device_service.report_command_result(db, device, command.id, 'executed', {}, None)
    report = service.report_snapshot(db, device.id, observation(profile, 2, 1, settings={'reserve_soc': '50'}))
    result = device_service.report_command_result(db, device, command.id, 'executed', {'report_id': str(report.id)}, None)
    assert result.status == 'executed'


def test_new_configuration_supersedes_command(db, context):
    user, station, device, profile = context
    setup_config(db, context)
    station.execution_mode = 'live'
    device.capabilities = {'inverter_write': True, 'inverter_settings_v1': True}
    device.last_heartbeat_at = utcnow()
    db.flush()
    old = service.save_desired(db, station, device.id, desired(profile, 1, settings={'reserve_soc': '50'}), user)
    service.save_desired(db, station, device.id, desired(profile, 2, settings={'reserve_soc': '60'}), user)
    assert db.get(Command, old.command_id).status == 'superseded'


def test_web_scope_rbac_csrf_and_render(client, db, context):
    reset_key('login_attempts:testclient')
    admin, station, device, profile = context
    viewer = make_user(db, email='viewer-config@test.local')
    make_membership(db, viewer, station.organization, role='viewer')
    other = make_station(db, make_org(db, 'other'), admin)
    foreign_device = make_device(db, other)
    db.commit()
    login(client, viewer.email, 'TestPass1234')
    url = f'/stations/{station.id}/devices/{device.id}/configuration'
    assert client.get(url).status_code == 200
    assert client.get(f'/stations/{station.id}/devices/{foreign_device.id}/configuration').status_code == 404
    headers = {'X-CSRF-Token': client.cookies.get('ems_csrf')}
    assert client.post(url, json=desired(profile).model_dump(mode='json'), headers=headers).status_code == 403
    login(client, admin.email, 'TestPass1234')
    assert client.post(url, json=desired(profile).model_dump(mode='json')).status_code == 403
    assert client.post(url, json=desired(profile).model_dump(mode='json'),
                       headers={'X-CSRF-Token': client.cookies.get('ems_csrf')}).status_code == 200
    assert db.scalar(select(InverterDesired).where(InverterDesired.device_id == device.id)) is not None


def test_device_http_snapshot_is_idempotent(client, db, context):
    from app.core.security import hash_password
    from app.models.device import DeviceCredential

    user, station, device, profile = context
    service.save_desired(db, station, device.id, desired(profile), user)
    db.add(DeviceCredential(device_id=device.id, secret_hash=hash_password('test-device-secret')))
    db.commit()
    headers = {'Authorization': f'Bearer {device.id}.test-device-secret'}
    assert client.get('/api/v1/inverter/config').status_code == 401
    response = client.get('/api/v1/inverter/config', headers=headers)
    assert response.status_code == 200
    assert response.json()['desired']['profile_hash'] == profile.digest
    payload = observation(profile).model_dump(mode='json')
    first = client.post('/api/v1/inverter/reported', json=payload, headers=headers)
    second = client.post('/api/v1/inverter/reported', json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert 'report_id' in first.json()


def test_concurrent_desired_edits_have_one_winner(engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete, text
    from sqlalchemy.orm import Session

    from app.models.audit import AuditLog
    from app.models.device import Device
    from app.models.inverter_config import InverterProfile
    from app.models.organization import Organization
    from app.models.station import Station
    from app.models.user import User

    suffix = uuid.uuid4().hex
    with Session(engine) as setup:
        user = make_user(setup, email=f'{suffix}@concurrency.test')
        org = make_org(setup, suffix)
        station = Station(organization_id=org.id, name='concurrent config', timezone='Europe/Bucharest')
        setup.add(station)
        setup.flush()
        device = make_device(setup, station)
        profile = service.approved_profile(setup, Profile.model_validate({
            'model': suffix, 'firmware': ['test'], 'source': 'concurrency fixture', 'protocol_revision': 'test',
            'registers': {'value': {'address': 1, 'encoding': 'u16', 'function': 3, 'scale': 1,
                                  'unit': 'test', 'minimum': 0, 'maximum': 10}},
        }), user)
        ids = user.id, org.id, station.id, device.id, profile.id
        setup.commit()
    barrier = Barrier(2)

    def edit():
        with Session(engine) as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            current_user = session.get(User, ids[0])
            current_station = session.get(Station, ids[2])
            current_profile = session.get(InverterProfile, ids[4])
            payload = desired(current_profile)
            barrier.wait(timeout=5)
            try:
                service.save_desired(session, current_station, ids[3], payload, current_user)
                session.commit()
                return 'saved'
            except service.ConfigConflict:
                session.rollback()
                return 'conflict'

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(edit) for _ in range(2)]
            assert sorted(f.result(timeout=10) for f in futures) == ['conflict', 'saved']
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(AuditLog).where(AuditLog.actor_user_id == ids[0]))
            cleanup.execute(delete(InverterDesired).where(InverterDesired.device_id == ids[3]))
            for model, identifier in [(Device, ids[3]), (Station, ids[2]), (InverterProfile, ids[4]), (User, ids[0]), (Organization, ids[1])]:
                cleanup.execute(delete(model).where(model.id == identifier))
            cleanup.commit()
