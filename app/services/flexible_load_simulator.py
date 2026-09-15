"""Simulator termic determinist pentru discovery/backtest, fara I/O sau control.

Puterea ceruta este un scenariu furnizat de apelant. Simulatorul nu decide si
nu trimite comenzi; el estimeaza raspunsul termic si semnaleaza short-cycling.
"""
from __future__ import annotations

from decimal import Decimal

from app.schemas.flexible_load import (
    ThermalMode,
    ThermalSimulationRequest,
    ThermalSimulationResult,
    ThermalSimulationResultStep,
)

ZERO = Decimal("0")


def _runtime_violation(
    *,
    previous_on: bool | None,
    previous_transition_at,
    current_on: bool,
    current_start,
    minimum_on_minutes: int,
    minimum_off_minutes: int,
) -> str | None:
    if previous_on is None or previous_on == current_on:
        return None
    elapsed_minutes = Decimal(str((current_start - previous_transition_at).total_seconds())) / Decimal("60")
    required = minimum_on_minutes if previous_on else minimum_off_minutes
    if elapsed_minutes < required:
        state = "pornit" if previous_on else "oprit"
        return f"short_cycle: echipamentul a ramas {state} doar {elapsed_minutes} minute; minim {required}"
    return None


def simulate_thermal_response(request: ThermalSimulationRequest) -> ThermalSimulationResult:
    """Ruleaza un model RC discretizat si pastreaza necunoscut costul incomplet.

    Formula pe pas:
      T_next = T_inside + (T_outside - T_inside) * min(dt/tau, 1)
               + signed_heat_kwh / thermal_capacity_kwh_per_c

    `requested_input_power_kw` este consum electric. COP il transforma in
    energie termica; racirea are semn negativ. Modul `auto` nu este simulat,
    deoarece directia reala nu poate fi dedusa fara feedback/provider.
    """
    indoor = request.initial_indoor_temperature_c
    result_steps: list[ThermalSimulationResultStep] = []
    total_energy = ZERO
    total_cost = ZERO
    has_unknown_cost = False
    previous_on: bool | None = None
    previous_transition_at = request.steps[0].starts_at

    for step in request.steps:
        if step.requested_mode == ThermalMode.auto and step.requested_input_power_kw > 0:
            raise ValueError("modul auto cu putere nenula necesita directie/feedback real si nu poate fi simulat")

        is_on = step.requested_input_power_kw > 0
        violation = _runtime_violation(
            previous_on=previous_on,
            previous_transition_at=previous_transition_at,
            current_on=is_on,
            current_start=step.starts_at,
            minimum_on_minutes=request.capabilities.minimum_on_minutes,
            minimum_off_minutes=request.capabilities.minimum_off_minutes,
        )
        if previous_on is not None and previous_on != is_on:
            previous_transition_at = step.starts_at

        hours = Decimal(str((step.ends_at - step.starts_at).total_seconds())) / Decimal("3600")
        energy = step.requested_input_power_kw * hours
        passive_fraction = min(hours / request.model.time_constant_hours, Decimal("1"))
        passive_delta = (step.outdoor_temperature_c - indoor) * passive_fraction
        direction = Decimal("-1") if step.requested_mode == ThermalMode.cool else Decimal("1")
        if step.requested_mode in {ThermalMode.off, ThermalMode.auto}:
            direction = ZERO
        thermal_delta = (
            direction
            * energy
            * request.model.coefficient_of_performance
            / request.model.thermal_capacity_kwh_per_c
        )
        next_indoor = indoor + passive_delta + thermal_delta

        cost = None
        if step.import_price_lei_kwh is None:
            has_unknown_cost = True
        else:
            cost = energy * step.import_price_lei_kwh
            total_cost += cost

        result_steps.append(
            ThermalSimulationResultStep(
                starts_at=step.starts_at,
                ends_at=step.ends_at,
                indoor_temperature_start_c=indoor,
                indoor_temperature_end_c=next_indoor,
                input_energy_kwh=energy,
                cost_lei=cost,
                runtime_violation=violation,
            )
        )
        total_energy += energy
        indoor = next_indoor
        previous_on = is_on

    return ThermalSimulationResult(
        steps=result_steps,
        total_input_energy_kwh=total_energy,
        total_cost_lei=None if has_unknown_cost else total_cost,
        has_unknown_cost=has_unknown_cost,
    )
