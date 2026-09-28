import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.audit import AuditLog
from app.models.organization import Organization
from app.models.station import Station
from app.models.tariff import Tariff, TariffVersion
from app.models.user import User
from app.services import romanian_tariff_service as service
from app.services import tariff_service
from tests.factories import make_membership, make_org, make_station, make_user
from tests.integration.test_tariffs_routes import _setup
from tests.unit.test_romanian_tariff import config
from tests.web_helpers import get_csrf, login


def submit(client, station, **overrides):
    page = client.get(f"/stations/{station.id}/tariffs")
    token = re.search(r'name="expected_revision" value="([a-f0-9]+)"', page.text).group(1)
    data = {
        **config().model_dump(mode="json"),
        "csrf_token": get_csrf(client),
        "expected_revision": token,
        **overrides,
    }
    return client.post(f"/stations/{station.id}/tariffs/romania", data=data, follow_redirects=False)


def test_setup_saves_linked_prices_exactly_and_preserves_history(client, db):
    org, admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    first = "2026-08-01T00:00"
    assert submit(client, station, effective_from=first).status_code == 303
    versions = service.latest_versions(db, station)
    buy, sell = versions["import"], versions["export"]
    db.refresh(buy)
    assert buy.invoice_breakdown == sell.invoice_breakdown
    assert buy.invoice_breakdown["green_certificates"] == "0.07401920"
    assert buy.other_regulated_lei_per_kwh == Decimal("0.11104320")
    assert buy.valid_from == sell.valid_from == datetime(2026, 7, 31, 21, tzinfo=UTC)
    assert tariff_service.compute_effective_price_lei_per_kwh(buy, None) == Decimal("1.1573204720")
    assert tariff_service.compute_effective_price_lei_per_kwh(sell, None) == Decimal("0.446370")
    assert (
        submit(
            client,
            station,
            effective_from="2026-08-20T00:00",
            active_energy="0,50",
            tg_in_active="true",
        ).status_code
        == 303
    )
    db.refresh(buy)
    assert buy.valid_to == datetime(2026, 8, 19, 21, tzinfo=UTC)
    assert buy.fixed_price_lei_per_kwh == Decimal("0.45")
    assert (
        tariff_service.get_current_tariff_version(
            db, station.id, "import", buy.valid_to - timedelta(microseconds=1)
        ).id
        == buy.id
    )
    current = tariff_service.get_current_tariff_version(db, station.id, "import", buy.valid_to)
    assert current.id != buy.id and current.fixed_price_lei_per_kwh == Decimal("0.50")
    assert current.transport_lei_per_kwh == Decimal("0.03645")
    assert (
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "tariff_pair_created", AuditLog.station_id == station.id
            )
        )
        == 2
    )
    page = client.get(f"/stations/{station.id}/tariffs")
    assert 'value="0.50"' in page.text and "0.07401920 lei/kWh" in page.text


def test_setup_rejects_stale_revision_and_never_saves_half_a_pair(client, db):
    org, admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    token = service.revision(service.latest_versions(db, station))
    assert submit(client, station).status_code == 303
    response = submit(client, station, expected_revision=token, active_energy="0.52")
    assert response.status_code == 422 and "modificate intre timp" in response.text
    assert 'value="0.52"' in response.text
    assert (
        db.scalar(
            select(func.count(TariffVersion.id)).join(Tariff).where(Tariff.station_id == station.id)
        )
        == 2
    )


def test_incompatible_export_contract_rolls_back_new_import(client, db):
    org, admin, viewer, station = _setup(db)
    tariff = tariff_service.get_or_create_tariff(
        db, station, "export", "indexed_opcom", "Existing export"
    )
    tariff_service.add_tariff_version(
        db,
        tariff,
        valid_from=utcnow() - timedelta(days=1),
        fixed_price_lei_per_kwh=None,
        opcom_margin_lei_per_kwh=Decimal(0),
        fixed_monthly_fee_lei=Decimal(0),
        variable_component_lei_per_kwh=Decimal(0),
        settlement_method="net_metering_15min",
        settlement_interval_days=30,
    )
    db.commit()
    login(client, admin.email, "Password1234")
    assert submit(client, station).status_code == 422
    assert db.scalar(select(func.count(Tariff.id)).where(Tariff.station_id == station.id)) == 1
    db.refresh(tariff)
    assert tariff.kind == "indexed_opcom" and tariff.name == "Existing export"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cfd", ""),
        ("tg", "NaN"),
        ("import_vat", "101"),
        ("green_certificates", "0.123456789"),
        ("active_energy", "0.001"),
    ],
)
def test_invalid_setup_preserves_user_values_without_writes(client, db, field, value):
    org, admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    response = submit(client, station, **{field: value})
    assert response.status_code == 422
    assert 'value="0.07401920"' in response.text or field == "green_certificates"
    assert db.scalar(select(func.count(Tariff.id)).where(Tariff.station_id == station.id)) == 0


