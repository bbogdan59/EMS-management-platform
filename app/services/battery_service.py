"""Read-only, source-scoped battery diagnostics. Domain arithmetic stays Decimal."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.device import Device
from app.models.station import StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.schemas.battery import (
    BatteryBucket,
    BatteryCurrent,
    BatteryDetail,
    BatteryExtreme,
    BatteryMetric,
    BatteryNotice,
    BatteryPeriod,
    BatterySoh,
    BatterySummary,
    BatteryTarget,
)
from app.services.aggregation_service import MAX_GAP_SECONDS

ZERO = Decimal(0)
HOLD = timedelta(seconds=MAX_GAP_SECONDS)
RANK = {"missing": -1, "declared": 0, "measured": 1, "estimated": 2, "stale": 3, "simulated": 4}


def decimal(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def seconds(delta):
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000


def midnight(day, timezone):
    return datetime.combine(day, time.min, ZoneInfo(timezone)).astimezone(UTC)


def worst(qualities):
    return max(qualities, key=RANK.__getitem__, default="missing")


def payload(row, target):
    if row is None:
        return {}
    diagnostics = row.diagnostics or {}
    if target.pack_id is not None:
        return next((p for p in diagnostics.get("battery_packs", []) if p.get("pack_id") == target.pack_id), {})
    return {**diagnostics.get("battery", {}), "power_w": row.battery_power_w, "soc_percent": row.battery_soc_percent}


def provenance(row, group, *, now=None, core=False):
    flags = {key for key, value in (row.quality_flags or {}).items() if value is True}
    if row.is_simulated:
        flags.add("simulated")
    if row.is_late:
        flags.add("late")
    group_quality = "measured" if core else group.get("quality", "measured")
    if group_quality != "measured":
        flags.add(group_quality)
    if now and not timedelta(0) <= now - row.measured_at <= timedelta(minutes=10):
        flags.add("stale")
    quality = "measured"
    if "derived" in flags:
        quality = "estimated"
    if "stale" in flags:
        quality = "stale"
    if "simulated" in flags:
        quality = "simulated"
    return quality, sorted(flags)


def discover(db, station, now):
    devices = db.scalars(select(Device).where(Device.station_id == station.id).order_by(Device.created_at)).all()
    latest = db.scalars(select(TelemetryRaw).where(
        TelemetryRaw.station_id == station.id, TelemetryRaw.measured_at <= now,
    ).distinct(TelemetryRaw.device_id).order_by(
        TelemetryRaw.device_id, TelemetryRaw.measured_at.desc(), TelemetryRaw.received_at.desc(), TelemetryRaw.id.desc(),
    )).all()
    rows = {row.device_id: row for row in latest}
    targets = []
    for device in devices:
        row = rows.get(device.id)
        source = row.source if row else ("deye_cloud" if (device.capabilities or {}).get("deye_cloud") else "device_rs485")
        packs = (row.diagnostics or {}).get("battery_packs", []) if row else []
        # An inverter bank and its constituent packs are alternatives, never summed.
        if not packs or row.battery_power_w is not None or row.battery_soc_percent is not None or (row.diagnostics or {}).get("battery"):
            targets.append(BatteryTarget(id=f"{device.id}:bank", device_id=device.id,
                                        label=f"{device.name} · banc raportat", source=source, scope="reported_bank"))
        for pack in packs:
            targets.append(BatteryTarget(id=f"{device.id}:pack:{pack['pack_id']}", device_id=device.id,
                                        pack_id=pack["pack_id"], label=pack.get("label") or f"Baterie {pack['pack_id']}",
                                        source=source, scope="pack"))
    configs = db.scalars(select(StationConfigVersion).where(StationConfigVersion.station_id == station.id)
                         .order_by(StationConfigVersion.created_at, StationConfigVersion.version)).all()
    return targets, rows, configs


def config_at(configs, at):
    return next((c for c in reversed(configs) if c.created_at <= at), None)


def capacity(group, configs, at, allow_config, usable=False):
    key = "usable_capacity_kwh" if usable else "nominal_capacity_kwh"
    value = decimal(group.get(key))
    if value is not None:
        return value, "source_declared", None
    config = config_at(configs, at) if allow_config else None
    if config is None:
        return None, None, None
    value = config.battery_available_capacity_kwh if usable else config.battery_reference_capacity_kwh
    return value, f"station_config:v{config.version}", config.created_at


def current(target, row, configs, allow_config, now):
    group = payload(row, target)

    def metric(key, unit, scale=Decimal(1)):
        value = decimal(group.get(key))
        core = target.pack_id is None and key in ("power_w", "soc_percent")
        supported = True if value is not None or (core and target.source == "deye_cloud") else (False if target.source == "deye_cloud" else None)
        quality, flags = provenance(row, group, now=now, core=core) if row else ("missing", [])
        return BatteryMetric(value=value / scale if value is not None else None, unit=unit, supported=supported,
                             quality=quality if value is not None else "missing", flags=flags,
                             source=target.source, measured_at=row.measured_at if row else None,
                             received_at=row.received_at if row else None)

    soc, power = metric("soc_percent", "%"), metric("power_w", "kW", Decimal(1000))
    caps = []
    for usable in (False, True):
        value, basis, at = capacity(group, configs, now, allow_config, usable)
        q, flags = provenance(row, group, now=now) if row and basis == "source_declared" else ("declared", [])
        caps.append(BatteryMetric(value=value, unit="kWh", quality=q if q in ("stale", "simulated") else "declared" if value is not None else "missing",
                                  supported=True if value is not None else None, source=basis, flags=flags,
                                  measured_at=at or (row.measured_at if row and basis else None)))
    soh_metric = metric("soh_percent", "%")
    kind = group.get("soh_kind") or ("estimated" if group.get("quality") == "derived" else "measured")
    if soh_metric.value is None or kind == "unavailable":
        kind = "unavailable"
        soh_metric.value = None
    soh = BatterySoh(**soh_metric.model_dump(), status=kind, confidence=group.get("soh_confidence", "unknown"),
                     method_version=group.get("soh_method_version"))
    soh.method = group.get("soh_method") or ("source_reported" if kind == "measured" else None)
    if kind == "estimated" and soh.quality == "measured":
        soh.quality = "estimated"
    nominal, usable = caps
    stored = BatteryMetric(unit="kWh", method="soc_times_nominal/v1")
    if soc.value is not None and nominal.value is not None:
        stored = BatteryMetric(value=soc.value * nominal.value / 100, unit="kWh", supported=True,
                               quality=worst(["estimated", soc.quality, nominal.quality]),
                               flags=sorted(set(soc.flags + nominal.flags)), source=nominal.source,
                               measured_at=soc.measured_at, method="soc_times_nominal/v1")
    state = group.get("state") or "unknown"
    if state != "fault" and power.value is not None:
        state = "charging" if power.value > 0 else "discharging" if power.value < 0 else "idle"
    freshness = "missing" if not row else "fresh" if timedelta(0) <= now - row.measured_at <= timedelta(minutes=10) else "stale"
    return BatteryCurrent(target=target, state=state, freshness=freshness, soc=soc, power=power,
                          voltage=metric("voltage_v", "V"), current=metric("current_a", "A"),
                          temperature=metric("temperature_c", "°C"), soh=soh, nominal_capacity=nominal,
                          usable_capacity=usable, stored_energy=stored, reported_cycles=metric("cycle_count", "cycles"))


def summary(db, station, *, now=None):
    now = now or datetime.now(UTC)
    targets, rows, configs = discover(db, station, now)
    return BatterySummary(station_id=station.id, timezone=station.timezone, generated_at=now,
                          batteries=[current(t, rows.get(t.device_id), configs, len(targets) == 1 and t.scope == "reported_bank", now) for t in targets])


class Accumulator:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.covered = dict.fromkeys(("power", "soc", "temperature", "efc"), ZERO)
        self.qualities = {key: set() for key in self.covered}
        self.flags = {key: set() for key in self.covered}
        self.charge = self.discharge = self.soc = self.efc = ZERO
        self.minimum = self.maximum = self.minimum_at = self.maximum_at = None
        self.bases = set()

    def add(self, row, target, group, start, end, configs, allow_config):
        start, end = max(self.start, start), min(self.end, end)
        if end <= start:
            return
        duration = seconds(end - start)
        for key, field in (("power", "power_w"), ("soc", "soc_percent"), ("temperature", "temperature_c")):
            value = decimal(group.get(field))
            if value is None:
                continue
            q, flags = provenance(row, group, core=target.pack_id is None and key in ("power", "soc"))
            self.covered[key] += duration
            self.qualities[key].add(q)
            self.flags[key].update(flags)
            if key == "soc":
                self.soc += value * duration
            elif key == "temperature":
                if self.minimum is None or value < self.minimum:
                    self.minimum, self.minimum_at = value, row.measured_at
                if self.maximum is None or value > self.maximum:
                    self.maximum, self.maximum_at = value, row.measured_at
            else:
                energy = value * duration / Decimal(3600000)
                self.charge += max(energy, ZERO)
                self.discharge += max(-energy, ZERO)
                boundaries = [start, *[c.created_at for c in configs if allow_config and start < c.created_at < end], end]
                for left, right in pairwise(boundaries):
                    nominal, basis, _ = capacity(group, configs, left, allow_config and not (row.diagnostics or {}).get("battery_packs"))
                    if nominal is None or nominal <= 0:
                        continue
                    self.efc += abs(value) * seconds(right - left) / (Decimal(7200000) * nominal)
                    self.covered["efc"] += seconds(right - left)
                    cap_q, cap_flags = provenance(row, group) if basis == "source_declared" else ("declared", [])
                    self.qualities["efc"].update((q, cap_q, "estimated"))
                    self.flags["efc"].update([*flags, *cap_flags])
                    self.bases.add(f"{basis}: {nominal} kWh")

    def result(self):
        duration = max(seconds(self.end - self.start), ZERO)
        coverage = {k: (v / duration if duration else ZERO) for k, v in self.covered.items()}
        return BatteryBucket(start=self.start, end=self.end,
                             charge_kwh=self.charge if self.covered["power"] else None,
                             discharge_kwh=self.discharge if self.covered["power"] else None,
                             soc_percent=self.soc / self.covered["soc"] if self.covered["soc"] else None,
                             temperature_min_c=self.minimum, temperature_max_c=self.maximum,
                             temperature_min_at=self.minimum_at, temperature_max_at=self.maximum_at,
                             efc=self.efc if self.covered["power"] and self.covered["efc"] == self.covered["power"] else None,
                             coverage=coverage, quality={k: worst(v) for k, v in self.qualities.items()},
                             flags={k: sorted(v) for k, v in self.flags.items()}, capacity_basis=sorted(self.bases))


def total(buckets):
    duration = sum((max(seconds(b.end - b.start), ZERO) for b in buckets), ZERO)
    covered = sum((b.coverage["power"] * max(seconds(b.end - b.start), ZERO) for b in buckets), ZERO)
    measured = [b for b in buckets if b.charge_kwh is not None]
    capacity_missing = any(b.efc is None for b in measured)
    coverage = covered / duration if duration else ZERO
    return BatteryPeriod(start=buckets[0].start, end=buckets[-1].end,
                         charge_kwh=sum((b.charge_kwh for b in measured), ZERO) if measured else None,
                         discharge_kwh=sum((b.discharge_kwh for b in measured), ZERO) if measured else None,
                         efc=sum((b.efc for b in measured), ZERO) if measured and not capacity_missing else None,
                         efc_quality=worst(b.quality["efc"] for b in measured) if measured and not capacity_missing else "missing",
                         coverage=coverage, quality=worst(b.quality["power"] for b in measured),
                         flags=sorted({f for b in buckets for f in b.flags["power"] + b.flags["efc"]}),
                         capacity_basis=sorted({basis for b in buckets for basis in b.capacity_basis}),
                         efc_reason="no_power_data" if not measured else "capacity_missing" if capacity_missing else "available" if coverage == 1 else "partial_history",
                         complete_days=sum(b.coverage["power"] == 1 for b in buckets))


def notices(live):
    if live is None:
        return [BatteryNotice(code="no_source", severity="info", title="Nicio sursa de baterie",
                              explanation="Conecteaza un dispozitiv EMS sau Deye Cloud pentru a vedea datele raportate.")]
    result = []
    if live.freshness != "fresh":
        result.append(BatteryNotice(code="stale", severity="warning", title="Date neactualizate",
                                    explanation="Verifica legatura cu dispozitivul. Valorile afisate sunt ultimele observatii, nu starea confirmata acum."))
    if live.temperature.quality == "measured" and live.temperature.value is not None:
        if live.temperature.value > 50:
            result.append(BatteryNotice(code="hot", severity="warning", title="Temperatura ridicata",
                                        explanation="Peste pragul operational de 50 °C. Verifica ventilatia si limitele din documentatia bateriei.",
                                        measured_at=live.temperature.measured_at))
        elif live.temperature.value < 0:
            result.append(BatteryNotice(code="cold", severity="warning", title="Temperatura sub 0 °C",
                                        explanation="Unele baterii limiteaza incarcarea la rece. Consulta limitele producatorului; aceasta indicatie nu confirma o defectiune.",
                                        measured_at=live.temperature.measured_at))
    if live.soh.status == "measured" and live.soh.quality == "measured" and live.soh.value is not None and live.soh.value < 70:
        result.append(BatteryNotice(code="low_soh", severity="warning", title="SOH raportat redus",
                                    explanation="Sursa raporteaza SOH sub 70%. Verifica diagnosticul BMS si conditiile de garantie cu instalatorul."))
    if live.state == "fault":
        result.append(BatteryNotice(code="fault", severity="warning", title="Sursa raporteaza o eroare",
                                    explanation="Consulta diagnosticul dispozitivului si instructiunile producatorului. Platforma nu modifica bateria."))
    return result


def detail(db, station, *, battery_id=None, days=14, end=None, day=None, now=None):
    now = now or datetime.now(UTC)
    local_today = now.astimezone(ZoneInfo(station.timezone)).date()
    end = end or local_today
    day = day or end
    if not 1 <= days <= 31 or end > local_today or end < date(1970, 2, 1):
        raise ValueError("Perioada trebuie sa aiba 1-31 zile si sa se incheie intre 1970-02-01 si astazi.")
    first = end - timedelta(days=days - 1)
    if not first <= day <= end:
        raise ValueError("Perioada trebuie sa aiba 1-31 zile, fara zile viitoare; ziua selectata trebuie sa fie in perioada.")
    targets, latest, configs = discover(db, station, now)
    target = next((t for t in targets if t.id == battery_id), None) if battery_id else next(iter(targets), None)
    if battery_id and target is None:
        raise LookupError("Bateria nu apartine surselor acestei statii.")
    allow_config = len(targets) == 1 and target.scope == "reported_bank"
    live = current(target, latest.get(target.device_id), configs, allow_config, now) if target else None
    # Temperature context always uses the seven days ending at the selected day.
    temp_first = day - timedelta(days=6)
    start = midnight(min(first, temp_first), station.timezone)
    stop = min(midnight(end + timedelta(days=1), station.timezone), now)
    daily_acc = {}
    cursor = min(first, temp_first)
    while cursor <= end:
        left = midnight(cursor, station.timezone)
        daily_acc[cursor] = Accumulator(left, min(midnight(cursor + timedelta(days=1), station.timezone), now))
        cursor += timedelta(days=1)
    hour_acc = []
    cursor = midnight(day, station.timezone)
    day_end = midnight(day + timedelta(days=1), station.timezone)
    while cursor < day_end:
        hour_acc.append(Accumulator(cursor, max(cursor, min(cursor + timedelta(hours=1), day_end, now))))
        cursor += timedelta(hours=1)
    if target:
        query = select(TelemetryRaw).where(
            TelemetryRaw.station_id == station.id, TelemetryRaw.device_id == target.device_id,
            TelemetryRaw.source == target.source, TelemetryRaw.measured_at >= start - HOLD,
            TelemetryRaw.measured_at < stop,
        ).order_by(TelemetryRaw.measured_at, TelemetryRaw.received_at, TelemetryRaw.id).execution_options(yield_per=1000)

        def integrate(row, until):
            left, right = max(row.measured_at, start), min(until, row.measured_at + HOLD, stop)
            if right <= left:
                return
            group = payload(row, target)
            local = left.astimezone(ZoneInfo(station.timezone)).date()
            while local <= right.astimezone(ZoneInfo(station.timezone)).date():
                if local in daily_acc:
                    daily_acc[local].add(row, target, group, left, right, configs, allow_config)
                local += timedelta(days=1)
            if right > hour_acc[0].start and left < day_end:
                for bucket in hour_acc:
                    bucket.add(row, target, group, left, right, configs, allow_config)

        previous = None
        for row in db.scalars(query):
            if previous is not None:
                integrate(previous, row.measured_at)
            previous = row
        if previous is not None:
            integrate(previous, stop)
    daily = [bucket.result() for local, bucket in daily_acc.items() if local >= first]
    hourly = [bucket.result() for bucket in hour_acc]
    temperatures = [bucket.result() for local, bucket in daily_acc.items() if temp_first <= local <= day]
    extremes = []
    for kind, field, at_field, select_extreme in (("coldest", "temperature_min_c", "temperature_min_at", min),
                                                 ("warmest", "temperature_max_c", "temperature_max_at", max)):
        candidates = [b for b in temperatures if getattr(b, field) is not None]
        if candidates:
            bucket = select_extreme(candidates, key=lambda b: getattr(b, field))
            extremes.append(BatteryExtreme(kind=kind, value_c=getattr(bucket, field), measured_at=getattr(bucket, at_field),
                                           quality=bucket.quality["temperature"]))
    day_total = total([daily_acc[day].result()])
    period = total(daily)
    # A current partial day is never counted as a complete measured calendar day.
    period.complete_days = sum(b.coverage["power"] == 1 and b.end <= midnight(local_today, station.timezone) for b in daily)
    return BatteryDetail(station_id=station.id, timezone=station.timezone, generated_at=now, targets=targets,
                         current=live, day=day, end_date=end, days=days, today=day_total, period=period,
                         hourly=hourly, daily=daily, temperature_days=temperatures, extremes=extremes, notices=notices(live),
                         max_hold_seconds=MAX_GAP_SECONDS)
