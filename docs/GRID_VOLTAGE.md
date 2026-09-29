# Grid voltage tracker

The station dashboard includes a read-only daily grid-voltage chart, source and
date selectors, L1/L2/L3 latest readings and extrema, zoom and an accessible table.
Today refreshes every 60 seconds while visible. Historical dates remain fixed.

## Contract

`GET /api/v1/stations/{station_id}/grid-voltage?day=YYYY-MM-DD&source_id=...`
requires station viewer access and returns `Cache-Control: no-store`.
The additive schema lives in `app/schemas/grid_voltage.py` and generated OpenAPI.
Missing `day` means today in the station timezone; dates are bounded by raw
telemetry retention (default 90 days) and today. The earliest day can be partial.
Unknown or other-station sources return 404. Source IDs are opaque selectors,
never authorization credentials. Provider serial numbers are not returned.

`start` and exclusive `end` are UTC instants derived from consecutive local
midnights: DST days contain 1380 or 1500 points, ordinary days 1440. Each phase has
one point per minute: arithmetic mean of actual samples, sample minimum/maximum,
count, quality and flags. These are **not** time-weighted electrical compliance
measurements. `observed_minutes / elapsed_minutes` counts minutes with at least
one valid reading, not continuous measurement coverage. No interpolation,
carry-forward or carry-in contributes to these sample statistics. Future and
unobserved minutes are null; explicit zero is valid. High-frequency spikes remain
in minute extrema even when the mean is lower. The chart's lower/upper lines and
tooltip expose these extrema.

`latest` is the last observation in the selected day, including an explicit
unknown when a phase stops reporting. Measurement and receipt timestamps are
explicit UTC instants. Its freshness is separate
from historical quality: old valid measurements remain measured in history, but
the latest reading is flagged stale after ten minutes. All contributing flags,
including simulated, stale, derived and late, propagate to minute/day results.
Different sources/inverters are never averaged together.

## Collection and ownership

Native EMS history reads the existing `TelemetryRaw.diagnostics.phases` contract;
only `circuit=grid` (the existing default) is accepted. Load, PV and battery
voltages never stand in for grid measurements. This immediately makes retained
native phase history available, without reingestion or a device protocol change.

Deye uses the existing read-only `/v1.0/device/latest` diagnostic poll. Only
explicit, unit-qualified `Grid Voltage L1/L2/L3` or `Grid L1/L2/L3 Voltage`
measurepoint labels (case/space insensitive) are accepted, in V. Ambiguous labels
(including MI, generic AC, load voltage or a missing phase identity) remain
unsupported; this is not certification of any inverter/firmware. The provider's
own `collectionTime` is required and existing polling freshness limits apply.
See [Deye's documented response structure](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html).
Duplicate phase fields and invalid/nonfinite values become unknown, never zero.

Deye observations are stored in `grid_voltage_samples`, with a unique station,
inventory-source UUID and observation timestamp. Repeated polling is idempotent
under PostgreSQL concurrency. Reconnection creates a separate source; inventory
removal retains historical observations. No provider serials or credentials are
stored in this table. Existing local-device preference/backoff remains in force.
There are no extra provider calls from web requests and no hardware writes.

The new table is isolated from energy integration so voltage-only observations
cannot interrupt power/energy coverage. Retention follows raw telemetry.
Migration `b322c222d903` archives the table as `legacy_grid_voltage_samples` on
downgrade and restores it on upgrade, preserving populated zero/NULL values.
No mobile, Home Assistant or edge implementation changes are needed for this
platform web feature; existing typed phase payloads remain compatible.