def test_setup_and_preview_scope_csrf_and_missing_energy(client, db):
    org, admin, viewer, station = _setup(db)
    login(client, viewer.email, "Password1234")
    endpoint = f"/stations/{station.id}/tariffs/romania"
    payload = {"tariff": config().model_dump(mode="json"), "import_kwh": None, "export_kwh": "0"}
    assert client.post(endpoint, data={"csrf_token": get_csrf(client)}).status_code == 403
    assert client.post(endpoint + "/preview", json=payload).status_code == 403
    headers = {"X-CSRF-Token": get_csrf(client)}
    response = client.post(endpoint + "/preview", headers=headers, json=payload)
    assert response.status_code == 200 and not response.json()["available"]
    assert response.json()["rates"]["export_net"] == "0.446370"
    assert response.headers["cache-control"] == "no-store"
    assert db.scalar(select(func.count(Tariff.id)).where(Tariff.station_id == station.id)) == 0
    foreign = make_station(db, make_org(db, "Other tariff tenant"), admin)
    db.commit()
    assert (
        client.post(
            f"/stations/{foreign.id}/tariffs/romania/preview", headers=headers, json=payload
        ).status_code
        == 403
    )


def test_wizard_saves_both_directions_and_advances(client, db):
    org, admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    page = client.get(f"/stations/{station.id}/tariffs?wizard=1")
    assert re.search(r'id="ro-tariff-form"[\s\S]*?name="wizard" value="1"', page.text)
    response = submit(client, station, wizard="1")
    assert response.status_code == 303 and "/preferences?wizard=1" in response.headers["location"]


def test_precision_and_breakdown_survive_downgrade_with_mixed_nulls(db, monkeypatch):
    org, admin, viewer, station = _setup(db)
    service.save_pair(
        db, station, config(), utcnow(), service.revision(service.latest_versions(db, station))
    )
    legacy = Tariff(
        station_id=station.id, direction="import", kind="indexed_opcom", name="Old", is_active=False
    )
    db.add(legacy)
    db.flush()
    old = TariffVersion(
        tariff_id=legacy.id,
        valid_from=utcnow(),
        fixed_price_lei_per_kwh=None,
        opcom_margin_lei_per_kwh=Decimal("0.01234567"),
        vat_rate_percent=None,
        fixed_monthly_fee_lei=0,
        variable_component_lei_per_kwh=0,
    )
    db.add(old)
    db.flush()
    ids = db.scalars(
        select(TariffVersion.id).join(Tariff).where(Tariff.station_id == station.id)
    ).all()
    query = text(
        "SELECT id, fixed_price_lei_per_kwh, opcom_margin_lei_per_kwh, other_regulated_lei_per_kwh, vat_rate_percent FROM tariff_versions WHERE id = ANY(:ids) ORDER BY id"
    )
    before = db.execute(query, {"ids": ids}).all()
    spec = spec_from_file_location(
        "invoice_migration",
        Path(__file__).parents[2] / "alembic/versions/e62a19d04b73_invoice_tariff_setup.py",
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db.connection())))
    migration.downgrade()
    assert db.execute(query, {"ids": ids}).all() == before
    archived = (
        db.execute(
            text("SELECT invoice_breakdown FROM legacy_tariff_breakdowns WHERE id=ANY(:ids)"),
            {"ids": ids},
        )
        .scalars()
        .all()
    )
    assert None in archived and any(v and v["green_certificates"] == "0.07401920" for v in archived)
    migration.upgrade()
    assert db.execute(query, {"ids": ids}).all() == before
    assert (
        db.scalar(select(TariffVersion.invoice_breakdown).where(TariffVersion.id == ids[0]))
        in archived
    )


def test_concurrent_setups_only_one_pair_can_commit(engine):
    suffix = uuid4().hex[:10]
    with Session(engine) as db:
        user = make_user(db, email=f"tariff-pair-{suffix}@test.local")
        org = make_org(db, f"Tariff pair {suffix}")
        make_membership(db, user, org, "organization_admin")
        station = make_station(db, org, user)
        db.commit()
        user_id, org_id, station_id = user.id, org.id, station.id
        token = service.revision(service.latest_versions(db, station))
    locked, release = Event(), Event()

    def save(hold=False):
        with Session(engine) as db:
            try:
                service.save_pair(db, db.get(Station, station_id), config(), utcnow(), token)
                if hold:
                    locked.set()
                    assert release.wait(10)
                db.commit()
                return "saved"
            except ValueError:
                db.rollback()
                return "conflict"

    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(save, True)
            assert locked.wait(10)
            second = pool.submit(save)
            release.set()
            assert [first.result(10), second.result(10)] == ["saved", "conflict"]
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count(TariffVersion.id))
                    .join(Tariff)
                    .where(Tariff.station_id == station_id)
                )
                == 2
            )
    finally:
        release.set()
        with Session(engine) as db:
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
