"""Scheme Pydantic pentru formularele de configurare tehnica, preferinte si
creare statie (issue #8). Nu inlocuiesc parsarea Form(...) din FastAPI --
rutele din `stations.py`/`organizations.py` construiesc un dict din campurile
de formular si il valideaza aici INAINTE de a crea vreo versiune noua. O
eroare de validare respinge INTREAGA cerere (nicio salvare partiala, nicio
versiune noua creata) si e re-afisata utilizatorului cu mesaje clare, per
camp -- niciodata inlocuita tacit cu o valoare implicita."""
from __future__ import annotations

from datetime import time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    """Baza comuna: campuri necunoscute respinse explicit, NaN/Infinity
    respinse explicit (desi Pydantic le respinge deja implicit pentru
    Decimal/float in aceasta versiune -- explicit aici pentru claritate si
    ca documentatie a intentiei, nu doar ca sa ne bazam pe un implicit)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PanelGroupInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    power_kwp: Decimal = Field(gt=0)
    azimuth_degrees: Decimal = Field(ge=0, lt=360)  # 0=N, 90=E, 180=S, 270=V
    tilt_degrees: Decimal = Field(ge=0, le=90)


class StationConfigInput(Strict):
    pv_installed_power_kw: Decimal = Field(gt=0)
    inverter_power_kw: Decimal = Field(gt=0)
    battery_reference_capacity_kwh: Decimal | None = Field(default=None, ge=0)
    battery_available_capacity_kwh: Decimal | None = Field(default=None, ge=0)
    battery_max_charge_power_kw: Decimal | None = Field(default=None, ge=0)
    battery_max_discharge_power_kw: Decimal | None = Field(default=None, ge=0)
    battery_charge_efficiency: Decimal = Field(default=Decimal("0.95"), gt=0, le=1)
    battery_discharge_efficiency: Decimal = Field(default=Decimal("0.95"), gt=0, le=1)
    grid_import_limit_kw: Decimal | None = Field(default=None, ge=0)
    grid_export_limit_kw: Decimal | None = Field(default=None, ge=0)
    ev_enabled: bool = False
    ev_battery_capacity_kwh: Decimal | None = Field(default=None, ge=0)
    ev_max_charge_power_kw: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    panel_groups: list[PanelGroupInput] = Field(min_length=1, max_length=32)
    expected_version: int = Field(ge=0)

    @model_validator(mode="after")
    def _coherent_limits(self) -> StationConfigInput:
        if (
            self.battery_reference_capacity_kwh is not None
            and self.battery_available_capacity_kwh is not None
            and self.battery_available_capacity_kwh > self.battery_reference_capacity_kwh
        ):
            raise ValueError("Capacitatea disponibila nu poate depasi capacitatea de referinta a bateriei.")
        if self.ev_enabled and self.ev_battery_capacity_kwh is None:
            raise ValueError("Capacitatea bateriei EV este necesara cand statia de incarcare EV e prezenta.")
        return self


class SocTargetInput(Strict):
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Ora locala HH:MM")
    target_soc_percent: Decimal = Field(ge=0, le=100)
    days_of_week: list[int] | None = Field(default=None, max_length=7)

    @field_validator("days_of_week")
    @classmethod
    def _valid_days(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days_of_week trebuie sa contina valori intre 0 (luni) si 6 (duminica).")
        return sorted(set(v))


class PreferenceInput(Strict):
    min_reserve_soc_percent: Decimal = Field(ge=0, le=100)
    max_normal_soc_percent: Decimal = Field(ge=0, le=100)
    max_optimization_energy_kwh: Decimal | None = Field(default=None, ge=0)
    allow_grid_charge: bool = False
    allow_battery_export: bool = False
    max_efc_per_day: Decimal | None = Field(default=None, ge=0)
    max_efc_per_month: Decimal | None = Field(default=None, ge=0)
    priority: str = "cost"
    soc_targets: list[SocTargetInput] = Field(default_factory=list, max_length=50)
    ev_required_energy_kwh: Decimal | None = Field(default=None, ge=0)
    ev_departure_time: time | None = None
    automation_suspended_until: str | None = Field(default=None, max_length=32)
    arbitrage_min_benefit_lei: Decimal = Field(default=Decimal("0"), ge=0)
    expected_version: int = Field(ge=0)

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: str) -> str:
        if v not in ("cost", "autonomy", "battery_protection"):
            raise ValueError("Prioritate invalida.")
        return v

    @model_validator(mode="after")
    def _soc_bounds_coherent(self) -> PreferenceInput:
        if self.min_reserve_soc_percent > self.max_normal_soc_percent:
            raise ValueError("SOC minim de rezerva nu poate depasi SOC maxim normal.")
        return self


class StationCreateInput(Strict):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=64)
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)
    pv_installed_power_kw: Decimal = Field(gt=0)
    inverter_power_kw: Decimal = Field(gt=0)
    battery_reference_capacity_kwh: Decimal | None = Field(default=None, ge=0)
    battery_max_charge_power_kw: Decimal | None = Field(default=None, ge=0)
    battery_max_discharge_power_kw: Decimal | None = Field(default=None, ge=0)
    grid_import_limit_kw: Decimal | None = Field(default=None, ge=0)
    grid_export_limit_kw: Decimal | None = Field(default=None, ge=0)
    ev_enabled: bool = False
