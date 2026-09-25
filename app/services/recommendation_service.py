from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from app.api.deps import StationAccess
from app.core.security import utcnow
from app.models.control import PlanOutcome, Recommendation
from app.models.optimization import Plan, PlanInterval
from app.models.preference import PreferenceVersion
from app.services import control_service as control
from app.services.ev_analytics_service import seconds
from app.services.ev_service import audit, lock_station

PRESETS = {
    "economy": {"version": 1, "title": "Economy", "changes": {"priority": "cost"}},
    "autonomy": {"version": 1, "title": "Autonomy", "changes": {"priority": "autonomy"}},
    "battery_protection": {"version": 1, "title": "Battery protection", "changes": {"priority": "battery_protection", "max_efc_per_day": "0.5"}},
    "backup": {"version": 1, "title": "Backup", "changes": {"priority": "autonomy", "min_reserve_soc_percent": "60"}},
    "vacation": {"version": 1, "title": "Vacation", "changes": {"priority": "battery_protection", "min_reserve_soc_percent": "60", "ev_required_energy_kwh": "0"}},
    "manual": {"version": 1, "title": "Manual", "changes": {"suspend_hours": 24}},
}


def plan_impact(plan):
    snapshot = plan.optimization_run.input_snapshot
    expected, baseline, shifted = Decimal(0), Decimal(0), Decimal(0)
    trusted = not plan.optimization_run.is_fallback and snapshot.get("soc", {}).get("quality") == "measured"
    trusted = trusted and all(snapshot.get(key) and all(q == "measured" for q in snapshot[key].values())
                              for key in ("pv_forecast_quality", "load_forecast_quality", "price_buy_quality"))
    for interval in plan.intervals:
        buy, sell = interval.price_import_lei_kwh, interval.price_export_lei_kwh
        if buy is None or sell is None:
            trusted = False
            continue
        hours = seconds(interval.interval_end - interval.interval_start) / 3600
        grid = interval.grid_power_target_kw
        net_load = interval.load_forecast_kw + interval.ev_charge_power_kw - interval.pv_forecast_kw
        expected += (max(grid, 0) * buy - max(-grid, 0) * sell) * hours
        baseline += (max(net_load, 0) * buy - max(-net_load, 0) * sell) * hours
        shifted += abs(interval.battery_power_target_kw) * hours
    quality_groups = [snapshot.get(k, {}) for k in ("pv_forecast_quality", "load_forecast_quality", "price_buy_quality")]
    count = sum(len(g) for g in quality_groups)
    measured = sum(sum(v == "measured" for v in g.values()) for g in quality_groups)
    return {
        "impact_lei": str(baseline - expected) if trusted else None,
        "managed_kwh": str(shifted) if trusted else None,
        "confidence": "estimated" if trusted else "insufficient_data",
        "coverage": str(Decimal(measured) / count) if count else "0",
        "baseline": "Same forecast PV/load/EV; direct self-consumption, no scheduled battery use. Excludes wear, fixed fees and terminal SOC value.",
        "reason_code": "forecast_cost_difference" if trusted else "untrusted_or_missing_inputs",
    }


def capture_plan(db, station, plan):
    fingerprint = control.digest({"kind": "plan", "plan_id": str(plan.id), "version": plan.version})
    existing = db.scalar(select(Recommendation).where(
        Recommendation.station_id == station.id, Recommendation.fingerprint == fingerprint,
    ))
    if existing:
        return existing
    for old in db.scalars(select(Recommendation).where(
        Recommendation.station_id == station.id, Recommendation.kind == "plan",
        Recommendation.status.in_(("available", "snoozed")),
    )):
        old.status = "superseded"
    record = Recommendation(
        station_id=station.id, plan_id=plan.id, kind="plan", fingerprint=fingerprint,
        snapshot={**plan_impact(plan), "title": "Plan energetic propus", "plan_version": plan.version,
                  "config_id": str(plan.optimization_run.station_config_version_id),
                  "preference_id": str(plan.optimization_run.preference_version_id)},
        expires_at=min(utcnow() + timedelta(hours=1), plan.optimization_run.horizon_start),
    )
    db.add(record)
    db.flush()
    return record


def preset_diff(preference, name):
    if name not in PRESETS:
        raise ValueError("Mod de operare necunoscut.")
    changes = dict(PRESETS[name]["changes"])
    if name in ("backup", "vacation"):
        changes["min_reserve_soc_percent"] = str(max(preference.min_reserve_soc_percent, Decimal(60)))
        if Decimal(changes["min_reserve_soc_percent"]) >= preference.max_normal_soc_percent:
            raise ValueError("Rezerva propusa depaseste plafonul curent; ajustati explicit preferintele.")
    if name == "battery_protection" and preference.max_efc_per_day is not None:
        changes["max_efc_per_day"] = str(min(preference.max_efc_per_day, Decimal(".5")))
    return {key: {"before": str(getattr(preference, key)) if key != "suspend_hours" else None, "after": value}
            for key, value in changes.items()}


