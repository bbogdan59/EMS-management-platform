"""Bounded, deterministic read-only prototype. No model receives source text."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.api.deps import StationAccess
from app.core.audit import record_audit
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.optimization import Plan
from app.models.telemetry import TelemetryAggregate
from app.services.dashboard_service import get_estimated_savings
from app.services.health_service import RULES

METRICS = {
    "import": ("grid_import_energy_kwh", "grid", "Energie importata", "kWh"),
    "battery": ("battery_charge_energy_kwh", "battery", "Energie de incarcare a bateriei", "kWh"),
    "solar": ("pv_energy_kwh", "pv", "Productie fotovoltaica", "kWh"),
    "load": ("load_energy_kwh", "load", "Consum", "kWh"),
}


def normalize(question):
    return "".join(
        c for c in unicodedata.normalize("NFKD", question.lower()) if not unicodedata.combining(c)
    )


def intent_for(question):
    q = normalize(question)
    if re.search(
        r"(ignore|ignora|system prompt|api.key|secret|token|parola|password|sql|alte organizatii|other tenants|https?://)",
        q,
    ):
        return "unsupported"
    if any(
        word in q
        for word in ("aplica", "seteaza", "schimb", "reserve", "rezerva", "turn on", "set ")
    ):
        return "recommendation"
    if any(word in q for word in ("alert", "sanatate", "health", "defect")):
        return "health"
    if any(word in q for word in ("plan", "gata", "ready", "ev-ul")):
        return "plan"
    if any(word in q for word in ("cost", "econom", "savings", "lei")):
        return "cost"
    if any(word in q for word in ("bater", "battery", "incarcat")):
        return "battery"
    if "import" in q:
        return "import"
    if any(word in q for word in ("solar", "pv", "product")):
        return "solar"
    if any(word in q for word in ("consum", "load")):
        return "load"
    return "unsupported"


def date_window(station, day: date):
    zone = ZoneInfo(station.timezone)
    return (
        datetime.combine(day, time.min, zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def answer(db, user, station_id, question, day=None, now=None):
    station, _ = StationAccess("viewer")(station_id=station_id, db=db, user=user)
    now = now or utcnow()
    if not 1 <= len(question.strip()) <= 1000:
        raise ValueError("Intrebarea trebuie sa aiba 1-1000 caractere.")
    intent = intent_for(question)
    local_day = now.astimezone(ZoneInfo(station.timezone)).date()
    if day is None:
        day = (
            local_day
            if any(w in normalize(question).split() for w in ("azi", "today"))
            else local_day - timedelta(days=1)
        )
    if day > local_day or day < local_day - timedelta(days=365):
        raise ValueError("Alege o zi din ultimul an, pana azi.")
    start, end = date_window(station, day)
    result = {
        "generated": True,
        "method": "deterministic_read_only_v1",
        "intent": intent,
        "status": "insufficient_data",
        "answer": "Nu exista suficiente date masurate pentru o concluzie.",
        "evidence": [],
        "recommendation_draft": None,
        "limitations": "Prototip determinist; nu stabileste cauza unei defectiuni si nu aplica schimbari.",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": station.timezone,
    }
    if intent in METRICS or intent == "cost":
        rows = db.scalars(
            select(TelemetryAggregate)
            .where(
                TelemetryAggregate.station_id == station.id,
                TelemetryAggregate.period_type == "hour",
                TelemetryAggregate.period_start >= start,
                TelemetryAggregate.period_end <= end,
            )
            .order_by(TelemetryAggregate.period_start)
        ).all()
        required = list(METRICS) if intent == "cost" else [intent]
        complete = True
        for key in required:
            field, coverage_key, label, unit = METRICS[key]
            total, seconds = Decimal(0), Decimal(0)
            ids, provenance = [], set()
            for row in rows:
                provenance.add(row.data_quality)
                value = getattr(row, field)
                if value is None:
                    continue
                total += value
                seconds += Decimal(
                    str((row.period_end - row.period_start).total_seconds())
                ) * Decimal(str(row.coverage.get(coverage_key, 0)))
                ids.append(str(row.id))
            coverage = seconds / Decimal(str((end - start).total_seconds()))
            trusted = (
                bool(ids)
                and provenance == {"measured"}
                and coverage >= Decimal(".9")
                and end <= now
            )
            complete &= trusted
            result["evidence"].append(
                {
                    "metric": field,
                    "label": label,
                    "value": str(total) if ids else None,
                    "unit": unit,
                    "coverage": str(coverage),
                    "provenance": sorted(provenance),
                    "aggregate_ids": ids,
                    "formula": f"SUM({field}) over non-overlapping hourly intervals",
                    "link": f"/stations/{station.id}/health",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
            )
        if complete and intent in METRICS:
            metric = result["evidence"][0]
            result.update(
                status="answered",
                answer=f"{metric['label']}: {metric['value']} kWh; acoperire {Decimal(metric['coverage']) * 100:.1f}%. Datele nu dovedesc singure cauza.",
            )
        elif complete:
            savings = get_estimated_savings(db, station, start, end)
            if (
                savings.get("available")
                and savings.get("coverage_ratio", 0) >= 0.9
                and savings.get("tariff_provenance_summary") != "estimated"
            ):
                result["evidence"].append(
                    {
                        "metric": "actual_net_cost_lei",
                        "value": savings["actual_net_cost_lei"],
                        "unit": "RON",
                        "formula": "import cost - export revenue using historical tariffs",
                        "coverage": savings["coverage_ratio"],
                        "provenance": savings.get("tariff_buy_provenance"),
                        "link": f"/stations/{station.id}/tariffs",
                    }
                )
                result.update(
                    status="answered",
                    answer=f"Cost net calculat: {savings['actual_net_cost_lei']} lei. Comparatiile de economii sunt estimari, nu masuratori.",
                )
        if result["status"] != "answered":
            result["answer"] = (
                "Concluzie indisponibila: zi incompleta, acoperire sub 90%, provenienta neobservata sau tarif lipsa. Vezi dovezile si acoperirea fiecarei metrici."
            )
    elif intent == "health":
        alerts = db.scalars(
            select(Alert)
            .where(
                Alert.station_id == station.id, Alert.created_at >= start, Alert.created_at < end
            )
            .order_by(Alert.created_at)
            .limit(50)
        ).all()
        result["evidence"] = [
            {
                "rule": a.category,
                "status": a.status,
                "severity": a.severity,
                "link": f"/stations/{station.id}/health#alert-{a.id}",
            }
            for a in alerts
            if a.category in RULES
        ]
        result.update(
            status="answered",
            answer=f"{len(result['evidence'])} incidente inregistrate in interval. Absenta incidentelor nu confirma ca toate masuratorile sunt disponibile.",
        )
    elif intent == "plan":
        plan = db.scalar(
            select(Plan)
            .where(
                Plan.station_id == station.id, Plan.published_at >= start, Plan.published_at < end
            )
            .order_by(Plan.version.desc())
            .limit(1)
        )
        if plan:
            result["evidence"] = [
                {
                    "plan_id": str(plan.id),
                    "version": plan.version,
                    "status": plan.status,
                    "execution_mode": plan.execution_mode,
                    "link": f"/stations/{station.id}/health",
                }
            ]
            result.update(
                status="answered",
                answer=f"Planul v{plan.version} este {plan.status}, in modul {plan.execution_mode}. Planul si ACK-ul nu confirma aplicarea sau atingerea tintei EV.",
            )
        else:
            result["answer"] = (
                "Nu exista un plan publicat in interval. Nu pot confirma ora de plecare sau necesarul EV."
            )
    elif intent == "recommendation":
        result.update(
            status="draft",
            answer="Propunere pentru analiza: revizuieste rezerva si limitele statiei. Impactul necesita o simulare cu date proaspete; propunerea nu este aplicata.",
            recommendation_draft={
                "type": "review_preferences",
                "station_id": str(station.id),
                "status": "draft",
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
                "applied": False,
            },
        )
    else:
        result.update(
            status="unsupported",
            answer="Pot explica importul, productia, consumul, bateria, costul, alertele si planurile propriei statii. Nu execut instructiuni din texte sau loguri.",
        )
    record_audit(
        db,
        action="assistant.read",
        resource_type="station",
        resource_id=str(station.id),
        actor_user_id=user.id,
        organization_id=station.organization_id,
        station_id=station.id,
        metadata={
            "intent": intent,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": result["status"],
        },
    )
    return result
