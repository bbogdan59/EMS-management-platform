from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.config import get_settings
from app.database import get_db
from app.schemas.device_api import (
    TelemetryBatchRequest,
    TelemetryBatchResult,
    TelemetryContractResponse,
)
from app.services import device_service

router = APIRouter()
settings = get_settings()


TELEMETRY_CONTRACT_V1 = {
    "schema_version": 1,
    "endpoint": "/api/v1/telemetry/batch",
    "deduplication_key": ["device_id", "boot_id", "sequence"],
    "time": {
        "measured_at": "timezone-aware instant; stored as UTC",
        "max_future_skew_seconds": int(device_service.MAX_FUTURE_SKEW.total_seconds()),
        "max_age_days": device_service.MAX_TELEMETRY_AGE.days,
        "late_after_seconds": int(device_service.LATE_TELEMETRY_THRESHOLD.total_seconds()),
    },
    "metrics": [
        {
            "name": "pv_power_w",
            "unit": "W",
            "nullable": True,
            "quality": "measured",
            "description": "PV active power.",
            "sign": "non_negative",
            "min_value": 0,
        },
        {
            "name": "load_power_w",
            "unit": "W",
            "nullable": True,
            "quality": "measured",
            "description": "Load active power.",
            "sign": "non_negative",
            "min_value": 0,
        },
        {
            "name": "battery_power_w",
            "unit": "W",
            "nullable": True,
            "quality": "measured",
            "description": "Battery active power at the battery/DC convention boundary.",
            "sign": "positive_charge_negative_discharge",
        },
        {
            "name": "grid_power_w",
            "unit": "W",
            "nullable": True,
            "quality": "measured",
            "description": "Grid active power.",
            "sign": "positive_import_negative_export",
        },
        {
            "name": "battery_soc_percent",
            "unit": "%",
            "nullable": True,
            "quality": "measured",
            "description": "Battery state of charge; explicit 0 is valid and distinct from null.",
            "sign": "bounded",
            "min_value": 0,
            "max_value": 100,
        },
        {
            "name": "ev_connected",
            "unit": None,
            "nullable": True,
            "quality": "reported",
            "description": "EV connection state.",
        },
        {
            "name": "ev_power_w",
            "unit": "W",
            "nullable": True,
            "quality": "measured",
            "description": "EV charging power.",
            "sign": "non_negative",
            "min_value": 0,
        },
    ],
    "quality_flags": {
        "type": "object",
        "purpose": "diagnostic flags reported by the device; flags do not create numeric values.",
    },
    "provenance": {
        "numeric_values_default": "measured",
        "categories": ["measured", "derived", "simulated", "stale"],
        "simulation_flag": "raw_payload.simulated == true",
        "derived_flag": "quality_flags.derived == true",
        "stale_flag": "server derives stale/late from measured_at vs received_at",
        "rule": (
            "Synthetic, simulated, derived or stale provenance must be declared explicitly; "
            "unsupported raw_payload fields are never promoted to measured telemetry."
        ),
    },
    "raw_payload": {
        "type": "object",
        "purpose": "diagnostic/source payload only; unsupported extended metrics are not treated as canonical telemetry in schema v1.",
    },
    "ack": {
        "ordered": True,
        "statuses": ["accepted", "duplicate", "rejected"],
        "retryable_reason_codes": ["future_timestamp"],
        "permanent_reason_codes": ["timestamp_too_old", *sorted(device_service.TELEMETRY_SEMANTIC_REASONS)],
    },
}


@router.get("/telemetry/contract", response_model=TelemetryContractResponse)
def telemetry_contract():
    """Contract public, versionat, pentru payload-urile de telemetrie acceptate.

    Endpointul este read-only si nu necesita autentificare: dispozitivul si
    simulatorul il pot folosi ca sursa comuna de adevar fara sa trimita un
    secret doar ca sa afle schema.
    """
    return TELEMETRY_CONTRACT_V1


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
