from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models.enums import ImportRunStatus
from app.models.market import ImportRun, MarketPriceInterval
from app.services import market_retention_service as svc
from tests.factories import make_market_day


def _mark_not_current(db, delivery_date, revision):
    for row in db.scalars(
        select(MarketPriceInterval).where(
            MarketPriceInterval.delivery_date == delivery_date, MarketPriceInterval.revision == revision
        )
    ):
        row.is_current = False
        db.add(row)


def _make_revisions(db, delivery_date: date, count: int) -> list[ImportRun]:
    """Creeaza `count` revizii REUSITE succesive pentru aceeasi zi -- doar
    ultima ramane `is_current=True` (comportamentul real de import,
    replicat manual aici la fel ca in test_market_analytics.py)."""
    runs = []
    for revision in range(1, count + 1):
        if runs:
            _mark_not_current(db, delivery_date, revision - 1)
        run = make_market_day(db, delivery_date, [100.0 + revision], revision=revision)
        runs.append(run)
    return runs


def _make_failed_attempt(db, delivery_date: date, revision: int) -> ImportRun:
    run = ImportRun(
        source="opcom_pzu", delivery_date=delivery_date, revision=revision,
        status=ImportRunStatus.failed.value, source_url="https://test.local",
        error_message="timeout",
    )
    db.add(run)
    db.flush()
    return run


def test_archives_excess_revisions_beyond_threshold(db):
    day = date(2026, 1, 10)
    runs = _make_revisions(db, day, 6)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()

    assert result.dates_over_threshold == 1
    assert result.archived_count == 1
    assert result.archived[0].revision == 1  # cea mai veche

    db.refresh(runs[0])
    assert runs[0].is_archived is True
    assert runs[0].archived_at is not None
    assert runs[0].archived_reason

    for run in runs[1:]:
        db.refresh(run)
        assert run.is_archived is False


def test_never_archives_more_than_needed(db):
    day = date(2026, 1, 11)
    _make_revisions(db, day, 8)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()

    assert result.archived_count == 3  # revizii 1,2,3 arhivate; 4,5,6,7,8 raman active
    archived_revisions = {a.revision for a in result.archived}
    assert archived_revisions == {1, 2, 3}


def test_never_archives_the_current_revision_even_if_old(db):
    """Plasa de siguranta explicita: chiar daca printr-un bug revizia curenta
    ar ajunge printre cele mai vechi (nu ar trebui sa se intample in flow-ul
    normal), retentia nu o arhiveaza niciodata."""
    day = date(2026, 1, 12)
    runs = _make_revisions(db, day, 6)
    # Forteaza artificial revizia 1 (cea mai veche) sa ramana "curenta"
    # simultan cu revizia 6 -- scenariu defensiv, nu unul normal.
    for row in db.scalars(select(MarketPriceInterval).where(MarketPriceInterval.revision == 1, MarketPriceInterval.delivery_date == day)):
        row.is_current = True
        db.add(row)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()

    archived_revisions = {a.revision for a in result.archived}
    assert 1 not in archived_revisions

    db.refresh(runs[0])
    assert runs[0].is_archived is False


def test_ignores_failed_attempts_entirely(db):
    """Tentativele esuate nu sunt confundate cu versiuni de pret -- nu conteaza
    la prag si nu sunt niciodata arhivate de aceasta politica."""
    day = date(2026, 1, 13)
    _make_revisions(db, day, 3)
    failed = _make_failed_attempt(db, day, revision=100)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()

    assert result.archived_count == 0
    db.refresh(failed)
    assert failed.is_archived is False


def test_stays_under_threshold_archives_nothing(db):
    day = date(2026, 1, 14)
    _make_revisions(db, day, 4)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5)

    assert result.dates_over_threshold == 0
    assert result.archived_count == 0


def test_dry_run_reports_without_mutating(db):
    day = date(2026, 1, 15)
    runs = _make_revisions(db, day, 7)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5, dry_run=True)

    assert result.dry_run is True
    assert result.archived_count == 2
    for run in runs:
        db.refresh(run)
        assert run.is_archived is False  # nimic scris in dry-run


def test_idempotent_second_run_archives_nothing_more(db):
    day = date(2026, 1, 16)
    _make_revisions(db, day, 7)
    db.commit()

    first = svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()
    assert first.archived_count == 2

    second = svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()
    assert second.archived_count == 0
    assert second.dates_over_threshold == 0


def test_never_hard_deletes_archived_rows_or_their_intervals(db):
    """Arhivarea e strict nedistructiva -- randul ImportRun si toate
    intervalele lui de pret raman interogabile dupa arhivare."""
    day = date(2026, 1, 17)
    runs = _make_revisions(db, day, 6)
    db.commit()

    svc.enforce_revision_retention(db, max_active_revisions=5)
    db.commit()

    archived_run_id = runs[0].id
    still_there = db.get(ImportRun, archived_run_id)
    assert still_there is not None
    intervals = db.scalars(
        select(MarketPriceInterval).where(MarketPriceInterval.import_run_id == archived_run_id)
    ).all()
    assert len(intervals) == 1  # intervalul din make_market_day([...]) ramane


def test_rejects_invalid_max_active_revisions(db):
    import pytest

    with pytest.raises(ValueError):
        svc.enforce_revision_retention(db, max_active_revisions=0)


def test_separate_delivery_dates_and_sources_are_independent(db):
    day1 = date(2026, 1, 18)
    day2 = date(2026, 1, 19)
    _make_revisions(db, day1, 6)
    _make_revisions(db, day2, 3)
    db.commit()

    result = svc.enforce_revision_retention(db, max_active_revisions=5)

    assert result.dates_over_threshold == 1
    assert all(a.delivery_date == day1.isoformat() for a in result.archived)
