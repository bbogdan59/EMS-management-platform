from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import ImportRunStatus
from app.services.opcom_service import (
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


def test_import_falls_back_to_synthetic_when_source_unreachable(db):
    # In mediul de test, opcom.ro nu e accesibil -> fallback sintetic, marcat explicit.
    d = date.today()
    run = import_opcom_day(db, d)
    assert run.status == ImportRunStatus.succeeded.value
    assert run.is_synthetic_fixture is True
    assert run.interval_count in (92, 96, 100)


def test_import_idempotent_revisions(db):
    d = date.today()
    run1 = import_opcom_day(db, d)
    run2 = import_opcom_day(db, d)
    assert run2.revision == run1.revision + 1

    from sqlalchemy import select

    from app.models.market import MarketPriceInterval

    current_count = db.scalar(
        select(MarketPriceInterval).where(
            MarketPriceInterval.delivery_date == d, MarketPriceInterval.is_current.is_(True)
        )
    )
    assert current_count is not None
    old_revision_still_present = db.scalar(
        select(MarketPriceInterval).where(
            MarketPriceInterval.delivery_date == d, MarketPriceInterval.revision == run1.revision
        )
    )
    assert old_revision_still_present is not None  # revizia veche ramane in baza (audit)


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
