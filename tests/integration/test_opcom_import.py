from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import ImportRunStatus
from app.services.opcom_service import import_opcom_day


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
