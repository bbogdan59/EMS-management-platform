"""Retentie/arhivare pentru reviziile de pret OPCOM importate (issue #51).

Fiecare incercare de import (`ImportRun`) primeste un numar de revizie nou,
INDIFERENT daca reuseste sau esueaza (vezi `opcom_service.import_opcom_day`)
-- asta inseamna ca `revision` singur NU distinge o "versiune de pret" de o
tentativa esuata/metadata de audit. O revizie de pret e strict un
`ImportRun` cu `status=succeeded` (singurele care au randuri
`MarketPriceInterval` asociate); tentativele esuate sunt ignorate complet
de politica de retentie de mai jos.

Politica: pentru fiecare zi de livrare, pastreaza cel mult
`DEFAULT_MAX_ACTIVE_REVISIONS` revizii REUSITE "active" (neangajate in
arhiva) -- cele mai vechi (dupa revision/fetched_at) devin arhivate.
Arhivarea e STRICT NEDISTRUCTIVA: seteaza `ImportRun.is_archived=True` si
metadate (motiv, timestamp) -- randul si toate `MarketPriceInterval`
asociate raman intacte in baza de date, reproductibile oricand (ex. pentru
un audit financiar sau o factura care s-a bazat pe acea revizie). Revizia
CURENTA (`is_current=True` pe intervalele ei) nu e niciodata arhivata,
indiferent de varsta -- e singura referinta "vie" folosita de restul
platformei (dashboard, optimizare).

Stergerea DEFINITIVA (hard-delete) nu este implementata aici deliberat --
necesita aprobare legal/ops explicita, in afara acestui cod (vezi
docs/LIMITATIONS.md)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.enums import ImportRunStatus
from app.models.market import ImportRun

logger = structlog.get_logger(__name__)

DEFAULT_MAX_ACTIVE_REVISIONS = 5
SOURCE = "opcom_pzu"


@dataclass
class ArchivedRevision:
    import_run_id: str
    delivery_date: str
    revision: int


@dataclass
class RetentionResult:
    dry_run: bool
    max_active_revisions: int
    dates_over_threshold: int
    archived: list[ArchivedRevision] = field(default_factory=list)

    @property
    def archived_count(self) -> int:
        return len(self.archived)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "max_active_revisions": self.max_active_revisions,
            "dates_over_threshold": self.dates_over_threshold,
            "archived_count": self.archived_count,
            "archived": [
                {"import_run_id": a.import_run_id, "delivery_date": a.delivery_date, "revision": a.revision}
                for a in self.archived
            ],
        }


def _lock_retention(db: Session, source: str) -> None:
    """Lock advisory PostgreSQL, scop-tranzactie -- serializeaza doua rulari
    concurente ale retentiei pentru aceeasi sursa (ex. declansare manuala
    din admin in timp ce job-ul planificat ruleaza), acelasi pattern ca
    `organization_service`/`_lock_admin_job_target`."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"market-retention:{source}", 0))))


def enforce_revision_retention(
    db: Session,
    source: str = SOURCE,
    max_active_revisions: int = DEFAULT_MAX_ACTIVE_REVISIONS,
    dry_run: bool = False,
) -> RetentionResult:
    """Idempotent: rulat de doua ori fara nicio schimbare intre timp nu
    arhiveaza nimic suplimentar (a doua rulare gaseste deja <= prag revizii
    NEARHIVATE per zi). Concurrent-safe prin lock-ul advisory de mai sus."""
    if max_active_revisions < 1:
        raise ValueError("max_active_revisions trebuie sa fie cel putin 1.")

    _lock_retention(db, source)

    dates_with_excess = db.scalars(
        select(ImportRun.delivery_date)
        .where(
            ImportRun.source == source,
            ImportRun.status == ImportRunStatus.succeeded.value,
            ImportRun.is_archived.is_(False),
        )
        .group_by(ImportRun.delivery_date)
        .having(func.count(ImportRun.id) > max_active_revisions)
    ).all()

    result = RetentionResult(
        dry_run=dry_run, max_active_revisions=max_active_revisions, dates_over_threshold=len(dates_with_excess)
    )

    for delivery_date in dates_with_excess:
        _archive_excess_for_date(db, source, delivery_date, max_active_revisions, dry_run, result)

    if not dry_run:
        db.flush()

    logger.info(
        "market_retention.enforced",
        source=source,
        dry_run=dry_run,
        max_active_revisions=max_active_revisions,
        dates_over_threshold=result.dates_over_threshold,
        archived_count=result.archived_count,
    )
    return result


def _archive_excess_for_date(
    db: Session,
    source: str,
    delivery_date: date,
    max_active_revisions: int,
    dry_run: bool,
    result: RetentionResult,
) -> None:
    runs = db.scalars(
        select(ImportRun)
        .where(
            ImportRun.source == source,
            ImportRun.delivery_date == delivery_date,
            ImportRun.status == ImportRunStatus.succeeded.value,
            ImportRun.is_archived.is_(False),
        )
        .order_by(ImportRun.revision.desc(), ImportRun.fetched_at.desc())
    ).all()

    # Primele `max_active_revisions` (cele mai recente) raman active; restul
    # sunt candidate la arhivare. Revizia curenta e mereu in acest cap (e
    # mereu cea mai recenta reusita) -- verificarea explicita de mai jos e o
    # plasa de siguranta suplimentara, nu singura protectie.
    to_archive = runs[max_active_revisions:]
    now = utcnow()
    for run in to_archive:
        is_current_revision = any(interval.is_current for interval in run.intervals)
        if is_current_revision:
            continue
        result.archived.append(
            ArchivedRevision(import_run_id=str(run.id), delivery_date=run.delivery_date.isoformat(), revision=run.revision)
        )
        if not dry_run:
            run.is_archived = True
            run.archived_at = now
            run.archived_reason = (
                f"retentie: peste pragul de {max_active_revisions} revizii active/zi "
                f"({len(runs)} revizii reusite gasite)"
            )
            db.add(run)
