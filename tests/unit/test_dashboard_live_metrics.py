"""Contract per-metrica versionat pentru fluxul SSE (issue #50):
`dashboard_service.get_live_metrics` -- fiecare metrica poarta explicit
`metric`/`value`/`unit`/`measured_at`/`received_at`/`quality`/`source`,
distinct de `get_summary` (neschimbat, folosit doar pentru randarea HTTP
initiala)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.security import utcnow
from app.models.telemetry import TelemetryRaw
from app.services import dashboard_service as dashboard
from tests.factories import make_device, make_org, make_station, make_user


def _station(db, suffix=""):
    user = make_user(db, email=f"livemetrics{suffix}@test.local")
    org = make_org(db, f"LiveMetrics Org {suffix}")
    return make_station(db, org, user, name=f"LiveMetrics Station {suffix}")


def _add_telemetry(db, station, **overrides):
    device = make_device(db, station)
    now = utcnow()
    defaults = {
        "device_id": device.id, "station_id": station.id, "boot_id": "b1", "sequence": 1,
        "measured_at": now, "received_at": now,
        "pv_power_w": Decimal("1200"), "load_power_w": Decimal("800"),
        "battery_power_w": Decimal("-100"), "grid_power_w": Decimal("-300"),
        "battery_soc_percent": Decimal("55.5"), "ev_connected": True, "ev_power_w": Decimal("0"),
        "is_simulated": False,
    }
    defaults.update(overrides)
    row = TelemetryRaw(**defaults)
    db.add(row)
    db.flush()
    return row


def _by_metric(metrics: list[dict]) -> dict[str, dict]:
    return {m["metric"]: m for m in metrics}


def test_live_metrics_include_full_envelope_for_telemetry_metric(db):
    station = _station(db)
    _add_telemetry(db, station)
    db.commit()

    metrics = dashboard.get_live_metrics(db, station)
    by_metric = _by_metric(metrics)
    pv = by_metric["pv_power_kw"]
    assert pv["value"] == 1.2
    assert pv["unit"] == "kW"
    assert pv["measured_at"] is not None
    assert pv["received_at"] is not None
    assert pv["quality"] == "measured"
    assert pv["source"] == "telemetry"


def test_live_metrics_mark_missing_when_no_telemetry_exists(db):
    station = _station(db, "missing")
    db.commit()

    by_metric = _by_metric(dashboard.get_live_metrics(db, station))
    assert by_metric["pv_power_kw"]["value"] is None
    assert by_metric["pv_power_kw"]["quality"] == "missing"
    assert by_metric["data_quality"]["value"] == "missing"


def test_live_metrics_mark_stale_beyond_threshold(db):
    station = _station(db, "stale")
    _add_telemetry(db, station, measured_at=utcnow() - timedelta(minutes=30))
    db.commit()

    by_metric = _by_metric(dashboard.get_live_metrics(db, station))
    assert by_metric["pv_power_kw"]["quality"] == "stale"
    assert by_metric["data_quality"]["value"] == "stale"


def test_live_metrics_mark_simulated(db):
    station = _station(db, "sim")
    _add_telemetry(db, station, is_simulated=True)
    db.commit()

    by_metric = _by_metric(dashboard.get_live_metrics(db, station))
    assert by_metric["pv_power_kw"]["quality"] == "simulated"


def test_live_metrics_price_missing_without_any_tariff(db):
    station = _station(db, "notariff")
    _add_telemetry(db, station)
    db.commit()

    by_metric = _by_metric(dashboard.get_live_metrics(db, station))
    assert by_metric["price_buy_lei_kwh"]["value"] is None
    assert by_metric["price_buy_lei_kwh"]["quality"] == "missing"
    assert by_metric["price_buy_lei_kwh"]["source"] == "tariff"


def test_live_metrics_evaluated_fields_carry_now_as_measured_at_with_no_received_at(db):
    station = _station(db, "evaluated")
    _add_telemetry(db, station)
    db.commit()

    by_metric = _by_metric(dashboard.get_live_metrics(db, station))
    plan_status = by_metric["execution_mode"]
    assert plan_status["received_at"] is None
    assert plan_status["measured_at"] is not None
    assert plan_status["source"] == "plan"


def test_get_summary_output_unchanged_alongside_new_live_metrics(db):
    """`get_summary` (folosit la randarea HTTP initiala) nu trebuie sa se
    schimbe deloc din cauza noii functii `get_live_metrics` -- regresie
    directa impotriva issue #50."""
    station = _station(db, "summary-unchanged")
    _add_telemetry(db, station)
    db.commit()

    summary = dashboard.get_summary(db, station)
    by_metric = _by_metric(dashboard.get_live_metrics(db, station))

    assert summary["pv_power_kw"] == by_metric["pv_power_kw"]["value"]
    assert summary["data_quality"] == by_metric["data_quality"]["value"]
    assert summary["last_update"] == by_metric["last_update"]["value"]