def create_preset(db, station, user, name):
    StationAccess("operator")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    config, preference = control.current_versions(db, station)
    if not preference or not config:
        raise ValueError("Configuratie incompleta.")
    diff = preset_diff(preference, name)
    snapshot = {"title": PRESETS[name]["title"], "preset": name, "preset_version": PRESETS[name]["version"],
                "config_id": str(config.id), "preference_id": str(preference.id), "diff": diff,
                "reason_code": "user_selected_preset", "impact_lei": None, "managed_kwh": None,
                "confidence": "requires_new_plan", "coverage": None,
                "limits_retained": "All technical, policy, expiry, consent and cycle limits remain active. A new plan requires separate approval."}
    fingerprint = control.digest({**snapshot, "request_window": utcnow().isoformat()[:13]})
    existing = db.scalar(select(Recommendation).where(Recommendation.station_id == station.id, Recommendation.fingerprint == fingerprint))
    if existing:
        return existing
    record = Recommendation(station_id=station.id, kind="preset", fingerprint=fingerprint, snapshot=snapshot,
                            expires_at=utcnow() + timedelta(minutes=30))
    db.add(record)
    db.flush()
    audit(db, station, user, "recommendation.preset_preview", record.id, {"preset": name, "version": 1})
    return record


def preview(db, station, record):
    if record.station_id != station.id:
        raise ValueError("Recomandare neautorizata.")
    if record.kind == "plan":
        result = control.preview(db, station, db.get(Plan, record.plan_id))
        outcomes = db.scalars(select(PlanOutcome).join(PlanInterval, PlanInterval.id == PlanOutcome.interval_id)
                             .where(PlanInterval.plan_id == record.plan_id)).all()
        return {**result, "outcomes": outcomes}
    state = control.control_state(db, station)
    return {"snapshot": record.snapshot, "preview_hash": control.digest(record.snapshot),
            "reasons": [] if state and state.mode == "assisted" else ["assisted_mode_required"], "outcomes": []}


def act(db, station, user, record, action, reason, preview_hash=None, snoozed_until=None):
    StationAccess("operator")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    record = db.scalar(select(Recommendation).where(Recommendation.id == record.id).with_for_update())
    if record.station_id != station.id or action not in ("apply", "dismiss", "snooze", "not_relevant") or not reason.strip() or len(reason) > 500:
        raise ValueError("Actiune sau motiv invalid.")
    if action == "apply" and record.status in ("applied", "awaiting_plan"):
        if preview_hash == preview(db, station, record)["preview_hash"]:
            return record
        raise ValueError("Reincercarea nu corespunde aprobarii existente.")
    if record.expires_at <= utcnow() or record.status not in ("available", "snoozed"):
        raise ValueError("Recomandare expirata sau inlocuita.")
    if action == "snooze":
        if not snoozed_until or snoozed_until.tzinfo is None or not utcnow() < snoozed_until < record.expires_at:
            raise ValueError("Amanarea trebuie sa fie inainte de expirare.")
        record.snoozed_until, record.status = snoozed_until, "snoozed"
    elif action in ("dismiss", "not_relevant"):
        record.status = "dismissed" if action == "dismiss" else "not_relevant"
    else:
        config, preference = control.current_versions(db, station)
        if str(config.id) != record.snapshot["config_id"] or str(preference.id) != record.snapshot["preference_id"]:
            raise ValueError("Preferintele sau configuratia au fost inlocuite.")
        if record.kind == "plan":
            control.approve_plan(db, station, db.get(Plan, record.plan_id), user, preview_hash)
            record.status = "applied"
        else:
            state = control.control_state(db, station)
            if not state or state.mode != "assisted" or preview_hash != control.digest(record.snapshot):
                raise ValueError("Necesita Assisted si confirmarea preview-ului curent.")
            values = {column.name: getattr(preference, column.name) for column in PreferenceVersion.__table__.columns
                      if column.name not in ("id", "created_at", "updated_at", "version", "created_by_user_id")}
            for field, change in record.snapshot["diff"].items():
                if field == "suspend_hours":
                    values["automation_suspended_until"] = utcnow() + timedelta(hours=int(change["after"]))
                elif field == "priority":
                    values[field] = change["after"]
                else:
                    values[field] = Decimal(change["after"])
            new_preference = PreferenceVersion(**values, version=preference.version + 1, created_by_user_id=user.id)
            db.add(new_preference)
            state.revision += 1
            control.cancel_pending(db, station, "preference_preset_changed")
            if record.snapshot["preset"] == "manual":
                control.suspend(db, station, "manual_override", user)
            record.status = "awaiting_plan"
    record.acted_by, record.acted_at, record.feedback = user.id, utcnow(), reason.strip()
    audit(db, station, user, "recommendation." + action, record.id, {"reason": reason.strip(), "status": record.status})
    db.flush()
    return record


