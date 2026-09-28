from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

COMPONENTS = (
    ("active_energy", "Energie activa"),
    ("distribution", "Distributie"),
    ("system_services", "Servicii de sistem"),
    ("tg", "Introducere in retea (TG)"),
    ("tl", "Extragere din retea (TL)"),
    ("cogeneration", "Contributie cogenerare"),
    ("cfd", "Contributie CfD"),
    ("green_certificates", "Certificate verzi"),
    ("excise", "Acciza"),
)

# A user-supplied invoice example, never a nationwide schedule of regulated rates.
INVOICE_EXAMPLE = {
    "active_energy": "0.450000",
    "distribution": "0.355340",
    "system_services": "0.014700",
    "tg": "0.003630",
    "tl": "0.036450",
    "cogeneration": "0.014500",
    "cfd": "0.000144",
    "green_certificates": "0.07401920",
    "excise": "0.007680",
    "tg_in_active": False,
}


def decimal_text(value):
    if isinstance(value, str):
        return value.strip().replace(",", ".")
    return value


class RomanianTariffInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    name: str = Field(default="Prosumator Romania", min_length=1, max_length=200)
    active_energy: Decimal = Field(ge=0, le=9999, decimal_places=8)
    distribution: Decimal = Field(ge=0, le=9999, decimal_places=8)
    system_services: Decimal = Field(ge=0, le=9999, decimal_places=8)
    tg: Decimal = Field(ge=0, le=9999, decimal_places=8)
    tl: Decimal = Field(ge=0, le=9999, decimal_places=8)
    cogeneration: Decimal = Field(ge=0, le=9999, decimal_places=8)
    cfd: Decimal = Field(ge=0, le=9999, decimal_places=8)
    green_certificates: Decimal = Field(ge=0, le=9999, decimal_places=8)
    excise: Decimal = Field(ge=0, le=9999, decimal_places=8)
    tg_in_active: bool = False
    import_vat: Decimal = Field(ge=0, le=100, decimal_places=2)
    export_vat: Decimal = Field(ge=0, le=100, decimal_places=2)
    monthly_fee: Decimal = Field(default=Decimal(0), ge=0, le=999999, decimal_places=2)

    @field_validator(
        *(key for key, _ in COMPONENTS), "import_vat", "export_vat", "monthly_fee", mode="before"
    )
    @classmethod
    def normalize_decimal(cls, value):
        return decimal_text(value)

    @model_validator(mode="after")
    def export_price_nonnegative(self):
        if self.tg > self.active_energy:
            raise ValueError("TG nu poate depasi pretul energiei active in acest model de export.")
        return self


class RomanianTariffPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    tariff: RomanianTariffInput
    import_kwh: Decimal | None = Field(default=None, ge=0, le=1000000000, decimal_places=6)
    export_kwh: Decimal | None = Field(default=None, ge=0, le=1000000000, decimal_places=6)

    @field_validator("import_kwh", "export_kwh", mode="before")
    @classmethod
    def normalize_decimal(cls, value):
        return None if value == "" else decimal_text(value)


def effective_instant(value: str, timezone: str, now: datetime) -> datetime:
    if not value.strip():
        return now.astimezone(UTC)
    try:
        local = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Data de inceput nu este valida.") from exc
    if local.tzinfo is not None:
        return local.astimezone(UTC)
    zone = ZoneInfo(timezone)
    candidates = {local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)}
    valid = [t for t in candidates if t.astimezone(zone).replace(tzinfo=None) == local]
    if len(valid) != 1:
        raise ValueError(
            "Ora de inceput este inexistenta sau ambigua la schimbarea orei. Alege o alta ora."
        )
    return valid[0]
