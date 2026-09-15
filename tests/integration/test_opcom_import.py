from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.enums import ImportRunStatus
from app.models.market import MarketPriceInterval
from app.services import opcom_service
from app.services.opcom_fixtures import generate_synthetic_csv
from app.services.opcom_service import (
    OpcomFetchError,
    get_real_imported_dates,
    has_successful_real_import,
    import_opcom_day,
)
from tests.factories import make_market_day


def test_import_unpublished_for_far_future_date(db):
    far_future = date.today() + timedelta(days=5)
    run = import_opcom_day(db, far_future)
    assert run.status == ImportRunStatus.unpublished.value
    assert run.interval_count is None


def test_import_falls_back_to_synthetic_when_source_unreachable(db, monkeypatch):
    # Sursa reala trebuie sa fie explicit indisponibila pentru acest test, nu doar
    # "de obicei" indisponibila in mediul de rulare -- opcom.ro poate fi accesibil
    # efectiv din unele medii CI (spre deosebire de acest sandbox), caz in care un
    # import REAL ar reusi si ar face `is_synthetic_fixture` fals, netestand deloc
    # fallback-ul urmarit aici. Simulam explicit indisponibilitatea.
    def _always_unreachable(url):
        raise OpcomFetchError("simulat indisponibil pentru test")

    monkeypatch.setattr(opcom_service, "_fetch_raw", _always_unreachable)

    d = date.today()
    run = import_opcom_day(db, d)
    assert run.status == ImportRunStatus.succeeded.value
    assert run.is_synthetic_fixture is True
    assert run.interval_count in (92, 96, 100)


def test_synthetic_fallback_does_not_replace_existing_real_current_import(db, monkeypatch):
    def _always_unreachable(url):
        raise OpcomFetchError("simulat indisponibil pentru test")

    monkeypatch.setattr(opcom_service, "_fetch_raw", _always_unreachable)

    d = date.today()
    real_run = make_market_day(db, d, [321.0], is_synthetic=False)
    fallback_run = import_opcom_day(db, d)

    assert fallback_run.status == ImportRunStatus.failed.value
    assert fallback_run.is_synthetic_fixture is True
    assert fallback_run.interval_count is None
    assert "NU a fost importat" in (fallback_run.error_message or "")

    current_intervals = db.scalars(
        select(MarketPriceInterval).where(
            MarketPriceInterval.delivery_date == d,
            MarketPriceInterval.is_current.is_(True),
        )
    ).all()
    assert {interval.revision for interval in current_intervals} == {real_run.revision}
    assert {interval.price_lei_per_mwh for interval in current_intervals} == {
        real_run.intervals[0].price_lei_per_mwh
    }


def test_import_same_content_keeps_current_revision_without_duplicate_intervals(db, monkeypatch):
    d = date(2026, 9, 9)
    csv_text = generate_synthetic_csv(d)

    def _same_content(url):
        return csv_text.encode("utf-8")

    monkeypatch.setattr(opcom_service, "_fetch_raw", _same_content)

    run1 = import_opcom_day(db, d)
    run2 = import_opcom_day(db, d)
    assert run1.status == ImportRunStatus.succeeded.value
    assert run2.status == ImportRunStatus.unchanged.value
    assert run2.interval_count == run1.interval_count

    current_revision = db.scalar(
        select(MarketPriceInterval.revision).where(
            MarketPriceInterval.delivery_date == d, MarketPriceInterval.is_current.is_(True)
        )
    )
    assert current_revision == run1.revision
    revision2_interval = db.scalar(
        select(MarketPriceInterval.id).where(
            MarketPriceInterval.delivery_date == d, MarketPriceInterval.revision == run2.revision
        )
    )
    assert revision2_interval is None


def test_has_successful_real_import_ignores_synthetic(db):
    d = date(2025, 5, 1)
    make_market_day(db, d, [100.0], is_synthetic=True)
    assert has_successful_real_import(db, d) is False  # e sintetic, nu conteaza ca import "real"

    make_market_day(db, d, [100.0], is_synthetic=False, revision=2)
    assert has_successful_real_import(db, d) is True


def test_get_real_imported_dates_used_by_backfill_skip_logic(db):
    d1, d2, d3 = date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3)
    make_market_day(db, d1, [100.0], is_synthetic=False)
    make_market_day(db, d2, [100.0], is_synthetic=True)  # nu trebuie sa apara -- e sintetic

    done = get_real_imported_dates(db, d1, d3)
    assert done == {d1}
