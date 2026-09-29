# Solar diagnostics v1

Authoritative Pydantic schemas: `app/schemas/solar.py` (read/configuration) and
`app/schemas/device_api.py` (ingest). Generated JSON Schemas are in this directory;
regenerate using `python -m scripts.export_solar_contract`, or `--check` in CI.

Authenticated station endpoints:

- `GET /api/v1/stations/{id}/solar`: inverter/input snapshot, source, quality,
  timestamps, capabilities, user configuration, distinct DC and AC observations.
- `GET /api/v1/stations/{id}/solar/trackers/{tracker_id}/history?range=24h|7d|30d`:
  weighted V/A/W, integrated `energy_kwh`, per-metric coverage, quality/flags.
- `PUT /api/v1/stations/{id}/solar/trackers/{tracker_id}`: operator/admin only,
  cookie CSRF, body `TrackerConfiguration` with current revision; 409 on conflict.

Device v1 remains backward compatible. Optional `mppt` contains up to eight
reported inputs (indices need not be contiguous):

```json
{
  "mppt": [{
    "index": 3,
    "label": "South roof",
    "kind": "mppt",
    "supported_metrics": ["voltage_v", "current_a", "power_w"],
    "voltage_v": "305.2", "current_a": null, "power_w": "0",
    "quality": "measured"
  }],
  "inverter": {"ac_output_power_w": "-15", "quality": "measured"}
}
```

V/A/W per PV input are finite and nonnegative; AC is signed, positive output,
negative inverter draw. Maximum absolute magnitude is below 99,999,999.
Absent `supported_metrics` retains observed capabilities for older clients;
an explicit list declares supported measurements even when a reading is NULL.
Values outside that list are rejected. Input kind `pv_input` avoids claiming an
independent MPPT when only a physical inverter port is known. Labels do not
authorize access. Unsupported inputs are absent, missing supported readings NULL.

JSON read values/coverage are decimal strings, UTC instants include offsets,
station timezone is explicit. Quality: measured/derived/simulated/stale/missing;
flags preserve simultaneous provenance (for example simulated and stale).
Client charts should use NULL gaps and no point symbols for dense history.

## Deye capabilities and verification boundary

The read-only Deye API exposes `deviceDataList`, `collectionTime`, and a
unit-qualified `dataList`; see the [official device/latest example](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html).
The adapter requests only connected inventory inverters, at most ten serials
per request. It accepts only explicitly returned `PV<n> Voltage/Current/Power`
or `MPPT<n> Voltage/Current/Power` labels, with V/A/W or kW units. Unknown keys,
absent timestamp, old data, other serials and ambiguous duplicate indices are
ignored. It never infers the input count from a device model name.

`deye-capabilities.json` documents one input, two partially reported inputs and
no supported PV inputs. These are **synthetic protocol fixtures, not real model
captures**; the verified hardware-model list is intentionally empty. Other Deye
measurepoint labels remain unavailable pending a sanitized device/latest capture
and an explicit tested mapping. No physical device/register support is certified
by these parser tests. Device-side validation belongs to EMS-device-code#1.
