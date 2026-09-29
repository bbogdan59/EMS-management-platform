# Battery diagnostics (#218)

The station overview links to `/stations/{station_id}/battery`. The page shows
current SOC and signed DC power, source-specific energy history, temperature
ranges and health/wear. The read API is published in `/api/openapi.json` and
uses the existing human session and station viewer authorization. Device
credentials cannot read it. Native mobile authentication is tracked in #200.

## Read contract

| GET endpoint | Result |
| --- | --- |
| `/api/v1/stations/{station_id}/battery-health/summary` | `BatterySummary`: current metrics for every source/bank and reported pack |
| `/api/v1/stations/{station_id}/battery-health?battery_id=…&days=14&end=2026-09-28&day=2026-09-27` | `BatteryDetail`: selected battery, hourly/daily aggregates, seven-day temperature context, EFC and notices |

`days` is 1–31; the UI offers 7/14/30. `end` defaults to the station's current
local date; `day` defaults to `end` and must fall inside the period. `today`
in the response always describes this selected `day`. Current observations
remain current even when looking at an earlier day. Future dates return 422;
an unknown or foreign battery ID returns 404 after station authorization.

Instants are timezone-aware UTC. Boundaries follow `station.timezone`, with
23/25 hourly buckets on DST transition days. Calendar periods include the end
date; today's elapsed portion is used for coverage, and future hours have
null values. `complete_days` excludes the unfinished current calendar day.

Decimal measurements serialize as JSON strings. Unknown numbers remain null;
observed zero remains zero. Each current metric includes unit, source,
measurement/receipt timestamps, quality and flags. `supported` is true for
known supported fields, false for unsupported cloud fields, and null when an
EMS device has not established support by reporting that metric. It is not a
claim of verified compatibility with a particular BMS model.

Battery IDs are opaque to clients. They distinguish `(device_id, pack_id)`;
the inverter's legacy battery values form a **reported bank**, not a computed
sum of packs. Multiple inverter sources may observe the same physical bank.
Never add their values, and never add a bank to its constituent packs.
The selector lists currently reported packs; a source must continue sending
its pack inventory with observations. Replacement hardware must use a new
pack ID. Historical values from another pack/source are never substituted.

## Calculations and evidence

Positive `battery_power_w` / pack `power_w` means charging; negative means
discharging. All energy is DC kWh with no additional inverter efficiency
factor. Within a source, each observation is held until the next message or
five minutes, whichever occurs first. An explicit missing metric terminates
its hold; it is not forward-filled across that message. Source-scoped history
may therefore have less coverage than older station-wide aggregates which
hold until the next non-null metric.

The service streams retained raw evidence in timestamp order and returns at
most 31 daily, 25 hourly and 7 temperature buckets. It includes up to five
minutes of carry-in. Missing spans lower **per-metric** coverage; charge and
discharge share power coverage. SOC is a time-weighted mean. Temperature
extrema include held observations intersecting the bucket, with original
observation timestamps. Stale, simulated, derived and late provenance survives
aggregation, including carry-in; a historical observation is not marked stale
merely because its date is in the past. Freshness of current values is ten
minutes, matching existing diagnostic rules.

**Observed EFC = (charged kWh + discharged kWh) / (2 × nominal capacity kWh).**
The method is `dc-throughput-over-two-nominal-capacity/v1`. Segments are split
at changes in source-declared or station-configured nominal capacity. Station
configuration only applies to an unambiguous single reported bank and from
the configuration version's creation time. It is never retroactive and never
distributed across packs. Missing/zero nominal capacity in any contributing
power segment makes EFC unavailable, while energy remains available. Partial
coverage produces observed partial EFC, not lifetime cycles. Usable capacity,
including explicit zero, does not replace the nominal denominator.

This history is limited by raw retention (default 90 days) and bounded queries.
Older station-wide rollups cannot be attributed reliably to a specific pack,
so they are not used to fabricate lifetime EFC. Source-reported lifetime
`cycle_count` is a separate value with the source's own cycle definition.
No existing aggregate keys, legacy EFC endpoints or stored history change.

Stored energy is explicitly estimated as SOC × nominal capacity / 100;
it is not guaranteed deliverable energy or energy above the reserve.

SOH is independent from SOC and configured capacities. It has
`measured | estimated | unavailable` status, source/timestamps, method/version
and confidence. Legacy typed SOH with measured quality is labelled **reported
by the source**, not manufacturer-certified. An estimated SOH requires method
and version and cannot trigger the existing measured-SOH health rule.

Temperature notices use fresh measured evidence only. Above 50 °C references
the existing operational diagnostic threshold; below 0 °C is an advisory to
check the manufacturer's charging limits, not a universal safety limit.
Notices are read-only explanations, not commands or new notification deliveries.

## Additive telemetry and source capabilities

Existing v1/v2 telemetry remains valid. Optional `battery` metadata:
`nominal_capacity_kwh`, `usable_capacity_kwh`, `cycle_count`, `soh_kind`,
`soh_method`, `soh_method_version`, `soh_confidence`.
Optional `battery_packs` accepts up to 32 typed items with these fields and the
existing battery voltage/current/temperature/state/quality fields, plus:

```json
{
  "pack_id": "rack-a",
  "label": "Rack A",
  "power_w": "1200",
  "soc_percent": "65",
  "nominal_capacity_kwh": "10.2",
  "temperature_c": "24",
  "soh_percent": "94",
  "soh_kind": "estimated",
  "soh_method": "bms-reported-estimate",
  "soh_method_version": "1",
  "soh_confidence": "unknown",
  "quality": "measured"
}
```

Pack IDs are unique per device, 1–64 characters `[A-Za-z0-9_.:-]`; duplicates
are rejected with `duplicate_metric`. Non-finite values are rejected. Typed
data is stored in validated `diagnostics`; arbitrary `raw_payload` is never
promoted. Pack energy is not added to the existing canonical bank aggregate.

Deye Cloud's verified adapter currently provides only SOC and signed battery
power. Other BMS values remain unsupported/null; station capacities can be
shown as declared where unambiguous. No speculative provider field names,
register addresses, device writes or claimed hardware compatibility are added.
Agent/BMS collection remains
[EMS-device-code #1](https://github.com/bbogdan59/EMS-device-code/issues/1).

## Validation

- PostgreSQL integrations: signed energy/EFC, capacity changes, zero/null,
  carry-in provenance, multiple sources/packs, DST/leap days, capabilities,
  human authorization and cross-tenant denial, OpenAPI and typed ingestion.
- Health regression: estimated SOH cannot activate a measured-SOH incident.
- Browser: real authenticated page, period/day selection and retry; deterministic
  visual fixtures for healthy/hot/cold/stale/unsupported/empty states, mobile
  width, theme switching and accessible data tables. Fixtures do not validate BMS.
- Hardware: not verified by these tests; no hardware is required or controlled.

Architecture: [ADR 0005](adr/0005-battery-diagnostics.md).
