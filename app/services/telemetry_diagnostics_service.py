from datetime import timedelta
from decimal import Decimal

from app.models.telemetry import TelemetryRaw


def quality(row: TelemetryRaw, as_of) -> str:
    if row.is_simulated or (row.quality_flags or {}).get("simulated"):
        return "simulated"
    if (row.quality_flags or {}).get("stale") or not timedelta(
        0
    ) <= as_of - row.measured_at <= timedelta(minutes=10):
        return "stale"
    return "derived" if (row.quality_flags or {}).get("derived") else "measured"


def counter_delta(previous: TelemetryRaw, current: TelemetryRaw, name: str) -> dict:
    result = {"energy_kwh": None, "quality": "unknown", "reason": "missing_counter"}
    before = next((c for c in previous.diagnostics.get("counters", []) if c["name"] == name), None)
    after = next((c for c in current.diagnostics.get("counters", []) if c["name"] == name), None)
    if not before or not after:
        return result
    if previous.device_id != current.device_id or current.measured_at <= previous.measured_at:
        return {**result, "reason": "invalid_order_or_device"}
    if before.get("reset_id") != after.get("reset_id"):
        return {**result, "reason": "counter_reset"}
    if any(c.get("quality") in ("simulated", "stale") for c in (before, after)) or any(
        r.is_simulated or r.quality_flags.get("stale") or r.quality_flags.get("simulated")
        for r in (previous, current)
    ):
        return {**result, "reason": "untrusted_provenance"}
    delta = Decimal(after["value"]) - Decimal(before["value"])
    if delta < 0:
        modulus = after.get("rollover_kwh")
        if not modulus or modulus != before.get("rollover_kwh"):
            return {**result, "reason": "counter_decreased"}
        delta += Decimal(modulus)
    return {
        "energy_kwh": str(delta),
        "quality": "derived",
        "reason": "counter_difference",
        "start": previous.measured_at.isoformat(),
        "end": current.measured_at.isoformat(),
    }