def ranked(db, station, now=None):
    now = now or utcnow()
    records = db.scalars(select(Recommendation).where(Recommendation.station_id == station.id)
                        .order_by(Recommendation.created_at.desc()).limit(100)).all()
    negative = {r.kind for r in records if r.status == "not_relevant" and r.acted_at > now - timedelta(days=7)}
    visible = [r for r in records if not (r.status == "snoozed" and r.snoozed_until > now)]
    return sorted(visible, key=lambda r: (r.expires_at <= now or r.status not in ("available", "snoozed"), r.kind in negative))


def compare_scenarios(db, station, plan, reserve, fixed_price, strategy):
    from app.services.optimization_service import _solve

    if not reserve.is_finite() or not 0 <= reserve < 100 or not fixed_price.is_finite() or not -100 <= fixed_price <= 100:
        raise ValueError("Scenariu numeric invalid.")
    config, pref = control.current_versions(db, station)
    if (plan.station_id != station.id or not config or not pref or
            plan.optimization_run.station_config_version_id != config.id or
            plan.optimization_run.preference_version_id != pref.id):
        raise ValueError("Configuratia scenariului a fost inlocuita.")
    if strategy not in ("cost", "autonomy", "battery_protection") or reserve >= pref.max_normal_soc_percent:
        raise ValueError("Strategie sau banda SOC invalida.")
    snapshot = plan.optimization_run.input_snapshot
    if plan_impact(plan)["confidence"] == "insufficient_data":
        raise ValueError("Date insuficiente pentru comparatie.")
    fields = ("pv_forecast_kw", "load_forecast_kw", "price_buy_lei_kwh", "price_sell_lei_kwh")
    if not all(snapshot.get(key) for key in fields) or any(set(snapshot[key]) != set(snapshot[fields[0]]) for key in fields):
        raise ValueError("Snapshot incomplet pentru comparatie.")
    horizon = [datetime.fromisoformat(t) for t in snapshot["pv_forecast_kw"]]
    if not horizon or len(horizon) > 192:
        raise ValueError("Orizont indisponibil.")
    horizon.sort()
    attrs = {column.name: getattr(pref, column.name) for column in PreferenceVersion.__table__.columns}
    results = []
    for label, changes, buy in (
        ("Current tariff / current preferences", {}, {t: float(snapshot["price_buy_lei_kwh"][t.isoformat()]) for t in horizon}),
        ("Current tariff / proposed reserve and strategy", {"min_reserve_soc_percent": reserve, "priority": strategy}, {t: float(snapshot["price_buy_lei_kwh"][t.isoformat()]) for t in horizon}),
        ("Hypothetical fixed tariff / proposed reserve and strategy", {"min_reserve_soc_percent": reserve, "priority": strategy}, dict.fromkeys(horizon, float(fixed_price))),
    ):
        result = _solve(station=station, config=config, preference=SimpleNamespace(**{**attrs, **changes}),
                        horizon=horizon, interval_minutes=plan.optimization_run.interval_minutes,
                        pv_series={t: float(snapshot["pv_forecast_kw"][t.isoformat()]) for t in horizon},
                        load_series={t: float(snapshot["load_forecast_kw"][t.isoformat()]) for t in horizon},
                        price_buy=buy, price_sell={t: float(snapshot["price_sell_lei_kwh"][t.isoformat()]) for t in horizon},
                        current_soc_kwh=float(snapshot["soc"]["kwh"]), db=db)
        cost = None
        if result["termination"] in ("optimal", "feasible"):
            cost = sum(((max(Decimal(str(i["grid_power_target_kw"])), 0) * Decimal(str(i["price_import_lei_kwh"]))
                         - max(-Decimal(str(i["grid_power_target_kw"])), 0) * Decimal(str(i["price_export_lei_kwh"])))
                        * seconds(i["interval_end"] - i["interval_start"]) / 3600 for i in result["intervals"]), Decimal(0))
        results.append({"scenario": label, "status": result["termination"], "forecast_net_cost_lei": str(cost) if cost is not None else None})
    return {"results": results, "method": "Existing HiGHS solver on the same forecast snapshot; hypothetical, not historical savings or an approved plan.", "applied": False}
