from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.config import get_settings
from app.database import get_db
from app.schemas.device_api import TelemetryBatchRequest, TelemetryBatchResult
from app.services import device_service

router = APIRouter()
settings = get_settings()


@router.post("/telemetry/batch", response_model=TelemetryBatchResult)
def ingest_telemetry(
    payload: TelemetryBatchRequest,
    device=Depends(get_authenticated_device),
    db: Session = Depends(get_db),
):
    """Ingestie batch de telemetrie. Deduplicare pe (device_id, boot_id, sequence):
    reincercarile dispozitivului dupa o eroare de retea sunt idempotente."""
    if len(payload.items) > settings.device_telemetry_batch_max_items:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Batch prea mare (max {settings.device_telemetry_batch_max_items} elemente).",
        )

    accepted, duplicates, rejected, errors, results = device_service.ingest_telemetry_batch(db, device, payload.items)
    db.commit()
    return TelemetryBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=errors,
        results=results,
    )
