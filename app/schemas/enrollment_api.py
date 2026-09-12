"""Scheme Pydantic pentru enrollment-ul automat al dispozitivelor (issue
#16), distinct de fluxul clasic cu cod de asociere (`ClaimRequest`/
`ClaimResponse` din `app/schemas/device_api.py`, neatins de acest fisier).
Documentat integral, cu exemple, in docs/API.md."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class EnrollRequest(BaseModel):
    """Cerere idempotenta: se poate retrimite oricand cu aceeasi identitate
    (acelasi `installation_uuid`+`provisioning_secret`) fara efecte adverse
    -- exact contractul cerut pentru recuperare dupa un raspuns pierdut."""

    installation_uuid: str = Field(
        ..., min_length=8, max_length=64,
        description="UUID persistent generat de dispozitiv la prima pornire. Doar corelare -- NU autorizeaza nicio statie/tenant.",
    )
    provisioning_secret: str = Field(
        ..., min_length=16, max_length=128,
        description="Secret generat si pastrat LOCAL de dispozitiv (nu de server). Dovada de posesie la reincercari.",
    )
    serial_number: str | None = Field(
        default=None, min_length=8, max_length=64,
        description="Serial public de inventar, imprimat pe eticheta unitatii; nu este secret.",
    )
    activation_code: str | None = Field(
        default=None, min_length=20, max_length=64,
        description="Device Code cu entropie mare, livrat sigilat clientului si consumat o singura data.",
    )
    hardware_info: dict = Field(default_factory=dict, description="Informatii libere despre hardware (model, serie, IMEI daca exista).")

    @field_validator("installation_uuid")
    @classmethod
    def _valid_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("installation_uuid trebuie sa fie un UUID valid.") from exc
        return v

    @field_validator("serial_number", "activation_code")
    @classmethod
    def _normalize_code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value is not None else None

    @model_validator(mode="after")
    def _activation_pair(self):
        if (self.serial_number is None) != (self.activation_code is None):
            raise ValueError("serial_number si activation_code trebuie trimise impreuna.")
        return self


class EnrollResponse(BaseModel):
    status: str = Field(..., description="pending | assigned | revoked")
    device_id: uuid.UUID | None = None
    station_id: uuid.UUID | None = None
    credential_secret: str | None = Field(
        default=None,
        description=(
            "Prezent DOAR cand status=assigned si dispozitivul nu a folosit inca noua "
            "credentiala (vezi POST /devices/heartbeat) -- recuperabil idempotent prin "
            "reapelarea /devices/enroll cu aceeasi identitate, pana la prima utilizare reusita."
        ),
    )
    enrollment_expires_at: datetime | None = None
    message: str | None = None
