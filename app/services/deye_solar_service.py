"""Read only reported, unit-qualified PV inputs; no model/register guessing."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.models.device import Device
from app.services.grid_voltage_service import ingest_deye, parse_deye
from app.services.solar_service import ingest

# Labels come from the response, not a fixed count inferred from a model name.
POINT = re.compile(r"^(PV|MPPT)\s*([1-9][0-9]?)\s*(Voltage|Current|Power)$", re.IGNORECASE)
UNITS = {
    "voltage": ("voltage_v", {"V": Decimal(1)}),
    "current": ("current_a", {"A": Decimal(1)}),
    "power": ("power_w", {"W": Decimal(1), "kW": Decimal(1000)}),
}


def parse_inputs(data):
    inputs = {}
    invalid = set()
    seen = set()
    points = data.get("dataList")
    if not isinstance(points, list):
        return []
    for point in points:
        if not isinstance(point, dict):
            continue
        match = POINT.fullmatch(str(point.get("key", "")))
        if not match:
            continue
        family, index, measurement = match.groups()
        field, units = UNITS[measurement.lower()]
        if point.get("unit") not in units:
            continue
        index = int(index)
        row = inputs.setdefault(
            index,
            {
                "index": index,
                "kind": "mppt" if family.upper() == "MPPT" else "pv_input",
                "supported_metrics": [],
                "quality": "measured",
            },
        )
        identity = index, field
        if identity in seen or row["kind"] != ("mppt" if family.upper() == "MPPT" else "pv_input"):
            invalid.add(index)
        seen.add(identity)
        row["supported_metrics"].append(field)
        try:
            value = Decimal(str(point.get("value"))) * units[point["unit"]]
            row[field] = str(value) if value.is_finite() and 0 <= value < 99999999 else None
        except (InvalidOperation, ValueError, TypeError):
            row[field] = None
    return [row for index, row in sorted(inputs.items()) if index not in invalid][:8]


def poll(db, connection, access_token, now):
    from app.services.deye_cloud_service import DeyeCloudError, _post

    device = db.get(Device, connection.device_id)
    links = {
        link.remote_device_sn: link
        for link in connection.device_links
        if link.remote_device_type == "INVERTER"
    }
    serials = sorted(links)
    count = 0
    for offset in range(0, len(serials), 10):
        requested = serials[offset : offset + 10]
        try:
            response = _post(
                "/v1.0/device/latest", body={"deviceList": requested}, access_token=access_token
            )
        except DeyeCloudError:
            continue  # Diagnostics must not interrupt the existing station-power feed.
        items = response.get("deviceDataList") if isinstance(response, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            serial = item.get("deviceSn")
            if serial not in requested or item.get("deviceType") != "INVERTER":
                continue
            try:
                at = datetime.fromtimestamp(item["collectionTime"], tz=UTC)
            except (KeyError, TypeError, OverflowError, OSError, ValueError):
                continue
            if not now - timedelta(minutes=10) <= at <= now + timedelta(seconds=30):
                continue
            phases = parse_deye(item)
            if phases:
                ingest_deye(
                    db, connection.station_id, links[serial].id,
                    f"Invertor Deye {serials.index(serial) + 1}", at, now, phases,
                )
            inputs = parse_inputs(item)
            if inputs:
                ingest(
                    db,
                    device,
                    "deye_cloud",
                    serial,
                    at,
                    now,
                    inputs,
                    label=f"Invertor Deye {serials.index(serial) + 1}",
                    model=str((links[serial].raw_snapshot or {}).get("deviceModel"))[:120]
                    if (links[serial].raw_snapshot or {}).get("deviceModel")
                    else None,
                )
                count += len(inputs)
    return count
