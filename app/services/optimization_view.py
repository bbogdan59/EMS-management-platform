"""View-model explicabil pentru planurile de optimizare (issue #47).

Deliberat separat de `optimization_service`: acest modul NU atinge modelul
matematic (Pyomo/HiGHS) si nu scrie in baza de date -- doar transforma
`PlanInterval`-urile deja publicate intr-o reprezentare usor de inteles
pentru un om fara cunostinte despre solver:

  - o "actiune" in limbaj natural per interval (`classify_action`);
  - un "motiv"/constrangere dominanta, dedus euristic din valorile deja
    publicate (SOC fata de banda de rezerva/plafon, pret, sens flux retea) --
    NU o citire directa a variabilelor interne ale solverului (acelea nu mai
    exista dupa ce planul a fost publicat);
  - gruparea intervalelor consecutive cu aceeasi actiune+motiv intr-un
    "segment" rezumat ("23:00-06:00, 7h -- incarca din retea, pret redus").

Functii pure, testabile fara baza de date/solver (vezi
`tests/unit/test_optimization_view.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

THRESHOLD_KW = 0.01
SOC_BAND_EPSILON_PERCENT = 0.5

ACTION_LABELS: dict[str, str] = {
    "charge_pv": "Incarca bateria din PV",
    "charge_grid": "Incarca bateria (si din retea)",
    "discharge": "Descarca bateria",
    "export": "Exporta in retea",
    "import": "Importa din retea",
    "hold": "Mentine (fara actiune)",
}


@dataclass(frozen=True)
class IntervalRow:
    interval_start: datetime
    interval_end: datetime
    pv_forecast_kw: float
    load_forecast_kw: float
    battery_power_target_kw: float  # + incarcare, - descarcare
    grid_power_target_kw: float  # + import, - export
    battery_soc_target_percent: float
    ev_charge_power_kw: float
    price_import_lei_kwh: float | None
    price_export_lei_kwh: float | None
    explanation: str | None
    action_code: str
    action_label: str
    dominant_reason: str
    interval_cost_lei: float | None  # + cost net, - beneficiu net; None daca lipsesc ambele preturi


@dataclass(frozen=True)
class PlanSegment:
    action_code: str
    action_label: str
    start: datetime
    end: datetime
    interval_count: int
    dominant_reason: str
    avg_battery_power_kw: float
    avg_grid_power_kw: float
    total_cost_lei: float | None


def classify_action(battery_power_target_kw: float, grid_power_target_kw: float) -> str:
    """Clasifica un interval intr-o actiune de baza, pornind STRICT de la
    setpoint-urile deja publicate (nu de la variabile interne ale solverului,
    care nu mai exista la acest punct)."""
    if battery_power_target_kw > THRESHOLD_KW:
        return "charge_grid" if grid_power_target_kw > THRESHOLD_KW else "charge_pv"
    if battery_power_target_kw < -THRESHOLD_KW:
        return "discharge"
    if grid_power_target_kw > THRESHOLD_KW:
        return "import"
    if grid_power_target_kw < -THRESHOLD_KW:
        return "export"
    return "hold"


def _dominant_reason(
    action_code: str,
    soc_percent: float,
    min_reserve_soc_percent: float | None,
    max_normal_soc_percent: float | None,
) -> str:
    """Constrangerea dominanta cel mai probabil sa fi determinat actiunea,
    dedusa euristic din SOC-ul fata de banda de preferinta -- nu o extragere
    a multiplicatorilor Lagrange reali ai solverului (planul deja publicat nu
    mai poarta acea informatie). Documentat explicit ca euristica in
    docs/LIMITATIONS.md."""
    if (
        min_reserve_soc_percent is not None
        and soc_percent <= min_reserve_soc_percent + SOC_BAND_EPSILON_PERCENT
        and action_code in ("hold", "charge_pv", "charge_grid")
    ):
        return "protejarea rezervei minime de SOC"
    if (
        max_normal_soc_percent is not None
        and soc_percent >= max_normal_soc_percent - SOC_BAND_EPSILON_PERCENT
        and action_code == "export"
    ):
        return "plafonul SOC normal a fost atins (surplusul PV e exportat)"
    return {
        "charge_grid": "pret de import redus (incarcare oportunista)",
        "charge_pv": "surplus de productie PV disponibil",
        "discharge": "acoperirea consumului fara import din retea",
        "export": "surplus de productie PV peste consum si capacitatea bateriei",
        "import": "productia PV si bateria nu acopera consumul",
        "hold": "echilibru cerere-oferta",
    }[action_code]


def build_rows(
    plan_intervals,
    *,
    min_reserve_soc_percent: float | None = None,
    max_normal_soc_percent: float | None = None,
    interval_hours: float = 0.25,
) -> list[IntervalRow]:
    """Construieste randurile explicabile pentru un plan. `plan_intervals`
    poate fi orice iterabil de obiecte cu atributele unui `PlanInterval`
    (ORM sau altceva echivalent, util in teste unitare fara DB)."""
    rows: list[IntervalRow] = []
    for pi in plan_intervals:
        battery_kw = float(pi.battery_power_target_kw)
        grid_kw = float(pi.grid_power_target_kw)
        soc_pct = float(pi.battery_soc_target_percent)
        code = classify_action(battery_kw, grid_kw)
        reason = _dominant_reason(code, soc_pct, min_reserve_soc_percent, max_normal_soc_percent)

        price_import = float(pi.price_import_lei_kwh) if pi.price_import_lei_kwh is not None else None
        price_export = float(pi.price_export_lei_kwh) if pi.price_export_lei_kwh is not None else None
        grid_import_kw = max(grid_kw, 0.0)
        grid_export_kw = max(-grid_kw, 0.0)
        # Un pret lipsa este necunoscut, nu zero. Avem nevoie doar de pretul
        # corespunzator fluxului efectiv din interval; un interval fara flux
        # are cost cunoscut 0 chiar daca nu exista tarife.
        required_price_missing = (
            (grid_import_kw > THRESHOLD_KW and price_import is None)
            or (grid_export_kw > THRESHOLD_KW and price_export is None)
        )
        cost = None if required_price_missing else round(
            (grid_import_kw * (price_import if price_import is not None else 0.0)
             - grid_export_kw * (price_export if price_export is not None else 0.0))
            * interval_hours,
            4,
        )

        rows.append(
            IntervalRow(
                interval_start=pi.interval_start,
                interval_end=pi.interval_end,
                pv_forecast_kw=float(pi.pv_forecast_kw),
                load_forecast_kw=float(pi.load_forecast_kw),
                battery_power_target_kw=battery_kw,
                grid_power_target_kw=grid_kw,
                battery_soc_target_percent=soc_pct,
                ev_charge_power_kw=float(pi.ev_charge_power_kw),
                price_import_lei_kwh=price_import,
                price_export_lei_kwh=price_export,
                explanation=pi.explanation,
                action_code=code,
                action_label=ACTION_LABELS[code],
                dominant_reason=reason,
                interval_cost_lei=cost,
            )
        )
    return rows


def group_segments(rows: list[IntervalRow]) -> list[PlanSegment]:
    """Grupeaza randuri consecutive cu aceeasi actiune SI acelasi motiv
    dominant intr-un singur segment rezumat in limbaj natural."""
    segments: list[PlanSegment] = []
    current: list[IntervalRow] = []
    for row in rows:
        if current and (row.action_code != current[-1].action_code or row.dominant_reason != current[-1].dominant_reason):
            segments.append(_finalize_segment(current))
            current = []
        current.append(row)
    if current:
        segments.append(_finalize_segment(current))
    return segments


def _finalize_segment(rows: list[IntervalRow]) -> PlanSegment:
    n = len(rows)
    # Totalul unui segment este cunoscut numai daca fiecare interval este
    # evaluabil; o singura valoare necunoscuta nu poate fi inlocuita cu zero.
    total_cost = None
    if all(r.interval_cost_lei is not None for r in rows):
        total_cost = round(sum(r.interval_cost_lei for r in rows), 4)
    return PlanSegment(
        action_code=rows[0].action_code,
        action_label=rows[0].action_label,
        start=rows[0].interval_start,
        end=rows[-1].interval_end,
        interval_count=n,
        dominant_reason=rows[0].dominant_reason,
        avg_battery_power_kw=round(sum(r.battery_power_target_kw for r in rows) / n, 3),
        avg_grid_power_kw=round(sum(r.grid_power_target_kw for r in rows) / n, 3),
        total_cost_lei=total_cost,
    )
